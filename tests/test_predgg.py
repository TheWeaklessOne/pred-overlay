"""Pred.gg sign-in and token refresh with synthetic answers: no browser, no network."""

import base64
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from predoverlay import api, auth, predgg
from predoverlay.errors import AuthenticationError, PayloadError


def answer(access="synthetic-access", refresh="synthetic-refresh", expires_in=1800):
    found = {"access_token": access, "expires_in": expires_in, "token_type": "Bearer"}
    if refresh is not None:
        found["refresh_token"] = refresh
    return found


class Folder(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir()))
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "session.json"


class PkceTests(unittest.TestCase):
    def test_challenge_is_the_s256_of_the_verifier(self):
        verifier, challenge = predgg.pkce()
        self.assertGreaterEqual(len(verifier), 43)
        digest = hashlib.sha256(verifier.encode()).digest()
        self.assertEqual(challenge, base64.urlsafe_b64encode(digest).rstrip(b"=").decode())
        self.assertNotEqual(predgg.pkce()[0], verifier)

    def test_authorize_url_names_the_public_client_and_the_challenge(self):
        url = predgg.authorize_url("http://127.0.0.1:47833/callback", "s" * 40, "c" * 43)
        self.assertTrue(url.startswith("https://pred.gg/api/oauth2/authorize?"))
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.assertEqual(
            query,
            {
                "response_type": "code",
                "client_id": predgg.CLIENT_ID,
                "redirect_uri": "http://127.0.0.1:47833/callback",
                "scope": "offline_access profile",
                "state": "s" * 40,
                "code_challenge": "c" * 43,
                "code_challenge_method": "S256",
            },
        )


class RecordTests(unittest.TestCase):
    def test_expiry_from_the_lifetime(self):
        self.assertEqual(
            predgg.record(answer(), now=1000),
            {
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "expires_at": 2800,
            },
        )

    def test_a_refresh_without_a_new_refresh_token_keeps_the_old_one(self):
        found = predgg.record(answer(refresh=None), now=0, previous={"refresh_token": "old"})
        self.assertEqual(found["refresh_token"], "old")

    def test_answers_without_a_token_or_lifetime_are_refused(self):
        for bad in (
            answer(access=None),
            answer(refresh=None),
            answer(expires_in=0),
            answer(expires_in="1"),
        ):
            with self.subTest(bad=bad), self.assertRaises(PayloadError):
                predgg.record(bad, now=0)


class SignInTests(Folder):
    def test_the_code_is_exchanged_with_the_verifier_and_the_tokens_stored(self):
        posted = []

        def login(authorize_url, exchange, **kwargs):
            url = authorize_url("http://127.0.0.1:47833/callback", "state")
            challenge = parse_qs(urlsplit(url).query)["code_challenge"][0]
            tokens = exchange("synthetic-code", "http://127.0.0.1:47833/callback")
            verifier = posted[0]["code_verifier"]
            digest = hashlib.sha256(verifier.encode()).digest()
            self.assertEqual(base64.urlsafe_b64encode(digest).rstrip(b"=").decode(), challenge)
            self.assertEqual(kwargs["port"], predgg.PORT)
            return tokens

        def post(form):
            posted.append(form)
            return answer()

        with mock.patch.object(predgg, "oauth_login", side_effect=login):
            predgg.sign_in(self.path, post=post, open_browser=False)
        self.assertEqual(posted[0]["grant_type"], "authorization_code")
        self.assertEqual(posted[0]["client_id"], predgg.CLIENT_ID)
        self.assertEqual(posted[0]["code"], "synthetic-code")
        self.assertNotIn("client_secret", posted[0])
        self.assertEqual(predgg.stored(self.path)["access_token"], "synthetic-access")


class SessionTests(Folder):
    def store(self, expires_at):
        auth.SessionStore(self.path).save(
            {
                "version": 1,
                "providers": {
                    "predgg": {
                        "access_token": "old-access",
                        "refresh_token": "old-refresh",
                        "expires_at": expires_at,
                    }
                },
            }
        )

    def test_no_session_needs_a_sign_in(self):
        with self.assertRaises(AuthenticationError):
            predgg.Session(self.path).token()
        self.assertFalse(predgg.Session(self.path).signed_in())

    def test_a_valid_token_is_used_as_it_is(self):
        self.store(10_000)
        post = mock.Mock()
        self.assertEqual(
            predgg.Session(self.path, post=post, clock=lambda: 0).token(), "old-access"
        )
        post.assert_not_called()

    def test_a_token_near_its_end_is_refreshed_and_saved(self):
        self.store(100)
        post = mock.Mock(return_value=answer("new-access", "new-refresh"))
        session = predgg.Session(self.path, post=post, clock=lambda: 50)
        self.assertEqual(session.token(), "new-access")
        form = post.call_args.args[0]
        self.assertEqual(
            form,
            {
                "grant_type": "refresh_token",
                "refresh_token": "old-refresh",
                "client_id": predgg.CLIENT_ID,
            },
        )
        self.assertEqual(predgg.stored(self.path)["refresh_token"], "new-refresh")
        self.assertEqual(predgg.stored(self.path)["expires_at"], 50 + 1800)

    def test_a_refused_refresh_asks_for_a_sign_in(self):
        self.store(0)
        post = mock.Mock(side_effect=AuthenticationError("refused", status=400))
        with self.assertRaises(AuthenticationError):
            predgg.Session(self.path, post=post, clock=lambda: 50).token()
        self.assertEqual(predgg.stored(self.path)["access_token"], "old-access")


class ClientTests(unittest.TestCase):
    def test_a_refused_token_is_refreshed_once(self):
        tokens = mock.Mock(side_effect=lambda force=False: "fresh" if force else "stale")
        client = api.Client(token=tokens, delay=0, retries=0)
        calls = []

        def request(path, body, headers=None, secrets=()):
            calls.append(headers["Authorization"])
            if headers["Authorization"] == "Bearer stale":
                raise AuthenticationError("HTTP 401", status=401)
            return {"data": {"ok": True}}

        with mock.patch.object(client.transport, "request", side_effect=request):
            self.assertEqual(client.query("{ ok }"), {"ok": True})
        self.assertEqual(calls, ["Bearer stale", "Bearer fresh"])

    def test_without_a_token_no_authorization_is_sent(self):
        client = api.Client(delay=0, retries=0)
        with mock.patch.object(client.transport, "request", return_value={"data": {}}) as request:
            client.query("{ ok }")
        self.assertNotIn("headers", request.call_args.kwargs)


class RefusalTests(unittest.TestCase):
    def test_a_403_is_no_refused_session_and_takes_no_refresh(self):
        from predoverlay.overlay import lanes

        tokens = mock.Mock(return_value="t")
        client = api.Client(token=tokens, delay=0, retries=0)
        with mock.patch.object(
            client.transport, "request", side_effect=AuthenticationError("HTTP 403", status=403)
        ):
            with self.assertRaises(AuthenticationError) as raised:
                client.query("{ ok }")
        self.assertEqual(tokens.call_args_list, [mock.call()])  # no forced refresh
        self.assertEqual(lanes.failure_kind(raised.exception), "other")
        self.assertEqual(lanes.failure_kind(AuthenticationError("HTTP 401", status=401)), "auth")

    def test_sign_out_deletes_the_tokens(self):
        with tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as folder:
            path = Path(folder) / "session.json"
            auth.SessionStore(path).save(
                {
                    "version": 1,
                    "providers": {
                        "predgg": {"access_token": "a", "refresh_token": "r", "expires_at": 1}
                    },
                }
            )
            self.assertTrue(predgg.sign_out(path))
            self.assertIsNone(predgg.stored(path))


class OwnWriteTests(Folder):
    def test_a_refresh_is_marked_as_the_apps_own_write(self):
        SessionTests.store(self, 100)
        post = mock.Mock(return_value=answer("new-access", "new-refresh"))
        predgg.Session(self.path, post=post, clock=lambda: 50).token()
        self.assertEqual(predgg.own_write(self.path), os.stat(self.path).st_mtime_ns)

    def test_a_refused_refresh_is_about_the_session(self):
        from predoverlay.overlay import lanes

        with mock.patch.object(predgg.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = predgg.urllib.error.HTTPError(
                predgg.TOKEN, 403, "Forbidden", {}, None
            )
            with self.assertRaises(AuthenticationError) as raised:
                predgg._post_form({"grant_type": "refresh_token"})
        self.assertEqual(lanes.failure_kind(raised.exception), "auth")

    def test_the_tracker_takes_only_another_programs_write_as_a_sign_in(self):
        from predoverlay.overlay import tracker

        SessionTests.store(self, 100)
        watch = tracker.Tracker({}, session_path=self.path, log=lambda m: None)
        watch.state.auth = "login"
        predgg._wrote(self.path)  # this app's own refresh rewrote it
        watch.check_session()
        self.assertEqual(watch.state.auth, "login")
        stamp = os.stat(self.path).st_mtime_ns + 10_000_000
        os.utime(self.path, ns=(stamp, stamp))  # the console's --sign-in
        watch.check_session()
        self.assertEqual(watch.state.auth, "ok")


class DroppedCommandTests(unittest.TestCase):
    def test_reads_dropped_after_a_refusal_come_back_as_failures(self):
        from predoverlay.overlay import core, lanes, tracker

        lane = lanes.Lane("test", lambda c: [], None)
        watch = tracker.Tracker({"predgg": lane}, log=lambda m: None)
        watch.state.inflight = {("search", 1), ("match",)}
        watch.state.epoch = 1
        lane.submit(("search", 1, {"a0": ("X",)}))
        lane.submit(("match", "m1"))
        lane.submit(("pending", []))
        watch.handle(("auth", False))
        back = [watch.events.get_nowait() for _ in range(watch.events.qsize())]
        self.assertIn(("found", 1, None), back)
        self.assertIn(("match", "m1", None), back)
        self.assertEqual([lane.inbox.get_nowait()], [("pending", [])])  # needs no session
        for event in back:
            watch.handle(event)
        self.assertNotIn(("search", 1), watch.state.inflight)
        self.assertNotIn(("match",), watch.state.inflight)
        del core


class RetiredTests(Folder):
    def test_an_omeda_only_session_file_is_left_alone(self):
        from predoverlay import runtime

        self.path.write_text(
            '{"version":1,"providers":{"omeda":{"social_token":"x"},"omeda_game":{"game_token":"y"}}}'
        )
        runtime._drop_retired(self.path, lambda m: None)
        self.assertIn(b"omeda", self.path.read_bytes())


class LateAnswerTests(unittest.TestCase):
    def test_a_read_that_lands_after_a_sign_out_does_not_sign_in_again(self):
        from predoverlay.overlay import core

        state = core.State()
        core.reduce(state, ("auth", False), 1.0)
        core.reduce(state, ("auth", True), 2.0)
        self.assertEqual(core.view(state).health, "login")
        core.reduce(state, ("sign_in",), 3.0)
        self.assertEqual(core.view(state).health, "ok")

    def test_a_graphql_error_quoting_a_searched_name_leaves_it_out(self):
        from predoverlay.errors import PredError

        client = api.Client(delay=0, retries=0)
        answer = {"errors": [{"message": 'No player "SecretName" here'}]}
        with mock.patch.object(client.transport, "request", return_value=answer):
            with self.assertRaises(PredError) as raised:
                client.query("query($n0:String!){x}", {"n0": "SecretName"})
        self.assertNotIn("SecretName", str(raised.exception))

    def test_store_writes_survive_a_quit(self):
        from predoverlay.overlay import lanes, tracker

        written = []
        lane = lanes.Lane("test", lambda c: written.append(c), None)
        watch = tracker.Tracker({"predgg": lane}, log=lambda m: None)
        lane.submit(("search", 1, {}))
        lane.submit(("pending", []))
        watch.shutdown()
        self.assertEqual(written, [("pending", [])])


if __name__ == "__main__":
    unittest.main()
