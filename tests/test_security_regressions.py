import unittest
import urllib.request
from unittest.mock import Mock, patch

from predoverlay.errors import PredError
from predoverlay.transport import JsonTransport


class SecurityRegressions(unittest.TestCase):
    def test_malformed_tokens_never_reach_headers_or_error_output(self):
        from predoverlay.api import Client

        for token in ("synthetic-secret\n", "synthetic-secret\rX: y"):
            with self.subTest(token=token):
                client = Client(token=lambda force=False, t=token: t, delay=0, retries=0)
                with patch.object(client.opener, "open") as send:
                    with self.assertRaises(PredError) as error:
                        client.query("{ ok }")
                    send.assert_not_called()
                self.assertNotIn("synthetic-secret", str(error.exception))

    def test_header_validation_is_pre_send(self):
        opener = Mock()
        t = JsonTransport("https://example.com", opener=opener, delay=0)
        with self.assertRaises(PredError) as error:
            t.request("/rpc", {}, headers={"Authorization": "Bearer synthetic-secret\n"})
        opener.open.assert_not_called()
        self.assertNotIn("synthetic-secret", str(error.exception))

    def test_default_redirect_following_injected_opener_is_rejected(self):
        opener = urllib.request.build_opener()
        t = JsonTransport("https://example.com", opener=opener, delay=0)
        with patch.object(opener, "open") as send:
            with self.assertRaisesRegex(PredError, "NoRedirect"):
                t.request("/rpc", {}, headers={"Authorization": "Bearer secret"})
            send.assert_not_called()
