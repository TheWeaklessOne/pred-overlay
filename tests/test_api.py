"""Offline tests of the anonymous Pred.gg client: queries only, no credentials.

Run from the project root: python3 -m unittest discover -s tests -v
"""

import io
import json
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

from predoverlay.api import Client, PredError


class ClientTransportTests(unittest.TestCase):
    """Exercise public methods through urllib requests, without real I/O."""

    def setUp(self):
        self.opener = Mock(spec=urllib.request.OpenerDirector)
        self.opener.open.side_effect = AssertionError("Unexpected transport request")
        for patcher in (
            patch("predoverlay.api.https_opener", return_value=self.opener),
            patch.dict("os.environ", {"PRED_ACCESS_TOKEN": "must-not-be-sent"}, clear=True),
            patch("predoverlay.transport.time.sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def respond_with(self, *responses):
        self.opener.open.side_effect = [
            response
            if isinstance(response, Exception)
            else io.BytesIO(json.dumps(response).encode("utf-8"))
            for response in responses
        ]

    def requests(self):
        return [entry.args[0] for entry in self.opener.open.call_args_list]

    def test_mutations_and_subscriptions_are_rejected_before_network(self):
        client = Client(delay=0)
        for document in (
            "mutation { changeName }",
            "# comment\n mutation Test { changeName }",
            "subscription { updates }",
        ):
            with (
                self.subTest(document=document),
                self.assertRaisesRegex(PredError, "Only GraphQL query"),
            ):
                client.query(document)
        self.opener.open.assert_not_called()

    def test_a_query_goes_straight_to_gql_without_credentials_or_version(self):
        self.respond_with({"data": {"answer": 1}})
        result = Client(delay=0).query("query($id:ID!){answer(id:$id)}", {"id": "player-1"})
        self.assertEqual(result, {"answer": 1})
        (request,) = self.requests()
        self.assertEqual(request.selector, "/gql")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("X-frontend-version"))
        self.assertEqual(json.loads(request.data)["variables"], {"id": "player-1"})

    def test_graphql_errors_reject_partial_data(self):
        for data in (None, {"answer": "must not be returned"}):
            with self.subTest(data=data):
                self.respond_with({"data": data, "errors": [{"message": "query denied"}]})
                with self.assertRaisesRegex(PredError, "query denied"):
                    Client(delay=0).query("query { answer }")

    def test_graphql_error_text_loses_player_ids(self):
        pid = "10000000-0000-0000-0000-00000000abcd"
        self.respond_with({"data": None, "errors": [{"message": f"player {pid} not found"}]})
        with self.assertRaises(PredError) as caught:
            Client(delay=0).query("query { answer }")
        self.assertNotIn(pid, str(caught.exception))
        self.assertIn("player <id> not found", str(caught.exception))

    def test_missing_graphql_data_is_rejected(self):
        self.respond_with({"data": None})
        with self.assertRaises(PredError):
            Client(delay=0).query("query { answer }")

    def test_an_http_error_is_not_retried_as_a_guest(self):
        error = urllib.error.HTTPError("https://pred.gg/gql", 401, "failure", {}, io.BytesIO())
        self.addCleanup(error.close)
        self.respond_with(error)
        with self.assertRaisesRegex(PredError, "401"):
            Client(delay=0).query("query { answer }")
        self.assertEqual(len(self.requests()), 1)


if __name__ == "__main__":
    unittest.main()
