"""The best rank of past seasons: Pred.gg's answer, when it shows, and what can go wrong."""

import unittest
from unittest import mock

from predoverlay.overlay import cards, core, lanes
from predoverlay.overlay.model import PlayerView
from predoverlay.overlay.rows import changed_groups, rank_step, row

ME = "00000000-0000-0000-0000-0000000000aa"
PEERS = [f"10000000-0000-0000-0000-{i:012d}" for i in range(1, 10)]
GOLD = {"placed": True, "division": 3, "tier": 2, "vp": 45}  # 745 points
DIAMOND = {"rank": "Diamond II", "season": "Season 1 - Split 2"}


def season(name, points, rank, end="2026-01-01T00:00:00Z", unranked=False):
    return {
        "peakPoints": points,
        "peakRank": {"name": rank} if rank else None,
        "unranked": unranked,
        "rating": {"name": name, "endTime": end},
    }


def player(profile=GOLD, peak=DIAMOND):
    return PlayerView(ME, "A#1", "ally", False, "ready", None, "off", profile, peak=peak)


class BestPeakTests(unittest.TestCase):
    def test_the_highest_finished_season_wins(self):
        raw = {
            "ratings": [
                season("Season 0", 900.0, "Platinum III"),
                season("Season 1 - Split 2", 1396.0, "Diamond II"),
                season("Season 1 - Split 3", 700.0, "Gold III"),
            ]
        }
        self.assertEqual(cards.best_peak(raw), DIAMOND)

    def test_the_rank_decides_even_where_peak_points_are_null_or_lag(self):
        raw = {
            "ratings": [
                season("Season 1 - Split 1", 1262.0, "Diamond III"),
                season("Season 1 - Split 3", 1213.0, "Diamond I"),
                season("Season 1 - Split 4", None, "Paragon"),
                season("Season 0", 551.0, "Platinum II"),
            ]
        }
        self.assertEqual(cards.best_peak(raw)["rank"], "Paragon")
        raw["ratings"].pop(2)
        self.assertEqual(cards.best_peak(raw)["rank"], "Diamond I")

    def test_paragon_tops_master_and_diamond(self):
        self.assertLess(rank_step("Diamond I"), rank_step("Master"))
        self.assertLess(rank_step("Master"), rank_step("Paragon"))
        self.assertLess(rank_step("Gold III"), rank_step("Gold II"))
        self.assertIsNone(rank_step("Something"))

    def test_running_unranked_and_incomplete_seasons_do_not_count(self):
        raw = {
            "ratings": [
                season("Season 1 - Split 6", 2000.0, "Paragon", end=None),
                season("Season 1 - Split 1", 1800.0, "Paragon", unranked=True),
                season("Season 0", 1500.0, None),
            ]
        }
        self.assertIsNone(cards.best_peak(raw))

    def test_players_pred_gg_does_not_know(self):
        self.assertIsNone(cards.best_peak(None))
        self.assertIsNone(cards.best_peak({"ratings": None}))

    def test_one_query_for_the_whole_roster(self):
        document, variables = cards.peak_query([ME, PEERS[0]])
        self.assertEqual(variables, {"p0": ME, "p1": PEERS[0]})
        self.assertIn("$p1:ID!", document)
        self.assertIn("p1:player(by:{id:$p1})", document)


class ShownPeakTests(unittest.TestCase):
    def test_above_the_current_rank(self):
        shown = row(player())["peak"]
        self.assertEqual(shown["rank"], "Diamond II")
        self.assertEqual(shown["tier"], "diamond")
        self.assertEqual(shown["season"], "Season 1 - Split 2")

    def test_an_unplaced_player_shows_it_too(self):
        self.assertIsNotNone(row(player(profile={"placed": False}))["peak"])

    def test_not_at_or_below_the_current_rank(self):
        same = dict(DIAMOND, rank="Gold II")
        self.assertIsNone(row(player(peak=same))["peak"])  # the same step, more VP
        lower = dict(DIAMOND, rank="Silver I")
        self.assertIsNone(row(player(peak=lower))["peak"])

    def test_not_next_to_a_rank_this_table_does_not_know(self):
        odd = {"placed": True, "division": 9, "tier": 1}
        self.assertEqual(row(player(profile=odd))["rank"], "RANKED")
        self.assertIsNone(row(player(profile=odd))["peak"])

    def test_nothing_before_the_current_rank_is_known(self):
        self.assertIsNone(row(player(profile=None))["peak"])

    def test_its_arrival_changes_only_its_own_field(self):
        before, after = row(player(peak=None)), row(player())
        self.assertEqual(changed_groups(before, after), {"peak"})


class PeakPipelineTests(unittest.TestCase):
    def accepted(self):
        state = core.State(me=ME, token_account=ME, phase="draft", display={"x": "y"})
        roster = [ME] + PEERS
        current = {"in_game": True, "match_id": "m", "player_ids": roster}
        core.reduce(state, ("instance", current), 1.0)
        return state, core.reduce(state, ("instance", current), 2.0)

    def test_asked_once_per_roster_and_shown_in_the_view(self):
        state, commands = self.accepted()
        self.assertIn("peaks", [c[0] for c in commands])
        core.reduce(state, ("peaks", state.epoch, {ME: DIAMOND}), 3.0)
        self.assertNotIn("peaks", [c[0] for c in core.reduce(state, ("tick",), 4.0)])
        me = next(p for p in core.view(state).allies if p.is_me)
        self.assertEqual(me.peak, DIAMOND)

    def test_failures_stop_after_the_retries(self):
        state, _ = self.accepted()
        for attempt in range(len(core.CARD_RETRY)):
            core.reduce(state, ("peaks", state.epoch, None), 3.0 + attempt)
        commands = core.reduce(state, ("tick",), 10_000.0)
        self.assertNotIn("peaks", [c[0] for c in commands])

    def test_the_lane_keeps_only_players_with_a_peak(self):
        client = mock.Mock()
        client.query.return_value = {
            "p0": {"ratings": [season("Season 0", 900.0, "Platinum III")]},
            "p1": None,
        }
        io = lanes.PredggIO.__new__(lanes.PredggIO)
        io.catalog = client
        ((kind, epoch, found),) = io.handle(("peaks", 7, (ME, PEERS[0])))
        self.assertEqual((kind, epoch, list(found)), ("peaks", 7, [ME]))

    def test_a_failing_query_retries_like_the_other_lookups(self):
        events = lanes.failure(("peaks", 7, (ME,)), "TransportError: nope")
        self.assertEqual(events[1], ("peaks", 7, None))


class PaintTests(unittest.TestCase):
    def test_the_card_grows_by_one_line_and_paints(self):
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QApplication

        from predoverlay.overlay import paint

        _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        plain, best = row(player(peak=None)), row(player())
        self.assertEqual(paint.card_size(best, "omeda")[1], paint.card_size(plain, "omeda")[1] + 24)
        image = QImage(700, 700, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        try:
            paint.paint_card(painter, best, paint.Motion())
            paint.paint_stack(painter, 640, [best], [plain], paint.Motion(), "5v5")
        finally:
            painter.end()


if __name__ == "__main__":
    unittest.main()
