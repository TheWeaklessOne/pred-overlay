"""Offline contract of the hero catalog query; fixtures are self-contained and synthetic."""

import unittest
from unittest.mock import Mock, patch

from predoverlay.catalog import CatalogClient
from predoverlay.errors import PredError


def hero():
    data = {
        "id": "900",
        "displayName": "Sparrow",
        "roles": ["CARRY"],
        "classes": ["SHARPSHOOTER"],
        "version": {"id": "169", "name": "1.17.2"},
    }
    return {"id": "19", "name": "Sparrow", "slug": "sparrow", "data": data}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock(spec=["query"])
        self.client.query.side_effect = AssertionError("Unexpected query")
        self.catalog = CatalogClient(self.client)
        # Accidental transport use is a test failure, even if the adapter changes.
        patcher = patch(
            "socket.socket", side_effect=AssertionError("Network is forbidden in tests")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def respond(self, data):
        self.client.query.side_effect = None
        self.client.query.return_value = data

    def query(self):
        self.client.query.assert_called_once()
        document, variables = self.client.query.call_args.args
        self.assertTrue(document.lstrip().startswith("query "))
        return document, variables

    def test_hero_catalog_selects_one_version_and_preserves_unavailable_entries(self):
        missing = {"id": "57", "name": "Seastrider", "slug": "ikra", "data": None}
        rows = [hero(), missing]
        self.respond({"heroes": rows})
        self.assertEqual(self.catalog.heroes(version=169), rows)
        query, variables = self.query()
        self.assertEqual(variables, {"version": "169"})
        self.assertIn("data(version: $version)", query)
        self.assertNotIn("matchesPaginated", query)

    def test_catalog_rejects_bad_response_containers_and_rows(self):
        for payload in (
            None,
            [],
            {},
            {"heroes": {}},
            {"heroes": [None]},
            {"heroes": [{"data": []}]},
            {"heroes": [{"id": "19"}]},
        ):
            with self.subTest(payload=payload):
                self.respond(payload)
                with self.assertRaises(PredError):
                    self.catalog.heroes()

    def test_invalid_versions_never_query(self):
        for version in (True, [], ""):
            with self.subTest(version=version), self.assertRaises(PredError):
                self.catalog.heroes(version=version)
        self.client.query.assert_not_called()

    def test_injected_client_errors_propagate_without_retry_or_fallback(self):
        error = PredError("Forbidden")
        self.client.query.side_effect = error
        with self.assertRaises(PredError) as raised:
            self.catalog.heroes()
        self.assertIs(raised.exception, error)
        self.client.query.assert_called_once()


if __name__ == "__main__":
    unittest.main()
