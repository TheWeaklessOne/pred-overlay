"""Synthetic authentication tests. No browser, remote request, or real credential use."""

import base64
import gc
import html
import http.client
import importlib.util
import io
import json
import os
import re
import socket
import stat
import tempfile
import threading
import time
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from predoverlay import auth

# Observed Valve field names and signed-list order; all identifying and secret
# values are synthetic. Tests bind state/return_to and refresh the nonce locally.
VALVE_CALLBACK_FIXTURE = {
    "state": "synthetic-state",
    "openid.ns": "http://specs.openid.net/auth/2.0",
    "openid.mode": "id_res",
    "openid.op_endpoint": "https://steamcommunity.com/openid/login",
    "openid.claimed_id": "https://steamcommunity.com/openid/id/76561198000000000",
    "openid.identity": "https://steamcommunity.com/openid/id/76561198000000000",
    "openid.return_to": "http://127.0.0.1:49152/callback?state=synthetic-state",
    "openid.response_nonce": "2026-10-26T00:00:00Zsynthetic-nonce",
    "openid.assoc_handle": "synthetic-association",
    "openid.signed": "signed,op_endpoint,claimed_id,return_to,response_nonce,assoc_handle",
    "openid.sig": base64.b64encode(b"\xfb\xff" * 16).decode(),
}


WINDOWS = os.name == "nt"
POSIX_ONLY = unittest.skipIf(WINDOWS, "POSIX modes, umask and uid ownership")


def session(omeda="synthetic-social-secret", predgg="synthetic-predgg-secret"):
    providers = {}
    if omeda is not None:
        providers["omeda"] = {"social_token": omeda}
    if predgg is not None:
        providers["predgg"] = {"access_token": predgg}
    return {"version": 1, "providers": providers}


def jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "e30." + encoded + ".synthetic-signature"


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        # macOS's /var and /tmp aliases are symlinks; use the actual temp directory.
        self.temp = tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir()))
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "session.json"
        self.store = auth.SessionStore(self.path)

    def write_raw(self, data, mode=0o600):
        self.path.write_bytes(data)
        self.path.chmod(mode)

    def test_missing_store_is_read_only_and_returns_empty(self):
        store = auth.SessionStore(self.directory / "missing" / "session.json")
        self.assertEqual(store.load(), session(None, None))
        self.assertEqual(store.status(), {"stored": False, "providers": {}})
        self.assertFalse(store.clear())
        self.assertFalse(store.path.parent.exists())

    def test_round_trip_providers_and_explicit_clear(self):
        self.store.save(session())
        self.assertEqual(self.store.load(), session())
        if not WINDOWS:
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        metadata = self.store.status()
        self.assertEqual(set(metadata["providers"]), {"omeda", "predgg"})
        self.assertNotIn("synthetic", json.dumps(metadata))
        self.assertTrue(self.store.clear())
        self.assertFalse(self.store.clear())
        self.assertEqual(list(self.directory.iterdir()), [])

    @POSIX_ONLY
    def test_save_creates_private_directories_even_with_permissive_umask(self):
        store = auth.SessionStore(self.directory / "new" / "nested" / "session.json")
        previous = os.umask(0)
        try:
            store.save(session())
        finally:
            os.umask(previous)
        for directory in (store.path.parent, store.path.parent.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)

    def test_atomic_replacement_keeps_old_file_until_private_temp_ready(self):
        self.store.save(session("old-secret", None))
        previous_inode = self.path.stat().st_ino
        replace = os.replace

        def inspect(source, target, **dir_fds):
            # Windows has no dir_fd and passes paths inside the vetted directory.
            if WINDOWS:
                self.assertEqual(Path(source).parent, self.directory)
                self.assertEqual(Path(target), self.path)
            else:
                self.assertEqual(dir_fds["src_dir_fd"], dir_fds["dst_dir_fd"])
                self.assertEqual(target, self.path.name)
            temporary = self.directory / source
            if not WINDOWS:
                self.assertEqual(stat.S_IMODE(temporary.stat().st_mode), 0o600)
            self.assertEqual(json.loads(temporary.read_text()), session())
            self.assertEqual(self.store.load(), session("old-secret", None))
            return replace(source, target, **dir_fds)

        with patch.object(auth.os, "replace", side_effect=inspect) as replacement:
            self.store.save(session())
        replacement.assert_called_once()
        self.assertNotEqual(previous_inode, self.path.stat().st_ino)
        self.assertEqual(self.store.load(), session())
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_failed_replace_or_fsync_preserves_old_file_and_removes_temp(self):
        self.store.save(session("old-secret", None))
        for method in ("replace", "fsync"):
            with self.subTest(method=method):
                with patch.object(auth.os, method, side_effect=OSError("synthetic-secret")):
                    with self.assertRaises(auth.AuthError) as raised:
                        self.store.save(session())
                self.assertNotIn("synthetic-secret", str(raised.exception))
                self.assertEqual(self.store.load(), session("old-secret", None))
                self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_temp_name_collision_does_not_remove_or_follow_existing_file(self):
        collision = self.directory / ".session-collision.tmp"
        collision.write_text("untouched")
        with patch.object(auth.secrets, "token_hex", return_value="collision"):
            with self.assertRaises(auth.AuthError):
                self.store.save(session())
        self.assertEqual(collision.read_text(), "untouched")
        self.assertFalse(self.path.exists())

    def test_serialized_size_is_checked_before_mutation(self):
        oversized = session('"' * 16384, "\\" * 16384)
        self.store.save(session())
        before = self.path.read_bytes()
        with self.assertRaises(auth.AuthError):
            self.store.save(oversized)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.directory.iterdir()), [self.path])
        missing = auth.SessionStore(self.directory / "absent" / "session.json")
        with self.assertRaises(auth.AuthError):
            missing.save(oversized)
        self.assertFalse(missing.path.parent.exists())
        # Largest representable document below the byte limit still round-trips.
        shorter = 16384
        while True:
            value = session('"' * 16384, "\\" * shorter)
            size = len((json.dumps(value, separators=(",", ":")) + "\n").encode())
            if size <= auth._MAX_SESSION_BYTES:
                break
            shorter -= 1
        self.assertLessEqual(auth._MAX_SESSION_BYTES - size, 1)
        self.store.save(value)
        self.assertEqual(self.store.load(), value)

    def test_leaf_symlink_is_rejected_for_all_operations(self):
        target = self.directory / "target.json"
        target.write_text("untouched")
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                self.path.symlink_to(self.directory / "absent" if dangling else target)
                for operation in (
                    self.store.load,
                    self.store.status,
                    self.store.clear,
                    lambda: self.store.save(session()),
                ):
                    with self.assertRaises(auth.AuthError):
                        operation()
                self.assertTrue(self.path.is_symlink())
                self.assertEqual(target.read_text(), "untouched")
                self.path.unlink()

    def test_parent_and_ancestor_symlinks_are_rejected(self):
        real = self.directory / "real"
        (real / "nested").mkdir(parents=True)
        alias = self.directory / "alias"
        alias.symlink_to(real, target_is_directory=True)
        for parent in (alias, alias / "nested"):
            store = auth.SessionStore(parent / "session.json")
            for operation in (store.load, store.clear, store.status, lambda: store.save(session())):
                with self.subTest(parent=parent), self.assertRaises(auth.AuthError):
                    operation()
        self.assertEqual(list(real.rglob("session.json")), [])

    def test_hardlinks_permissions_and_nonregular_files_fail_closed(self):
        self.store.save(session())
        link = self.directory / "hardlink"
        os.link(self.path, link)
        with self.assertRaises(auth.AuthError):
            self.store.load()
        with self.assertRaises(auth.AuthError):
            self.store.save(session())
        link.unlink()
        if not WINDOWS:
            for mode in (0o644, 0o660, 0o400, 0o1600):
                self.path.chmod(mode)
                for operation in (
                    self.store.load,
                    self.store.clear,
                    lambda: self.store.save(session()),
                ):
                    with self.subTest(mode=mode), self.assertRaises(auth.AuthError):
                        operation()
            self.path.unlink()
            os.mkfifo(self.path, 0o600)
            with self.assertRaises(auth.AuthError):
                self.store.load()
        self.path.unlink()
        self.path.mkdir(mode=0o700)
        with self.assertRaises(auth.AuthError):
            self.store.load()

    @POSIX_ONLY
    def test_writable_parent_and_wrong_owner_are_rejected(self):
        self.directory.chmod(0o777)
        try:
            with self.assertRaises(auth.AuthError):
                self.store.save(session())
        finally:
            self.directory.chmod(0o700)
        self.store.save(session())
        with patch.object(auth.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(auth.AuthError):
                self.store.load()

    def test_swapped_leaf_before_replace_never_writes_through_symlink(self):
        self.store.save(session("old-secret", None))
        victim = self.directory / "victim"
        victim.write_text("untouched")
        original = self.store._existing
        calls = 0

        def swap(directory):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.path.unlink()
                self.path.symlink_to(victim)
            return original(directory)

        with patch.object(self.store, "_existing", side_effect=swap):
            with self.assertRaises(auth.AuthError):
                self.store.save(session())
        self.assertEqual(victim.read_text(), "untouched")
        self.assertTrue(self.path.is_symlink())
        self.assertFalse(list(self.directory.glob(".session-*.tmp")))

    def test_corrupt_duplicate_oversized_and_invalid_sessions_do_not_leak(self):
        for raw in (
            b'{"secret":"synthetic-secret",',
            b"\xffsynthetic-secret",
            b'{"version":1,"version":1,"providers":{}}',
            b"x" * 65537,
            b"[]",
            b'{"version":true,"providers":{}}',
            json.dumps(
                {"version": 1, "providers": {"unknown-secret": {"token": "secret"}}}
            ).encode(),
        ):
            with self.subTest(raw_length=len(raw)):
                self.write_raw(raw)
                with self.assertRaises(auth.AuthError) as raised:
                    self.store.load()
                self.assertNotIn("synthetic-secret", str(raised.exception))
                self.assertNotIn("unknown-secret", str(raised.exception))

    def test_provider_fields_cannot_be_mixed_or_persist_assertions(self):
        for value in (
            {"token": "secret"},
            {"version": 1, "providers": {"omeda": {"access_token": "secret"}}},
            {"version": 1, "providers": {"predgg": {"social_token": "secret"}}},
            {
                "version": 1,
                "providers": {"omeda": {"social_token": "secret", "providerToken": "assertion"}},
            },
            session("", None),
            session("secret\n", None),
            session("a" * 16385, None),
        ):
            with self.subTest(value_type=type(value)), self.assertRaises(auth.AuthError):
                self.store.save(value)
        self.assertFalse(self.path.exists())

    def test_expiry_is_metadata_only_and_explicitly_unverified(self):
        token = jwt({"exp": 200, "sub": "private-identity", "email": "private-email"})
        self.store.save(session(token, "opaque-secret"))
        with patch.object(auth.time, "time", return_value=201):
            metadata = self.store.status()
        self.assertEqual(
            metadata["providers"]["omeda"]["expiry_hint"],
            {
                "source": "unverified_jwt_claim",
                "verified": False,
                "expires_at": 200,
                "expired": True,
            },
        )
        self.assertIsNone(metadata["providers"]["predgg"]["expiry_hint"])
        for secret in (token, "private-identity", "private-email", "opaque-secret"):
            self.assertNotIn(secret, json.dumps(metadata))
        self.assertEqual(self.store.load()["providers"]["omeda"]["social_token"], token)

    def test_invalid_expiry_never_becomes_verified_or_a_token_validity_decision(self):
        for payload in (
            {},
            {"exp": True},
            {"exp": "100"},
            {"exp": float("inf")},
            {"exp": -1},
            {"exp": 10**1000},
            [],
            "secret",
        ):
            with self.subTest(payload_type=type(payload)):
                self.store.save(session(jwt(payload), None))
                self.assertIsNone(self.store.status()["providers"]["omeda"]["expiry_hint"])
        self.store.save(session("not.a.jwt", None))
        self.assertIsNone(self.store.status()["providers"]["omeda"]["expiry_hint"])

    def test_store_never_prints_secrets(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.store.save(session())
            self.store.load()
            self.store.status()
            self.store.clear()
        self.assertEqual(output.getvalue(), "")


class LoginAttempt:
    """Drive a real ephemeral loopback listener with only synthetic assertions."""

    def __init__(self, exchange=None, timeout=3, **kwargs):
        self.exchange = exchange if exchange is not None else Mock(return_value={"verified": True})
        self.result = self.error = None
        self.ready = threading.Event()
        self.root = None
        self.timeout = timeout
        self.kwargs = kwargs

    def __enter__(self):
        def ready(root):
            self.root = root
            self.ready.set()

        def run():
            try:
                self.result = auth.steam_login(
                    self.exchange, timeout=self.timeout, on_ready=ready, **self.kwargs
                )
            except BaseException as error:
                self.error = error

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        if not self.ready.wait(2):
            raise AssertionError("Synthetic login listener did not start")
        self.address = urlsplit(self.root)
        status, headers, page = self.request("/?" + self.address.query)
        if status != 200:
            raise AssertionError("Local login page failed")
        self.page, self.headers = page, headers
        self.login = html.unescape(re.search(r'href="(https:[^"]+)"', page).group(1))
        login_query = parse_qs(urlsplit(self.login).query)
        self.callback = login_query["openid.return_to"][0]
        self.state = parse_qs(urlsplit(self.callback).query)["state"][0]
        return self

    def request(self, target, headers=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.address.port, timeout=2)
        try:
            connection.request(method, target, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            connection.close()

    def raw(self, data):
        with socket.create_connection(("127.0.0.1", self.address.port), timeout=2) as client:
            client.sendall(data)
            response = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    return response
                response += chunk

    def assertion(self, **updates):
        query = dict(VALVE_CALLBACK_FIXTURE)
        query.update(
            {
                "state": self.state,
                "openid.return_to": self.callback,
                "openid.response_nonce": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                + "synthetic-nonce",
            }
        )
        query.update(updates)
        return query

    def submit(self, query=None, *, follow=True):
        query = self.assertion() if query is None else query
        response = self.request("/callback?" + urlencode(query))
        if follow and response[0] == 303:
            self.request(response[1]["Location"])
        return response

    def wait(self):
        self.thread.join(2)
        if self.thread.is_alive():
            raise AssertionError("Synthetic login did not stop")

    def __exit__(self, *args):
        if self.thread.is_alive():
            try:
                response = self.request("/cancel?state=" + self.state)
                if response[0] == 303:
                    self.request("/complete")
            except (OSError, http.client.HTTPException):
                pass
        self.thread.join(3)
        if self.thread.is_alive():
            raise AssertionError("Synthetic login listener leaked")


class SteamLoginTests(unittest.TestCase):
    def setUp(self):
        browser = patch.object(auth.webbrowser, "open", return_value=True)
        self.browser = browser.start()
        self.addCleanup(browser.stop)

    def assert_closed(self, attempt):
        with socket.socket() as client:
            client.settimeout(0.3)
            self.assertNotEqual(client.connect_ex(("127.0.0.1", attempt.address.port)), 0)
        self.assertFalse(
            any(thread.name == "pred-auth-listener" for thread in threading.enumerate())
        )

    def test_success_uses_original_endpoint_exact_exchange_and_local_browser_url(self):
        verified = {"pragmaTokens": {"pragmaSocialToken": "synthetic-verified-token"}}
        exchange = Mock(return_value=verified)
        with LoginAttempt(exchange) as attempt:
            self.assertEqual(attempt.address.hostname, "127.0.0.1")
            self.assertGreater(attempt.address.port, 0)
            launch = parse_qs(attempt.address.query)
            self.assertEqual(set(launch), {"start"})
            self.assertGreaterEqual(len(launch["start"][0]), 40)
            self.assertEqual(attempt.address.path, "/")
            login = urlsplit(attempt.login)
            self.assertEqual(login.scheme + "://" + login.netloc + login.path, auth.STEAM_ENDPOINT)
            params = parse_qs(login.query)
            self.assertEqual(params["openid.mode"], ["checkid_setup"])
            self.assertEqual(params["openid.realm"], [attempt.root.split("?", 1)[0]])
            self.assertGreaterEqual(len(attempt.state), 40)
            query = attempt.assertion()
            status, headers, page = attempt.submit(query)
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/complete")
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertNotIn(query["openid.sig"], page)
            self.assertNotIn("synthetic-verified-token", page)
            attempt.wait()
            self.assertIsNone(attempt.error)
            self.assertIs(attempt.result, verified)
            exchange.assert_called_once()
            payload = exchange.call_args.args[0]
            self.assertEqual(set(payload), {"providerId", "providerToken"})
            self.assertEqual(payload["providerId"], "STEAM")
            self.assertIsInstance(payload["providerToken"], str)
            self.assertEqual(
                json.loads(payload["providerToken"]),
                {target: query["openid." + source] for target, source in auth._FIELDS.items()},
            )
            self.browser.assert_called_once_with(attempt.root)
        self.assert_closed(attempt)

    def test_bare_root_and_wrong_launch_key_reveal_no_state_or_authorization_url(self):
        with LoginAttempt(open_browser=False) as attempt:
            for path in (
                "/",
                "/?start=wrong",
                "/?start=wrong&start=wrong",
                "/?state=" + attempt.state,
            ):
                with self.subTest(path=path):
                    status, _, page = attempt.request(path)
                    self.assertIn(status, (400, 403))
                    self.assertNotIn(attempt.state, page)
                    self.assertNotIn(attempt.callback, page)
                    self.assertNotIn(auth.STEAM_ENDPOINT, page)
                    self.assertNotIn(attempt.address.query, page)
            attempt.exchange.assert_not_called()
            attempt.submit()
            attempt.wait()
            self.assertIsNone(attempt.error)

    def test_browser_can_be_disabled_and_state_changes_each_attempt(self):
        states = []
        for _ in range(2):
            with LoginAttempt(open_browser=False) as attempt:
                states.append(attempt.state)
                attempt.submit()
                attempt.wait()
        self.assertNotEqual(states[0], states[1])
        self.browser.assert_not_called()

    def test_invalid_callbacks_never_exchange_or_consume_then_valid_succeeds(self):
        with LoginAttempt(open_browser=False) as attempt:
            changes = [
                {"state": "wrong-state"},
                {"state": ""},
                {"openid.mode": "check_authentication"},
                {"openid.ns": "http://specs.openid.net/auth/1.1"},
                {"openid.op_endpoint": "https://steamcommunity.com.evil.invalid/openid/login"},
                {"openid.op_endpoint": "http://steamcommunity.com/openid/login"},
                {"openid.claimed_id": "https://steamcommunity.com/openid/id/7656119800000000"},
                {"openid.identity": "https://steamcommunity.com/openid/id/76561198000000001"},
                {"openid.return_to": attempt.callback + "&extra=1"},
                {"openid.return_to": attempt.callback.replace("127.0.0.1", "localhost")},
                {"openid.sig": "forged-signature"},
                {"openid.sig": ""},
                {"openid.assoc_handle": "a" * 256},
                {"openid.assoc_handle": "secret\nvalue"},
                {"openid.response_nonce": "2020-01-01T00:00:00Zsynthetic-stale"},
                {"openid.response_nonce": "2099-01-01T00:00:00Zsynthetic-future"},
                {"openid.response_nonce": "2026-02-30T00:00:00Zbad-date"},
                {"openid.response_nonce": "2026-10-26T00:00:00+00:00wrong-zone"},
                {"openid.unexpected": "unexpected"},
            ]
            for change in changes:
                with self.subTest(field=list(change)[0]):
                    status, _, page = attempt.submit(attempt.assertion(**change))
                    self.assertEqual(status, 400)
                    self.assertNotIn("wrong-state", page)
                    attempt.exchange.assert_not_called()
            for field in attempt.assertion():
                query = attempt.assertion()
                del query[field]
                with self.subTest(missing=field):
                    self.assertEqual(attempt.submit(query)[0], 400)
                    attempt.exchange.assert_not_called()
            attempt.submit()
            attempt.wait()
            self.assertEqual(attempt.result, {"verified": True})

    def test_required_signed_fields_cannot_be_omitted_or_duplicated(self):
        with LoginAttempt(open_browser=False) as attempt:
            signed = attempt.assertion()["openid.signed"].split(",")
            required = {"op_endpoint", "claimed_id", "return_to", "response_nonce", "assoc_handle"}
            variants = [
                ",".join(field for field in signed if field != missing) for missing in required
            ]
            variants += [
                ",".join(signed + ["claimed_id"]),
                ",".join(signed) + ",",
                ",".join(signed + ["unknown"]),
                ",".join(signed + ["sig"]),
                ",".join(signed + ["signed"]),
            ]
            for value in variants:
                with self.subTest(signed=value):
                    self.assertEqual(
                        attempt.submit(attempt.assertion(**{"openid.signed": value}))[0], 400
                    )
            attempt.exchange.assert_not_called()

    def test_observed_valve_signed_list_reaches_verifier_unchanged(self):
        verified = {"access_token": "synthetic-verified-token", "profile": {"synthetic": True}}
        exchange = Mock(return_value=verified)
        with LoginAttempt(exchange, open_browser=False) as attempt:
            query = attempt.assertion()
            self.assertEqual(
                query["openid.signed"],
                "signed,op_endpoint,claimed_id,return_to,response_nonce,assoc_handle",
            )
            self.assertNotIn("identity", query["openid.signed"].split(","))
            self.assertEqual(attempt.submit(query)[0], 303)
            attempt.wait()
            self.assertIsNone(attempt.error)
            self.assertIs(attempt.result, verified)
            exchange.assert_called_once()
            body = exchange.call_args.args[0]
            self.assertEqual(set(body), {"providerId", "providerToken"})
            assertion = json.loads(body["providerToken"])
            self.assertEqual(assertion["signed"], query["openid.signed"])
            self.assertEqual(assertion["identity"], assertion["claimedId"])

    def test_valve_unsigned_identity_must_equal_signed_claimed_id(self):
        with LoginAttempt(open_browser=False) as attempt:
            query = attempt.assertion(
                **{"openid.identity": "https://steamcommunity.com/openid/id/76561198000000001"}
            )
            self.assertNotIn("identity", query["openid.signed"].split(","))
            self.assertEqual(attempt.submit(query)[0], 400)
            attempt.exchange.assert_not_called()
            attempt.submit()
            attempt.wait()
            attempt.exchange.assert_called_once()
            self.assertIsNone(attempt.error)

    def test_optional_signed_and_identity_entries_remain_supported(self):
        variants = (
            "op_endpoint,claimed_id,return_to,response_nonce,assoc_handle",
            "op_endpoint,claimed_id,identity,return_to,response_nonce,assoc_handle",
        )
        for signed in variants:
            with self.subTest(signed=signed), LoginAttempt(open_browser=False) as attempt:
                self.assertEqual(
                    attempt.submit(attempt.assertion(**{"openid.signed": signed}))[0], 303
                )
                attempt.wait()
                self.assertIsNone(attempt.error)
                attempt.exchange.assert_called_once()

    def test_nonce_age_skew_and_attempt_start_boundaries(self):
        with LoginAttempt(open_browser=False) as attempt:
            now = int(time.time())
            for offset, start_offset, valid in (
                (-300, -600, True),
                (-301, -600, False),
                (30, -600, True),
                (31, -600, False),
                (-30, 0, True),
                (-31, 0, False),
            ):
                nonce = (
                    datetime.fromtimestamp(now + offset, timezone.utc).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    )
                    + "nonce"
                )
                query = attempt.assertion(**{"openid.response_nonce": nonce})
                with (
                    self.subTest(offset=offset, start_offset=start_offset),
                    patch.object(auth.time, "time", return_value=now),
                ):
                    if valid:
                        self.assertIsInstance(
                            auth._assertion(
                                query, attempt.state, attempt.callback, now + start_offset
                            ),
                            dict,
                        )
                    else:
                        with self.assertRaises(ValueError):
                            auth._assertion(
                                query, attempt.state, attempt.callback, now + start_offset
                            )
            attempt.exchange.assert_not_called()

    def test_duplicates_encoding_parameter_and_size_limits(self):
        with LoginAttempt(open_browser=False) as attempt:
            query = urlencode(attempt.assertion())
            targets = [
                "/callback?" + query + "&state=" + attempt.state,
                "/callback?" + query + "&%73tate=" + attempt.state,
                "/callback?" + query + "&openid.sig=synthetic-duplicate",
                "/callback?" + query + "&bad=%GG",
                "/callback?" + query + "&bad=%FF",
                "/callback?" + query + "&bare",
                "/callback?" + query + "&bad=",
                "/callback?" + "&".join("x%d=v" % index for index in range(17)),
                "/callback?" + "x" * 8192,
            ]
            for target in targets:
                with self.subTest(length=len(target)):
                    self.assertEqual(attempt.request(target)[0], 400)
            attempt.exchange.assert_not_called()

    def test_host_path_method_and_request_target_are_strict(self):
        with LoginAttempt(open_browser=False) as attempt:
            target = "/callback?" + urlencode(attempt.assertion())
            for host in (
                "localhost:" + str(attempt.address.port),
                "127.0.0.1",
                "evil.invalid",
                "127.0.0.1:" + str(attempt.address.port + 1),
                attempt.address.netloc + ",evil.invalid",
            ):
                with self.subTest(host=host):
                    self.assertEqual(attempt.request(target, {"Host": host})[0], 400)
            for path in (
                "/callback/",
                "/%63allback",
                "/a/../callback",
                "//callback",
                attempt.root + "callback",
                "/callback#fragment",
            ):
                with self.subTest(path=path):
                    response = attempt.raw(
                        (
                            "GET "
                            + path
                            + "?"
                            + urlencode(attempt.assertion())
                            + " HTTP/1.1\r\nHost: "
                            + attempt.address.netloc
                            + "\r\n\r\n"
                        ).encode()
                    )
                    self.assertRegex(response.split(b"\r\n")[0], rb"HTTP/1.0 (400|404)")
            for hosts in (
                "",
                "Host: " + attempt.address.netloc + "\r\nHost: " + attempt.address.netloc + "\r\n",
            ):
                response = attempt.raw(
                    ("GET " + target + " HTTP/1.1\r\n" + hosts + "\r\n").encode()
                )
                self.assertIn(b" 400 ", response.split(b"\r\n")[0])
            self.assertEqual(attempt.request(target, method="POST")[0], 501)
            self.assertEqual(attempt.request(target, {"Transfer-Encoding": "chunked"})[0], 400)
            self.assertEqual(attempt.request(target, {"Content-Length": "1"})[0], 400)
            self.assertEqual(attempt.request("/favicon.ico")[0], 404)
            attempt.exchange.assert_not_called()
            attempt.submit()
            attempt.wait()
            self.assertIsNone(attempt.error)

    def test_forgery_is_sent_once_to_verifier_and_never_becomes_identity(self):
        entered, release = threading.Event(), threading.Event()

        def reject(body):
            entered.set()
            release.wait(2)
            raise ValueError("synthetic-sig-secret synthetic-token-secret")

        exchange = Mock(side_effect=reject)
        with LoginAttempt(exchange, open_browser=False) as attempt:
            query = attempt.assertion()
            self.assertEqual(attempt.submit(query)[0], 303)
            self.assertTrue(entered.wait(1))
            self.assertEqual(attempt.submit(query)[0], 409)
            self.assertIsNone(attempt.result)
            release.set()
            attempt.wait()
            exchange.assert_called_once()
            self.assertIsNone(attempt.result)
            self.assertIsInstance(attempt.error, auth.AuthError)
            self.assertNotIn("synthetic", "".join(traceback.format_exception(attempt.error)))
        self.assert_closed(attempt)

    def test_empty_or_non_dict_exchange_results_are_rejected(self):
        for result in ({}, None, "synthetic-token"):
            with (
                self.subTest(result_type=type(result)),
                LoginAttempt(Mock(return_value=result), open_browser=False) as attempt,
            ):
                attempt.submit()
                attempt.wait()
                self.assertIsNone(attempt.result)
                self.assertIsInstance(attempt.error, auth.AuthError)

    def test_cancel_requires_state_and_closes_without_exchange(self):
        with LoginAttempt(open_browser=False) as attempt:
            query = {"state": "wrong", "openid.ns": auth.OPENID_NS, "openid.mode": "cancel"}
            self.assertEqual(attempt.submit(query)[0], 400)
            self.assertEqual(attempt.request("/cancel?state=wrong")[0], 400)
            query["state"] = attempt.state
            self.assertEqual(attempt.submit(query)[0], 303)
            attempt.wait()
            self.assertIsInstance(attempt.error, auth.AuthCancelled)
            attempt.exchange.assert_not_called()
        self.assert_closed(attempt)

    def test_timeout_closes_listener_and_incomplete_http_connection(self):
        started = time.monotonic()
        with LoginAttempt(timeout=0.3, open_browser=False) as attempt:
            with socket.create_connection(("127.0.0.1", attempt.address.port), timeout=1) as client:
                client.sendall(b"GET /callback?synthetic-secret HTTP/1.1\r\n")
                attempt.wait()
                self.assertEqual(client.recv(1024), b"")
            self.assertIsInstance(attempt.error, auth.AuthTimeout)
            attempt.exchange.assert_not_called()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assert_closed(attempt)

    def test_timeout_and_local_cancel_discard_late_exchange(self):
        for cancel in (False, True):
            entered, release, returned = threading.Event(), threading.Event(), threading.Event()

            def slow(body):
                entered.set()
                release.wait(3)
                returned.set()
                return {"token": "late-synthetic-secret"}

            with (
                self.subTest(cancel=cancel),
                LoginAttempt(slow, timeout=0.4, open_browser=False) as attempt,
            ):
                attempt.submit()
                self.assertTrue(entered.wait(1))
                if cancel:
                    self.assertEqual(attempt.request("/cancel?state=" + attempt.state)[0], 303)
                attempt.wait()
                self.assertIsInstance(
                    attempt.error, auth.AuthCancelled if cancel else auth.AuthTimeout
                )
                self.assertIsNone(attempt.result)
                self.assert_closed(attempt)
                release.set()
                self.assertTrue(returned.wait(1))
                self.assertIsNone(attempt.result)

    def test_keyboard_interrupt_readiness_and_browser_failure_close_listener(self):
        for failure in (KeyboardInterrupt(), ValueError("synthetic-secret"), None):
            urls = []

            def ready(root):
                urls.append(root)
                if failure is not None:
                    raise failure

            self.browser.return_value = False
            with (
                self.subTest(failure=type(failure)),
                self.assertRaises(
                    KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else auth.AuthError
                ) as raised,
            ):
                auth.steam_login(Mock(), timeout=1, on_ready=ready)
            self.assertNotIn(
                "synthetic-secret", "".join(traceback.format_exception(raised.exception))
            )
            address = urlsplit(urls[0])
            with socket.socket() as client:
                self.assertNotEqual(client.connect_ex(("127.0.0.1", address.port)), 0)

    def test_no_access_logs_assertion_echo_or_automatic_persistence(self):
        output = io.StringIO()
        with (
            redirect_stdout(output),
            redirect_stderr(output),
            patch.object(
                auth.SessionStore, "save", side_effect=AssertionError("Unexpected persistence")
            ),
        ):
            with LoginAttempt(open_browser=False) as attempt:
                request = (
                    b"SECRET-METHOD /synthetic-request-secret HTTP/1.1\r\nHost: "
                    + attempt.address.netloc.encode()
                    + b"\r\n\r\n"
                )
                response = attempt.raw(request)
                self.assertNotIn(b"synthetic-request-secret", response)
                self.assertNotIn(b"SECRET-METHOD", response)
                attempt.submit()
                attempt.wait()
        self.assertEqual(output.getvalue(), "")

    def test_invalid_arguments_do_not_bind_or_open_browser(self):
        for kwargs in (
            {"timeout": 0},
            {"timeout": -1},
            {"timeout": float("inf")},
            {"timeout": float("nan")},
            {"timeout": True},
            {"port": -1},
            {"port": 65536},
            {"port": True},
            {"on_ready": "not-callable"},
        ):
            with self.subTest(kwargs=kwargs), patch.object(auth, "HTTPServer") as server:
                with self.assertRaises(auth.AuthError):
                    auth.steam_login(Mock(), **kwargs)
                server.assert_not_called()
        self.browser.assert_not_called()


class ImportTests(unittest.TestCase):
    def test_import_has_no_file_network_browser_or_output_side_effects(self):
        output = io.StringIO()
        spec = importlib.util.spec_from_file_location(
            "predoverlay._auth_import_test", auth.__file__
        )
        module = importlib.util.module_from_spec(spec)
        # Earlier tests leave cyclic garbage whose finalizers may warn; collect it
        # now so only output produced by the import itself is captured.
        gc.collect()
        with (
            redirect_stdout(output),
            redirect_stderr(output),
            patch("os.open", side_effect=AssertionError("Unexpected file access")),
            patch("socket.socket", side_effect=AssertionError("Unexpected socket")),
            patch("webbrowser.open", side_effect=AssertionError("Unexpected browser")),
        ):
            spec.loader.exec_module(module)
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
