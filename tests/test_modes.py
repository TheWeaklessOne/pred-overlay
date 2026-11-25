"""Game modes that come and go: mode names, team sizes."""

import unittest

from predoverlay import modes
from predoverlay.overlay import core, phases, scoreboard
from predoverlay.overlay.tracker import log_event

ME = "00000000-0000-0000-0000-0000000000aa"


class ModeNameTests(unittest.TestCase):
    def test_labels_for_known_and_unknown_modes(self):
        self.assertEqual(modes.label("TEAM_VS_TEAM"), "STANDARD")
        self.assertEqual(modes.label("TEAM_VS_AI"), "VS AI")
        self.assertEqual(modes.label("SOME_NEW_MODE"), "SOME NEW MODE")

    def test_mode_from_the_map_url(self):
        self.assertEqual(modes.experience_label("BP_Experience_Game_Overclocked"), "OVERCLOCKED")
        self.assertEqual(modes.experience_label("BP_Experience_Game_Ranked"), "RANKED")
        self.assertEqual(modes.experience_label("BP_Experience_Brawl_Night"), "BRAWL NIGHT")
        self.assertEqual(modes.experience_label("BP_Experience_Game_Normal"), "STANDARD")
        self.assertIsNone(modes.experience_label("BP_Experience_Game_3v3"))  # only the size
        self.assertIsNone(modes.experience_label(None))
        self.assertTrue(modes.is_pve(modes.experience_label("BP_Experience_Game_Practice")))
        self.assertFalse(modes.is_pve("OVERCLOCKED"))

    def test_roster_sizes(self):
        self.assertEqual(
            [modes.roster_mode(n) for n in (2, 4, 6, 8, 10)], ["1v1", "2v2", "3v3", "4v4", "5v5"]
        )
        self.assertEqual([modes.roster_mode(n) for n in (0, 1, 7, 12)], [None] * 4)


class MatchModeTests(unittest.TestCase):
    def feed(self, state, line):
        event = phases.parse(line)
        kind, payload = log_event(event)
        core.reduce(state, (kind, payload), payload["at_ts"])

    def test_the_mode_name_comes_from_the_loading_line_and_clears_in_the_menu(self):
        state = core.State(me=ME)
        stamp = "[2026.11.04-00.30.08:749][440]"
        self.feed(state, stamp + "LogPredTask: Task Started: Pragma_MatchmakingAccept")
        self.feed(
            state,
            stamp + "LogNet: Browse: 1.2.3.4:1/Game/Maps/TwilightRC1?userId=" + ME + "?userName=X"
            "?game=DefaultGameMode?Experience=BP_Experience_Game_Overclocked?NumberOfPlayers=10",
        )
        self.assertEqual((state.phase, core.view(state).mode_name), ("loading", "OVERCLOCKED"))
        self.feed(state, stamp + "LogNet: Browse: /Game/Maps/OTA?Name=Player")
        self.assertIsNone(core.view(state).mode_name)


PRACTICE = (
    "[2026.11.04-00.30.08:749][440]LogNet: Browse: 1.2.3.4:1/Game/Maps/TwilightRC1?userId="
    + ME
    + "?userName=Me?game=DefaultGameMode?Experience=BP_Experience_Game_Practice"
)


class RosterTests(unittest.TestCase):
    FRIENDS = ("Me", "Bob", "Cat")

    def drafted(self, names=FRIENDS):
        state = core.State(me=ME, my_name="Me", display={"x": "y"})
        core.reduce(state, ("log", {"phase": "draft", "at_ts": 1.0}), 1.0)
        self.read(state, "draft", {("ally", i): name for i, name in enumerate(names)})
        return state

    def read(self, state, kind, names, now=2.0):
        texts = {slot: [name, name] for slot, name in names.items()}
        texts = {**texts, ("anchor", 0): ["ALLY", "ALLY"]}
        core.reduce(state, ("screen", kind, state.phase_at, texts), now)

    def bots(self, state):
        kind, payload = log_event(phases.parse(PRACTICE))
        core.reduce(state, (kind, payload), payload["at_ts"])
        return payload["at_ts"]

    def test_the_board_keeps_five_rows_while_names_are_unread(self):
        state = self.drafted(self.FRIENDS[:2])
        self.assertEqual(core.view(state).mode, "5v5")  # the screens read are 5v5 ones
        self.read(state, "draft", {("ally", i): f"Player{i}" for i in range(2, 5)}, now=5.0)
        self.assertEqual(core.view(state).mode, "5v5")

    def test_a_match_against_bots_keeps_my_team_and_says_so(self):
        state = self.drafted()
        self.bots(state)
        view = core.view(state)
        self.assertEqual(view.status, "mode not supported")
        self.assertEqual([p.read_as for p in view.allies], list(self.FRIENDS))
        self.assertEqual(view.enemies, ())

    def test_the_bots_on_the_loading_screen_are_not_read(self):
        state = self.drafted()
        at = self.bots(state)
        rows = {("top", i): name for i, name in enumerate(self.FRIENDS)}
        rows.update({("bottom", i): f"Bot{i}" for i in range(3)})
        self.read(state, "loading", rows, now=at + 3.0)
        self.assertEqual(core.view(state).enemies, ())


class SmallBoardTests(unittest.TestCase):
    def test_a_two_player_team_has_two_rows(self):
        x = scoreboard.PANELS["ally"][0] + 50
        third = scoreboard.ROW_TOPS[2] + 80
        self.assertEqual(scoreboard.row_at(x, scoreboard.ROW_TOPS[1] + 80, 2), ("ally", 1))
        self.assertIsNone(scoreboard.row_at(x, third, 2))
        self.assertFalse(scoreboard.on_board(x, third, 2))
        self.assertTrue(scoreboard.on_board(x, third))


if __name__ == "__main__":
    unittest.main()
