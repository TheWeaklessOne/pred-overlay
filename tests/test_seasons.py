"""Season history, the strength order, team balance and the TAB card's verdict line."""

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
    team_balance,
    verdict,
)

ME = "00000000-0000-0000-0000-0000000000aa"
PEERS = [f"10000000-0000-0000-0000-{i:012d}" for i in range(1, 10)]
GOLD = {"placed": True, "division": 3, "tier": 2, "vp": 45, "level": 300}
DAY = 86400


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
    profile = dict(profile, created=created)
    return PlayerView(
        ME, "A#1", "ally", False, "ready", None, "off", profile, peak=peak, seasons=seasons
    )


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
        io.catalog = client
        ((_, _, found),) = io.handle(("peaks", 7, (ME, PEERS[0], PEERS[1])))
        self.assertEqual(found[ME]["peak"]["rank"], "Platinum III")
        self.assertEqual(len(found[ME]["seasons"]), 1)
        self.assertEqual(
            found[PEERS[0]], {"peak": None, "seasons": (), "main": None}
        )  # known, no ratings
        self.assertNotIn(PEERS[1], found)


class StrengthOrderTests(unittest.TestCase):
    def profiles(self):
        def rank(division, tier, vp=50, level=100):
            return {
                "placed": True,
                "division": division,
                "tier": tier,
                "vp": vp,
                "level": level,
            }

        return {
            ME: rank(3, 1),  # Gold III
            PEERS[0]: rank(4, 2),  # Platinum II
            PEERS[1]: {"placed": False, "level": 9},
            PEERS[2]: rank(3, 3),  # Gold I
            PEERS[3]: {"placed": False, "level": 5},
        }

    def accepted(self):
        state = core.State(me=ME, token_account=ME, phase="draft", display={"x": "y"})
        roster = [ME] + PEERS
        current = {"in_game": True, "match_id": "m", "player_ids": roster}
        core.reduce(state, ("instance", current), 1.0)
        core.reduce(state, ("instance", current), 2.0)
        return state

    def allies(self, state):
        return [p.player_id for p in core.view(state).allies]

    def test_strongest_first_and_the_unplaced_last_by_peak(self):
        state = self.accepted()
        peak = {"peak": {"rank": "Diamond I", "season": "S"}, "seasons": ()}
        core.reduce(state, ("peaks", state.epoch, {PEERS[3]: peak}), 3.0)
        core.reduce(state, ("profiles", state.epoch, self.profiles()), 3.0)
        self.assertEqual(self.allies(state), [PEERS[0], PEERS[2], ME, PEERS[3], PEERS[1]])

    def test_order_is_frozen_when_profiles_land(self):
        state = self.accepted()
        core.reduce(state, ("profiles", state.epoch, self.profiles()), 3.0)
        before = self.allies(state)
        peak = {"peak": {"rank": "Paragon", "season": "S"}, "seasons": ()}
        core.reduce(state, ("peaks", state.epoch, {PEERS[3]: peak}), 4.0)
        self.assertEqual(self.allies(state), before)  # late data never moves a row
        self.assertEqual(before[-2:], [PEERS[1], PEERS[3]])  # level decided it

    def test_teams_stay_who_they_are(self):
        state = self.accepted()
        everyone = core.view(state)
        core.reduce(state, ("profiles", state.epoch, self.profiles()), 3.0)
        view = core.view(state)
        self.assertEqual({p.player_id for p in view.allies}, {p.player_id for p in everyone.allies})

    def test_a_new_match_forgets_the_order(self):
        state = self.accepted()
        core.reduce(state, ("profiles", state.epoch, self.profiles()), 3.0)
        core.reduce(state, ("instance", {"in_game": True, "match_id": "n", "player_ids": []}), 5.0)
        self.assertEqual(state.strength, {})


class TopRankPointsTests(unittest.TestCase):
    MASTER = {"placed": True, "division": 6, "tier": 1}
    PARAGON = {"placed": True, "paragon": True, "division": 6, "tier": 1}

    def test_paragon_sorts_above_a_master_with_more_vp(self):
        low_paragon = rank_points(dict(self.PARAGON, vp=20))
        self.assertGreater(low_paragon, rank_points(dict(self.MASTER, vp=90)))

    def test_vp_runs_on_above_a_hundred_at_the_top(self):
        self.assertEqual(rank_points(dict(self.MASTER, vp=250)), 1750)
        self.assertEqual(rank_points(dict(self.MASTER, vp=-5)), 1500)

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
        self.assertEqual(self.line(()), "NO RANKED HISTORY · LV 300")

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
        diamond = {"placed": True, "division": 5, "tier": 1, "vp": 0, "level": 40}
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
        from predoverlay.overlay.model import Encounter

        _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        peak = {"rank": "Diamond I", "season": "S"}
        met = Encounter(matches_ago=12, hero="IGGY & SCORCH", side="against", won=False)
        unplaced = {"placed": False, "level": 7}
        states = [
            player(None),
            player(None, peak=peak),
            player(None, profile=unplaced, created=0),
            player(None, peak=peak, created=10**12),  # a new account
        ]
        rows = [row(view, now=0) for view in states]
        met_row = row(player(None), now=0)
        met_row["encounter"] = row(
            PlayerView(ME, "A#1", "ally", False, "ready", None, "off", GOLD, encounters=(met,))
        )["encounter"]
        rows.append(met_row)
        drawn = []
        real = paint.text

        def spy(p_, x, y, s, f, color, align="l", w=None):
            drawn.append((round(x, 2), align))
            return real(p_, x, y, s, f, color, align, w)

        image = QImage(640, 60, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        rect = QRectF(paint.PLATE_L, 0, paint.PLATE_R - paint.PLATE_L, paint.STACK_ROW_H)
        with mock.patch.object(paint, "text", spy):
            for data in rows:
                paint.stack_row(painter, rect, data, paint.Motion())
        painter.end()
        lv = paint._advance("LV ", paint.tnum(paint.font(12, 500, 0.08, caps=True)))
        left = {
            paint.COL_NAME,
            round(paint.COL_NAME + lv, 2),
            paint.COL_NAME + paint.TAB_AGE,
            paint.COL_RANK,
            paint.COL_PEAK,
            paint.COL_TEXT,
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
    def render(self, data, source="omeda", seen=None):
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QApplication

        from predoverlay.overlay import paint

        _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        width, height = paint.card_size(data, source, seen)
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        try:
            paint.paint_card(painter, data, paint.Motion(), source, seen)
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
        self.render(row(player(seasons, profile={"placed": False, "level": 3})))


if __name__ == "__main__":
    unittest.main()
