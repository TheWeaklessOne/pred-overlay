"""Offline adapter contracts; fixtures are self-contained and synthetic."""

import copy
import unittest
from unittest.mock import Mock, patch

from predoverlay.catalog import CatalogClient
from predoverlay.errors import PredError

PLAYER = "00000000-0000-0000-0000-000000000001"
MATCH = "10000000-0000-0000-0000-000000000001"
WINDOW = {"timeframe": {"startTime": "2026-10-19T00:00:00Z", "endTime": "2026-10-26T00:00:00Z"}}


def hero(detail=False):
    data = {
        "id": "900",
        "displayName": "Sparrow",
        "roles": ["CARRY"],
        "classes": ["SHARPSHOOTER"],
        "version": {"id": "169", "name": "1.17.2"},
    }
    if detail:
        data.update(
            {
                "mainAttributes": {},
                "attributes": [{"stat": "HEALTH", "values": [1, 2]}],
                "abilities": [{"key": "PRIMARY", "cooldown": [10], "cost": [20]}],
                "recommendedSkills": ["PRIMARY"],
                "perks": None,
            }
        )
    return {"id": "19", "name": "Sparrow", "slug": "sparrow", "data": data}


def item(detail=False):
    data = {
        "id": "901",
        "displayName": "Vanquisher",
        "version": {"id": "169"},
        "isHidden": False,
        "price": 850,
        "totalPrice": 3100,
    }
    if detail:
        data.update(
            {
                "stats": [{"stat": "CRITICAL_CHANCE", "value": 20, "showPercent": True}],
                "effects": [{"text": "<EffectText>Execute</>"}],
                "buildsFrom": [{"item": {"id": "72"}}],
                "buildsInto": [],
                "blocks": [],
                "blockedBy": [],
            }
        )
    return {"id": "175", "name": "Vanquisher", "slug": "vanquisher", "data": data}


def match():
    return {
        "id": MATCH,
        "version": {"id": "169"},
        "heroBans": None,
        "matchPlayers": [
            {
                "id": "123",
                "name": "HIDDEN",
                "player": {"id": PLAYER, "name": None},
                "hero": None,
                "inventoryItemData": [None, {"item": {"id": "175"}, "version": {"id": "169"}}],
                "transactions": [{"gameTime": 10, "transactionType": "UNDO_BUY", "itemData": None}],
                "abilityOrder": [{"gameTime": 0, "ability": "PRIMARY"}],
                "goldEarnedAtInterval": [350, 350, 700],
                "perks": None,
                "perkData": [None],
                "wardPlacements": [{"gameTime": 20, "location": {"x": 0, "y": 0, "z": 0}}],
                "wardDestructions": [],
            }
        ],
        "heroKills": [
            {
                "gameTime": 11,
                "killerPlayer": None,
                "killedPlayer": {"id": PLAYER},
                "killerHero": None,
                "killedHero": None,
                "location": {"x": 1, "y": 2, "z": 3},
            }
        ],
        "objectiveKills": [],
        "structureDestructions": [],
    }


def leaderboard():
    return {
        "ratingId": "12",
        "ratingType": "CURRENT",
        "limit": 20,
        "offset": 0,
        "totalCount": 100,
        "results": [
            {
                "player": {"id": PLAYER, "name": None},
                "rank": None,
                "ranking": 7,
                "points": None,
                "percentile": None,
            }
        ],
    }


def matchup():
    return {
        "metric": "WINRATE",
        "order": "DESC",
        "isAlly": False,
        "sameRole": True,
        "filter": {
            "gameModes": None,
            "roles": ["CARRY"],
            "ranks": None,
            "versions": ["169"],
            "matchupRoles": None,
        },
        "results": [
            {
                "matchupHero": {"id": "4", "slug": "feng-mao"},
                "matchesPlayed": 3,
                "winrate": 1.0,
                "loserate": 0.0,
            }
        ],
    }


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

    def test_item_catalog_does_not_filter_hidden_items_or_scale_prices(self):
        record = item()
        record["data"]["isHidden"] = True
        self.respond({"items": [record]})
        self.assertEqual(self.catalog.items(), [record])
        self.assertEqual(self.query()[1], {"version": None})

    def test_versions_preserve_server_order_and_null_metadata(self):
        rows = [{"id": "9", "name": None}, {"id": "100", "name": "1.0"}]
        self.respond({"versions": rows})
        self.assertEqual(self.catalog.versions(), rows)
        self.query()

    def test_detail_uses_numeric_id_or_slug_as_variables(self):
        for kind, ref, expected in (
            ("hero", 19, {"id": "19"}),
            ("hero", "sparrow", {"slug": "sparrow"}),
            ("item", "175", {"id": "175"}),
            ("item", "vanquisher", {"slug": "vanquisher"}),
        ):
            with self.subTest(kind=kind, ref=ref):
                self.client.reset_mock()
                record = hero(True) if kind == "hero" else item(True)
                self.respond({kind: record})
                self.assertEqual(getattr(self.catalog, kind)(ref, version="169"), record)
                self.assertEqual(self.query()[1], {"key": expected, "version": "169"})

    def test_detail_preserves_raw_game_markup_and_percentage_units(self):
        record = item(True)
        self.respond({"item": record})
        result = self.catalog.item("vanquisher")
        self.assertEqual(result["data"]["stats"][0]["value"], 20)
        self.assertEqual(result["data"]["effects"][0]["text"], "<EffectText>Execute</>")

    def test_reference_is_never_interpolated_into_document(self):
        text = 'sparrow") { currentUser { id } }'
        self.respond({"hero": {"data": None}})
        self.catalog.hero(text)
        document, variables = self.query()
        self.assertNotIn(text, document)
        self.assertEqual(variables["key"], {"slug": text})

    def test_missing_detail_differs_from_missing_version_data(self):
        for kind in ("hero", "item"):
            with self.subTest(kind=kind):
                self.respond({kind: None})
                with self.assertRaisesRegex(PredError, "not found"):
                    getattr(self.catalog, kind)("19")
                self.respond({kind: {"id": "19", "data": None}})
                self.assertIsNone(getattr(self.catalog, kind)("19")["data"])

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

    def test_nested_catalog_arrays_are_validated(self):
        cases = [
            ("hero", hero(True), "abilities", {}),
            ("hero", hero(True), "attributes", [None]),
            ("hero", hero(True), "perks", [None]),
            ("item", item(True), "effects", "text"),
            ("item", item(True), "buildsFrom", [{"item": []}]),
        ]
        for kind, record, field, bad in cases:
            with self.subTest(kind=kind, field=field):
                record["data"][field] = bad
                self.respond({kind: record})
                with self.assertRaises(PredError):
                    getattr(self.catalog, kind)("19")

    def test_match_events_fetch_one_full_uuid_and_preserve_null_slots_and_events(self):
        record = match()
        self.respond({"match": record})
        result = self.catalog.match_events(MATCH.replace("-", ""))
        self.assertEqual(result, record)
        self.assertEqual(self.query()[1], {"id": MATCH})
        member = result["matchPlayers"][0]
        self.assertIsNone(member["inventoryItemData"][0])
        self.assertEqual(member["transactions"][0]["transactionType"], "UNDO_BUY")
        self.assertEqual(member["goldEarnedAtInterval"], [350, 350, 700])

    def test_match_prefix_or_invalid_uuid_never_queries(self):
        for value in (MATCH[:8], "", None, 123):
            with self.subTest(value=value), self.assertRaises(PredError):
                self.catalog.match_events(value)
        self.client.query.assert_not_called()

    def test_missing_or_different_match_is_rejected(self):
        for record in (None, {**match(), "id": PLAYER}):
            with self.subTest(record=record):
                self.respond({"match": record})
                with self.assertRaises(PredError):
                    self.catalog.match_events(MATCH)

    def test_malformed_match_events_fail_instead_of_returning_partial_data(self):
        for field, bad in (
            ("heroKills", None),
            ("objectiveKills", [None]),
            ("matchPlayers", {}),
            ("structureDestructions", [{"location": []}]),
        ):
            with self.subTest(field=field):
                record = match()
                record[field] = bad
                self.respond({"match": record})
                with self.assertRaises(PredError):
                    self.catalog.match_events(MATCH)

    def test_malformed_nested_match_records_are_rejected(self):
        for field, bad in (
            ("transactions", [{"itemData": []}]),
            ("abilityOrder", [None]),
            ("inventoryItemData", [False]),
            ("goldEarnedAtInterval", {}),
            ("wardPlacements", [{"location": None}]),
        ):
            with self.subTest(field=field):
                record = match()
                record["matchPlayers"][0][field] = bad
                self.respond({"match": record})
                with self.assertRaises(PredError):
                    self.catalog.match_events(MATCH)

    def test_each_player_breakdown_uses_its_own_input_type_and_preserves_filters(self):
        cases = [
            ("hero", "heroStatistics", "PlayerHeroStatisticFilterInput", {"hero": {"id": "19"}}),
            ("role", "roleStatistics", "PlayerRoleStatisticFilterInput", {"role": "CARRY"}),
            (
                "gamemode",
                "gamemodeStatistics",
                "PlayerGameModeStatisticFilterInput",
                {"gameMode": "STANDARD"},
            ),
            (
                "interval",
                "intervalStatistics",
                "PlayerIntervalStatisticFilterInput",
                {"ratingData": [{"rating": {"id": "12"}}]},
            ),
        ]
        for kind, field, input_type, row in cases:
            with self.subTest(kind=kind):
                self.client.reset_mock()
                filters = copy.deepcopy(WINDOW)
                before = copy.deepcopy(filters)
                wrapper = {"results": [row]}
                self.respond({"player": {field: wrapper}})
                self.assertEqual(self.catalog.player_breakdown(PLAYER, kind, filters), wrapper)
                document, variables = self.query()
                self.assertIn("$filter: " + input_type, document)
                self.assertEqual(variables, {"id": PLAYER, "filter": before})
                self.assertEqual(filters, before)
                if kind == "interval":
                    self.assertIn("interval: DAY", document)

    def test_unsupported_breakdown_and_filters_fail_before_query(self):
        for kind, filters in (
            ("unknown", None),
            ([], None),
            ("hero", {"versions": ["169"]}),
            ("role", {"heroes": ["19"]}),
            ("role", []),
        ):
            with self.subTest(kind=kind), self.assertRaises(PredError):
                self.catalog.player_breakdown(PLAYER, kind, filters)
        self.client.query.assert_not_called()

    def test_daily_breakdown_requires_bounded_timezone_aware_window(self):
        cases = [
            None,
            {},
            {"timeframe": {"startTime": "2026-01-01T00:00:00Z"}},
            {"timeframe": {"startTime": "invalid", "endTime": "2026-01-02T00:00:00Z"}},
            {"timeframe": {"startTime": "2026-01-01", "endTime": "2026-01-02"}},
            {"timeframe": {"startTime": "2026-01-02T00:00:00Z", "endTime": "2026-01-01T00:00:00Z"}},
            {"timeframe": {"startTime": "2024-01-01T00:00:00Z", "endTime": "2026-01-01T00:00:00Z"}},
        ]
        for filters in cases:
            with self.subTest(filters=filters), self.assertRaises(PredError):
                self.catalog.player_breakdown(PLAYER, "interval", filters)
        self.client.query.assert_not_called()

    def test_breakdown_missing_or_malformed_is_not_an_empty_result(self):
        for player in (
            None,
            {"heroStatistics": None},
            {"heroStatistics": {}},
            {"heroStatistics": {"results": None}},
            {"heroStatistics": {"results": [None]}},
            {"heroStatistics": {"results": [{"hero": []}]}},
        ):
            with self.subTest(player=player):
                self.respond({"player": player})
                with self.assertRaises(PredError):
                    self.catalog.player_breakdown(PLAYER, "hero")
        self.respond({"player": {"heroStatistics": {"results": []}}})
        self.assertEqual(self.catalog.player_breakdown(PLAYER, "hero"), {"results": []})

    def test_leaderboard_returns_one_page_without_renumbering_or_filling_nulls(self):
        page = leaderboard()
        self.respond({"leaderboardPaginated": page})
        self.assertEqual(self.catalog.leaderboard(12), page)
        self.assertEqual(self.query()[1], {"rating": "12", "limit": 20, "offset": 0})
        self.assertEqual(page["results"][0]["ranking"], 7)
        self.assertIsNone(page["results"][0]["points"])

    def test_leaderboard_accepts_server_cap_and_empty_page_beyond_total(self):
        for page, offset in (
            ({**leaderboard(), "limit": 1}, 0),
            ({**leaderboard(), "results": [], "offset": 200}, 200),
        ):
            with self.subTest(offset=offset):
                self.respond({"leaderboardPaginated": page})
                self.assertEqual(self.catalog.leaderboard("12", offset=offset), page)

    def test_leaderboard_rejects_invalid_bounds_before_query(self):
        for limit, offset in ((0, 0), (51, 0), (True, 0), (1.5, 0), (20, -1), (20, False)):
            with self.subTest(limit=limit, offset=offset), self.assertRaises(PredError):
                self.catalog.leaderboard("12", limit, offset)
        self.client.query.assert_not_called()

    def test_leaderboard_rejects_wrong_page_or_rating(self):
        for updates in (
            {"offset": 1},
            {"limit": 21},
            {"limit": 0},
            {"totalCount": 0},
            {"totalCount": True},
            {"ratingId": "11"},
            {"ratingType": "PEAK"},
            {"results": {}},
            {"results": [{"player": [], "rank": None}]},
        ):
            with self.subTest(updates=updates):
                self.respond({"leaderboardPaginated": {**leaderboard(), **updates}})
                with self.assertRaises(PredError):
                    self.catalog.leaderboard("12")

    def test_matchup_has_no_invented_limit_and_keeps_small_samples(self):
        result = matchup()
        self.respond({"hero": {"matchupStatistic": result}})
        filters = {"roles": ["CARRY"], "versions": ["169"]}
        self.assertEqual(self.catalog.matchup(19, filters), result)
        document, variables = self.query()
        self.assertNotIn("limit", document)
        self.assertNotIn("offset", document)
        self.assertEqual(variables, {"id": "19", "filter": filters})
        self.assertEqual(result["results"][0]["matchesPlayed"], 3)

    def test_matchup_rejects_unsupported_limit_or_region_filter(self):
        for filters in ({"limit": 10}, {"regions": ["EUROPE"]}, {"timeframe": WINDOW["timeframe"]}):
            with self.subTest(filters=filters), self.assertRaises(PredError):
                self.catalog.matchup("19", filters)
        self.client.query.assert_not_called()

    def test_matchup_checks_wrappers_and_nested_lists(self):
        bad_filter = matchup()
        bad_filter["filter"]["roles"] = "CARRY"
        for hero_value in (
            None,
            {"matchupStatistic": None},
            {"matchupStatistic": []},
            {"matchupStatistic": {**matchup(), "results": [None]}},
            {"matchupStatistic": {**matchup(), "results": [{"matchupHero": None}]}},
            {"matchupStatistic": bad_filter},
        ):
            with self.subTest(hero=hero_value):
                self.respond({"hero": hero_value})
                with self.assertRaises(PredError):
                    self.catalog.matchup("19")

    def test_invalid_ids_and_versions_never_query(self):
        calls = [
            lambda: self.catalog.heroes(version=True),
            lambda: self.catalog.items(version=[]),
            lambda: self.catalog.hero(""),
            lambda: self.catalog.item(None),
            lambda: self.catalog.player_breakdown("not-uuid", "hero"),
            lambda: self.catalog.leaderboard(""),
            lambda: self.catalog.matchup(False),
        ]
        for invoke in calls:
            with self.assertRaises(PredError):
                invoke()
        self.client.query.assert_not_called()

    def test_injected_client_errors_propagate_without_retry_or_fallback(self):
        error = PredError("Forbidden")
        self.client.query.side_effect = error
        with self.assertRaises(PredError) as raised:
            self.catalog.versions()
        self.assertIs(raised.exception, error)
        self.client.query.assert_called_once()


if __name__ == "__main__":
    unittest.main()
