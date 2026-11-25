import io
import json
import unittest
import urllib.error
from unittest.mock import Mock

from predoverlay import transport
from predoverlay.errors import AuthenticationError, PayloadError, PredError, RateLimitError
from predoverlay.transport import JsonTransport, redact, retry_seconds


class TlsTests(unittest.TestCase):
    """Every HTTPS request verifies through one context, and on Windows that context
    lets Windows verify: Python alone trusts only the roots Windows already fetched."""

    def test_windows_verifies_the_certificates(self):
        import os
        import ssl

        context = transport.tls_context()
        self.assertIs(context, transport.tls_context())  # made once for this thread
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        if os.name == "nt":
            self.assertEqual(type(context).__module__.split(".")[0], "truststore")
            self.assertIsNone(transport.tls_fallback())

    def test_each_thread_and_each_opener_has_a_context_of_its_own(self):
        # A truststore context holds one lock through a handshake: shared, a stalled
        # handshake of the update check held every lookup for its whole timeout.
        import threading

        seen = []
        worker = threading.Thread(target=lambda: seen.append(transport.tls_context()))
        worker.start()
        worker.join()
        self.assertIsNot(seen[0], transport.tls_context())
        first, second = transport.https_opener(), transport.https_opener()
        contexts = [
            h._context
            for opener in (first, second)
            for h in opener.handlers
            if hasattr(h, "_context")
        ]
        self.assertEqual(len(contexts), 2)
        self.assertIsNot(contexts[0], contexts[1])
        self.assertIsNot(contexts[0], transport.tls_context())

    def test_without_the_package_pythons_own_roots_verify_and_the_app_says_so(self):
        import ssl
        import sys
        from unittest import mock

        with (
            mock.patch.dict(sys.modules, {"truststore": None}),
            mock.patch.object(transport, "_fallback", None),
        ):
            context = transport._new_context()
            self.assertIsInstance(context, ssl.SSLContext)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertIn("truststore", transport.tls_fallback())

    def test_the_opener_uses_it_and_follows_no_redirect(self):
        import urllib.request

        opener = transport.https_opener()
        https = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual(len(https), 1)
        self.assertIs(type(https[0]._context), type(transport.tls_context()))
        self.assertTrue(any(type(h) is transport.NoRedirect for h in opener.handlers))
        transport.require_redirect_blocker(opener)  # raises if a redirect would be followed

    def test_every_request_the_app_makes_goes_through_it(self):
        from unittest import mock

        from predoverlay import api, portraits, predgg, updater

        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = b"{}"
        reply.__enter__.return_value.geturl.return_value = "https://api.github.com/x"
        with mock.patch("urllib.request.urlopen", return_value=reply) as urlopen:
            updater._get("https://api.github.com/x", 100)
            portraits._download("icon")
        for call in urlopen.call_args_list:
            self.assertIs(call.kwargs["context"], transport.tls_context())
        with mock.patch.object(transport, "https_opener") as made:
            JsonTransport("https://pred.gg")
            self.assertEqual(made.call_count, 1)
        with mock.patch.object(api, "https_opener") as made:
            api.Client()
            self.assertEqual(made.call_count, 1)
        with mock.patch.object(predgg, "https_opener") as made:
            made.return_value.open.side_effect = OSError("no network in a test")
            with self.assertRaises(PredError):
                predgg._post_form({"grant_type": "refresh_token"})
            self.assertEqual(made.call_count, 1)


class TransportTests(unittest.TestCase):
    def make(self, *events, redirects=True):
        opener = Mock()
        opener.open.side_effect = [
            e if isinstance(e, Exception) else io.BytesIO(json.dumps(e).encode()) for e in events
        ]
        return JsonTransport(
            "https://example.com",
            delay=0,
            retries=0,
            same_origin_redirects=redirects,
            opener=opener,
        ), opener

    def test_same_origin_gateway_redirect_keeps_accept_auth_and_body(self):
        redirect = urllib.error.HTTPError(
            "https://example.com/v1/rpc",
            307,
            "redirect",
            {"Location": "https://example.com/v1/rpc?gateway=one"},
            None,
        )
        transport, opener = self.make(redirect, {"ok": True})
        self.assertEqual(
            transport.request("/v1/rpc", {"x": 1}, headers={"Authorization": "Bearer secret"}),
            {"ok": True},
        )
        a, b = [c.args[0] for c in opener.open.call_args_list]
        self.assertEqual(a.data, b.data)
        self.assertEqual(b.get_header("Accept"), "application/json")
        self.assertEqual(b.get_header("Authorization"), "Bearer secret")

    def test_cross_origin_and_downgrade_never_receive_credentials(self):
        for location in [
            "https://evil.example/v1/rpc",
            "http://example.com/v1/rpc",
            "https://example.com.evil/v1/rpc",
            "https://example.com:444/v1/rpc",
            "https://user@example.com/v1/rpc",
        ]:
            with self.subTest(location=location):
                t, o = self.make(
                    urllib.error.HTTPError(
                        "https://example.com/v1/rpc", 307, "redirect", {"Location": location}, None
                    )
                )
                with self.assertRaises(PredError):
                    t.request("/v1/rpc", {}, headers={"Authorization": "Bearer secret"})
                self.assertEqual(o.open.call_count, 1)

    def test_redirect_cannot_change_operation_path(self):
        t, o = self.make(
            urllib.error.HTTPError(
                "https://example.com/v1/rpc", 307, "redirect", {"Location": "/different"}, None
            )
        )
        with self.assertRaises(PredError):
            t.request("/v1/rpc", {})
        self.assertEqual(o.open.call_count, 1)

    def test_redirects_are_bounded(self):
        events = [
            urllib.error.HTTPError(
                "https://example.com/rpc",
                307,
                "redirect",
                {"Location": "/rpc?gateway=" + str(i)},
                None,
            )
            for i in range(4)
        ]
        t, o = self.make(*events)
        with self.assertRaises(PredError):
            t.request("/rpc", {})
        self.assertEqual(o.open.call_count, 4)

    def test_retry_after_date_is_parsed(self):
        self.assertGreater(retry_seconds("Wed, 01 Jan 2031 00:00:00 GMT"), 30)
        self.assertEqual(retry_seconds("12"), 12)
        self.assertIsNone(retry_seconds("nonsense"))

    def test_long_rate_limit_is_returned_without_blocking(self):
        t, o = self.make(
            urllib.error.HTTPError(
                "https://example.com/q", 429, "limited", {"Retry-After": "900"}, None
            )
        )
        with self.assertRaises(RateLimitError) as e:
            t.request("/q")
        self.assertEqual(e.exception.retry_after, 900)

    def test_invalid_json_is_actionable(self):
        t, o = self.make()
        o.open.side_effect = None
        o.open.return_value = io.BytesIO(b"<html>broken</html>")
        with self.assertRaises(PayloadError):
            t.request("/q")

    def test_error_body_and_secrets_never_echo(self):
        t, o = self.make(
            urllib.error.HTTPError(
                "https://example.com/q", 400, "Bearer private", {}, io.BytesIO(b"Bearer private")
            )
        )
        with self.assertRaises(PredError) as e:
            t.request("/q", {}, headers={"Authorization": "Bearer private"}, secrets=["private"])
        self.assertNotIn("private", str(e.exception))

    def test_auth_is_not_retried_by_generic_transport(self):
        t, o = self.make(urllib.error.HTTPError("https://example.com/q", 401, "no", {}, None))
        with self.assertRaises(AuthenticationError):
            t.request("/q")
        self.assertEqual(o.open.call_count, 1)

    def test_bad_relative_target_has_no_io(self):
        t, o = self.make()
        with self.assertRaises(PredError):
            t.request("//other.example/q")
        o.open.assert_not_called()

    def test_redacts_bearer_and_jwt_without_exact_secret(self):
        text = "Bearer abc.def.ghi and eyJabcdefghijklmno123456789.aabb.ccdd"
        self.assertNotIn("abc.def", redact(text))
        self.assertNotIn("eyJ", redact(text))
