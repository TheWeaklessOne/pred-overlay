"""Game modes that come and go: queue folding, the menu strip, mode names, team sizes."""

import unittest

from predoverlay import game, modes
from predoverlay.overlay import core, phases, scoreboard
from predoverlay.overlay.tracker import log_event


def row(queue, players, region="EUROPE", platform="CROSS_PLAY", custom="", parties=None):
    return {
        "matchmakingQueueKey": {
            "extMatchmakingKey": {
                "queueName": queue,
                "regionName": region,
                "subGameMode": "FIVE_VS_FIVE",
                "omedaCustomMatchmakingKey": custom,
                "platform": platform,
            }
        },
        "playersInQueue": float(players),
        "partiesInQueue": float(parties if parties is not None else players),
    }


# The shape of the live 1.18 payload (counts made up).
PAYLOAD = {
    "queueInfo": [
        row("RANKED", 31),
        row("TEAM_VS_TEAM", 27),
        row("TEAM_VS_TEAM", 2, platform="PSN"),
        row("OVERCLOCKED", 5),
        row("ARAM", 13),
        row("TEAM_VS_AI", 0),
        row("CUSTOM", 4, custom="2121\x1fCRUCIBLE"),  # a private lobby
        row("CUSTOM", 7, custom="marie\x1fRANKED"),  # never adds to the public queue
        row("CUSTOM", 9, custom=""),  # a private lobby
        row("CUSTOM", 2, custom="4242"),  # a lobby code, no mode
        row("SOME_NEW_MODE", 3),
        row("TEAM_VS_TEAM", 6, region="MENA", platform="PSN"),  # no cross-play row there
    ]
}


class QueueFoldingTests(unittest.TestCase):
    def setUp(self):
        self.counts = game.queue_counts(PAYLOAD)

    def test_modes_are_keyed_by_their_real_name(self):
        eu = self.counts["EUROPE"]
        self.assertNotIn("CRUCIBLE", eu)
        self.assertEqual(eu["OVERCLOCKED"], (5, 5))
        self.assertEqual(eu["SOME_NEW_MODE"], (3, 3))
        self.assertNotIn("CUSTOM", eu)
        self.assertNotIn("", eu)
        self.assertNotIn("4242", eu)

    def test_only_cross_play_counts_unless_there_is_nothing_else(self):
        self.assertEqual(self.counts["EUROPE"]["TEAM_VS_TEAM"], (27, 27))
        self.assertEqual(self.counts["MENA"]["TEAM_VS_TEAM"], (6, 6))

    def test_cross_play_row_after_a_console_row_replaces_it(self):
        payload = {"queueInfo": [row("ARAM", 2, platform="PSN"), row("ARAM", 8)]}
        self.assertEqual(game.queue_counts(payload)["EUROPE"]["ARAM"], (8, 8))


class ModeNameTests(unittest.TestCase):
    def test_labels_for_known_and_unknown_modes(self):
        self.assertEqual(modes.label("TEAM_VS_TEAM"), "STANDARD")
        self.assertEqual(modes.label("TEAM_VS_AI"), "VS AI")
        self.assertEqual(modes.label("SOME_NEW_MODE"), "SOME NEW MODE")
        self.assertLess(modes.position("OVERCLOCKED"), modes.position("SOME_NEW_MODE"))
        self.assertLess(modes.position("SOME_NEW_MODE"), modes.position("TEAM_VS_AI"))

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


class StripTests(unittest.TestCase):
    def strip(self):
        from predoverlay.overlay import motion

        return motion.Strip(lambda text, kind: 10.0 * len(text))

    def test_an_unknown_mode_opens_a_slot_with_a_readable_label(self):
        strip = self.strip()
        strip.sync({"RANKED": (5, 5), "SOME_NEW_MODE": (3, 3)}, 1.0, "EUROPE", 1.0)
        labels = [s.label for s in strip.slots.values()]
        self.assertEqual(labels, ["RANKED", "SOME NEW MODE"])

    def test_known_modes_keep_their_place_and_extra_modes_are_left_out(self):
        strip = self.strip()
        queues = {
            "TEAM_VS_AI": (1, 1),
            "ZETA": (50, 50),
            "ALPHA": (40, 40),
            "ARAM": (2, 2),
            "OVERCLOCKED": (3, 3),
            "TEAM_VS_TEAM": (9, 9),
            "RANKED": (8, 8),
        }
        strip.sync(queues, 1.0, "EUROPE", 1.0)
        shown = [k for k, s in strip.slots.items() if not s.leaving]
        self.assertEqual(len(shown), modes.MAX_SLOTS)
        self.assertEqual(shown[:4], ["RANKED", "TEAM_VS_TEAM", "OVERCLOCKED", "ARAM"])
        self.assertNotIn("TEAM_VS_AI", shown)  # least important goes first

    def test_a_busy_new_mode_beats_an_empty_known_one(self):
        strip = self.strip()
        queues = {
            "RANKED": (8, 8),
            "TEAM_VS_TEAM": (9, 9),
            "OVERCLOCKED": (3, 3),
            "ARAM": (0, 0),
            "CRUCIBLE": (2, 2),
            "FESTIVAL": (120, 80),
        }
        strip.sync(queues, 1.0, "EUROPE", 1.0)
        shown = [k for k, s in strip.slots.items() if not s.leaving]
        self.assertIn("FESTIVAL", shown)
        self.assertNotIn("ARAM", shown)

    def test_long_labels_are_cut_to_the_longest_known_one(self):
        strip = self.strip()
        strip.sync({"AN_EXTREMELY_LONG_FEATURED_MODE": (1, 1)}, 1.0, "EUROPE", 1.0)
        (item,) = strip.frame(10.0).items
        self.assertEqual(item.label_w, 10.0 * len(modes.LONGEST_LABEL))

    def test_menu_width_fits_the_fullest_strip(self):
        from PySide6.QtWidgets import QApplication

        from predoverlay.overlay import paint

        _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        width = paint.menu_width("EUROPE")
        strip = paint.new_strip()
        queues = {k: (8888, 0) for k in ("RANKED", "TEAM_VS_TEAM", "OVERCLOCKED", "ARAM", "X_Y")}
        strip.sync(queues, 1.0, "EUROPE", 1.0)
        self.assertLessEqual(strip.frame(10.0).width + paint.QUEUE_GAP + paint.BADGE_W, width)


class MatchModeTests(unittest.TestCase):
    ME = "00000000-0000-0000-0000-0000000000aa"

    def feed(self, state, line):
        event = phases.parse(line)
        kind, payload = log_event(event)
        core.reduce(state, (kind, payload), payload["at_ts"])

    def test_the_mode_name_comes_from_the_loading_line_and_clears_in_the_menu(self):
        state = core.State(me=self.ME, token_account=self.ME)
        stamp = "[2026.11.04-00.30.08:749][440]"
        self.feed(state, stamp + "LogPredTask: Task Started: Pragma_MatchmakingAccept")
        self.feed(
            state,
            stamp
            + "LogNet: Browse: 1.2.3.4:1/Game/Maps/TwilightRC1?userId="
            + self.ME
            + "?userName=X"
            "?game=DefaultGameMode?Experience=BP_Experience_Game_Overclocked?NumberOfPlayers=10",
        )
        self.assertEqual((state.phase, core.view(state).mode_name), ("loading", "OVERCLOCKED"))
        self.feed(state, stamp + "LogNet: Browse: /Game/Maps/OTA?Name=Player")
        self.assertIsNone(core.view(state).mode_name)


class RosterTests(unittest.TestCase):
    ME = "00000000-0000-0000-0000-0000000000aa"
    OTHERS = [f"10000000-0000-0000-0000-{i:012d}" for i in range(1, 10)]

    def instance(self, state, ids, now):
        core.reduce(state, ("instance", {"in_game": True, "match_id": "m", "player_ids": ids}), now)

    def test_a_partial_list_that_grows_is_read_again(self):
        state = core.State(me=self.ME, token_account=self.ME, phase="draft")
        part = [self.ME] + self.OTHERS[:3]
        self.instance(state, part, 1.0)
        self.instance(state, part, 2.0)
        self.assertEqual(len(state.roster), 4)  # looked like 2v2
        full = [self.ME] + self.OTHERS
        self.instance(state, full, 12.0)
        self.instance(state, full, 13.0)
        self.assertEqual(len(state.roster), 10)
        self.assertEqual(core.view(state).mode, "5v5")

    def test_eight_or_two_players_are_not_shown(self):
        for ids in ([self.ME] + self.OTHERS[:7], [self.ME, self.OTHERS[0]]):
            state = core.State(me=self.ME, token_account=self.ME, phase="draft")
            self.instance(state, ids, 1.0)
            self.instance(state, ids, 2.0)
            self.assertEqual(state.roster, ())
            self.assertEqual(core.view(state).status, "mode not supported")

    def test_a_match_against_bots_drops_its_human_only_roster(self):
        state = core.State(me=self.ME, token_account=self.ME, phase="draft")
        friends = [self.ME] + self.OTHERS[:3]
        self.instance(state, friends, 1.0)
        self.instance(state, friends, 2.0)
        line = (
            "[2026.11.04-00.30.08:749][440]LogNet: Browse: 1.2.3.4:1/Game/Maps/TwilightRC1?userId="
            + self.ME
            + "?userName=X?game=DefaultGameMode?Experience=BP_Experience_Game_Practice"
        )
        kind, payload = log_event(phases.parse(line))
        core.reduce(state, (kind, payload), payload["at_ts"])
        self.assertEqual(state.roster, ())
        self.instance(state, friends, 3.0)
        self.instance(state, friends, 4.0)
        self.assertEqual(state.roster, ())  # not taken again


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
