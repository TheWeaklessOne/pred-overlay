"""Opt-in session storage and Steam login through a caller-owned Omeda exchange.

Nothing runs on import. ``steam_login`` never persists credentials or trusts the
claimed Steam identity: ``exchange(body)`` must verify the assertion using the
official authenticateV1 endpoint, reject service errors, and return a nonempty
dict only on success. It must impose its own transport timeout. Late results
after timeout/cancellation are discarded; Python cannot kill a stuck callable.
"""

import base64
import binascii
import html
import json
import math
import os
import re
import secrets
import socket
import stat
import threading
import time
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from .errors import PredError

STEAM_ENDPOINT = "https://steamcommunity.com/openid/login"
OPENID_NS = "http://specs.openid.net/auth/2.0"
_SELECT = "http://specs.openid.net/auth/2.0/identifier_select"
_TOKEN_FIELDS = {"omeda": "social_token", "omeda_game": "game_token", "predgg": "access_token"}
_MAX_SESSION_BYTES = 65536
_FIELDS = {
    "claimedId": "claimed_id",
    "ns": "ns",
    "mode": "mode",
    "opEndpoint": "op_endpoint",
    "identity": "identity",
    "returnTo": "return_to",
    "responseNonce": "response_nonce",
    "assocHandle": "assoc_handle",
    "signed": "signed",
    "sig": "sig",
}
# Valve signs claimed_id but may omit identity from the signed list. The exact
# identity == claimed_id check below binds it to that signed claim; Omeda still
# verifies the original assertion, including Valve's self-listed signed field.
_SIGNED = {"op_endpoint", "claimed_id", "return_to", "response_nonce", "assoc_handle"}
_WINDOWS = os.name == "nt"
# Symlinks and junctions redirect a path; other reparse points (cloud files) do not.
# The stat module defines these tags only on Windows.
_LINK_TAGS = (
    {stat.IO_REPARSE_TAG_SYMLINK, stat.IO_REPARSE_TAG_MOUNT_POINT} if _WINDOWS else frozenset()
)


class AuthError(PredError):
    """An authentication/storage error with a credential-free message."""


class AuthCancelled(AuthError):
    """The user cancelled login."""


class AuthTimeout(AuthError):
    """The login deadline elapsed."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _session(value):
    """Whitelist credential fields so assertions/arbitrary metadata stay off disk."""
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "providers"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["providers"], dict)
    ):
        raise AuthError("Invalid session format.")
    providers = {}
    for provider, record in value["providers"].items():
        field = _TOKEN_FIELDS.get(provider)
        if (
            field is None
            or not isinstance(record, dict)
            or set(record) != {field}
            or not isinstance(record[field], str)
            or not record[field]
            or len(record[field]) > 16384
            or any(ord(c) < 33 or ord(c) > 126 for c in record[field])
        ):
            raise AuthError("Invalid session provider or credential field.")
        providers[provider] = {field: record[field]}
    return {"version": 1, "providers": providers}


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) in _LINK_TAGS


def _entry(directory, name):
    """Address ``name`` inside a held directory fd (POSIX) or a vetted path (Windows)."""
    if _WINDOWS:
        return os.path.join(directory, name), {}
    return name, {"dir_fd": directory}


def _expiry_hint(token):
    """Decode only exp; this is NOT signature or session validity verification."""
    try:
        parts = token.split(".")
        if len(parts) != 3 or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[1]):
            return None
        payload = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)),
            object_pairs_hook=_unique_object,
        )
        exp = payload.get("exp") if isinstance(payload, dict) else None
        if type(exp) not in (int, float) or not math.isfinite(exp) or not 0 <= exp <= 253402300799:
            return None
        return {
            "source": "unverified_jwt_claim",
            "verified": False,
            "expires_at": exp,
            "expired": exp <= time.time(),
        }
    except (ValueError, UnicodeError, binascii.Error, OverflowError, RecursionError):
        return None


class SessionStore:
    """A POSIX 0600 JSON file at an explicit caller-selected path.

    ``load()`` returns credentials (never print its result); missing files return
    ``{"version": 1, "providers": {}}``. ``save(session)`` atomically replaces the
    whole document, so concurrent writers are last-writer-wins. The only allowed
    records are ``omeda: {social_token: str}`` and ``predgg: {access_token: str}``.
    ``status()`` returns metadata only. ``clear()`` unlinks and returns a bool;
    unlinking is not guaranteed secure erasure. No default credential path exists.

    Symlinks in any path component and hard-linked/non-private session files are
    rejected. The final parent must be user-owned and not group/world-writable.
    Missing directories are created as 0700 only during an explicit save.

    Windows has no dir_fd, O_NOFOLLOW or POSIX modes: every path component is
    checked for symlinks/junctions, an opened file must be the vetted entry, and
    privacy relies on the inherited profile ACLs instead of 0600/uid checks.
    """

    def __init__(self, path):
        self.path = Path(os.path.abspath(os.fspath(path)))
        if not self.path.name:
            raise AuthError("A session file path is required.")

    @contextmanager
    def _directory(self, create=False):
        if _WINDOWS:
            yield self._windows_directory(create)
            return
        fd = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            fd = os.open(self.path.anchor, flags)
            for part in self.path.parent.parts[1:]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise AuthError(
                    "Session directory must be owned by you and not writable by others."
                )
            yield fd
        finally:
            if fd is not None:
                os.close(fd)

    def _windows_directory(self, create):
        directory = self.path.anchor
        for part in self.path.parent.parts[1:]:
            directory = os.path.join(directory, part)
            if create:
                try:
                    os.mkdir(directory, 0o700)
                except FileExistsError:
                    pass
            info = os.stat(directory, follow_symlinks=False)
            if _is_link(info) or not stat.S_ISDIR(info.st_mode):
                raise AuthError("Session directory path must not contain links or junctions.")
        return directory

    @staticmethod
    def _check_file(info):
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (
                not _WINDOWS and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600)
            )
        ):
            raise AuthError("Session file must be a regular, user-owned 0600 file with no links.")

    def _existing(self, directory):
        name, at = _entry(directory, self.path.name)
        try:
            info = os.stat(name, **at, follow_symlinks=False)
        except FileNotFoundError:
            return False
        self._check_file(info)
        return True

    def _open_existing(self, directory):
        name, at = _entry(directory, self.path.name)
        if not _WINDOWS:
            return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, **at)
        # Without O_NOFOLLOW, vet the entry first and require the handle to be that file.
        expected = os.stat(name, follow_symlinks=False)
        self._check_file(expected)
        fd = os.open(name, os.O_RDONLY | os.O_BINARY)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino):
            os.close(fd)
            raise AuthError("Session file changed while it was being opened.")
        return fd

    @staticmethod
    def _sync_directory(directory):
        if not _WINDOWS:  # Windows cannot fsync a directory.
            os.fsync(directory)

    def load(self) -> dict:
        """Return a validated credential document; never log this return value."""
        try:
            with self._directory() as directory:
                fd = self._open_existing(directory)
                try:
                    self._check_file(os.fstat(fd))
                    with os.fdopen(fd, "rb", closefd=False) as stream:
                        raw = stream.read(_MAX_SESSION_BYTES + 1)
                finally:
                    os.close(fd)
                if len(raw) > _MAX_SESSION_BYTES:
                    raise AuthError("Session file is too large.")
                return _session(json.loads(raw, object_pairs_hook=_unique_object))
        except FileNotFoundError:
            return {"version": 1, "providers": {}}
        except (OSError, ValueError, UnicodeError, RecursionError):
            raise AuthError("Could not safely read the session file.") from None

    def save(self, session: dict) -> None:
        """Persist only when the caller explicitly chooses to call save."""
        data = (json.dumps(_session(session), separators=(",", ":")) + "\n").encode("utf-8")
        if len(data) > _MAX_SESSION_BYTES:
            raise AuthError("Session file would be too large.")
        temporary = None
        try:
            with self._directory(create=True) as directory:
                try:
                    self._existing(directory)
                    candidate = ".session-" + secrets.token_hex(16) + ".tmp"
                    name, at = _entry(directory, candidate)
                    # O_EXCL never follows an existing link, including on Windows.
                    nofollow = os.O_BINARY if _WINDOWS else os.O_NOFOLLOW
                    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600, **at)
                    temporary = candidate
                    try:
                        with os.fdopen(fd, "wb", closefd=False) as stream:
                            if not _WINDOWS:
                                os.fchmod(fd, 0o600)
                            stream.write(data)
                            stream.flush()
                            os.fsync(fd)
                    finally:
                        os.close(fd)
                    # Recheck immediately before replacement; replace never follows a leaf link.
                    self._existing(directory)
                    if _WINDOWS:
                        os.replace(*(_entry(directory, n)[0] for n in (temporary, self.path.name)))
                    else:
                        os.replace(
                            temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory
                        )
                    temporary = None
                    self._sync_directory(directory)
                finally:
                    if temporary is not None:
                        name, at = _entry(directory, temporary)
                        os.unlink(name, **at)
        except OSError:
            raise AuthError("Could not safely save the session file.") from None

    def clear(self) -> bool:
        """Delete only this session file; return whether one existed."""
        try:
            with self._directory() as directory:
                if not self._existing(directory):
                    return False
                name, at = _entry(directory, self.path.name)
                os.unlink(name, **at)
                self._sync_directory(directory)
                return True
        except FileNotFoundError:
            return False
        except OSError:
            raise AuthError("Could not safely clear the session file.") from None

    def status(self) -> dict:
        """Return whitelisted metadata, without tokens or decoded identity claims."""
        session = self.load()
        providers = {}
        for provider, record in session["providers"].items():
            providers[provider] = {
                "token_present": True,
                "expiry_hint": _expiry_hint(record[_TOKEN_FIELDS[provider]]),
            }
        return {"stored": bool(providers), "providers": providers}


def _query(query):
    if re.search(r"%(?![0-9A-Fa-f]{2})", query):
        raise ValueError("Invalid encoding")
    pairs = parse_qsl(
        query,
        keep_blank_values=True,
        strict_parsing=True,
        encoding="utf-8",
        errors="strict",
        max_num_fields=16,
    )
    result = _unique_object(pairs)
    if any(
        not key or not value or any(ord(c) < 33 or ord(c) > 126 for c in key + value)
        for key, value in pairs
    ):
        raise ValueError("Invalid parameter")
    return result


def _assertion(query, state, callback, started):
    if not secrets.compare_digest(query.get("state", ""), state):
        raise ValueError("Invalid state")
    if query.get("openid.ns") != OPENID_NS:
        raise ValueError("Invalid namespace")
    if query.get("openid.mode") == "cancel":
        if set(query) != {"state", "openid.ns", "openid.mode"}:
            raise ValueError("Invalid cancellation")
        return None
    if set(query) != {"state"} | {"openid." + field for field in _FIELDS.values()}:
        raise ValueError("Unexpected fields")
    if (
        query["openid.mode"] != "id_res"
        or query["openid.op_endpoint"] != STEAM_ENDPOINT
        or query["openid.return_to"] != callback
        or not re.fullmatch(
            r"https://steamcommunity.com/openid/id/[0-9]{17}", query["openid.claimed_id"]
        )
        or query["openid.identity"] != query["openid.claimed_id"]
    ):
        raise ValueError("Invalid assertion")
    signed = query["openid.signed"].split(",")
    if (
        len(signed) != len(set(signed))
        or not _SIGNED.issubset(signed)
        or not set(signed).issubset(set(_FIELDS.values()) - {"sig"})
    ):
        raise ValueError("Invalid signed fields")
    nonce = query["openid.response_nonce"]
    if not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z[!-~]{0,235}", nonce
    ):
        raise ValueError("Invalid nonce")
    timestamp = (
        datetime.strptime(nonce[:20], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    )
    now = time.time()
    if timestamp < max(started - 30, now - 300) or timestamp > now + 30:
        raise ValueError("Stale nonce")
    if len(query["openid.assoc_handle"]) > 255:
        raise ValueError("Invalid association")
    signature = base64.b64decode(query["openid.sig"], validate=True)
    if len(signature) not in (20, 32):
        raise ValueError("Invalid signature encoding")
    return {
        "providerId": "STEAM",
        "providerToken": json.dumps(
            {target: query["openid." + source] for target, source in _FIELDS.items()}
        ),
    }


def steam_login(exchange, *, open_browser=True, timeout=180, port=0, on_ready=None) -> dict:
    """Return only the verified exchange dict; never save or print credentials.

    ``on_ready(local_root_url)`` runs after the listener starts. Its root-path URL
    includes a random launch key; pass it through unchanged and keep it private.
    The optional browser opens that same local URL. Bare / reveals no login state.
    Invalid callbacks do not consume login;
    one well-formed assertion does, even if the exchange rejects its signature.
    Nonces may be at most 300 seconds old, 30 seconds in the future, or 30 seconds
    before this attempt started; all timestamp hints are checked against UTC.
    AuthCancelled/AuthTimeout/AuthError have sanitized messages. Ctrl-C propagates
    after teardown. Hooks must return promptly; exchange owns transport timeouts.
    """
    if not callable(exchange) or (on_ready is not None and not callable(on_ready)):
        raise AuthError("Login requires callable exchange and readiness hooks.")
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or timeout <= 0
        or type(port) is not int
        or not 0 <= port <= 65535
    ):
        raise AuthError("Login requires a positive finite timeout and a valid local port.")
    started, deadline = time.time(), time.monotonic() + timeout
    state = secrets.token_urlsafe(32)
    launch_key = secrets.token_urlsafe(32)
    lock = threading.Lock()
    finished, page_seen = threading.Event(), threading.Event()
    outcome = {"active": True, "consumed": False, "result": None, "error": None}

    def finish(result=None, error=None):
        with lock:
            if outcome["active"] and not finished.is_set() and time.monotonic() < deadline:
                outcome.update(result=result, error=error)
                finished.set()

    def verify(body):
        try:
            result = exchange(body)
            if not isinstance(result, dict) or not result:
                raise ValueError("Missing exchange result")
        except BaseException:
            # Never propagate provider exceptions: they may include assertions or tokens.
            finish(
                error=AuthError("Omeda could not verify this login. Start a new login to retry.")
            )
        else:
            finish(result=result)

    class LocalServer(HTTPServer):
        allow_reuse_address = False

        def __init__(self, *args):
            self.connections = set()
            self.connection_lock = threading.Lock()
            self.stopping = False
            super().__init__(*args)

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(min(1.0, timeout))
            with self.connection_lock:
                if self.stopping:
                    connection.close()
                    raise OSError("Local listener stopped")
                self.connections.add(connection)
            return connection, address

        def close_request(self, request):
            with self.connection_lock:
                self.connections.discard(request)
            super().close_request(request)

        def handle_error(self, request, client_address):
            # socketserver's default would print an exception traceback.
            pass

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_error(self, code, message=None, explain=None):
            self.page(code, "The local login request could not be accepted.")

        def page(self, code, message, *, extra="", location=None):
            data = (
                '<!doctype html><html lang="en"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                "<title>Pred CLI login</title><h1>Pred CLI login</h1><p>"
                + html.escape(message)
                + "</p>"
                + extra
                + "</html>"
            ).encode("utf-8")
            self.send_response(code)
            for name, value in {
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
                "X-Content-Type-Options": "nosniff",
                "Connection": "close",
            }.items():
                self.send_header(name, value)
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            target = self.raw_requestline.split(b" ")[1]
            if (
                self.headers.get_all("Host", []) != [host]
                or not target.startswith(b"/")
                or target.startswith(b"//")
                or b"#" in target
                or len(target) > 8192
                or self.headers.get("Transfer-Encoding") is not None
                or self.headers.get_all("Content-Length", []) not in ([], ["0"])
            ):
                return self.page(400, "The local login request could not be accepted.")
            try:
                parsed = urlsplit(target.decode("ascii"))
                if parsed.scheme or parsed.netloc:
                    raise ValueError("Invalid target")
                query = _query(parsed.query)
            except (ValueError, UnicodeError):
                return self.page(400, "The local login request could not be accepted.")
            if parsed.path == "/":
                if set(query) != {"start"} or not secrets.compare_digest(
                    query["start"], launch_key
                ):
                    return self.page(
                        403, "Open the private local sign-in link provided by the CLI."
                    )
                return self.page(
                    200,
                    "Continue to Steam to sign in. Enter your password only on Steam's website.",
                    extra='<p><a href="'
                    + html.escape(login, quote=True)
                    + '">Continue with Steam</a></p>'
                    '<p><a href="/cancel?state=' + state + '">Cancel login</a></p>',
                )
            if parsed.path == "/complete" and not query:
                if finished.is_set():
                    message = (
                        "Signed in. Return to the CLI. You can close this tab."
                        if outcome["result"] is not None
                        else "Login did not finish. Return to the CLI for details."
                    )
                    self.page(200, message)
                    page_seen.set()
                else:
                    self.page(
                        200,
                        "Checking your login. Return to the CLI for the result. You can close this tab.",
                    )
                return
            if parsed.path == "/cancel":
                if set(query) != {"state"} or not secrets.compare_digest(query["state"], state):
                    return self.page(400, "This cancellation link is not valid.")
                with lock:
                    if finished.is_set() or not outcome["active"]:
                        return self.page(
                            409, "This login has already been used. Return to the CLI."
                        )
                    outcome["consumed"] = True
                finish(error=AuthCancelled("Steam login was cancelled."))
            elif parsed.path == "/callback":
                try:
                    body = _assertion(query, state, callback, started)
                except (ValueError, UnicodeError, binascii.Error, OverflowError):
                    return self.page(
                        400,
                        "This login response is not valid. Return to Steam to finish signing in.",
                    )
                with lock:
                    if outcome["consumed"] or not outcome["active"] or time.monotonic() >= deadline:
                        return self.page(
                            409, "This login has already been used or expired. Start a new login."
                        )
                    outcome["consumed"] = True
                if body is None:
                    finish(error=AuthCancelled("Steam login was cancelled."))
                else:
                    threading.Thread(
                        target=verify, args=(body,), daemon=True, name="pred-auth-exchange"
                    ).start()
            else:
                return self.page(404, "This local login page does not exist.")
            # No assertion in the response body, Location, logs, or completion URL.
            self.page(303, "Return to the CLI for your login result.", location="/complete")

    try:
        server = LocalServer(("127.0.0.1", port), Handler)
    except OSError:
        raise AuthError("Could not start the local login listener.") from None
    host = "127.0.0.1:" + str(server.server_port)
    realm = "http://" + host + "/"
    root = realm + "?" + urlencode({"start": launch_key})
    callback = realm + "callback?" + urlencode({"state": state})
    login = (
        STEAM_ENDPOINT
        + "?"
        + urlencode(
            {
                "openid.mode": "checkid_setup",
                "openid.ns": OPENID_NS,
                "openid.identity": _SELECT,
                "openid.claimed_id": _SELECT,
                "openid.return_to": callback,
                "openid.realm": realm,
            }
        )
    )
    worker = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
        name="pred-auth-listener",
    )
    try:
        worker.start()
        try:
            if on_ready is not None:
                on_ready(root)
            if open_browser and not finished.is_set() and not webbrowser.open(root):
                raise AuthError(
                    "Could not open the browser. Retry using the local URL readiness hook."
                )
        except Exception:
            raise AuthError(
                "Could not open the local login page. Retry with a local URL readiness hook."
            ) from None
        if not finished.wait(max(0, deadline - time.monotonic())):
            raise AuthTimeout("Steam login timed out. Start a new login to retry.")
        # Give the browser a brief opportunity to follow the clean redirect.
        page_seen.wait(min(0.5, max(0, deadline - time.monotonic())))
        if outcome["error"] is not None:
            raise outcome["error"]
        return outcome["result"]
    finally:
        with lock:
            outcome["active"] = False
            outcome["result"] = None
        with server.connection_lock:
            server.stopping = True
            for connection in server.connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if worker.is_alive():
            server.shutdown()
            worker.join(timeout=1)
        server.server_close()
