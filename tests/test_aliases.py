"""Console players: learned after a match, recognised the next time, never named."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from predoverlay.overlay import aliases, core, lookup

ME = "me-id"
T0 = 1_000_000.0
TOP = ["GTame52", "SonyName1", "tiikvarn", "SonyName2", "SonyName3"]
BOTTOM = ["mate1", "mate2", "SonyMate", "SilverFalcon", "mate4"]
ROLES = ("CARRY", "SUPPORT", "MIDLANE", "JUNGLE", "OFFLANE")


def texts(top, bottom):
    found = {("top", i): [n, n] for i, n in enumerate(top)}
    found.update({("bottom", i): [n, n] for i, n in enumerate(bottom)})
    return found


def finished_match():
    players = [
        {
            "player_id": f"e{i}",
            "name": "HIDDEN",
            "team": "DUSK",
            "role": r,
            "hero": "x",
            "kda": (1, 1, 1),
        }
        for i, r in enumerate(ROLES)
    ]
    players += [
        {
            "player_id": ME if i == 3 else f"a{i}",
            "name": "SilverFalcon" if i == 3 else "HIDDEN",  # Pred.gg shows my name
            "team": "DAWN",
            "role": r,
            "hero": "x",
            "kda": (1, 1, 1),
        }
        for i, r in enumerate(ROLES)
    ]
    return {"match_id": "m1", "ended_at": T0, "winning_team": "DAWN", "players": players}


class Learning(unittest.TestCase):
    def setUp(self):
        self.state = core.State()
        self.sent = []
        self.step("aliases", "a" * 32, {}, [])
        self.now = T0

    def step(self, *event, now=None):
        commands = core.reduce(
            self.state, event, now if now is not None else getattr(self, "now", T0)
        )
        self.sent += commands
        return commands

    def play(self, match_id="m1"):
        self.step("log", {"phase": "accepted", "at_ts": self.now})
        self.step(
            "log",
            {
                "phase": "loading",
                "at_ts": self.now,
                "map": "x",
                "user_id": ME,
                "user_name": "SilverFalcon",
                "experience": "BP_Experience_Game_Ranked",
            },
        )
        self.step("screen", "loading", self.state.phase_at, texts(TOP, BOTTOM))
        found = (
            {slot: None for slot in self.sent[-1][2]}
            if self.sent and self.sent[-1][0] == "search"
            else {}
        )
        searches = [c for c in self.sent if c[0] == "search"]
        if searches:
            self.step("found", searches[-1][1], {slot: None for slot in searches[-1][2]})
        self.step(
            "log",
            {
                "phase": "finished",
                "match_id": match_id,
                "reason": "REASON_FINISHED",
                "at_ts": self.now + 1800,
            },
        )
        self.now += 1800
        return found

    def test_after_the_match_the_loading_names_learn_their_players_by_team_and_role(self):
        self.play()
        self.assertEqual([c for c in self.step("tick", now=self.now + 10) if c[0] == "match"], [])
        asked = [
            c for c in self.step("tick", now=self.now + core.LEARN_AFTER + 1) if c[0] == "match"
        ]
        self.assertEqual(asked, [("match", "m1")])
        taught = [c for c in self.step("match", "m1", finished_match()) if c[0] == "remember"]
        ((_, learned),) = taught
        self.assertEqual(len(set(learned.values())), 9)  # everyone but me
        self.assertEqual(len(learned), 27)  # each by key, by spelling, and "spelling known"
        self.assertIn("e1", learned.values())
        self.assertNotIn("SonyName1", json.dumps(learned))  # digests, never names

        # The next match: the console player is known at once, no search for them.
        self.sent.clear()
        self.now += 600
        self.step("log", {"phase": "accepted", "at_ts": self.now})
        self.step(
            "log",
            {
                "phase": "loading",
                "at_ts": self.now,
                "map": "x",
                "user_id": ME,
                "user_name": "SilverFalcon",
                "experience": "BP_Experience_Game_Ranked",
            },
        )
        self.step("screen", "loading", self.state.phase_at, texts(TOP, BOTTOM))
        self.assertEqual(self.state.ids["e1"]["pid"], "e1")
        self.assertFalse([c for c in self.sent if c[0] == "search"])

    def test_a_match_pred_gg_has_not_listed_yet_is_asked_for_again_then_left(self):
        self.play()
        at = self.now + core.LEARN_AFTER + 1
        for delay in core.LEARN_RETRY:
            self.assertTrue([c for c in self.step("tick", now=at) if c[0] == "match"])
            self.step("match", "m1", None, now=at)
            at += delay + 1
        self.assertTrue([c for c in self.step("tick", now=at) if c[0] == "match"])
        self.step("match", "m1", None, now=at)
        self.assertEqual(self.state.learns, [])

    def test_columns_in_the_other_order_are_told_by_the_pc_players(self):
        # Dusk's loading screen runs OFFLANE..CARRY, Dawn's CARRY..OFFLANE (2026-11-19).
        self.play()
        details = finished_match()
        flip = dict(zip(ROLES, reversed(ROLES)))
        named = {"GTame52": "e0", "tiikvarn": "e2", "mate1": "a0", "mate2": "a1", "mate4": "a4"}
        for p in details["players"]:
            p["role"] = flip[p["role"]]
            if p["player_id"] in named.values():
                p["name"] = next(n for n, pid in named.items() if pid == p["player_id"])
        self.step("tick", now=self.now + core.LEARN_AFTER + 1)
        ((_, learned),) = [c for c in self.step("match", "m1", details) if c[0] == "remember"]
        self.assertEqual(len(set(learned.values())), 9)
        sony1 = aliases.digest(self.state.salt, lookup.name_key("SonyName1"))
        self.assertEqual(learned[sony1], "e1")  # the column read, not the role it would be

    def test_with_no_pc_name_to_tell_the_order_only_the_middle_column_is_learned(self):
        self.play()
        details = finished_match()
        for p in details["players"]:
            p["name"] = "HIDDEN"
        self.step("tick", now=self.now + core.LEARN_AFTER + 1)
        ((_, learned),) = [c for c in self.step("match", "m1", details) if c[0] == "remember"]
        self.assertEqual(set(learned.values()), {"e2", "a2"})

    def test_my_name_misread_on_the_loading_screen_still_tells_the_column_order(self):
        self.play_read(BOTTOM[:3] + ["SilverFa1con"] + BOTTOM[4:])
        details = finished_match()
        flip = dict(zip(ROLES, reversed(ROLES)))
        for p in details["players"]:
            p["role"] = flip[p["role"]]
        self.step("tick", now=self.now + core.LEARN_AFTER + 1)
        ((_, learned),) = [c for c in self.step("match", "m1", details) if c[0] == "remember"]
        self.assertIn("a0", learned.values())  # my team learned: my column by who I am

    def play_read(self, bottom):
        global BOTTOM
        keep, BOTTOM = BOTTOM, bottom
        try:
            self.play()
        finally:
            BOTTOM = keep

    def test_two_players_on_one_role_teach_nothing_about_it(self):
        self.play()
        details = finished_match()
        details["players"][1]["role"] = "CARRY"  # e1 too: CARRY and SUPPORT are unclear now
        self.step("tick", now=self.now + core.LEARN_AFTER + 1)
        ((_, learned),) = [c for c in self.step("match", "m1", details) if c[0] == "remember"]
        self.assertNotIn("e0", learned.values())
        self.assertNotIn("e1", learned.values())


class Labels(unittest.TestCase):
    def profile(self, console):
        return {
            "current": {"rank": "Gold II", "points": None},
            "console": console,
            "seasons": (),
            "peak": None,
        }

    def view_of(self, console, found=True):
        state = core.State(me=ME, my_name="SilverFalcon", salt="b" * 32)
        core.reduce(state, ("log", {"phase": "draft", "at_ts": 1.0}), 1.0)
        core.reduce(
            state,
            (
                "screen",
                "draft",
                1.0,
                {("ally", 0): ["PSN-Name", "PSN-Name"], ("anchor", 0): ["ALLY", "ALLY"]},
            ),
            2.0,
        )
        core.reduce(
            state,
            ("found", state.epoch, {"a0": {"player_id": "p", "name": None} if found else None}),
            3.0,
        )
        if found:
            core.reduce(state, ("profiles", state.epoch, {"p": self.profile(console)}), 4.0)
        return core.view(state).allies[0]

    def test_a_console_player_is_labelled_and_the_label_stays(self):
        first, again = self.view_of(True), self.view_of(True)
        self.assertEqual(first.name, core.CONSOLE_PLAYER)  # what they are, not a name
        self.assertRegex(first.tag, r"^[A-Z]+ [A-Z]+$")  # the overlay's own tag
        self.assertNotIn("PSN", first.tag)
        self.assertEqual(first.tag, again.tag)
        self.assertEqual(first.read_as, "PSN-Name")  # the TAB board is still read by it

    def test_a_player_pred_gg_does_not_know_gets_a_label_only(self):
        unknown = self.view_of(False, found=False)
        # Read with no doubt and not found: Pred.gg hides console names.
        self.assertEqual((unknown.name, unknown.state), (core.LIKELY_CONSOLE, "error"))
        self.assertEqual(unknown.why, "console")
        self.assertRegex(unknown.tag, r"^[A-Z]+ [A-Z]+$")
        from predoverlay.overlay import rows

        self.assertTrue(rows.row(unknown)["unknown"])  # the row says when data comes

    def no_data(self, texts, answer):
        state = core.State(me=ME, my_name="SilverFalcon", salt="b" * 32)
        core.reduce(state, ("log", {"phase": "draft", "at_ts": 1.0}), 1.0)
        core.reduce(
            state,
            ("screen", "draft", 1.0, {("ally", 0): texts, ("anchor", 0): ["ALLY", "ALLY"]}),
            2.0,
        )
        answer(state)
        return core.view(state).allies[0]

    def test_an_unsure_reading_not_found_may_be_a_misread(self):
        # The model unsure (version 0 next to Windows OCR's): no sign of a console player.
        view = self.no_data(
            ["PSN-Narne", "PSN-Name", "PSN-Name", ""],
            lambda s: core.reduce(s, ("found", s.epoch, {"a0": None}), 3.0),
        )
        self.assertEqual((view.name, view.why), (core.UNKNOWN_PLAYER, "misread"))
        from predoverlay.overlay import rows

        self.assertEqual(rows.row(view)["unknown"], "misread")

    def test_a_found_player_whose_profile_never_came_keeps_the_pred_gg_name(self):
        def answer(state):
            core.reduce(
                state, ("found", state.epoch, {"a0": {"player_id": "p", "name": "PcName"}}), 3.0
            )
            state.missing.add("p")  # the profile request came back without them

        view = self.no_data(["PcName", "PcName"], answer)
        # Pred.gg answered, with no profile for them: not "did not answer"
        self.assertEqual((view.name, view.why, view.tag), ("PcName", "noprofile", None))

    def test_labels_differ_between_names_and_between_pcs(self):
        key = lookup.name_key("PSN-Name")
        self.assertNotEqual(aliases.label("a" * 32, key), aliases.label("c" * 32, key))
        self.assertNotEqual(aliases.digest("a" * 32, key), aliases.digest("a" * 32, "OTHER"))


class StoreTests(unittest.TestCase):
    def test_round_trip_keeps_no_names_and_a_damaged_file_starts_afresh(self):
        with tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as folder:
            store = aliases.Store(Path(folder) / "known-players.json")
            salt, players, pending, fresh = store.load()
            self.assertEqual((players, pending, fresh), ({}, [], "fresh"))
            self.assertEqual(len(salt), 32)
            entry = {aliases.digest(salt, "SONYNAME"): "pid-1"}
            waiting = [
                {"match_id": "m", "rows": [["ally", "CARRY", "a" * 24]], "due": 1.0, "tries": 0}
            ]
            store.save(salt, entry, waiting)
            self.assertEqual(store.load(), (salt, entry, waiting, "ok"))
            self.assertNotIn("SONYNAME", store.path.read_text())
            store.path.write_text("{broken")
            fresh_salt, fresh_players, _, again = store.load()
            self.assertEqual((fresh_players, again), ({}, "fresh"))
            self.assertNotEqual(fresh_salt, salt)


class CheckedLearning(Learning):
    def test_a_team_whose_pc_names_do_not_fit_their_columns_teaches_nothing(self):
        self.play()
        details = finished_match()
        # Pred.gg shows one enemy's name, and it is not the name read in that role's column.
        details["players"][0]["name"] = "SomeoneElse"
        self.step("tick", now=self.now + core.LEARN_AFTER + 1)
        ((_, learned),) = [c for c in self.step("match", "m1", details) if c[0] == "remember"]
        self.assertFalse({"e0", "e1", "e2", "e3", "e4"} & set(learned.values()))
        self.assertIn("a0", learned.values())  # my team checks out

    def test_a_pending_match_survives_a_restart_of_the_app(self):
        # As the core writes it, through the file and back: the rows gained a fifth item
        # (where the role was read) and the store's check still counted four.
        self.play()
        pending = [c for c in self.sent if c[0] == "pending"][-1][1]
        self.assertEqual({len(row) for row in pending[0]["rows"]}, {5})
        with tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as folder:
            store = aliases.Store(Path(folder) / "known-players.json")
            store.save("a" * 32, {}, pending)
            _, _, kept, state = store.load()
        self.assertEqual(state, "ok")
        self.assertEqual([x["match_id"] for x in kept], ["m1"])
        self.assertEqual(kept[0]["rows"], [list(row) for row in pending[0]["rows"]])

    def test_a_pending_row_of_another_shape_is_not_taken(self):
        row = ["enemy", "MIDLANE", "d" * 24, "e" * 24]
        entry = {"match_id": "m1", "due": 1.0, "tries": 0, "rows": [row]}
        for bad in (row + ["a screen"], row + ["board", "more"], row[:2], row[:3] + ["short"]):
            self.assertFalse(aliases._pending(dict(entry, rows=[bad])), bad)
        for good in (row, row[:3], row + ["board"], row + ["loading"], row + [None]):
            self.assertTrue(aliases._pending(dict(entry, rows=[good])), good)

    def test_pending_matches_are_handed_to_the_store_and_come_back(self):
        self.play()
        pending = [c for c in self.sent if c[0] == "pending"]
        self.assertEqual(pending[-1][1][0]["match_id"], "m1")
        self.assertNotIn("SonyName1", str(pending))  # digests only
        fresh = core.State()
        core.reduce(fresh, ("aliases", "a" * 32, {}, pending[-1][1]), T0)
        commands = core.reduce(fresh, ("tick",), self.now + core.LEARN_AFTER + 1)
        self.assertIn(("match", "m1"), commands)


class StoreFailureTests(unittest.TestCase):
    def test_a_file_that_cannot_be_read_is_not_taken_for_a_fresh_one(self):
        from unittest import mock

        with tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as folder:
            store = aliases.Store(Path(folder) / "known-players.json")
            store.path.write_text("{}")
            with mock.patch.object(Path, "read_text", side_effect=PermissionError("locked")):
                self.assertEqual(store.load()[3], "unreadable")

    def test_a_save_that_fails_is_a_note_not_an_outage(self):
        from unittest import mock

        from predoverlay.overlay import lanes

        store = mock.Mock()
        store.save.side_effect = PermissionError("locked")
        io = lanes.PredggIO(None, client=mock.Mock(), public=mock.Mock(), store=store, salt="s")
        events = io.handle(("pending", []))
        self.assertEqual([e[0] for e in events], ["note"])


class ForgetTests(Learning):
    def test_a_learned_name_whose_pc_profile_disagrees_is_forgotten(self):
        self.play()
        self.step("tick", now=self.now + core.LEARN_AFTER + 1)
        self.step("match", "m1", finished_match())
        self.sent.clear()
        self.now += 600
        self.step("log", {"phase": "accepted", "at_ts": self.now})
        self.step(
            "log",
            {
                "phase": "loading",
                "at_ts": self.now,
                "map": "x",
                "user_id": ME,
                "user_name": "SilverFalcon",
                "experience": "BP_Experience_Game_Ranked",
            },
        )
        self.step("screen", "loading", self.state.phase_at, texts(TOP, BOTTOM))
        self.assertEqual(self.state.ids["e1"]["pid"], "e1")
        profile = {
            "current": None,
            "console": False,
            "name": "NotTheSameName",
            "seasons": (),
            "peak": None,
        }
        self.step("profiles", self.state.epoch, {"e1": profile})
        forgotten = [c for c in self.sent if c[0] == "remember" and None in c[1].values()]
        self.assertTrue(forgotten)
        self.assertNotIn("e1", self.state.aliases.values())
        self.assertIsNone(self.state.ids["e1"]["pid"])


class TextTests(unittest.TestCase):
    def test_a_file_that_is_not_utf8_starts_afresh(self):
        with tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as folder:
            store = aliases.Store(Path(folder) / "known-players.json")
            store.path.write_bytes(b"\xff\xfe{\x00")
            self.assertEqual(store.load()[3], "fresh")

    def test_a_match_listed_without_my_row_is_asked_for_again(self):
        state = core.State(me=ME)
        core.reduce(
            state,
            (
                "aliases",
                "a" * 32,
                {},
                [
                    {
                        "match_id": "m1",
                        "rows": [["enemy", "CARRY", "0" * 24]],
                        "due": 0.0,
                        "tries": 0,
                    }
                ],
            ),
            0.0,
        )
        core.reduce(state, ("tick",), 1.0)
        core.reduce(state, ("match", "m1", {"match_id": "m1", "players": []}), 2.0)
        self.assertEqual([x["tries"] for x in state.learns], [1])


class OddFileTests(unittest.TestCase):
    def test_deep_nesting_and_odd_pending_entries_do_not_break_the_start(self):
        with tempfile.TemporaryDirectory(dir=os.path.realpath(tempfile.gettempdir())) as folder:
            store = aliases.Store(Path(folder) / "known-players.json")
            store.path.write_text("[" * 100000 + "]" * 100000)
            self.assertEqual(store.load()[3], "fresh")
            entry = {"match_id": "m", "rows": [], "due": float("nan"), "tries": 0}
            store.save("s" * 32, {}, [entry, dict(entry, due=1.0, tries=-5)])
            self.assertEqual(store.load()[2], [])


class AcceptTests(Learning):
    def test_a_new_match_asks_for_the_last_one_at_once_and_the_delay_is_logged(self):
        self.play()
        self.step("log", {"phase": "accepted", "at_ts": self.now + 20}, now=self.now + 20)
        asked = [c for c in self.step("tick", now=self.now + 21) if c[0] == "match"]
        self.assertEqual(asked, [("match", "m1")])  # before the minute is up
        self.step("match", "m1", finished_match(), now=self.now + 22)
        self.assertTrue(any("listed 22 s after it ended" in n for n in self.state.notes))


if __name__ == "__main__":
    unittest.main()


class SpellingTests(Learning):
    """A console player learned with their spelling is recognised only by it."""

    def learn_then_read(self, top):
        self.play()
        self.step("tick", now=self.now + core.LEARN_AFTER + 1)
        self.step("match", "m1", finished_match())
        self.now += 600
        self.step("log", {"phase": "accepted", "at_ts": self.now})
        self.step(
            "log",
            {
                "phase": "loading",
                "at_ts": self.now,
                "map": "x",
                "user_id": ME,
                "user_name": "SilverFalcon",
                "experience": "BP_Experience_Game_Ranked",
            },
        )
        self.step("screen", "loading", self.state.phase_at, texts(top, BOTTOM))
        return self.state.ids["e1"]["pid"]

    def test_the_same_spelling_finds_the_player(self):
        self.assertEqual(self.learn_then_read(TOP), "e1")

    def test_a_name_that_only_folds_alike_does_not(self):
        top = list(TOP)
        top[1] = "S0nyName1"  # the same key, a zero for the O
        self.assertEqual(lookup.name_key(top[1]), lookup.name_key(TOP[1]))
        self.assertIsNone(self.learn_then_read(top))

    def test_what_reads_alike_on_screen_is_one_spelling(self):
        same = [("tlikvarn", "tIikvarn"), ("Name_1", "Name-1"), ("NAME 1", "Name1"), ("Кот", "KOT")]
        for a, b in same:
            self.assertEqual(lookup.strict_key(a), lookup.strict_key(b), (a, b))
        apart = [("Ghost", "Gh0st"), ("Ghost", "ghost_"), ("Ghost", "Gho�st")]
        for a, b in apart:
            self.assertNotEqual(lookup.strict_key(a), lookup.strict_key(b), (a, b))

    def test_a_name_learned_before_spellings_were_kept_is_found_by_its_key(self):
        self.state.aliases = {aliases.digest(self.state.salt, lookup.name_key("SonyName1")): "e1"}
        from predoverlay.overlay import identity

        ident = identity.new()
        identity.name(self.state, dict(core.seating.new("enemy", 0), reading="S0nyName1"), ident)
        self.assertEqual(ident["pid"], "e1")
