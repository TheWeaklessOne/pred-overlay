"""Session storage and a browser sign-in on a loopback listener (OAuth 2 with PKCE).

Nothing runs on import. ``oauth_login`` never persists credentials; the caller
owns the provider: the authorize page and the code exchange (see predgg.py).
Late results after timeout/cancellation are discarded; Python cannot kill a
stuck callable.
"""

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
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from .errors import PredError

# The one record kept: Pred.gg's tokens for the user's own account.
_RECORD = {"access_token": str, "refresh_token": str, "expires_at": (int, float)}
# Records older versions stored and nothing reads any more: dropped when the
# file is read, so the next save leaves them out (the Omeda session).
_RETIRED = {"omeda", "omeda_game"}
_MAX_SESSION_BYTES = 65536
# Seconds the listener stays up for a browser on the self-refreshing page to fetch the
# result: it comes back each second, and the listener closes as soon as it did.
RESULT_WAIT = 5.0
EXCHANGE_GRACE = 60  # seconds the code exchange may take once the provider answered in time
# The local pages, in the overlay's colours (no fonts or images are loaded).
_PAGE_STYLE = (
    "html{background:#010914;color:#E9ECF0;font:16px/1.5 Bahnschrift,'Segoe UI',sans-serif}"
    "main{max-width:420px;margin:18vh auto 0;padding:28px 32px;background:#06101C;"
    "border:1px solid #404864;border-top:2px solid #C7925B}"
    ".brand{margin:0;color:#C7925B;font-size:13px;letter-spacing:.18em;text-transform:uppercase}"
    "h1{margin:6px 0 10px;font-size:26px;font-weight:600;letter-spacing:.04em}"
    "p{color:#BDC2C7}a{color:#EDD01E}"
)
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


def _token(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 16384
        and not any(ord(c) < 33 or ord(c) > 126 for c in value)
    )


def _predgg(record):
    """A valid Pred.gg record, or None for one in an older shape (dropped, not fatal)."""
    if not isinstance(record, dict) or set(record) != set(_RECORD):
        return None
    expires = record["expires_at"]
    if (
        not _token(record["access_token"])
        or not _token(record["refresh_token"])
        or type(expires) not in (int, float)
        or not math.isfinite(expires)
        or not 0 <= expires <= 253402300799
    ):
        raise AuthError("Invalid session credential field.")
    return {key: record[key] for key in _RECORD}


def _session(value):
    """Whitelist credential fields so codes and arbitrary metadata stay off disk."""
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
        if provider in _RETIRED:
            continue
        if provider != "predgg":
            raise AuthError("Invalid session provider.")
        found = _predgg(record)
        if found is not None:
            providers["predgg"] = found
    return {"version": 1, "providers": providers}


def _is_link(info):
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) in _LINK_TAGS


def _entry(directory, name):
    """Address ``name`` inside a held directory fd (POSIX) or a vetted path (Windows)."""
    if _WINDOWS:
        return os.path.join(directory, name), {}
    return name, {"dir_fd": directory}


def _replace_patiently(source, target, tries=10, pause=0.05):
    """os.replace that waits out a moment of another open handle (Windows sharing)."""
    for attempt in range(tries):
        try:
            return os.replace(source, target)
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(pause)


class SessionStore:
    """A POSIX 0600 JSON file at an explicit caller-selected path.

    ``load()`` returns credentials (never print its result); missing files return
    ``{"version": 1, "providers": {}}``. ``save(session)`` atomically replaces the
    whole document, so concurrent writers are last-writer-wins. The only allowed
    record is ``predgg: {access_token, refresh_token, expires_at}``.
    ``status()`` returns metadata only. ``clear()`` unlinks and returns a bool;
    unlinking is not guaranteed secure erasure. No default credential path exists.

    Symlinks in any path component and hard-linked/non-private session files are
    rejected. The final parent must be user-owned and not group/world-writable.
    Missing directories are created as 0700 only during an explicit save.

    Windows has no dir_fd, O_NOFOLLOW or POSIX modes: the session's own folder must
    not be a symlink or junction (those above it may be: a profile moved to another
    drive is one), an opened file must be the vetted entry, and privacy relies on
    the inherited profile ACLs instead of 0600/uid checks.
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
        """The session's folder, made if asked. The folder itself must be a plain one.
        Those above it may be junctions or mounted volumes: a profile or AppData moved
        to another drive is one, and refusing it would make signing in impossible."""
        parts = self.path.parent.parts[1:]
        directory = self.path.anchor
        for index, part in enumerate(parts):
            directory = os.path.join(directory, part)
            if create:
                try:
                    os.mkdir(directory, 0o700)
                except FileExistsError:
                    pass
            info = os.stat(directory)  # through a junction above: where it leads
            if not stat.S_ISDIR(info.st_mode):
                raise AuthError("The session's folder path holds something that is no folder.")
            if index == len(parts) - 1 and _is_link(os.stat(directory, follow_symlinks=False)):
                raise AuthError("The session's own folder must not be a link or junction.")
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
                        names = [_entry(directory, n)[0] for n in (temporary, self.path.name)]
                        _replace_patiently(*names)
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
        """Return whitelisted metadata, without tokens."""
        record = self.load()["providers"].get("predgg")
        if record is None:
            return {"stored": False, "providers": {}}
        expires = record["expires_at"]
        return {
            "stored": True,
            "providers": {"predgg": {"expires_at": expires, "expired": expires <= time.time()}},
        }


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
        not key
        or (not value and key not in _FREE_TEXT)
        or any(ord(c) < 33 or ord(c) > 126 for c in key + ("" if key in _FREE_TEXT else value))
        for key, value in pairs
    ):
        raise ValueError("Invalid parameter")
    return result


# Parameters an OAuth server words for people, or lists with spaces (the granted scope):
# nothing reads them, and refusing the whole answer for them left a sign-in waiting out
# its three minutes.
_FREE_TEXT = ("error_description", "error_uri", "scope")


def _authorization(query, state):
    """The code from Pred.gg's answer on the callback, or None when the user declined."""
    if not secrets.compare_digest(query.get("state", ""), state):
        raise ValueError("Invalid state")
    if "error" in query:
        if not set(query) <= {"state", "error", "error_description", "error_uri", "iss"}:
            raise ValueError("Unexpected fields")
        return None
    if not set(query) <= {"state", "code", "iss", "scope"} or not query.get("code"):
        raise ValueError("Unexpected fields")
    if len(query["code"]) > 2048:
        raise ValueError("Invalid code")
    return query["code"]


def oauth_login(
    authorize_url,
    exchange,
    *,
    open_browser=True,
    timeout=180,
    port=0,
    on_ready=None,
    cancel=None,
) -> dict:
    """Run one browser sign-in and return the verified exchange dict; never saves it.

    ``authorize_url(redirect_uri, state)`` returns the provider's authorize page for
    this attempt; ``exchange(code, redirect_uri)`` turns the code into tokens (a
    non-empty dict) and must impose its own transport timeout. The callback is
    ``http://127.0.0.1:<port>/callback``: the provider must have that exact URL
    registered, so ``port`` is fixed in production (0 picks a free one in tests).

    ``on_ready(local_root_url)`` runs after the listener starts. Its root-path URL
    includes a random launch key; pass it through unchanged and keep it private.
    The optional browser opens that same local URL, which sends it straight on to
    the provider. Bare / reveals no login state. ``cancel``, a threading.Event,
    ends the attempt with AuthCancelled once it is set. Invalid callbacks do not
    consume the attempt; one well-formed answer does, even if the exchange fails.
    AuthCancelled/AuthTimeout/AuthError have sanitized messages.
    """
    if (
        not callable(authorize_url)
        or not callable(exchange)
        or (on_ready is not None and not callable(on_ready))
    ):
        raise AuthError("Login requires callable authorize, exchange and readiness hooks.")
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or timeout <= 0
        or type(port) is not int
        or not 0 <= port <= 65535
    ):
        raise AuthError("Login requires a positive finite timeout and a valid local port.")
    deadline = time.monotonic() + timeout
    state = secrets.token_urlsafe(32)
    launch_key = secrets.token_urlsafe(32)
    lock = threading.Lock()
    finished, page_seen, polled = threading.Event(), threading.Event(), threading.Event()
    outcome = {"active": True, "consumed": False, "result": None, "error": None}

    def limit():
        """The deadline, moved on once the provider answered: the exchange takes time."""
        return deadline + (EXCHANGE_GRACE if outcome["consumed"] else 0)

    def finish(result=None, error=None):
        with lock:
            if outcome["active"] and not finished.is_set() and time.monotonic() < limit():
                outcome.update(result=result, error=error)
                finished.set()

    def verify(code):
        try:
            result = exchange(code, callback)
            if not isinstance(result, dict) or not result:
                raise ValueError("Missing exchange result")
        except BaseException as exc:
            # Never propagate provider exceptions: they may include codes or tokens.
            # Their class and HTTP status are safe to show and say what went wrong.
            status = getattr(exc, "status", None)
            cause = type(exc).__name__ + (f", HTTP {status}" if isinstance(status, int) else "")
            # A network failure names its own cause (TransportError.cause: an error class,
            # never text from the provider): a certificate or proxy problem is not Pred.gg
            # refusing anything.
            reason = getattr(exc, "cause", None)
            if isinstance(reason, str) and reason.isidentifier():
                cause += f": {reason}"
            finish(
                error=AuthError(
                    f"Pred.gg did not finish this sign-in ({cause}). Start a new one to retry."
                )
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
            self.page(code, "The local sign-in request could not be accepted.")

        def page(self, code, message, *, extra="", location=None, title=None, head=""):
            data = (
                '<!doctype html><html lang="en"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                + head
                + "<title>Pred Overlay</title><style>"
                + _PAGE_STYLE
                + "</style><main><p class=brand>Pred Overlay</p><h1>"
                + html.escape(title or "Sign-in")
                + "</h1><p>"
                + html.escape(message)
                + "</p>"
                + extra
                + "</main></html>"
            ).encode("utf-8")
            self.send_response(code)
            for name, value in {
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
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
                return self.page(400, "The local sign-in request could not be accepted.")
            try:
                parsed = urlsplit(target.decode("ascii"))
                if parsed.scheme or parsed.netloc:
                    raise ValueError("Invalid target")
                query = _query(parsed.query)
            except (ValueError, UnicodeError):
                return self.page(400, "The local sign-in request could not be accepted.")
            if parsed.path == "/":
                if set(query) != {"start"} or not secrets.compare_digest(
                    query["start"], launch_key
                ):
                    return self.page(
                        403, "Open the private local sign-in link provided by Pred Overlay."
                    )
                # Straight on to the provider: the app's own window says what to do.
                return self.page(
                    303,
                    "Opening Pred.gg's sign-in page.",
                    extra='<p><a href="' + html.escape(login, quote=True) + '">Continue</a></p>',
                    location=login,
                )
            if parsed.path == "/complete" and not query:
                if finished.is_set():
                    if outcome["result"] is not None:
                        self.page(
                            200,
                            "You can close this tab and go back to the game.",
                            title="Signed in",
                        )
                    else:
                        self.page(
                            200,
                            "Pred Overlay says what went wrong. You can close this tab.",
                            title="Sign-in did not finish",
                        )
                    page_seen.set()
                else:
                    self.page(
                        200,
                        "Pred.gg is confirming the sign-in. This page updates by itself.",
                        title="Checking",
                        head='<meta http-equiv="refresh" content="1">',
                    )
                    polled.set()
                return
            if parsed.path == "/cancel":
                if set(query) != {"state"} or not secrets.compare_digest(query["state"], state):
                    return self.page(400, "This cancellation link is not valid.")
                with lock:
                    if finished.is_set() or not outcome["active"]:
                        return self.page(
                            409, "This sign-in has already been used. Return to Pred Overlay."
                        )
                    outcome["consumed"] = True
                finish(error=AuthCancelled("The sign-in was cancelled."))
            elif parsed.path == "/callback":
                try:
                    code = _authorization(query, state)
                except (ValueError, UnicodeError):
                    return self.page(
                        400,
                        "This sign-in answer is not valid. Start a new sign-in from Pred Overlay.",
                    )
                with lock:
                    if outcome["consumed"] or not outcome["active"] or time.monotonic() >= deadline:
                        return self.page(
                            409, "This sign-in has already been used or expired. Start a new one."
                        )
                    outcome["consumed"] = True
                if code is None:
                    finish(error=AuthCancelled("The sign-in was declined on Pred.gg."))
                else:
                    threading.Thread(
                        target=verify, args=(code,), daemon=True, name="pred-auth-exchange"
                    ).start()
            else:
                return self.page(404, "This local sign-in page does not exist.")
            # No code in the response body, Location, logs, or completion URL.
            self.page(303, "Return to Pred Overlay for your sign-in result.", location="/complete")

    try:
        server = LocalServer(("127.0.0.1", port), Handler)
    except OSError:
        raise AuthError(
            "Could not start the local sign-in listener (is another sign-in open?)."
        ) from None
    host = "127.0.0.1:" + str(server.server_port)
    realm = "http://" + host + "/"
    root = realm + "?" + urlencode({"start": launch_key})
    callback = realm + "callback"
    try:
        login = authorize_url(callback, state)
        if not isinstance(login, str) or not login.startswith("https://"):
            raise ValueError("Invalid authorize URL")
    except Exception:
        server.server_close()
        raise AuthError("Could not build the sign-in page address.") from None
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
                    "Could not open the browser. Check that Windows has a default browser,"
                    " then start the sign-in again."
                )
        except AuthError:
            raise
        except Exception:
            raise AuthError(
                "Could not open the local sign-in page. Start the sign-in again."
            ) from None
        while not finished.is_set():
            if cancel is not None and cancel.is_set():
                raise AuthCancelled("The sign-in was cancelled.")
            left = limit() - time.monotonic()
            if left <= 0:
                raise AuthTimeout("The sign-in timed out. Start a new one to retry.")
            finished.wait(min(left, 0.25))
        # Give the browser a brief opportunity to follow the clean redirect.
        # A browser on the self-refreshing page comes back within a second.
        page_seen.wait(RESULT_WAIT)
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
