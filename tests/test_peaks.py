"""The best rank of past seasons: Pred.gg's answer, when it shows, and what can go wrong."""

import dataclasses
import unittest
from unittest import mock

from predoverlay.overlay import cards, core, lanes
from predoverlay.overlay.model import Failure, PlayerView
from predoverlay.overlay.rows import changed_groups, rank_step, row

ME = "00000000-0000-0000-0000-0000000000aa"
PEERS = [f"10000000-0000-0000-0000-{i:012d}" for i in range(1, 10)]
GOLD = {"current": {"rank": "Gold II", "points": 745.0}}
UNPLACED = {"current": {"rank": None, "points": None}}
DIAMOND = {"rank": "Diamond II", "season": "Season 1 - Split 2"}


def season(name, points, rank, end="2026-01-01T00:00:00Z", unranked=False):
    return {
        "peakPoints": points,
        "peakRank": {"name": rank} if rank else None,
        "unranked": unranked,
        "rating": {"name": name, "endTime": end},
    }


def player(profile=GOLD, peak=DIAMOND):
    return PlayerView(ME, "A#1", "ally", False, "ready", profile, peak=peak)


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


class ShownPeakTests(unittest.TestCase):
    def test_above_the_current_rank(self):
        shown = row(player())["peak"]
        self.assertEqual(shown["rank"], "Diamond II")
        self.assertEqual(shown["tier"], "diamond")
        self.assertEqual(shown["season"], "Season 1 - Split 2")

    def test_an_unplaced_player_shows_it_too(self):
        self.assertIsNotNone(row(player(profile=UNPLACED))["peak"])

    def test_not_at_or_below_the_current_rank(self):
        same = dict(DIAMOND, rank="Gold II")
        self.assertIsNone(row(player(peak=same))["peak"])  # the same step, more VP
        lower = dict(DIAMOND, rank="Silver I")
        self.assertIsNone(row(player(peak=lower))["peak"])

    def test_not_next_to_a_rank_this_table_does_not_know(self):
        odd = {"current": {"rank": "Champion", "points": 2000.0}}
        self.assertEqual(row(player(profile=odd))["rank"], "Champion")
        self.assertIsNone(row(player(profile=odd))["peak"])

    def test_nothing_before_the_current_rank_is_known(self):
        self.assertIsNone(row(player(profile=None))["peak"])

    def test_its_arrival_changes_only_its_own_field(self):
        before, after = row(player(peak=None)), row(player())
        self.assertEqual(changed_groups(before, after), {"peak"})


class PeakPipelineTests(unittest.TestCase):
    def accepted(self):
        """The draft shows my own name: my row is known from the log, no search needed."""
        state = core.State(me=ME, my_name="Me", display={"x": "y"})
        core.reduce(state, ("log", {"phase": "draft", "at_ts": 1.0}), 1.0)
        texts = {("ally", 0): ["Me", "Me"]}
        texts = {**texts, ("anchor", 0): ["ALLY", "ALLY"]}
        return state, core.reduce(state, ("screen", "draft", state.phase_at, texts), 2.0)

    def test_it_comes_with_the_profile_and_reaches_the_view(self):
        state, commands = self.accepted()
        self.assertIn(("profiles", state.epoch, (ME,)), commands)
        profile = dict(GOLD, peak=DIAMOND, seasons=())
        core.reduce(state, ("profiles", state.epoch, {ME: profile}), 3.0)
        self.assertNotIn("profiles", [c[0] for c in core.reduce(state, ("tick",), 4.0)])
        (me,) = core.view(state).allies
        self.assertEqual((me.is_me, me.peak), (True, DIAMOND))

    def test_failures_stop_after_the_retries(self):
        state, _ = self.accepted()
        now = 3.0
        for _ in range(len(core.CARD_RETRY)):
            core.reduce(state, ("profiles", state.epoch, None), now)
            now = state.failures[("profiles", state.epoch)][1]
            core.reduce(state, ("tick",), now)
        commands = core.reduce(state, ("tick",), 10_000.0)
        self.assertNotIn("profiles", [c[0] for c in commands])
        (me,) = core.view(state).allies
        self.assertEqual((me.state, me.peak), ("error", None))

    def test_the_lane_keeps_only_players_pred_gg_knows(self):
        client = mock.Mock()
        client.query.return_value = {
            "p0": {"ratings": [season("Season 0", 900.0, "Platinum III")]},
            "p1": None,
        }
        io = lanes.PredggIO.__new__(lanes.PredggIO)
        io.client = client
        *_, (kind, epoch, found) = io.handle(("profiles", 7, (ME, PEERS[0])))
        self.assertEqual((kind, epoch, list(found)), ("profiles", 7, [ME]))
        self.assertEqual(found[ME]["peak"], {"rank": "Platinum III", "season": "Season 0"})

    def test_a_failing_query_retries_like_the_other_lookups(self):
        problem = Failure("network", "TransportError: nope")
        events = lanes.failure(("profiles", 7, (ME,)), problem)
        self.assertEqual(events[1], ("profiles", 7, None))


class PaintTests(unittest.TestCase):
    def test_the_card_adds_a_season_table_and_paints(self):
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QApplication

        from predoverlay.overlay import paint

        _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        seasons = (
            {
                "name": "S",
                "final": "Gold I",
                "peak": "Diamond II",
                "ranking": None,
                "peak_ranking": None,
                "percentile": None,
                "peak_date": None,
                "unranked": False,
            },
        )
        plain = row(player(peak=None))
        best = row(dataclasses.replace(player(), seasons=seasons))
        grown = paint.VERDICT_H + paint.TABLE_HDR + 2 * paint.SEASON_H + paint.card.CREDIT_H
        self.assertEqual(paint.card_size(best)[1], paint.card_size(plain)[1] + grown)
        image = QImage(700, 700, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        try:
            paint.paint_card(painter, best, paint.Motion())
            paint.paint_stack(painter, 640, [best], [plain], paint.Motion(), "5v5")
        finally:
            painter.end()


if __name__ == "__main__":
    unittest.main()
