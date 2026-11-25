"""Synthetic authentication tests. No browser, remote request, or real credential use."""

import gc
import http.client
import importlib.util
import io
import json
import os
import socket
import stat
import tempfile
import threading
import time
import traceback
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from predoverlay import auth

WINDOWS = os.name == "nt"
POSIX_ONLY = unittest.skipIf(WINDOWS, "POSIX modes, umask and uid ownership")


def session(access="synthetic-access-secret", refresh="synthetic-refresh-secret", expires=2e9):
    providers = {}
    if access is not None:
        providers["predgg"] = {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": expires,
        }
    return {"version": 1, "providers": providers}


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
        self.assertEqual(store.load(), session(None))
        self.assertEqual(store.status(), {"stored": False, "providers": {}})
        self.assertFalse(store.clear())
        self.assertFalse(store.path.parent.exists())

    def test_round_trip_providers_and_explicit_clear(self):
        self.store.save(session())
        self.assertEqual(self.store.load(), session())
        if not WINDOWS:
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        metadata = self.store.status()
        self.assertEqual(set(metadata["providers"]), {"predgg"})
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
        self.store.save(session("old-secret"))
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
            self.assertEqual(self.store.load(), session("old-secret"))
            return replace(source, target, **dir_fds)

        with patch.object(auth.os, "replace", side_effect=inspect) as replacement:
            self.store.save(session())
        replacement.assert_called_once()
        self.assertNotEqual(previous_inode, self.path.stat().st_ino)
        self.assertEqual(self.store.load(), session())
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_failed_replace_or_fsync_preserves_old_file_and_removes_temp(self):
        self.store.save(session("old-secret"))
        for method in ("replace", "fsync"):
            with self.subTest(method=method):
                with patch.object(auth.os, method, side_effect=OSError("synthetic-secret")):
                    with self.assertRaises(auth.AuthError) as raised:
                        self.store.save(session())
                self.assertNotIn("synthetic-secret", str(raised.exception))
                self.assertEqual(self.store.load(), session("old-secret"))
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
        oversized = session("a" * 16385)
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
        largest = session("a" * 16384, "b" * 16384)
        self.store.save(largest)
        self.assertEqual(self.store.load(), largest)

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
        # The session's own folder as a link is refused everywhere. A link above it is
        # refused too, except on Windows: a profile or AppData moved to another drive is
        # a junction there, and refusing it made signing in impossible.
        refused = [alias] if os.name == "nt" else [alias, alias / "nested"]
        for parent in refused:
            store = auth.SessionStore(parent / "session.json")
            for operation in (store.load, store.clear, store.status, lambda: store.save(session())):
                with self.subTest(parent=parent), self.assertRaises(auth.AuthError):
                    operation()
        self.assertEqual(list(real.rglob("session.json")), [])
        if os.name == "nt":
            probe = alias / "nested" / "probe.tmp"
            try:  # some sandboxes refuse every new file behind a link, Python's own too
                os.close(os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
            except OSError:
                self.skipTest("this environment creates no file through a link")
            probe.unlink()
            store = auth.SessionStore(alias / "nested" / "session.json")
            store.save(session())
            self.assertTrue((real / "nested" / "session.json").is_file())
            self.assertIsNotNone(store.load())

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
        self.store.save(session("old-secret"))
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

    def test_retired_omeda_records_are_dropped_not_an_unreadable_session(self):
        legacy = session()
        legacy["providers"]["omeda"] = {"social_token": "synthetic-omeda-secret"}
        legacy["providers"]["omeda_game"] = {"game_token": "synthetic-game-secret"}
        self.write_raw(json.dumps(legacy).encode())
        self.assertEqual(self.store.load(), session())
        self.store.save(self.store.load())
        self.assertNotIn(b"omeda", self.path.read_bytes())

    def test_a_pred_gg_record_of_an_older_shape_is_dropped(self):
        old = {"version": 1, "providers": {"predgg": {"access_token": "synthetic-old-secret"}}}
        self.write_raw(json.dumps(old).encode())
        self.assertEqual(self.store.load(), session(None))

    def test_fields_cannot_be_mixed_or_persist_anything_else(self):
        for value in (
            {"token": "secret"},
            {"version": 1, "providers": {"steam": {"access_token": "secret"}}},
            session(""),
            session("secret\n"),
            session("a" * 16385),
            session(refresh=""),
            session(expires="soon"),
            session(expires=float("inf")),
            session(expires=-1),
            session(expires=True),
        ):
            with self.subTest(value=str(value)[:60]), self.assertRaises(auth.AuthError):
                self.store.save(value)
        self.assertFalse(self.path.exists())

    def test_status_is_metadata_only(self):
        self.store.save(session(expires=200))
        with patch.object(auth.time, "time", return_value=201):
            metadata = self.store.status()
        self.assertEqual(
            metadata,
            {"stored": True, "providers": {"predgg": {"expires_at": 200, "expired": True}}},
        )
        self.assertNotIn("synthetic", json.dumps(metadata))

    def test_store_never_prints_secrets(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.store.save(session())
            self.store.load()
            self.store.status()
            self.store.clear()
        self.assertEqual(output.getvalue(), "")


AUTHORIZE = "https://provider.invalid/authorize"


def authorize(redirect_uri, state):
    return AUTHORIZE + "?" + urlencode({"redirect_uri": redirect_uri, "state": state})


class LoginAttempt:
    """Drive a real ephemeral loopback listener with only synthetic codes."""

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
                self.result = auth.oauth_login(
                    authorize, self.exchange, timeout=self.timeout, on_ready=ready, **self.kwargs
                )
            except BaseException as error:
                self.error = error

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        if not self.ready.wait(2):
            raise AssertionError("Synthetic login listener did not start")
        self.address = urlsplit(self.root)
        status, headers, page = self.request("/?" + self.address.query)
        if status != 303:
            raise AssertionError("Local login page failed")
        self.page, self.headers = page, headers
        self.login = headers["Location"]
        login_query = parse_qs(urlsplit(self.login).query)
        self.callback = login_query["redirect_uri"][0]
        self.state = login_query["state"][0]
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

    def answer(self, **updates):
        query = {"code": "synthetic-code-secret", "state": self.state}
        query.update(updates)
        return {k: v for k, v in query.items() if v is not None}

    def submit(self, query=None, *, follow=True):
        query = self.answer() if query is None else query
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


class OAuthLoginTests(unittest.TestCase):
    def setUp(self):
        browser = patch.object(auth.webbrowser, "open", return_value=True)
        self.browser = browser.start()
        self.addCleanup(browser.stop)
        # No real browser comes back for the result page here: do not wait long for it.
        wait = patch.object(auth, "RESULT_WAIT", 0.3)
        wait.start()
        self.addCleanup(wait.stop)

    def assert_closed(self, attempt):
        with socket.socket() as client:
            client.settimeout(0.3)
            self.assertNotEqual(client.connect_ex(("127.0.0.1", attempt.address.port)), 0)
        self.assertFalse(
            any(thread.name == "pred-auth-listener" for thread in threading.enumerate())
        )

    def test_success_exchanges_the_code_once_with_the_exact_callback(self):
        verified = {"access_token": "synthetic-verified-token"}
        exchange = Mock(return_value=verified)
        with LoginAttempt(exchange) as attempt:
            self.assertEqual(attempt.address.hostname, "127.0.0.1")
            launch = parse_qs(attempt.address.query)
            self.assertEqual(set(launch), {"start"})
            self.assertGreaterEqual(len(launch["start"][0]), 40)
            self.assertTrue(attempt.login.startswith(AUTHORIZE + "?"))
            self.assertEqual(
                attempt.callback, "http://127.0.0.1:%d/callback" % attempt.address.port
            )
            self.assertGreaterEqual(len(attempt.state), 40)
            status, headers, page = attempt.submit()
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/complete")
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")
            self.assertNotIn("synthetic-code-secret", page)
            attempt.wait()
            self.assertIsNone(attempt.error)
            self.assertIs(attempt.result, verified)
            exchange.assert_called_once_with("synthetic-code-secret", attempt.callback)
            self.browser.assert_called_once_with(attempt.root)
        self.assert_closed(attempt)

    def test_bare_root_and_wrong_launch_key_reveal_no_state_or_authorize_url(self):
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
                    self.assertNotIn(AUTHORIZE, page)
                    self.assertNotIn(attempt.address.query, page)
            attempt.exchange.assert_not_called()
            attempt.submit()
            attempt.wait()
            self.assertIsNone(attempt.error)

    def test_state_changes_each_attempt_and_the_browser_can_be_skipped(self):
        states = []
        for _ in range(2):
            with LoginAttempt(open_browser=False) as attempt:
                states.append(attempt.state)
                attempt.submit()
                attempt.wait()
        self.assertNotEqual(states[0], states[1])
        self.browser.assert_not_called()

    def test_invalid_answers_never_exchange_or_consume_then_a_valid_one_succeeds(self):
        with LoginAttempt(open_browser=False) as attempt:
            for change in (
                {"state": "wrong-state"},
                {"state": None},
                {"code": None},
                {"code": "c" * 2049},
                {"extra": "unexpected"},
                {"error": "access_denied", "state": "wrong-state"},
                {"error": "access_denied", "unexpected": "1"},
            ):
                with self.subTest(change=sorted(change)):
                    status, _, page = attempt.submit(attempt.answer(**change))
                    self.assertEqual(status, 400)
                    self.assertNotIn("wrong-state", page)
                    attempt.exchange.assert_not_called()
            attempt.submit()
            attempt.wait()
            self.assertEqual(attempt.result, {"verified": True})

    def test_a_declined_consent_cancels_without_an_exchange(self):
        # However the server words it: a description with spaces used to be refused as
        # an invalid parameter, and the sign-in waited out its three minutes.
        for description in ("no", "The user denied the request."):
            with LoginAttempt(open_browser=False) as attempt:
                answer = attempt.answer(
                    code=None, error="access_denied", error_description=description
                )
                self.assertEqual(attempt.submit(answer)[0], 303, description)
                attempt.wait()
                self.assertIsInstance(attempt.error, auth.AuthCancelled)
                attempt.exchange.assert_not_called()
            self.assert_closed(attempt)

    def test_only_the_free_text_parameters_may_hold_spaces(self):
        self.assertEqual(
            auth._query("state=s&error=access_denied&error_description=denied+by+the+user"),
            {"state": "s", "error": "access_denied", "error_description": "denied by the user"},
        )
        for query in ("state=a+b&code=c", "state=s&code=c+d", "state=s&error=access+denied"):
            with self.assertRaises(ValueError):
                auth._query(query)
        # The granted scope is a list with spaces; a description may be empty.
        self.assertEqual(
            auth._query("state=s&code=c&scope=offline_access+profile")["scope"],
            "offline_access profile",
        )
        self.assertEqual(
            auth._query("state=s&error=access_denied&error_description=")["error_description"], ""
        )
        with self.assertRaises(ValueError):
            auth._query("state=s&code=")

    def test_duplicates_encoding_and_size_limits(self):
        with LoginAttempt(open_browser=False) as attempt:
            query = urlencode(attempt.answer())
            for target in (
                "/callback?" + query + "&state=" + attempt.state,
                "/callback?" + query + "&%73tate=" + attempt.state,
                "/callback?" + query + "&bad=%GG",
                "/callback?" + query + "&bad=%FF",
                "/callback?" + query + "&bare",
                "/callback?" + query + "&bad=",
                "/callback?" + "&".join("x%d=v" % index for index in range(17)),
                "/callback?" + "x" * 8192,
            ):
                with self.subTest(length=len(target)):
                    self.assertEqual(attempt.request(target)[0], 400)
            attempt.exchange.assert_not_called()

    def test_host_path_method_and_request_target_are_strict(self):
        with LoginAttempt(open_browser=False) as attempt:
            target = "/callback?" + urlencode(attempt.answer())
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
                "/callback#x",
            ):
                with self.subTest(path=path):
                    response = attempt.raw(
                        (
                            "GET " + path + "?" + urlencode(attempt.answer()) + " HTTP/1.1\r\n"
                            "Host: " + attempt.address.netloc + "\r\n\r\n"
                        ).encode()
                    )
                    self.assertRegex(response.split(b"\r\n")[0], rb"HTTP/1.0 (400|404)")
            self.assertEqual(attempt.request(target, method="POST")[0], 501)
            self.assertEqual(attempt.request(target, {"Transfer-Encoding": "chunked"})[0], 400)
            self.assertEqual(attempt.request(target, {"Content-Length": "1"})[0], 400)
            self.assertEqual(attempt.request("/favicon.ico")[0], 404)
            attempt.exchange.assert_not_called()
            attempt.submit()
            attempt.wait()
            self.assertIsNone(attempt.error)

    def test_a_failed_exchange_consumes_the_attempt_and_leaks_nothing(self):
        entered, release = threading.Event(), threading.Event()

        def reject(code, redirect_uri):
            entered.set()
            release.wait(2)
            raise ValueError("synthetic-code-secret synthetic-token-secret")

        exchange = Mock(side_effect=reject)
        with LoginAttempt(exchange, open_browser=False) as attempt:
            self.assertEqual(attempt.submit()[0], 303)
            self.assertTrue(entered.wait(1))
            self.assertEqual(attempt.submit()[0], 409)
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
            self.assertEqual(attempt.request("/cancel?state=wrong")[0], 400)
            self.assertEqual(attempt.request("/cancel?state=" + attempt.state)[0], 303)
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

            def slow(code, redirect_uri):
                entered.set()
                release.wait(3)
                returned.set()
                return {"token": "late-synthetic-secret"}

            with (
                self.subTest(cancel=cancel),
                patch.object(auth, "EXCHANGE_GRACE", 0),
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

    def test_the_app_can_cancel_a_waiting_attempt(self):
        cancel = threading.Event()
        with LoginAttempt(open_browser=False, cancel=cancel) as attempt:
            cancel.set()
            attempt.wait()
            self.assertIsInstance(attempt.error, auth.AuthCancelled)
            self.assert_closed(attempt)

    def test_the_exchange_may_outlast_the_deadline_once_the_provider_answered(self):
        def slow(code, redirect_uri):
            time.sleep(0.8)
            return {"token": "synthetic"}

        with (
            patch.object(auth, "RESULT_WAIT", 0.1),
            LoginAttempt(slow, timeout=0.4, open_browser=False) as attempt,
        ):
            attempt.submit()
            attempt.wait()
            self.assertIsNone(attempt.error)
            self.assertEqual(attempt.result, {"token": "synthetic"})

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
                auth.oauth_login(authorize, Mock(), timeout=1, on_ready=ready)
            self.assertNotIn(
                "synthetic-secret", "".join(traceback.format_exception(raised.exception))
            )
            address = urlsplit(urls[0])
            with socket.socket() as client:
                self.assertNotEqual(client.connect_ex(("127.0.0.1", address.port)), 0)

    def test_no_access_logs_code_echo_or_automatic_persistence(self):
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
        for args, kwargs in (
            ((authorize, Mock()), {"timeout": 0}),
            ((authorize, Mock()), {"timeout": float("nan")}),
            ((authorize, Mock()), {"timeout": True}),
            ((authorize, Mock()), {"port": -1}),
            ((authorize, Mock()), {"port": 65536}),
            ((authorize, Mock()), {"on_ready": "not-callable"}),
            (("not-callable", Mock()), {}),
            ((authorize, "not-callable"), {}),
        ):
            with self.subTest(kwargs=kwargs), patch.object(auth, "HTTPServer") as server:
                with self.assertRaises(auth.AuthError):
                    auth.oauth_login(*args, **kwargs)
                server.assert_not_called()
        self.browser.assert_not_called()

    def test_a_bad_authorize_url_closes_the_listener(self):
        for bad in (lambda r, s: "http://insecure.invalid/", lambda r, s: 1 / 0):
            with self.subTest(bad=bad), self.assertRaises(auth.AuthError):
                auth.oauth_login(bad, Mock(), open_browser=False, timeout=1)
        self.assertFalse(
            any(thread.name == "pred-auth-listener" for thread in threading.enumerate())
        )


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
