import unittest
import urllib.request
from unittest.mock import Mock, patch

from predoverlay.api import Client
from predoverlay.errors import PayloadError, PredError
from predoverlay.official import OfficialClient
from predoverlay.transport import JsonTransport


class SecurityRegressions(unittest.TestCase):
    def test_malformed_tokens_never_reach_headers_or_error_output(self):
        for constructor in (Client, OfficialClient):
            for token in (
                "synthetic-secret\n",
                "synthetic-secret\rX: y",
                "",
                {"secret": "synthetic-secret"},
            ):
                with self.subTest(constructor=constructor, token_type=type(token).__name__):
                    with self.assertRaises(PredError) as error:
                        constructor(token=token)
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

    def test_profile_probe_failure_restores_old_client_token(self):
        client = OfficialClient(token="old-token")
        client.transport.request = Mock(
            return_value={"pragmaTokens": {"pragmaSocialToken": "new-token"}}
        )
        client.me = Mock(side_effect=PredError("probe failed"))
        with self.assertRaises(PredError):
            client.exchange_steam({"providerId": "STEAM", "providerToken": "signed-data"})
        self.assertEqual(client.token, "old-token")

    def test_malformed_guest_expiry_does_not_change_auth_state(self):
        for expiry in (None, "bad", float("inf"), float("nan"), True, -1, 0):
            with (
                self.subTest(expiry_type=type(expiry).__name__),
                patch.dict("os.environ", {}, clear=True),
            ):
                client = Client()
                client.version = "version"
                client._http = Mock(
                    return_value={"access_token": "candidate-token", "expires_in": expiry}
                )
                with self.assertRaises(PayloadError):
                    client.authenticate()
                self.assertIsNone(client.token)
                self.assertEqual(client.expires_at, 0)
