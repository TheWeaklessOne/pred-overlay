"""The role screen before the pick screen: names on plates under the roles picked."""

import os
import unittest

from test_core import DRAFT_NAMES, Flow, draft_texts

from predoverlay.overlay import core, roster

# Never a window on the real screen (the game may be up).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def role_texts(plates, anchor=True):
    """{(column, place in its stack): name} -> a draft reading of the role screen."""
    texts = {("role", c * roster.ROLE_STACK + k): [n, n] for (c, k), n in plates.items()}
    if anchor:
        texts[("roles", 0)] = ["MIDLANE", ""]
    return texts


class RoleScreenTests(Flow):
    def test_names_on_plates_are_taken_and_looked_up_before_the_pick_screen(self):
        self.enter_draft()
        self.step(
            "screen",
            "draft",
            self.state.phase_at,
            role_texts({(0, 0): "vanir", (0, 1): "SilverFalcon", (2, 0): "y00tsa"}),
        )
        readings = sorted(s["reading"] for s in self.state.seats.values())
        self.assertEqual(readings, ["SilverFalcon", "vanir", "y00tsa"])
        (search,) = self.of("search")  # the others are looked up at once, in one
        self.assertEqual(len(search[2]), 2)

    def test_a_name_picked_later_is_searched_once_the_last_search_answered(self):
        self.enter_draft()
        plates = {(0, 0): "vanir"}
        self.step("screen", "draft", self.state.phase_at, role_texts(plates))
        (search,) = self.of("search")  # asked at once
        self.sent.clear()
        plates[(1, 0)] = "y00tsa"
        self.step("screen", "draft", self.state.phase_at, role_texts(plates))
        self.assertFalse(self.of("search"))  # the first is still out
        self.step("found", self.state.epoch, {})
        (search,) = self.of("search")
        self.assertEqual(list(search[2].values())[0][0], "y00tsa")

    def test_a_reading_without_the_role_labels_or_my_name_counts_for_nothing(self):
        self.enter_draft()
        # The pick screen's hero grid has dark tiles too: no labels, no plates of ours.
        self.step(
            "screen", "draft", self.state.phase_at, role_texts({(1, 1): "Bayle"}, anchor=False)
        )
        self.assertEqual(self.state.seats, {})

    def test_my_own_name_on_a_plate_stands_in_for_the_labels(self):
        self.enter_draft()
        texts = role_texts({(4, 0): "SilverFaIcon", (3, 0): "Tirn"}, anchor=False)
        self.step("screen", "draft", self.state.phase_at, texts)
        self.assertEqual(len(self.state.seats), 2)

    def test_a_player_who_has_not_picked_yet_does_not_go(self):
        self.enter_draft()
        plates = {(0, 0): "vanir", (2, 0): "y00tsa", (3, 0): "PelmVorr"}
        self.step("screen", "draft", self.state.phase_at, role_texts(plates))
        for _ in range(5):  # vanir clicks another role; the plate is gone for a while
            self.step("screen", "draft", self.state.phase_at, role_texts({(2, 0): "y00tsa"}))
        self.assertEqual(
            sorted(s["reading"] for s in self.state.seats.values() if not s["gone"]),
            ["PelmVorr", "vanir", "y00tsa"],
        )

    def test_no_more_than_a_team_comes_from_the_role_screen(self):
        self.enter_draft()
        plates = {(c, 0): f"Player{c}x" for c in range(6)}
        self.step("screen", "draft", self.state.phase_at, role_texts(plates))
        self.assertEqual(len(self.state.seats), core.TEAM)

    def test_the_pick_screen_then_matches_the_same_players_by_name(self):
        self.enter_draft()
        plates = {(0, 0): "SILVERFALCON", (1, 0): "HULGARVENTAS4", (2, 0): "URVAK"}
        self.step("screen", "draft", self.state.phase_at, role_texts(plates))
        before = set(self.state.seats)
        self.step("screen", "draft", self.state.phase_at, draft_texts(DRAFT_NAMES))
        live = {k: s for k, s in self.state.seats.items() if not s["gone"]}
        self.assertEqual(len(live), 5)
        self.assertTrue(before <= set(live))  # the plates' players kept their slots
        # The pick screen's rows give the order now.
        self.assertEqual(sorted(s["order"] for s in live.values()), [0, 1, 2, 3, 4])


class PlateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtGui import QGuiApplication

        cls.app = QGuiApplication.instance() or QGuiApplication([])

    def box(self, plate=True, border=True):
        from PySide6.QtGui import QColor, QImage, QPainter

        width, height = 2 * roster.ROLE_W, 2 * roster.ROLE_H
        image = QImage(width, height, QImage.Format.Format_RGB32)
        painter = QPainter(image)
        for x in range(0, width, 6):  # the map behind: light and uneven
            shade = 90 + (x * 37) % 80
            painter.fillRect(x, 0, 6, height, QColor(shade, shade, shade))
        if plate:
            painter.fillRect(9, 1, 240, 33, QColor(35, 40, 52))
            if border:
                painter.fillRect(9, 1, 240, 2, QColor(72, 78, 95))
                painter.fillRect(9, 32, 240, 2, QColor(72, 78, 95))
            painter.fillRect(80, 10, 90, 15, QColor(230, 230, 230))  # the name
        painter.end()
        return image

    def test_a_plate_is_found_and_its_inside_given(self):
        left, right = roster.plate(self.box())
        self.assertLessEqual(abs(left - 9), 1)
        self.assertLessEqual(abs(right - 249), 1)
        line = roster.plate_line(self.box(), (left, right))
        self.assertEqual(line.height(), 2 * roster.ROLE_H)
        top = line.pixelColor(5, 1)
        self.assertLess(top.red(), 60)  # the border line painted over in the plate's dark

    def test_no_plate_or_a_dark_spot_without_a_border_is_not_one(self):
        self.assertIsNone(roster.plate(self.box(plate=False)))
        self.assertIsNone(roster.plate(self.box(border=False)))

    def test_the_draft_reading_has_every_role_box_and_the_labels(self):
        boxes = roster.boxes("draft")
        roles = [k for k in boxes if k[0] == "role"]
        self.assertEqual(len(roles), len(roster.ROLE_X) * roster.ROLE_STACK)
        self.assertIn(("roles", 0), boxes)
        self.assertIn(("anchor", 0), boxes)


if __name__ == "__main__":
    unittest.main()


class PlayStatsTests(unittest.TestCase):
    RAW = {
        "roleStatistics": {"results": [{"role": "OFFLANE", "matchesPlayed": 40, "matchesWon": 22}]},
        "heroStatistics": {
            "results": [
                {
                    "hero": {"name": "LtBelica"},
                    "matchesPlayed": 20,
                    "matchesWon": 11,
                    "totalKills": 100,
                    "totalDeaths": 50,
                    "totalAssists": 60,
                }
            ]
        },
        "form": {
            "results": [
                {"team": "DUSK", "match": {"winningTeam": "DUSK"}},
                {"team": "DAWN", "match": {"winningTeam": "DAWN"}},
                {"team": "DAWN", "match": {"winningTeam": "DAWN"}},
                {"team": "DUSK", "match": {"winningTeam": "DAWN"}},
            ]
        },
    }

    def test_pred_gg_totals_become_the_cards_hero_role_and_form(self):
        from predoverlay.overlay import cards, rows

        stats = cards.play_stats(self.RAW)
        data = {"stats": stats, "me": False}
        how = rows.plays(data, {"hero": "LT. BELICA", "role": "OFFLANE"})
        self.assertEqual(how["hero"], "20 GAMES · 55% · KDA 3.2")
        self.assertEqual(how["role"], "40 GAMES · 55%")
        self.assertEqual(how["form"], [True, True, True, False])
        self.assertEqual(rows.streak(how["form"]), "3 WINS IN A ROW")

    def test_a_hero_never_played_and_a_few_games_say_so(self):
        from predoverlay.overlay import cards, rows

        stats = cards.play_stats(self.RAW)
        how = rows.plays({"stats": stats, "me": False}, {"hero": "STEEL", "role": "CARRY"})
        self.assertEqual((how["hero"], how["role"]), ("FIRST GAME", "FIRST GAME"))
        stats["roles"]["CARRY"] = (3, 3)
        how = rows.plays({"stats": stats, "me": False}, {"role": "CARRY"})
        self.assertEqual(how["role"], "3 GAMES")  # no win rate on three games

    def test_nothing_for_me_or_without_statistics(self):
        from predoverlay.overlay import cards, rows

        self.assertIsNone(cards.play_stats({}))
        stats = cards.play_stats(self.RAW)
        self.assertIsNone(rows.plays({"stats": stats, "me": True}, {"hero": "STEEL"}))
        self.assertIsNone(rows.streak([True, False, True]))


class PartyTests(unittest.TestCase):
    @staticmethod
    def member(team, recent=None, pid=None, mates=None, total=0):
        return {
            "team": team,
            "pid": pid,
            "recent": recent or {},
            "mates": mates or {},
            "total": total,
        }

    def test_players_who_shared_a_side_three_times_lately_are_a_party(self):
        m = self.member
        members = {
            "a0": m("ally", {"m1": "DUSK", "m2": "DAWN", "m3": "DUSK"}),
            "a1": m("ally", {"m1": "DUSK", "m2": "DAWN", "m3": "DUSK", "m9": "DAWN"}),
            "a2": m("ally", {"m1": "DAWN", "m2": "DUSK"}),  # met them, on the other side
            "e0": m("enemy", {"m1": "DUSK", "m2": "DAWN"}),  # their team now: not ours
        }
        found = core.parties(members)
        self.assertEqual(set(found), {"a0", "a1"})
        self.assertEqual(
            found["a0"], {"size": 2, "group": 0, "with": ("a1",), "games": 3, "won": None}
        )

    def test_pred_gg_s_count_of_games_together_makes_a_party(self):
        m = self.member
        members = {
            "a0": m("ally", pid="me", mates={"duo": (935, 480), "rnd": (7, 4)}, total=1134),
            "a1": m("ally", pid="duo", total=1200),
            "a2": m("ally", pid="rnd", total=300),  # seven games: chance in 1134
        }
        found = core.parties(members)
        self.assertEqual(set(found), {"a0", "a1"})
        self.assertEqual((found["a1"]["games"], found["a1"]["won"]), (935, 480))

    def test_two_matches_together_are_not_a_party_and_trios_join_up(self):
        m = self.member
        two = {
            "a0": m("ally", {"m1": "DUSK", "m2": "DAWN"}),
            "a1": m("ally", {"m1": "DUSK", "m2": "DAWN"}),
        }
        self.assertEqual(core.parties(two), {})  # the same randoms queued again, maybe
        trio = {
            "e0": m("enemy", {"m1": "DUSK", "m2": "DUSK", "m5": "DAWN"}),
            "e1": m("enemy", {"m1": "DUSK", "m2": "DUSK", "m5": "DAWN", "m3": "DAWN"}),
            "e2": m("enemy", {"m3": "DAWN", "m4": "DAWN", "m2": "DUSK", "m6": "DUSK"}),
        }
        trio["e2"]["recent"].update({"m1": "DUSK"})
        found = core.parties(trio)
        self.assertEqual({v["size"] for v in found.values()}, {3})
        self.assertEqual(set(found["e0"]["with"]), {"e1", "e2"})

    def test_the_view_names_the_partners_as_the_panel_shows_them(self):
        from predoverlay.overlay import rows

        state = core.State(me="me", my_name="Me", salt="b" * 32)
        core.reduce(state, ("log", {"phase": "draft", "at_ts": 1.0}), 1.0)
        texts = {("ally", 0): ["Alpha", "Alpha"], ("ally", 1): ["Bravo", "Bravo"]}
        texts[("anchor", 0)] = ["ALLY", "ALLY"]
        core.reduce(state, ("screen", "draft", 1.0, texts), 2.0)
        found = {
            "a0": {"player_id": "pa", "name": "Alpha"},
            "a1": {"player_id": "pb", "name": "Bravo"},
        }
        core.reduce(state, ("found", state.epoch, found), 3.0)
        stats = {"recent": [], "form": [], "roles": {}, "heroes": {}, "total": 500}
        alpha = {"current": None, "peak": None, "seasons": (), "main": None}
        alpha["stats"] = dict(stats, mates={"pb": (120, 66)})
        bravo = dict(alpha, stats=dict(stats, mates={}))
        core.reduce(state, ("profiles", state.epoch, {"pa": alpha, "pb": bravo}), 4.0)
        by = {p.read_as: p for p in core.view(state).allies}
        self.assertEqual(by["Bravo"].party["with"], ("Alpha",))
        self.assertEqual(
            rows.party_line(by["Alpha"].party),
            "DUO WITH Bravo \u00b7 120 GAMES TOGETHER \u00b7 55% WON",
        )


class HabitTests(unittest.TestCase):
    NOW = 1_795_000_000.0  # 2026-11-18

    def stats(self, **kw):
        from datetime import datetime, timezone

        today = datetime.fromtimestamp(self.NOW, timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
        base = {
            "roles": {"CARRY": (500, 250)},
            "heroes": {"sparrow": {"name": "Sparrow", "played": 100, "won": 50}},
            "form": [True, False] + [True] * 8,
            "played": [("Sparrow", today), ("Sparrow", today)]
            + [(h, "2026-01-01T00:00:00Z") for h in ("Grux", "Kwang", "Steel", "Gideon")] * 2,
            "total": 500,
        }
        base.update(kw)
        return {"stats": base, "me": False}

    def facts(self, data):
        from predoverlay.overlay import rows

        return [text for text, _ in rows.habits(data, now=self.NOW)]

    def test_games_today_and_nothing_else_for_an_ordinary_player(self):
        self.assertEqual(self.facts(self.stats()), ["2 GAMES TODAY \u00b7 1\u20131"])

    def test_games_today_are_this_sitting_across_midnight_and_not_before_a_break(self):
        from datetime import datetime, timezone

        def at(hours_ago):
            t = datetime.fromtimestamp(self.NOW - hours_ago * 3600, timezone.utc)
            return t.strftime("%Y-%m-%dT%H:%M:%SZ")

        heroes = ("Grux", "Kwang", "Steel", "Gideon", "Sparrow")
        old = [(h, "2026-01-01T00:00:00Z") for h in heroes]
        sitting = [(h, at(t)) for h, t in zip(heroes, (0.5, 1.2, 1.9, 2.6))]
        sitting += [("Sparrow", at(6))] + old
        self.assertEqual(self.facts(self.stats(played=sitting)), ["4 GAMES TODAY · 3–1"])
        away = [(h, at(t)) for h, t in zip(heroes, (3.5, 4.2, 4.9))] + old + old[:2]
        self.assertEqual(self.facts(self.stats(played=away)), [])  # back after a break

    def test_games_today_count_from_when_the_profile_came(self):
        from datetime import datetime, timezone

        def at(hours_ago):
            t = datetime.fromtimestamp(self.NOW - hours_ago * 3600, timezone.utc)
            return t.strftime("%Y-%m-%dT%H:%M:%SZ")

        heroes = ("Grux", "Kwang", "Steel", "Gideon", "Sparrow")
        old = [(h, "2026-01-01T00:00:00Z") for h in heroes]
        played = [(h, at(t)) for h, t in zip(heroes, (1.8, 2.5, 3.2))] + old + old[:2]
        data = self.stats(played=played)
        data["stats"]["at"] = self.NOW
        from predoverlay.overlay import rows

        later = [f for f, _ in rows.habits(data, now=self.NOW + 40 * 60)]  # mid-match
        self.assertEqual(later, ["3 GAMES TODAY \u00b7 2\u20131"])

    def test_one_hero_lately_beats_the_long_view(self):
        played = [("Lt. Belica", "2026-01-01T00:00:00Z")] * 7 + [
            ("Grux", "2026-01-01T00:00:00Z")
        ] * 3
        self.assertEqual(
            self.facts(self.stats(played=played)), ["MOSTLY LT. BELICA \u00b7 7 OF LAST 10"]
        )

    def test_one_hero_for_good_needs_a_big_share_of_many_games(self):
        heroes = {"sparrow": {"name": "Sparrow", "played": 200, "won": 100}}
        played = [("Grux", "2026-01-01T00:00:00Z")] * 3 + [("Kwang", "2026-01-01T00:00:00Z")] * 7
        played[3:] = [("Kwang", "2026-01-01T00:00:00Z"), ("Steel", "2026-01-01T00:00:00Z")] * 3 + [
            ("Gideon", "2026-01-01T00:00:00Z")
        ]
        facts = self.facts(self.stats(heroes=heroes, played=played))
        self.assertEqual(facts, ["ONE-TRICK SPARROW \u00b7 40% OF 500 GAMES"])

    def test_a_fresh_account_winning_a_lot_is_stated_in_numbers(self):
        data = self.stats(roles={"MIDLANE": (40, 30)}, total=40, played=[], form=[])
        from predoverlay.overlay import rows

        (fact,) = rows.habits(data, now=self.NOW)
        self.assertEqual(fact, ("FEW GAMES, WINS A LOT \u00b7 75% IN 40", True))

    def test_nothing_for_me(self):
        data = self.stats()
        data["me"] = True
        self.assertEqual(self.facts(data), [])


class DraftHeroTests(Flow):
    def test_a_placeholder_is_told_from_a_hero_by_comparison(self):
        self.step("display", {"Kwang": "Kwang", "Wraith": "Wraith"})
        for slip in ("W\u00e4iting...", "Waitinq...", "Waiing...", "Writing...", "Pucking..."):
            self.assertIsNone(core._picked(self.state, [slip, slip]), slip)
        self.assertEqual(core._picked(self.state, ["Wraith", "Waith", "Wraith"]), "WRAITH")

    def test_a_misread_placeholder_picks_no_hero_and_an_empty_read_keeps_one(self):
        self.step("display", {"Kwang": "Kwang", "Wraith": "Wraith", "Steel": "Steel"})
        self.enter_draft()
        texts = draft_texts(DRAFT_NAMES)
        for hero in (["Waiting...", "Waiting...", "Walting..."], ["Wa1ting..."], ["Wai"]):
            texts[("hero", 1)] = hero
            self.step("screen", "draft", self.state.phase_at, texts)
            by = {s["reading"]: s for s in self.state.seats.values()}
            self.assertIsNone(by["HULGARVENTAS4"].get("hero"), hero)
        texts[("hero", 1)] = ["Steel", "Steel"]
        self.step("screen", "draft", self.state.phase_at, texts)
        texts[("hero", 1)] = ["", ""]  # one reading the line came out empty
        self.step("screen", "draft", self.state.phase_at, texts)
        texts[("hero", 1)] = ["Stee1"]  # one version alone: not enough to change it
        self.step("screen", "draft", self.state.phase_at, texts)
        by = {s["reading"]: s for s in self.state.seats.values()}
        self.assertEqual(by["HULGARVENTAS4"]["hero"], "STEEL")

    def test_the_hero_picked_shows_with_their_record_on_it(self):
        from predoverlay.overlay import rows

        self.step("display", {"Steel": "Steel", "LtBelica": "Lt. Belica"})
        self.enter_draft()
        texts = draft_texts(DRAFT_NAMES)
        texts[("hero", 1)] = ["Stee1", "Steel", "Steel"]
        texts[("hero", 2)] = ["Waitin9...", "Waiting..."]
        self.step("screen", "draft", self.state.phase_at, texts)
        by = {s["reading"]: s for s in self.state.seats.values()}
        self.assertEqual(by["HULGARVENTAS4"]["hero"], "STEEL")
        self.assertIsNone(by["BRENK141"].get("hero"))
        stats = {"heroes": {"steel": {"name": "Steel", "played": 143, "won": 82}}}
        self.assertEqual(rows.picked_line("STEEL", stats), "Steel \u00b7 143 G \u00b7 57%")
        self.assertEqual(
            rows.picked_line("STEEL", {"heroes": {"x": {}}}), "Steel \u00b7 first game"
        )
        self.assertIsNone(rows.picked_line(None, stats))


class ReviewFixTests(unittest.TestCase):
    def test_heroes_are_matched_and_shown_by_the_games_names(self):
        from predoverlay.overlay import rows

        stats = {
            "heroes": {
                "wood": {
                    "name": "Wood",
                    "played": 300,
                    "won": 160,
                    "kills": 1,
                    "deaths": 1,
                    "assists": 1,
                }
            },
            "played": [("Wood", None)] * 10,
            "form": [True] * 10,
            "roles": {"SUPPORT": (300, 160)},
            "total": 300,
        }
        shown = core.shown_stats(stats, {"Wood": "Mourn"})
        data = {"stats": shown, "me": False}
        self.assertTrue(rows.plays(data, {"hero": "MOURN"})["hero"].startswith("300 GAMES"))
        facts = [f for f, _ in rows.habits(data)]
        self.assertIn("MOSTLY MOURN \u00b7 10 OF LAST 10", facts)
        self.assertEqual(core.shown_stats(shown, {"Wood": "Mourn"}), shown)  # idempotent

    def test_one_player_in_two_slots_is_no_party(self):
        member = {
            "team": "ally",
            "pid": "p1",
            "recent": {"m1": "DUSK", "m2": "DUSK", "m3": "DUSK"},
            "mates": {},
            "total": 100,
        }
        self.assertEqual(core.parties({"a0": dict(member), "a1": dict(member)}), {})

    def test_an_error_inside_the_statistics_keeps_the_ranks(self):
        from unittest import mock

        from predoverlay import api

        client = api.Client(token=None)
        answer = {
            "data": {"p0": {"name": "x", "heroStatistics": None}},
            "errors": [{"message": "boom", "path": ["p0", "heroStatistics"]}],
        }
        with mock.patch.object(client.transport, "request", return_value=answer):
            found = client.query("query{x}", tolerate=("heroStatistics",))
            self.assertEqual(found["p0"]["name"], "x")
            with self.assertRaises(Exception):
                client.query("query{x}")
        answer["errors"].append({"message": "other", "path": ["p0", "ratings"]})
        with mock.patch.object(client.transport, "request", return_value=answer):
            with self.assertRaises(Exception):
                client.query("query{x}", tolerate=("heroStatistics",))

    def test_hero_names_keep_the_games_spelling_in_capitals(self):
        from predoverlay.overlay import rows

        shown = {n: rows._hero_shown(n) for n in ("Lt. Belica", "Feng Mao", "GRIM.exe", "LtBelica")}
        self.assertEqual(
            shown,
            {
                "Lt. Belica": "LT. BELICA",
                "Feng Mao": "FENG MAO",
                "GRIM.exe": "GRIM.EXE",
                "LtBelica": "LTBELICA",  # only with no catalog: shown_stats renames
            },
        )

    def test_a_player_nulled_by_a_statistics_error_is_asked_again_for_ranks(self):
        from unittest import mock

        from predoverlay import api
        from predoverlay.overlay import lanes

        client = api.Client(token=None)
        first = {
            "data": {"seasons": [], "p0": None, "p1": None, "p2": {"name": "c"}},
            "errors": [{"message": "boom", "path": ["p0", "heroStatistics", "results"]}],
        }
        second = {"data": {"p0": {"name": "a"}}}
        lane = lanes.PredggIO(None, client=client, public=client)
        with mock.patch.object(client.transport, "request", side_effect=[first, second]) as sent:
            events = lane.handle(("profiles", 1, ("A", "B", "C")))
        (_, _, found) = events[-1]
        self.assertEqual(sorted(found), ["A", "C"])  # B: no player, not asked again
        self.assertEqual(sent.call_count, 2)
        self.assertNotIn("heroStatistics", sent.call_args.args[1]["query"])

    def test_a_failing_retry_for_ranks_keeps_the_others(self):
        from unittest import mock

        from predoverlay import api
        from predoverlay.overlay import lanes

        client = api.Client(token=None)
        first = {
            "data": {"seasons": [], "p0": None, "p1": {"name": "b"}},
            "errors": [{"message": "boom", "path": ["p0", "heroStatistics", "results"]}],
        }
        second = {"data": {"p0": None}, "errors": [{"message": "boom", "path": ["p0", "name"]}]}
        lane = lanes.PredggIO(None, client=client, public=client)
        with mock.patch.object(client.transport, "request", side_effect=[first, second]):
            events = lane.handle(("profiles", 1, ("A", "B")))
        (_, _, found) = events[-1]
        self.assertEqual(sorted(found), ["B"])
