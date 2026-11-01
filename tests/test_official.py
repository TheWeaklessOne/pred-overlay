import io
import unittest
import urllib.error
from unittest.mock import Mock, patch

from predoverlay.api import PredError
from predoverlay.official import GAME_SHARD, OfficialClient

PID = "20000000-0000-0000-0000-000000000001"


class OfficialTests(unittest.TestCase):
    def test_name_and_discriminator_are_separate_fields(self):
        client = OfficialClient(token="test")
        client.request = Mock(return_value={"response": {"payload": {"pragmaPlayerId": PID}}})
        result = client.lookup("Sample-one#1001")
        self.assertTrue(result["found"])
        self.assertEqual(
            client.request.call_args.args[1]["payload"],
            {"gameShardId": GAME_SHARD, "displayName": "Sample-one", "discriminator": "1001"},
        )

    def test_bare_name_rejected_without_network(self):
        client = OfficialClient(token="test")
        client.request = Mock()
        with self.assertRaises(PredError):
            client.lookup("QWZX")
        client.request.assert_not_called()

    def test_http_success_with_no_id_is_not_a_match(self):
        client = OfficialClient(token="test")
        client.request = Mock(
            return_value={
                "response": {
                    "payload": {
                        "identifier": "Unknown#1234",
                        "message": "No playerId found for displayName Unknown#1234",
                    }
                }
            }
        )
        self.assertFalse(client.lookup("Unknown#1234")["found"])

    def test_reverse_lookup_uses_pragma_player_id(self):
        client = OfficialClient(token="test")
        client.request = Mock(
            return_value={
                "response": {"payload": {"displayName": "Sample-one", "discriminator": "1001"}}
            }
        )
        self.assertEqual(client.name(PID)["full_name"], "Sample-one#1001")
        self.assertEqual(client.request.call_args.args[1]["payload"]["pragmaPlayerId"], PID)

    def test_batch_field_and_missing_ids_and_private_fields(self):
        other = "20000000-0000-0000-0000-000000000003"
        client = OfficialClient(token="test")
        client.request = Mock(
            return_value={
                "response": {
                    "payload": {
                        "playerIdentities": [
                            {
                                "pragmaPlayerId": PID,
                                "pragmaDisplayName": {
                                    "displayName": "Sample-one",
                                    "discriminator": "1001",
                                },
                                "idProviderAccounts": [{"accountId": "not-to-be-exported"}],
                            }
                        ]
                    }
                }
            }
        )
        result = client.identities([PID, other, PID])
        self.assertEqual(result["not_returned"], [other])
        self.assertNotIn("idProviderAccounts", result["results"][0])
        self.assertEqual(client.request.call_args.args[1]["payload"]["playerIds"], [PID, other])

    def test_only_verified_read_only_methods_are_accepted(self):
        client = OfficialClient(token="test")
        client.request = Mock()
        with self.assertRaises(PredError):
            client.rpc("AccountRpc.DeleteAccountV1Request", {})
        client.request.assert_not_called()

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_token_fails_before_transport(self):
        client = OfficialClient()
        client.opener = Mock()
        with self.assertRaisesRegex(PredError, "OMEDA_SOCIAL_TOKEN"):
            client.lookup("Sample-one#1001")
        client.opener.open.assert_not_called()

    def test_server_error_redacts_credential(self):
        client = OfficialClient(token="private-test-token")
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError(
            "https://example.invalid", 400, "bad", {}, io.BytesIO(b"error private-test-token")
        )
        with self.assertRaises(PredError) as error:
            client.lookup("Sample-one#1001")
        self.assertNotIn("private-test-token", str(error.exception))

    def test_service_error_is_not_a_success(self):
        client = OfficialClient(token="test")
        client.request = Mock(return_value={"serviceError": {"error": "AccessDenied"}})
        with self.assertRaisesRegex(PredError, "AccessDenied"):
            client.lookup("Sample-one#1001")


if __name__ == "__main__":
    unittest.main()
