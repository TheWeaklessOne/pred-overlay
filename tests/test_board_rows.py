"""The TAB board as a source of the roster: both teams, by role, read all match."""

import os
import unittest

from test_core import ME, Flow, loading_texts

from predoverlay.overlay import core, scoreboard
from predoverlay.overlay.rows import ROLES

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ENEMIES = ["GLAVERODSEN", "DRMOVIK3810", "HANVELL", "LIKEMIRV3LL_999", "ZOMBERVIRTE1805"]
ALLIES = ["SILVERFALCON", "K_REV-M", "KARN", "LAMU_11", "KAROV-BANDEGOSO"]


def board(team_names, team="enemy", sure=True):
    """A board reading of one team: role order, the hero line, two equal versions when
    the model was sure."""
    return {
        (team, i): {
            "text": name,
            "texts": [name, name] if sure else ["", name],
            "hero": "GRUX",
            "role": ROLES[i],
        }
        for i, name in enumerate(team_names)
    }


class BoardSourceTests(Flow):
    def in_match(self):
        """A draft read, then the match with the loading screen never read."""
        self.enter_draft()
        self.read_draft(ALLIES)
        self.answer_search({k: None for k in ("a1", "a2", "a3", "a4")})
        self.step("log", {"phase": "loading", "at_ts": self.now, "map": "TwilightRC1"})
        self.sent.clear()

    def seats(self, team):
        return [s for s in self.state.seats.values() if s["team"] == team and not s["gone"]]

    def test_the_board_gives_seats_to_players_the_roster_lacks(self):
        self.in_match()
        self.step("board_read", f"{self.state.epoch}", "tab", board(ENEMIES))
        enemies = self.seats("enemy")
        self.assertEqual(sorted(s["reading"] for s in enemies), sorted(ENEMIES))
        self.assertEqual({s["role"] for s in enemies}, set(ROLES))
        self.assertEqual({s["role_from"] for s in enemies}, {"board"})
        (search,) = self.of("search")
        self.assertEqual(len(search[2]), 5)
        view = core.view(self.state)
        self.assertEqual(len(view.enemies), 5)
        self.assertEqual({p.hero for p in view.enemies}, {"GRUX"})

    def test_my_team_on_the_board_joins_the_draft_seats_by_name(self):
        self.in_match()
        self.step("board_read", f"{self.state.epoch}", "tab", board(ALLIES, "ally"))
        self.assertEqual(len(self.seats("ally")), 5)  # no seat twice
        roles = {s["reading"]: s["role"] for s in self.seats("ally")}
        self.assertEqual(roles["KARN"], ROLES[2])

    def test_a_misread_row_adds_no_one_to_a_full_team(self):
        self.in_match()
        self.step("board_read", f"{self.state.epoch}", "tab", board(ENEMIES))
        misread = board(["QQQQQQ"] + ENEMIES[1:])
        self.step("board_read", f"{self.state.epoch}", "tab", misread)
        self.assertEqual(len(self.seats("enemy")), 5)

    def test_a_row_with_no_name_to_match_takes_the_seat_of_its_role(self):
        self.enter_draft()
        self.read_draft(ALLIES)
        self.step("log", {"phase": "loading", "at_ts": self.now, "map": "TwilightRC1"})
        top = ["Glaverodsen", "DrMovik3810", "Hanvell", "LikeMirv3ll_999", "Zombervirte1805"]
        self.step("screen", "loading", self.state.phase_at, loading_texts(top, ALLIES))
        before = {k: s["reading"] for k, s in self.state.seats.items() if s["team"] == "enemy"}
        # On the board the midlane row reads as something else entirely (a console tag).
        rows = board(ENEMIES)
        rows[("enemy", 2)].update(text="Q", texts=["", "Q"], role=core._role(2))
        self.step("board_read", f"{self.state.epoch}", "tab", rows)
        after = {k: s["reading"] for k, s in self.state.seats.items() if s["team"] == "enemy"}
        self.assertEqual(before, after)  # every row found its seat: by name, or by role
        self.assertEqual(len(self.seats("enemy")), 5)

    def test_another_match_rows_are_left_out(self):
        self.in_match()
        self.step("board_read", "not-this-match", "tab", board(ENEMIES))
        self.assertFalse(self.seats("enemy"))

    def test_a_match_known_only_from_the_board_still_teaches(self):
        self.in_match()
        self.step("board_read", f"{self.state.epoch}", "tab", board(ENEMIES))
        learn = core._learning(self.state, "m1", self.now)
        sources = {row[4] for row in learn["rows"] if row[0] == "enemy"}
        self.assertEqual(sources, {"board"})
        self.assertEqual(len([r for r in learn["rows"] if r[0] == "enemy"]), 5)


class WholeTeamTests(Flow):
    """The board lists a team row by row: with four rows placed, the fifth is the fifth
    player, whatever the loading screen read there."""

    TOP = ["Xq", "Brambleton", "Corvalis7", "Dunmore_x", "Elderwick"]
    MINE = ["SilverFalcon", "Farrowby", "Garnet_44", "Hollis-B", "Ironwood9"]

    def in_match(self):
        self.enter_draft()
        self.read_draft([name.upper() for name in self.MINE])
        self.answer_search({})  # nobody of my team is on Pred.gg
        self.step("log", {"phase": "loading", "at_ts": self.now, "map": "TwilightRC1"})
        self.step("screen", "loading", self.state.phase_at, loading_texts(self.TOP, self.MINE))
        self.answer_search({})  # nor of theirs: every name was asked for once
        self.sent.clear()

    def enemies(self):
        return [s for s in self.state.seats.values() if s["team"] == "enemy" and not s["gone"]]

    def on_board(self, **other):
        """The enemy team as the board lists it (the seats' own roles: the loading
        screen's columns may be mirrored), with some rows read as another name."""
        by_role = {s["role"]: s["reading"] for s in self.enemies()}
        return board([other.get(by_role[role], by_role[role]).upper() for role in ROLES])

    def test_the_row_left_joins_the_seat_left_and_its_spelling_is_searched(self):
        self.in_match()
        sid, seat = next((k, s) for k, s in self.state.seats.items() if s["reading"] == "Xq")
        self.step("board_read", f"{self.state.epoch}", "tab", self.on_board(Xq="ABELARDO"))
        self.assertEqual(len(self.enemies()), 5)  # no sixth seat, no row thrown away
        self.assertIn("ABELARDO", [text for _, text in seat["evidence"]["board"]])
        self.assertEqual(seat["role_from"], "board")
        asked = [text for c in self.of("search") for text in c[2].get(sid, ())]
        self.assertIn("ABELARDO", asked)

    def test_two_rows_left_are_not_guessed(self):
        self.in_match()
        rows = self.on_board(Xq="ABELARDO", Brambleton="BERENGAR")
        self.step("board_read", f"{self.state.epoch}", "tab", rows)
        self.assertEqual(sum(1 for s in self.enemies() if s["evidence"].get("board")), 3)

    def test_a_row_that_was_not_read_leaves_the_others_alone(self):
        self.in_match()
        seat = next(s for s in self.enemies() if s["reading"] == "Xq")
        rows = self.on_board(Xq="ABELARDO")
        other = next(k for k, row in rows.items() if row["text"] == "ELDERWICK")
        rows[other].update(text="", texts=["", ""])  # four rows read: not the whole team
        self.step("board_read", f"{self.state.epoch}", "tab", rows)
        self.assertNotIn("board", seat["evidence"])

    def test_six_seats_are_still_a_five_a_side_board(self):
        self.enter_draft()
        self.read_draft([name.upper() for name in self.MINE] + ["EXTRAROW"])
        self.assertEqual(core.view(self.state).mode, "5v5")


class ReviewTests(Flow):
    """The findings of the review of the seats rebuild (2026-11-21)."""

    TOP = ["Yolanda", "Wendell", "Victor", "Ursula", "Xavier"]

    def loading(self, draft_names, columns, top=None):
        self.enter_draft()
        self.read_draft(draft_names)
        self.step("log", {"phase": "loading", "at_ts": self.now, "map": "TwilightRC1"})
        texts = loading_texts(top or self.TOP, columns)
        self.step(
            "screen", "loading", self.state.phase_at, {k: v for k, v in texts.items() if v[0]}
        )

    def allies(self):
        return sorted(p.read_as for p in core.view(self.state).allies)

    def test_a_draft_seat_unlike_its_loading_column_gives_way_to_it(self):
        columns = ["SilverFalcon", "Bobcat", "Charlie", "Dingo", "Zebulon"]
        self.loading(["SILVERFALCON", "BOBCAT", "CHARLIE", "DINGO", "QWRTZ"], columns)
        expected = sorted(["SILVERFALCON", "BOBCAT", "CHARLIE", "DINGO", "Zebulon"])
        self.assertEqual(self.allies(), expected)

    def test_the_end_board_adds_no_one_and_sets_no_role(self):
        self.loading(ALLIES, ALLIES)
        roles = {k: s["role"] for k, s in self.state.seats.items()}
        junk = board(["FRAGMENT", "VICTORY", "DEFEAT", "KWZQPL", "RRTTUU"])
        self.step("board_read", f"{self.state.epoch}", "end", junk)
        self.assertEqual({k: s["role"] for k, s in self.state.seats.items()}, roles)

    def test_a_board_row_takes_only_a_role_the_board_gave(self):
        self.loading(ALLIES, ALLIES)
        rows = board(["XAVIRE9", "", "", "", "Y0LNDQQ"])  # unmatched by name
        rows = {k: v for k, v in rows.items() if v["text"]}
        self.step("board_read", f"{self.state.epoch}", "tab", rows)
        froms = {s["role_from"] for s in self.state.seats.values() if s["team"] == "enemy"}
        self.assertEqual(froms, {"loading"})  # a loading role is never stamped exact

    def test_an_unsure_board_row_still_names_a_player_the_loading_screen_missed(self):
        self.loading(ALLIES, ALLIES, top=self.TOP[:4] + [""])
        rows = board(self.TOP[:4] + ["MARLIN"])
        rows[("enemy", 4)].update(texts=["MARL1NQ", "MARLIN"])  # model and OCR differ
        self.step("board_read", f"{self.state.epoch}", "tab", rows)
        enemies = sorted(p.read_as for p in core.view(self.state).enemies)
        self.assertIn("MARLIN", enemies)
        self.assertEqual(len(enemies), 5)

    def test_the_board_hero_does_not_replace_the_picked_one(self):
        self.enter_draft()
        texts = {("ally", i): [n, n] for i, n in enumerate(ALLIES)}
        texts[("anchor", 0)] = ["ALLY", "ALLY"]
        texts[("hero", 2)] = ["Steel", "Steel"]
        self.step("screen", "draft", self.state.phase_at, texts)
        self.step("board_read", f"{self.state.epoch}", "tab", board(ALLIES, "ally"))
        heroes = {s["reading"]: s["hero"] for s in self.state.seats.values()}
        self.assertEqual(heroes["KARN"], "STEEL")  # the pick line's, not the board's GRUX


class ReaderRowsTests(unittest.TestCase):
    def test_every_row_is_kept_with_its_versions_and_new_players_read_again(self):
        lines = {str(i).encode(): [n, "GRUX"] for i, n in enumerate(ENEMIES)}
        lines.update({f"a{i}".encode(): [c * 6, "STEEL"] for i, c in enumerate("ABCDE")})
        clock = [100.0]
        reader = scoreboard.Reader(read=lambda png: lines[png], clock=lambda: clock[0])
        crops = {("enemy", i): str(i).encode() for i in range(5)}
        crops.update({("ally", i): f"a{i}".encode() for i in range(5)})
        names = {"ally": {f"p{c}": c * 6 for c in "ABCDE"}}
        reader.start(GAME, lambda: crops, names, size=5)
        while reader.busy:
            pass
        rows = reader.read_rows
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[("enemy", 0)]["texts"], ["", ENEMIES[0]])  # OCR's, no model
        self.assertEqual(rows[("enemy", 1)]["role"], ROLES[1])
        self.assertFalse(reader.wanted(GAME))  # every named player matched
        clock[0] += scoreboard.RETRY_AFTER
        self.assertFalse(reader.wanted(GAME, [f"p{c}" for c in "ABCDE"]))
        self.assertTrue(reader.wanted(GAME, [f"p{c}" for c in "ABCDE"] + ["e1"]))

    def test_a_name_the_model_picked_from_the_roster_is_no_sure_reading(self):
        from predoverlay.overlay.scoreboard import Box

        class Picking:  # the model, unsure alone, picks the roster's name for the line
            def read(self, lines, known=None):
                return {key: ("ABCDEF", -1.0, True) for key in lines}

        reader = scoreboard.Reader(
            read=lambda png: ["ABCD3F", "GRUX"], clock=lambda: 0.0, names=Picking()
        )
        reader.match_id = GAME
        boxes = {("ally", 0): Box("l", lambda: [b"x"])}
        reader._run(GAME, boxes, {"ally": {"p": "ABCDEF"}}, size=1)
        self.assertEqual(reader.read_rows[("ally", 0)]["texts"], ["ABCDEF", "ABCD3F"])


GAME = "10000000-0000-0000-0000-000000000009"
_ = ME

if __name__ == "__main__":
    unittest.main()
