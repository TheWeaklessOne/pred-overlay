import io
import json
import unittest
import urllib.error
from unittest.mock import Mock

from predoverlay.errors import AuthenticationError, PayloadError, PredError, RateLimitError
from predoverlay.transport import JsonTransport, redact, retry_seconds


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
