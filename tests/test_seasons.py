"""Season history, the strength order, team balance and the TAB card's verdict line."""

import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from predoverlay.overlay import cards, core, lanes
from predoverlay.overlay.model import PlayerView
from predoverlay.overlay.rows import (
    rank_from_points,
    rank_points,
    row,
    step_rank,
    step_vp,
    team_balance,
    verdict,
)

ME = "00000000-0000-0000-0000-0000000000aa"
PEERS = [f"10000000-0000-0000-0000-{i:012d}" for i in range(1, 10)]
GOLD = {"current": {"rank": "Gold II", "points": 745.0}}
UNPLACED = {"current": {"rank": None, "points": None}}
DAY = 86400


def iso(epoch):
    """A unix time as Pred.gg writes it."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rating(name, end, rank="Gold I", peak="Platinum III", **fields):
    entry = {
        "rating": {"name": name, "endTime": end},
        "rank": {"name": rank} if rank else None,
        "peakRank": {"name": peak} if peak else None,
        "unranked": False,
        "points": 850.0,
        "peakPoints": 920.0,
        "percentile": 0.1779,
        "ranking": 1200,
        "peakRanking": 900,
        "peakTimestamp": "2026-02-07T18:04:48.723610Z",
    }
    entry.update(fields)
    return entry


def season(final, peak=None, name="S"):
    return {
        "name": name,
        "final": final,
        "peak": peak or final,
        "ranking": None,
        "peak_ranking": None,
        "percentile": None,
        "peak_date": None,
        "unranked": final is None,
    }


def player(seasons, profile=GOLD, peak=None, created=0, now=None):
    profile = dict(profile, first_played=iso(created))
    return PlayerView(ME, "A#1", "ally", False, "ready", profile, peak=peak, seasons=seasons)


class SeasonHistoryTests(unittest.TestCase):
    def test_finished_seasons_newest_first_with_readable_values(self):
        raw = {
            "ratings": [
                rating("Season 0", "2025-04-28T09:00:00Z", rank="Silver I", peak="Gold III"),
                rating("Split 5", "2026-11-03T10:00:00Z"),
                rating("Split 6", None, rank="Gold III"),  # running: the profile has it
            ]
        }
        history = cards.season_history(raw)
        self.assertEqual([s["name"] for s in history], ["Split 5", "Season 0"])
        newest = history[0]
        self.assertEqual((newest["final"], newest["peak"]), ("Gold I", "Platinum III"))
        self.assertEqual((newest["ranking"], newest["peak_ranking"]), (1200, 900))
        self.assertEqual(newest["percentile"], 18)  # share above: lower is better
        self.assertEqual(newest["peak_date"], "FEB 26")

    def test_nulls_stay_empty(self):
        raw = {
            "ratings": [
                rating(
                    "Split 4",
                    "2026-06-30T10:00:00Z",
                    rank=None,
                    peak=None,
                    percentile=None,
                    ranking=None,
                    peakRanking=None,
                    peakTimestamp=None,
                )
            ]
        }
        (only,) = cards.season_history(raw)
        self.assertIsNone(only["final"])
        self.assertIsNone(only["percentile"])
        self.assertIsNone(only["peak_date"])
        self.assertEqual(cards.season_history({"ratings": None}), ())
        self.assertIsNone(cards.season_history(None))

    def test_a_rank_name_we_do_not_know_stays_out_of_the_table(self):
        entry = rating("Split 7", "2027-01-01T00:00:00Z", rank="Champion", peak="Elite")
        (only,) = cards.season_history({"ratings": [entry]})
        self.assertEqual((only["final"], only["peak"]), (None, None))
        self.assertFalse(only["unranked"])

    def test_unranked_season_shows_no_ranks(self):
        raw = {"ratings": [rating("Split 3", "2026-02-24T12:00:00Z", unranked=True)]}
        (only,) = cards.season_history(raw)
        self.assertTrue(only["unranked"])
        self.assertIsNone(only["final"])

    def test_a_skipped_season_is_kept_but_not_one_before_the_first(self):
        raw = {
            "ratings": [
                rating("Split 2", "2025-11-25T12:00:00Z"),
                rating("Split 5", "2026-11-03T10:00:00Z"),
            ]
        }
        catalog = [
            {"name": "Season 0", "endTime": "2025-04-28T09:00:00Z"},
            {"name": "Split 2", "endTime": "2025-11-25T12:00:00Z"},
            {"name": "Split 3", "endTime": "2026-02-24T12:00:00Z"},
            {"name": "Split 5", "endTime": "2026-11-03T10:00:00Z"},
            {"name": "Split 6", "endTime": None},
        ]
        history = cards.season_history(raw, catalog)
        self.assertEqual([s["name"] for s in history], ["Split 5", "Split 3", "Split 2"])
        self.assertTrue(history[1]["unranked"])

    def test_the_lane_keeps_peak_and_history_per_player(self):
        client = mock.Mock()
        client.query.return_value = {
            "seasons": [{"name": "S", "endTime": "2026-01-01T00:00:00Z"}],
            "p0": {"ratings": [rating("S", "2026-01-01T00:00:00Z")]},
            "p1": {"ratings": []},
            "p2": None,
        }
        io = lanes.PredggIO.__new__(lanes.PredggIO)
        io.client = client
        *_, (kind, epoch, found) = io.handle(("profiles", 7, (ME, PEERS[0], PEERS[1])))
        self.assertEqual((kind, epoch), ("profiles", 7))
        self.assertEqual(found[ME]["peak"]["rank"], "Platinum III")
        self.assertEqual(len(found[ME]["seasons"]), 1)
        known = found[PEERS[0]]  # known, no ratings
        self.assertEqual((known["peak"], known["seasons"], known["main"]), (None, (), None))
        self.assertNotIn(PEERS[1], found)


class StrengthOrderTests(unittest.TestCase):
    NAMES = ("Me", "Peer0", "Peer1", "Peer2", "Peer3")  # draft rows top to bottom

    def profiles(self):
        def rank(name, points):
            return {"current": {"rank": name, "points": points}}

        return {
            ME: rank("Gold III", 650.0),
            PEERS[0]: rank("Platinum II", 1050.0),
            PEERS[1]: UNPLACED,
            PEERS[2]: rank("Gold I", 850.0),
            PEERS[3]: UNPLACED,
        }

    def accepted(self):
        """My team read off the draft and found on Pred.gg; no profile yet."""
        state = core.State(me=ME, my_name="Me", display={"x": "y"})
        core.reduce(state, ("log", {"phase": "draft", "at_ts": 1.0}), 1.0)
        texts = {("ally", i): [name, name] for i, name in enumerate(self.NAMES)}
        texts = {**texts, ("anchor", 0): ["ALLY", "ALLY"]}
        core.reduce(state, ("screen", "draft", state.phase_at, texts), 1.0)
        hits = {f"a{i}": {"player_id": PEERS[i - 1], "name": self.NAMES[i]} for i in range(1, 5)}
        core.reduce(state, ("found", state.epoch, hits), 2.0)
        return state

    def allies(self, state):
        return [state.ids[p.player_id]["pid"] for p in core.view(state).allies]

    def test_strongest_first_and_the_unplaced_last_by_peak(self):
        state = self.accepted()
        profiles = self.profiles()
        profiles[PEERS[3]] = dict(UNPLACED, peak={"rank": "Diamond I", "season": "S"})
        core.reduce(state, ("profiles", state.epoch, profiles), 3.0)
        self.assertEqual(self.allies(state), [PEERS[0], PEERS[2], ME, PEERS[3], PEERS[1]])

    def test_order_is_frozen_when_profiles_land(self):
        state = self.accepted()
        core.reduce(state, ("profiles", state.epoch, self.profiles()), 3.0)
        before = self.allies(state)
        later = dict(UNPLACED, peak={"rank": "Paragon", "season": "S"})
        core.reduce(state, ("profiles", state.epoch, {PEERS[3]: later}), 4.0)
        self.assertEqual(self.allies(state), before)  # late data never moves a row
        self.assertEqual(before[-2:], [PEERS[1], PEERS[3]])  # a tie keeps the screen's order

    def test_teams_stay_who_they_are(self):
        state = self.accepted()
        everyone = core.view(state)
        core.reduce(state, ("profiles", state.epoch, self.profiles()), 3.0)
        view = core.view(state)
        self.assertEqual({p.player_id for p in view.allies}, {p.player_id for p in everyone.allies})

    def test_a_new_match_forgets_the_order(self):
        state = self.accepted()
        core.reduce(state, ("profiles", state.epoch, self.profiles()), 3.0)
        self.assertTrue(state.strength)
        core.reduce(state, ("log", {"phase": "menu", "at_ts": 5.0}), 5.0)
        self.assertEqual(state.strength, {})


class TopRankPointsTests(unittest.TestCase):
    def rank(self, name, points=None):
        return {"current": {"rank": name, "points": points}}

    def test_without_points_paragon_sorts_above_master(self):
        self.assertGreater(rank_points(self.rank("Paragon")), rank_points(self.rank("Master")))

    def test_points_run_on_above_a_hundred_at_the_top(self):
        self.assertEqual(rank_points(self.rank("Master", 1750.0)), 1750.0)
        self.assertEqual(step_vp(self.rank("Master", 1750.0)), 250)
        self.assertEqual(rank_points(self.rank("Master", -5.0)), 1500)  # the step's floor

    def test_averages_above_the_ladder_still_read_as_a_rank(self):
        self.assertEqual(rank_from_points(1950), "Master")
        self.assertEqual(rank_from_points(850), "Gold I")


class TeamBalanceTests(unittest.TestCase):
    def rows(self, *points):
        return [{"points": p} for p in points]

    def test_even_ally_and_enemy(self):
        strong, weak = self.rows(900, 900, 900), self.rows(700, 700, 700)
        close = self.rows(880, 880, 880)
        self.assertEqual(team_balance(strong, close)["text"], "EVEN")
        self.assertEqual(team_balance(strong, weak), {"text": "\u2212200 VP", "side": "ally"})
        self.assertEqual(team_balance(weak, strong), {"text": "+200 VP", "side": "enemy"})

    def test_needs_both_averages(self):
        unplaced = self.rows(None, None, None, None, 800)
        self.assertIsNone(team_balance(self.rows(900, 900, 900), unplaced))


class VerdictTests(unittest.TestCase):
    NOW = 400 * DAY  # accounts created at 0 are over a year old here

    def line(self, seasons, rank="Gold II", created=0, peak=None):
        view = player(seasons, peak=peak, created=created)
        days = (self.NOW - created) // DAY
        found = verdict(view, rank, days, self.NOW)
        return found and found["text"]

    def test_unknown_history_says_nothing(self):
        self.assertIsNone(self.line(None))

    def test_no_ranked_history(self):
        self.assertEqual(self.line(()), "NO RANKED HISTORY")

    def test_fast_climb_needs_a_young_account(self):
        seasons = [season("Diamond II"), season("Gold I")]
        young = self.NOW - 100 * DAY
        self.assertEqual(self.line(seasons, created=young), "FAST CLIMB · DIAMOND IN 2 SEASONS")
        self.assertNotIn("FAST CLIMB", self.line(seasons))  # a veteran with few seasons

    def test_fast_climb_with_an_unknown_age_goes_by_the_seasons(self):
        view = player([season("Diamond II"), season("Gold I")])
        self.assertTrue(verdict(view, "Gold II", None, self.NOW)["alert"])
        many = player([season("Diamond II")] * 4)
        self.assertEqual(verdict(many, "Diamond II", None, self.NOW)["text"], "STEADY")

    def test_a_break_comes_before_fast_climb(self):
        seasons = [season(None), season(None), season("Diamond II")]
        young = self.NOW - 100 * DAY
        self.assertEqual(self.line(seasons, created=young), "BACK AFTER 2 SEASONS · WAS DIAMOND")

    def test_back_after_a_break(self):
        seasons = [season(None), season(None)] + [season("Platinum III")] * 4
        self.assertEqual(self.line(seasons), "BACK AFTER 2 SEASONS · WAS PLATINUM")

    def test_below_usual_rank(self):
        seasons = [season("Platinum II")] * 5
        self.assertEqual(self.line(seasons), "BELOW USUAL RANK · USUAL PLATINUM II")

    def test_below_usual_waits_out_the_rank_reset(self):
        ended = datetime.fromtimestamp(self.NOW - 20 * DAY, timezone.utc)
        end = ended.strftime("%Y-%m-%dT%H:%M:%SZ")
        seasons = [dict(season("Platinum II"), end=end)] * 5
        self.assertNotIn("BELOW USUAL", self.line(seasons) or "")
        later = self.NOW + 2 * DAY
        view = player(seasons)
        found = verdict(view, "Gold II", 400, later)
        self.assertEqual(found["text"], "BELOW USUAL RANK · USUAL PLATINUM II")

    def test_climbing_falling_steady(self):
        up = [season("Gold III"), season("Silver I"), season("Silver II"), season("Silver II")]
        self.assertEqual(self.line(up, rank="Gold III"), "CLIMBING")
        down = [season("Gold III"), season("Gold II"), season("Gold I"), season("Gold I")]
        self.assertEqual(self.line(down, rank="Gold III"), "FALLING")
        flat = [season("Gold II"), season("Gold I"), season("Gold II"), season("Gold II")]
        self.assertEqual(self.line(flat), "STEADY")

    def test_too_little_to_say(self):
        self.assertIsNone(self.line([season("Gold II"), season("Gold II")]))

    def test_step_rank_reverses_rank_step(self):
        for name in ("Bronze III", "Gold I", "Diamond II", "Master", "Paragon"):
            from predoverlay.overlay.rows import rank_step

            self.assertEqual(step_rank(rank_step(name)), name)


class YoungHighTests(unittest.TestCase):
    def young(self, profile, peak=None, days=100):
        view = player((), profile=profile, peak=peak, created=0)
        return row(view, now=days * DAY)["young_high"]

    def test_young_and_already_high(self):
        self.assertTrue(self.young(GOLD, peak={"rank": "Diamond III", "season": "S"}))
        diamond = {"current": {"rank": "Diamond III", "points": 1200.0}}
        self.assertTrue(self.young(diamond))

    def test_old_or_not_high(self):
        self.assertFalse(self.young(GOLD))
        high = {"rank": "Paragon", "season": "S"}
        self.assertFalse(self.young(GOLD, peak=high, days=400))


class GridTests(unittest.TestCase):
    """Every row of the panel draws each field at the same x, whatever its data."""

    def test_one_x_per_column_in_every_data_state(self):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QApplication

        from predoverlay.overlay import paint

        _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        peak = {"rank": "Diamond I", "season": "S"}
        states = [
            player(None),
            player(None, peak=peak),
            player(None, profile=UNPLACED, created=0),
            player(None, peak=peak, created=time.time()),  # a new account
        ]
        rows = [row(view, now=0) for view in states]
        drawn = []
        real = paint.text

        def spy(p_, x, y, s, f, color, align="l", w=None):
            drawn.append((round(x, 2), align))
            return real(p_, x, y, s, f, color, align, w)

        image = QImage(640, 60, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        rect = QRectF(paint.PLATE_L, 0, paint.PLATE_R - paint.PLATE_L, paint.STACK_ROW_H)
        with (  # every module a row's text comes from
            mock.patch.object(paint.panel, "text", spy),
            mock.patch.object(paint.cells, "text", spy),
        ):
            for data in rows:
                paint.stack_row(painter, rect, data, paint.Motion())
        painter.end()
        self.assertTrue(drawn)
        lv = paint._advance("LV ", paint.tnum(paint.font(12, 500, 0.08, caps=True)))
        left = {
            paint.COL_NAME,
            round(paint.COL_NAME + lv, 2),
            paint.COL_NAME + paint.TAB_AGE,
            paint.COL_RANK,
            round(paint.COL_RANK + paint._advance("PEAK ", paint._peak_font()), 2),
        }
        flag = paint.COL_NAME_R - 5
        right = {paint.COL_RANK_R, paint.COL_R, flag}
        self.assertLessEqual({x for x, a in drawn if a == "l"}, left)
        self.assertLessEqual({x for x, a in drawn if a == "r"}, right)


class RevealTests(unittest.TestCase):
    def test_a_new_order_crossfades_each_slot_while_shown(self):
        import dataclasses

        from PySide6.QtWidgets import QApplication

        from predoverlay.overlay import window

        _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        now = [1000.0]
        with (
            mock.patch.object(window.win32, "game_in_front", lambda: True),
            mock.patch.object(window.win32, "key_down", lambda code: False),
            mock.patch.object(window.win32, "keep_on_top", lambda hwnd: None),
            mock.patch.object(window.win32, "make_inert", lambda hwnd: None),
        ):
            view = window.preview_view("draft", 999.0, 1)
            tracker = mock.Mock(snapshot=view, tab_held=False)
            overlay = window.Overlay(tracker, clock=lambda: now[0])
            overlay.tick()
            now[0] += 2.0
            overlay.tick()
            started = overlay.rows_started
            now[0] += 2.0
            flipped = tuple(reversed(view.allies))
            tracker.snapshot = dataclasses.replace(view, version=2, allies=flipped)
            overlay.tick()
            self.assertEqual(overlay.rows_started, started)
            self.assertIn("name", overlay.mixes[flipped[0].player_id])
            self.assertEqual(
                [r["player_id"] for r in overlay.allies], [p.player_id for p in flipped]
            )
            overlay.hide()


class CardTests(unittest.TestCase):
    def render(self, data, seen=None):
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QApplication

        from predoverlay.overlay import paint

        _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        width, height = paint.card_size(data, seen)
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        try:
            paint.paint_card(painter, data, paint.Motion(), seen)
        finally:
            painter.end()
        return height

    def history(self, count):
        return tuple(season("Gold II", name=f"Season 1 - Split {i}") for i in range(count, 0, -1))

    def test_the_table_grows_with_seasons_and_older_ones_fold(self):
        from predoverlay.overlay import paint

        three = self.render(row(player(self.history(3))))
        seven = self.render(row(player(self.history(7))))
        self.assertEqual(seven - three, 4 * paint.SEASON_H)
        nine = self.render(row(player(self.history(paint.MAX_SEASONS))))
        twelve = self.render(row(player(self.history(12))))
        self.assertEqual(nine, twelve)  # nothing scrolls: the oldest fold into one row

    def test_no_history_means_no_table(self):
        from predoverlay.overlay import paint

        base = row(player(None))
        self.assertEqual(self.render(base), paint.HEAD_H + paint.CARD_PAD_B)  # no data: head only

    def test_placeholders_and_unplaced_rows_paint(self):
        seasons = (season(None), season("Gold II"))
        self.render(row(player(seasons, profile=UNPLACED)))


if __name__ == "__main__":
    unittest.main()
