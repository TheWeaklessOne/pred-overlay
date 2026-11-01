"""Offline regression tests for pagination, authentication, and API failures.

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
            patch("predoverlay.api.urllib.request.build_opener", return_value=self.opener),
            patch.dict("os.environ", {}, clear=True),
            patch("predoverlay.api.time.sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_mutations_and_subscriptions_are_rejected_before_network(self):
        client = Client(token="provided", delay=0)
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

    def respond_with(self, *responses):
        self.opener.open.side_effect = [
            response
            if isinstance(response, Exception)
            else io.BytesIO(json.dumps(response).encode("utf-8"))
            for response in responses
        ]

    def requests(self):
        return [entry.args[0] for entry in self.opener.open.call_args_list]

    def paths(self):
        return [request.selector for request in self.requests()]

    @staticmethod
    def http_error(code, message="failure"):
        return urllib.error.HTTPError("https://pred.gg/gql", code, message, {}, None)

    def test_graphql_errors_reject_partial_data(self):
        for data in (None, {"answer": "must not be returned"}):
            with self.subTest(data=data):
                self.respond_with(
                    {"version": "build"},
                    {"data": data, "errors": [{"message": "query denied"}]},
                )
                with self.assertRaisesRegex(PredError, "query denied"):
                    Client(token="supplied", delay=0).query("query { answer }")

    def test_missing_graphql_data_is_rejected(self):
        self.respond_with({"version": "build"}, {"data": None})
        with self.assertRaises(PredError):
            Client(token="supplied", delay=0).query("query { answer }")

    def test_guest_http_401_refreshes_and_replays_same_query(self):
        self.respond_with(
            {"version": "build-before"},
            {"access_token": "guest-before", "expires_in": 300},
            self.http_error(401),
            {"version": "build-after"},
            {"access_token": "guest-after", "expires_in": 300},
            {"data": {"answer": 42}},
        )
        result = Client(delay=0).query("query($id:ID!){answer(id:$id)}", {"id": "player-1"})
        self.assertEqual(result, {"answer": 42})
        self.assertEqual(
            self.paths(),
            [
                "/_app/version.json",
                "/auth/token",
                "/gql",
                "/_app/version.json",
                "/auth/token",
                "/gql",
            ],
        )
        before, after = self.requests()[2], self.requests()[5]
        self.assertEqual(before.data, after.data)
        self.assertEqual(before.get_header("Authorization"), "Bearer guest-before")
        self.assertEqual(after.get_header("Authorization"), "Bearer guest-after")
        self.assertEqual(after.get_header("X-frontend-version"), "build-after")

    def test_guest_http_401_only_refreshes_once(self):
        self.respond_with(
            {"version": "one"},
            {"access_token": "guest-one"},
            self.http_error(401),
            {"version": "two"},
            {"access_token": "guest-two"},
            self.http_error(401),
        )
        with self.assertRaisesRegex(PredError, "401"):
            Client(delay=0).query("query { answer }")
        self.assertEqual(self.paths().count("/auth/token"), 2)
        self.assertEqual(self.paths().count("/gql"), 2)

    def test_supplied_token_is_never_downgraded_to_guest(self):
        for source in ("argument", "environment"):
            for failure in ("http", "graphql"):
                with self.subTest(source=source, failure=failure):
                    self.opener.reset_mock()
                    error = (
                        self.http_error(401)
                        if failure == "http"
                        else {
                            "errors": [{"message": "Authentication failed"}],
                            "data": {"answer": 42},
                        }
                    )
                    self.respond_with({"version": "build"}, error)
                    with patch.dict(
                        "os.environ",
                        {"PRED_ACCESS_TOKEN": "provided-secret"} if source == "environment" else {},
                        clear=True,
                    ):
                        client = Client(
                            token="provided-secret" if source == "argument" else None, delay=0
                        )
                        with self.assertRaises(PredError):
                            client.query("query { answer }")
                    self.assertEqual(self.paths(), ["/_app/version.json", "/gql"])
                    self.assertEqual(
                        self.requests()[1].get_header("Authorization"), "Bearer provided-secret"
                    )

    def test_token_is_redacted_from_graphql_errors(self):
        token = "private-bearer-secret"
        self.respond_with(
            {"version": "build"},
            {
                "errors": [
                    {"message": "Rejected " + token},
                    {"message": "Token " + token + " invalid"},
                ]
            },
        )
        with self.assertRaises(PredError) as raised:
            Client(token=token, delay=0).query("query { answer }")
        self.assertNotIn(token, str(raised.exception))
        self.assertIn("Rejected", str(raised.exception))

    def test_token_is_not_exposed_from_http_error_details(self):
        token = "private-http-secret"
        self.respond_with({"version": "build"}, self.http_error(403, "Rejected " + token))
        with self.assertRaises(PredError) as raised:
            Client(token=token, delay=0).query("query { answer }")
        self.assertNotIn(token, str(raised.exception))
        self.assertIn("403", str(raised.exception))

    def test_token_is_not_exposed_from_transport_error_details(self):
        token = "private-transport-secret"
        self.respond_with(
            {"version": "build"},
            *(urllib.error.URLError("Rejected bearer " + token) for _ in range(3)),
        )
        with self.assertRaises(PredError) as raised:
            Client(token=token, delay=0).query("query { answer }")
        self.assertNotIn(token, str(raised.exception))
        self.assertEqual(self.paths().count("/gql"), 3)

    def test_version_is_discovered_and_forwarded_without_hardcoding(self):
        for version in ("new-build-a83c", "future-build-f17d"):
            with self.subTest(version=version):
                self.opener.reset_mock()
                self.respond_with(
                    {"version": version},
                    {"access_token": "guest"},
                    {"data": {"answer": 1}},
                    {"data": {"answer": 2}},
                )
                client = Client(delay=0)
                self.assertEqual(client.query("query { answer }"), {"answer": 1})
                self.assertEqual(client.query("query { answer }"), {"answer": 2})
                self.assertEqual(
                    self.paths(), ["/_app/version.json", "/auth/token", "/gql", "/gql"]
                )
                self.assertIsNone(self.requests()[0].get_header("X-frontend-version"))
                for request in self.requests()[1:]:
                    self.assertEqual(request.get_header("X-frontend-version"), version)
                self.assertIsNone(self.requests()[1].get_header("Authorization"))


if __name__ == "__main__":
    unittest.main()
