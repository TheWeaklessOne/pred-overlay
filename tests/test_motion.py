"""The panel's motion in the real window with a fake clock: seam, wave, one-shots, no idle."""

import dataclasses
import unittest
from unittest import mock

from PySide6.QtCore import QPoint
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from predoverlay.overlay import model, motion, paint, scoreboard, window
from predoverlay.overlay.motion import Seam


class SeamModelTests(unittest.TestCase):
    def test_a_handoff_closes_moves_and_opens_within_about_600_ms(self):
        seam = Seam(10.0)
        start = seam.frame(10.0)
        self.assertEqual((start.body, start.rule, start.moved, start.done), (1, 1, False, False))
        middle = seam.frame(10.1)
        self.assertTrue(0 < middle.body < 1)
        self.assertEqual(middle.rule, 1.0)  # the rule holds until the body has folded into it
        shut = seam.frame(10.0 + seam.close_time - 0.001)
        self.assertEqual(shut.body, 0.0)
        self.assertFalse(shut.moved)
        opening = seam.frame(10.0 + seam.close_time)
        self.assertTrue(opening.moved)
        self.assertEqual(opening.body, 0.0)
        self.assertAlmostEqual(opening.rule, 0.0)
        self.assertFalse(seam.frame(10.0 + seam.total - 0.01).done)
        self.assertTrue(seam.frame(10.0 + seam.total + 0.001).done)
        self.assertLessEqual(seam.total, 0.61)

    def test_the_rule_leads_the_body_when_opening(self):
        seam = Seam(0.0)
        early = seam.frame(seam.close_time + 0.05)
        self.assertGreater(early.rule, 0.0)
        self.assertEqual(early.body, 0.0)

    def test_the_match_start_only_closes_and_is_quicker(self):
        seam = Seam(0.0, opens=False)
        self.assertLess(seam.total, Seam(0.0).close_time + 0.01)
        self.assertFalse(seam.frame(0.1).moved)
        self.assertTrue(seam.frame(seam.total).done)

    def test_a_late_frame_skips_ahead_instead_of_stretching(self):
        seam = Seam(0.0)
        self.assertTrue(seam.frame(5.0).done)

    def test_stop_freezes_it_where_it_is(self):
        seam = Seam(0.0)
        seam.stop(0.1)
        self.assertEqual(seam.frame(0.1), seam.frame(9.0))
        self.assertFalse(seam.frame(9.0).done)


def unloaded(player):
    return dataclasses.replace(player, state="loading", profile=None, peak=None, seasons=())


class WindowCase(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        self.now = [1000.0]
        self.focus = [True]
        self.keys = set()
        self.cursor = [QPoint(0, 0)]
        patches = (
            mock.patch.object(window.win32, "game_in_front", lambda: self.focus[0]),
            mock.patch.object(window.win32, "key_down", lambda code: code in self.keys),
            mock.patch.object(window.win32, "keep_on_top", lambda hwnd: None),
            mock.patch.object(window.win32, "make_inert", lambda hwnd: None),
            mock.patch.object(window.QCursor, "pos", staticmethod(lambda: self.cursor[0])),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tracker = mock.Mock(
            snapshot=model.ViewModel(phase="menu", phase_at=self.now[0]), tab_held=False
        )
        self.overlay = window.Overlay(self.tracker, clock=lambda: self.now[0])
        self.addCleanup(self.overlay.close)

    def step(self, ticks=1, each=0.05):
        for _ in range(ticks):
            self.now[0] += each
            self.overlay.tick()

    def view(self, phase, version=None, **extra):
        self.version = getattr(self, "version", 1) + 1
        view = window.preview_view(phase, phase_at=self.now[0], version=version or self.version)
        return dataclasses.replace(view, **extra)

    def settle_in_draft(self, **extra):
        self.tracker.snapshot = self.view("draft", **extra)
        self.step(30)
        return self.tracker.snapshot

    def paint_now(self):
        image = QImage(700, 700, QImage.Format.Format_ARGB32)
        self.overlay.render(image)

    def updates(self, ticks):
        self.paint_now()  # what is on screen is drawn: only motion may ask for more
        with mock.patch.object(self.overlay, "update") as update:
            self.step(ticks)
        return update.call_count


class FailureTests(WindowCase):
    def setUp(self):
        super().setUp()
        self.settle_in_draft()
        self.logged = []
        self.overlay.log = self.logged.append

    def test_a_failing_tick_stops_the_frames_and_logs_once(self):
        self.overlay.frame.start()
        with mock.patch.object(self.overlay, "_tick", side_effect=TypeError("boom")):
            self.step(3)
        self.assertFalse(self.overlay.frame.isActive())
        self.assertEqual(len(self.logged), 1)

    def test_the_same_failure_after_a_good_tick_is_logged_again(self):
        with mock.patch.object(self.overlay, "_tick", side_effect=TypeError("boom")):
            self.step(1)
        self.step(1)
        with mock.patch.object(self.overlay, "_tick", side_effect=TypeError("boom")):
            self.step(1)
        self.assertEqual(len(self.logged), 2)

    def test_a_failing_frame_step_is_caught(self):
        self.overlay.frame.start()
        with mock.patch.object(self.overlay, "_advance_seam", side_effect=ValueError("boom")):
            self.overlay._frame_step()
            self.overlay._frame_step()
        self.assertFalse(self.overlay.frame.isActive())
        self.assertEqual(len(self.logged), 1)

    def test_a_failing_paint_is_caught_and_the_painter_released(self):
        with mock.patch.object(window.paint, "paint_stack", side_effect=TypeError("boom")):
            self.paint_now()
            self.paint_now()
        self.assertEqual(len(self.logged), 1)
        self.paint_now()
        self.assertNotIn("paint", self.overlay.failures)


class SeamWindowTests(WindowCase):
    def test_a_resort_does_not_replay_the_rows_on_the_next_show(self):
        view = self.settle_in_draft()
        self.tracker.snapshot = dataclasses.replace(
            view, version=view.version + 1, allies=tuple(reversed(view.allies))
        )
        self.step(5)
        started = self.overlay.rows_started
        self.focus[0] = False
        self.step(10)
        self.focus[0] = True
        self.step(10)
        self.assertEqual(self.overlay.rows_started, started)

    def test_a_screen_change_during_a_seam_retakes_the_picture(self):
        self.settle_in_draft()
        self.tracker.snapshot = self.view("loading")
        self.step(1)
        shot = self.overlay.seam_shot
        self.assertIsNotNone(shot)
        self.overlay._screen_changed()
        self.assertIsNot(self.overlay.seam_shot, shot)

    def test_draft_to_loading_hands_over_without_a_fade(self):
        self.settle_in_draft()
        started = self.overlay.rows_started
        draft_y = self.overlay.y()
        self.tracker.snapshot = self.view("loading")
        self.step(1)
        self.assertIsNotNone(self.overlay.seam)
        self.assertEqual(self.overlay.layout_name, "draft")  # still closing where it was
        self.paint_now()
        layouts, alpha = [], []
        for _ in range(14):
            self.step(1)
            self.paint_now()
            layouts.append(self.overlay.layout_name)
            alpha.append(self.overlay._progress(self.now[0]))
        self.assertEqual(min(alpha), 1.0)  # the seam moves it, no fade at all
        self.assertIsNone(self.overlay.seam)
        self.assertEqual(layouts[-1], "loading")
        self.assertNotEqual(self.overlay.y(), draft_y)
        self.assertEqual(self.overlay.rows_started, started)  # the rows do not replay

    def test_the_picture_is_taken_again_at_the_new_place(self):
        self.settle_in_draft()
        self.tracker.snapshot = self.view("loading")
        self.step(1)
        first = self.overlay.seam_shot
        self.assertFalse(first.isNull())
        self.step(8)
        self.assertTrue(self.overlay.seam_moved)
        self.assertIsNot(self.overlay.seam_shot, first)

    def test_losing_focus_mid_seam_fades_from_where_it_stood(self):
        self.settle_in_draft()
        self.tracker.snapshot = self.view("loading")
        self.step(2)
        self.focus[0] = False
        self.step(1)
        self.assertIsNotNone(self.overlay.seam.stopped)
        self.assertFalse(self.overlay.shown)
        self.paint_now()  # frozen seam under the fade
        self.step(8)
        self.assertIsNone(self.overlay.seam)
        self.assertFalse(self.overlay.isVisible())

    def test_coming_back_after_an_abort_shows_the_loading_panel(self):
        self.settle_in_draft()
        self.tracker.snapshot = self.view("loading")
        self.step(2)
        self.focus[0] = False
        self.step(8)
        self.focus[0] = True
        self.step(10)
        self.assertEqual((self.overlay.layout_name, self.overlay.shown), ("loading", True))
        self.assertIsNone(self.overlay.seam)

    def test_a_dodge_mid_seam_leaves_for_the_menu_cleanly(self):
        self.settle_in_draft()
        self.tracker.snapshot = self.view("loading")
        self.step(2)
        self.tracker.snapshot = model.ViewModel(phase="menu", phase_at=self.now[0], version=99999)
        self.step(14)
        self.assertEqual(self.overlay.layout_name, "menu")
        self.assertIsNone(self.overlay.seam)

    def test_another_roster_cancels_it(self):
        self.settle_in_draft()
        self.tracker.snapshot = self.view("loading")
        self.step(2)
        other = self.tracker.snapshot
        fresh = [dataclasses.replace(p, player_id=p.player_id[:-1] + "f") for p in other.allies]
        self.tracker.snapshot = dataclasses.replace(other, allies=tuple(fresh), version=88888)
        self.step(1)
        self.assertIsNone(self.overlay.seam)

    def test_the_match_start_closes_through_the_rule_and_hides(self):
        self.tracker.snapshot = self.view("loading")
        self.step(30)
        self.assertTrue(self.overlay.shown)
        started = dataclasses.replace(
            self.tracker.snapshot, started_at=self.now[0] - 60, version=77777
        )
        self.tracker.snapshot = started
        self.step(1)
        self.assertIsNotNone(self.overlay.seam)
        self.assertFalse(self.overlay.seam.opens)
        self.assertEqual(self.overlay._progress(self.now[0]), 1.0)
        self.step(6)
        self.assertIsNone(self.overlay.seam)
        self.assertFalse(self.overlay.shown)
        self.assertFalse(self.overlay.isVisible())

    def test_alt_tab_is_a_plain_fade(self):
        self.settle_in_draft()
        self.focus[0] = False
        self.step(1)
        self.assertIsNone(self.overlay.seam)
        self.assertFalse(self.overlay.shown)

    def test_f8_hiding_a_pin_in_the_match_is_a_plain_fade(self):
        self.tracker.snapshot = dataclasses.replace(
            self.view("loading"), phase_at=self.now[0] - 400, started_at=self.now[0] - 300
        )
        self.step(4)
        hotkey = self.overlay.hotkey
        self.keys.add(hotkey)
        self.step(1)
        self.keys.discard(hotkey)
        self.step(12)
        self.assertTrue(self.overlay.shown)  # pinned
        self.keys.add(hotkey)
        self.step(1)
        self.assertIsNone(self.overlay.seam)
        self.assertFalse(self.overlay.shown)

    def test_no_slide_a_fading_panel_stays_in_its_place(self):
        self.assertFalse(hasattr(window, "SLIDE"))


class WaveCase(WindowCase):
    def loaded_after_waiting(self):
        view = window.preview_view("draft", phase_at=self.now[0], version=5)
        waiting = dataclasses.replace(
            view,
            allies=tuple(unloaded(p) for p in view.allies),
            enemies=tuple(unloaded(p) for p in view.enemies),
        )
        self.tracker.snapshot = waiting
        self.step(30)
        return dataclasses.replace(view, version=6)


class WaveTests(WaveCase):
    def test_several_rows_land_one_after_another(self):
        ready = self.loaded_after_waiting()
        self.tracker.snapshot = ready
        self.step(1)
        now = self.now[0]
        starts = [self.overlay.mixes[r["player_id"]]["rank"][1] for r in self.overlay.allies]
        self.assertAlmostEqual(starts[0], now, places=2)
        self.assertAlmostEqual(starts[1] - starts[0], window.WAVE_STEP, places=3)
        self.assertLessEqual(max(starts) - now, window.WAVE_MAX + 1e-6)

    def test_the_wave_never_runs_longer_than_its_cap(self):
        ready = self.loaded_after_waiting()
        self.tracker.snapshot = ready
        self.step(1)
        starts = [groups["rank"][1] for groups in self.overlay.mixes.values() if "rank" in groups]
        self.assertLessEqual(max(starts) - min(starts), window.WAVE_MAX + 1e-6)

    def test_one_row_changing_starts_at_once(self):
        view = self.settle_in_draft()
        first = dataclasses.replace(view.allies[0], profile=dict(view.allies[0].profile, level=999))
        self.tracker.snapshot = dataclasses.replace(
            view, allies=(first,) + view.allies[1:], version=view.version + 1
        )
        self.step(1)
        mix = self.overlay.mixes[first.player_id]
        self.assertAlmostEqual(mix["meta"][1], self.now[0], places=3)

    def test_a_second_batch_follows_the_first_without_a_pile_up(self):
        ready = self.loaded_after_waiting()
        self.tracker.snapshot = ready
        self.step(1)
        first_end = self.overlay.wave_free
        changed = dataclasses.replace(
            ready.allies[0], profile=dict(ready.allies[0].profile, level=999)
        )
        self.tracker.snapshot = dataclasses.replace(
            ready, allies=(changed,) + ready.allies[1:], version=7
        )
        self.now[0] += 0.05
        self.overlay.tick()
        mix = self.overlay.mixes[changed.player_id]
        self.assertGreaterEqual(mix["meta"][1], first_end - window.WAVE_OVERLAP - 1e-6)

    def test_a_row_still_arriving_takes_its_data_as_it_settles(self):
        view = window.preview_view("draft", phase_at=self.now[0], version=5)
        waiting = dataclasses.replace(
            view,
            allies=tuple(unloaded(p) for p in view.allies),
            enemies=tuple(unloaded(p) for p in view.enemies),
        )
        self.tracker.snapshot = waiting
        self.step(2)
        self.tracker.snapshot = dataclasses.replace(view, version=6)
        self.step(1)
        last = self.overlay.enemies[-1]["player_id"]
        settled = (
            self.overlay.rows_started
            + (len(self.overlay.allies) + len(self.overlay.enemies) - 1) * window.ROW_STAGGER
            + window.ROW_TIME
        )
        self.assertGreaterEqual(self.overlay.mixes[last]["rank"][1], settled - 1e-6)

    def test_a_row_waits_with_its_old_value_until_its_turn(self):
        ready = self.loaded_after_waiting()
        self.tracker.snapshot = ready
        self.step(1)
        self.paint_now()  # negative times in the mixes must draw the old field

    def test_the_team_average_arrives_after_the_wave(self):
        ready = self.loaded_after_waiting()
        self.tracker.snapshot = ready
        self.step(1)
        self.assertGreaterEqual(self.overlay.head_at["ally"], self.overlay.wave_free - 1e-6)

    def test_a_resort_crossfades_in_place_instead_of_replaying(self):
        view = self.settle_in_draft()
        started = self.overlay.rows_started
        flipped = tuple(reversed(view.allies))
        self.tracker.snapshot = dataclasses.replace(view, version=view.version + 1, allies=flipped)
        self.step(1)
        self.assertEqual(self.overlay.rows_started, started)
        self.assertEqual([r["player_id"] for r in self.overlay.allies][0], flipped[0].player_id)
        self.assertIn("name", self.overlay.mixes[flipped[0].player_id])

    def test_ranks_that_arrive_with_the_sort_still_strike(self):
        ready = self.loaded_after_waiting()
        sorted_view = dataclasses.replace(ready, allies=tuple(reversed(ready.allies)))
        self.tracker.snapshot = sorted_view
        self.step(1)
        struck = [g for g in self.overlay.mixes.values() if "strike" in g]
        self.assertGreater(len(struck), 3)


class OneShotTests(WaveCase):
    def strikes(self):
        now = self.now[0]
        return {
            pid
            for pid, groups in self.overlay.mixes.items()
            if "strike" in groups and now < groups["strike"][1] + paint.STRIKE
        }

    def test_a_rank_strikes_once(self):
        ready = self.loaded_after_waiting()
        self.tracker.snapshot = ready
        self.step(1)
        self.assertTrue(self.strikes())
        self.step(40)
        self.assertFalse(self.strikes())
        changed = dataclasses.replace(
            ready.allies[0], profile=dict(ready.allies[0].profile, vp=3, division=4)
        )
        self.tracker.snapshot = dataclasses.replace(
            ready, allies=(changed,) + ready.allies[1:], version=9
        )
        self.step(1)
        self.assertIn("rank", self.overlay.mixes[changed.player_id])  # a quiet swap
        self.assertFalse(self.strikes())

    def test_data_present_when_the_panel_appears_does_not_strike(self):
        self.tracker.snapshot = self.view("draft")
        self.step(3)
        self.assertFalse(self.strikes())

    def test_data_landing_while_hidden_does_not_strike_later(self):
        ready = self.loaded_after_waiting()
        self.focus[0] = False
        self.step(8)
        self.tracker.snapshot = ready
        self.step(2)
        self.focus[0] = True
        self.step(1)
        self.assertFalse(self.strikes())
        self.assertFalse(self.overlay.mixes.get(ready.allies[0].player_id))

    def peaked(self, view, ranks):
        def with_peak(player, rank):
            peak = {"rank": rank, "season": "Season 0"}
            return dataclasses.replace(player, peak=peak)

        allies = [with_peak(p, r) for p, r in zip(view.allies, ranks)]
        allies += view.allies[len(ranks) :]
        return dataclasses.replace(view, allies=tuple(allies), version=view.version + 1)

    def glints(self):
        return {pid for pid, groups in self.overlay.mixes.items() if "glint" in groups}

    def bare(self):
        view = window.preview_view("draft", phase_at=self.now[0], version=5)
        clear = tuple(dataclasses.replace(p, peak=None) for p in view.allies)
        return dataclasses.replace(view, allies=clear)

    def test_only_the_highest_peaks_shine_and_two_at_most(self):
        view = self.bare()
        self.tracker.snapshot = view
        self.step(30)
        self.tracker.snapshot = self.peaked(
            view, ("Paragon", "Paragon", "Master", "Diamond II", "Master")
        )
        self.step(1)
        self.assertEqual(len(self.glints()), 2)

    def test_a_glint_comes_after_the_wave(self):
        view = self.bare()
        self.tracker.snapshot = view
        self.step(30)
        self.tracker.snapshot = self.peaked(view, ("Paragon",))
        self.step(1)
        pid = view.allies[0].player_id
        self.assertGreaterEqual(
            self.overlay.mixes[pid]["glint"][1], self.overlay.mixes[pid]["peak"][1]
        )

    def test_a_platinum_peak_does_not_shine(self):
        view = self.bare()
        self.tracker.snapshot = view
        self.step(30)
        self.tracker.snapshot = self.peaked(view, ("Diamond I", "Platinum II"))
        self.step(1)
        self.assertFalse(self.glints())

    def test_an_arriving_panel_reveals_the_wave_of_a_new_roster_again(self):
        ready = self.loaded_after_waiting()
        self.tracker.snapshot = ready
        self.step(40)
        other = window.preview_view("draft", phase_at=self.now[0], version=20)
        fresh = dataclasses.replace(
            other,
            allies=tuple(
                dataclasses.replace(unloaded(p), player_id=p.player_id[:-1] + "e")
                for p in other.allies
            ),
            enemies=tuple(
                dataclasses.replace(unloaded(p), player_id=p.player_id[:-1] + "e")
                for p in other.enemies
            ),
        )
        self.tracker.snapshot = fresh
        self.step(30)
        self.assertFalse(self.overlay.landed)


class IdleTests(WindowCase):
    def test_a_settled_panel_repaints_nothing(self):
        self.settle_in_draft()
        self.step(10)
        self.assertFalse(self.overlay.frame.isActive())
        self.assertEqual(self.updates(40), 0)

    def test_waiting_rows_breathe_three_times_and_then_stop(self):
        view = window.preview_view("draft", phase_at=self.now[0], version=5)
        self.tracker.snapshot = dataclasses.replace(
            view,
            allies=tuple(unloaded(p) for p in view.allies),
            enemies=tuple(unloaded(p) for p in view.enemies),
        )
        self.step(5)
        self.assertGreater(self.updates(5), 0)
        self.step(int(window.PULSE * window.PULSES / 0.05) + 2)
        self.assertEqual(self.updates(40), 0)
        self.assertEqual(self.overlay._pulse(self.now[0]), 1.0)

    def test_a_status_that_states_a_problem_does_not_breathe(self):
        self.tracker.snapshot = model.ViewModel(
            phase="draft", phase_at=self.now[0], status="mode not supported", status_note=True
        )
        self.step(30)
        self.assertEqual(self.overlay._pulse(self.now[0]), 1.0)
        self.assertEqual(self.updates(20), 0)

    def test_a_plain_status_plate_also_stops(self):
        self.tracker.snapshot = model.ViewModel(phase="draft", phase_at=self.now[0], status="draft")
        self.step(int(window.PULSE * window.PULSES / 0.05) + 2)
        self.assertEqual(self.updates(20), 0)

    def test_the_pulse_ends_where_it_started(self):
        self.overlay.revealed_at = 0.0
        self.assertEqual(self.overlay._pulse(0.0), 1.0)
        self.assertAlmostEqual(self.overlay._pulse(window.PULSE * window.PULSES - 1e-6), 1.0, 3)

    def test_a_stopped_seam_needs_no_frames_of_its_own(self):
        self.settle_in_draft()
        self.tracker.snapshot = self.view("loading")
        self.step(2)
        self.focus[0] = False
        self.step(10)
        self.assertFalse(self.overlay.frame.isActive())


class GrowTests(WindowCase):
    def test_in_the_draft_rows_arrive_from_the_end_the_panel_grows_from(self):
        self.tracker.snapshot = model.ViewModel(phase="draft", phase_at=self.now[0], status="draft")
        self.step(10)
        self.tracker.snapshot = self.view("draft")
        self.step(1)
        self.assertTrue(self.overlay.rows_reverse)
        self.now[0] += window.GROW_ROWS_AFTER + 0.05
        motion_now = self.overlay._motion(self.now[0])
        self.assertGreater(motion_now.row(9), motion_now.row(0))

    def test_on_the_loading_screen_rows_arrive_from_the_top(self):
        self.tracker.snapshot = model.ViewModel(
            phase="loading", phase_at=self.now[0], status="match"
        )
        self.step(10)
        self.tracker.snapshot = self.view("loading")
        self.step(1)
        self.assertFalse(self.overlay.rows_reverse)

    def test_the_status_keeps_its_brightness_as_the_panel_takes_over(self):
        self.tracker.snapshot = model.ViewModel(phase="draft", phase_at=self.now[0], status="draft")
        self.step(10)
        self.tracker.snapshot = self.view("draft")
        expected = paint.status_alpha(self.overlay._pulse(self.now[0] + 0.05))
        self.step(1)
        self.assertAlmostEqual(self.overlay.grow[3], expected, places=2)

    def test_the_draft_stays_quiet_and_the_loading_screen_may_be_loud(self):
        self.settle_in_draft()
        quiet = self.overlay._motion(self.now[0])
        self.assertFalse(quiet.loud)
        self.assertLessEqual(quiet.rise, 4)
        self.tracker.snapshot = self.view("loading")
        self.step(16)
        self.assertTrue(self.overlay._motion(self.now[0]).loud)


class MenuTests(WindowCase):
    def test_the_badge_strikes_once_on_a_fresh_reveal(self):
        self.step(2)
        self.assertIsNotNone(self.overlay.badge_at)
        self.assertTrue(self.overlay._animating(self.now[0]))
        self.step(30)
        struck = self.overlay.badge_at
        self.focus[0] = False
        self.step(8)
        self.focus[0] = True
        self.step(4)
        self.assertEqual(self.overlay.badge_at, struck)  # the same reveal: no replay


class CardMotionTests(WindowCase):
    def setUp(self):
        super().setUp()
        view = window.preview_view("loading", phase_at=self.now[0] - 500, version=1)
        self.card_view = dataclasses.replace(view, started_at=self.now[0] - 400)
        self.tracker.snapshot = self.card_view
        left, top, scale = self.overlay._frame()

        def at(team, index):
            x = scoreboard.PANELS[team][0] + 50
            return QPoint(
                round(left + x * scale), round(top + (scoreboard.ROW_TOPS[index] + 80) * scale)
            )

        self.at = at
        self.cursor[0] = at("ally", 0)
        ids = {("ally", i): p.player_id for i, p in enumerate(self.card_view.allies)}
        ids.update({("enemy", i): p.player_id for i, p in enumerate(self.card_view.enemies)})
        self.overlay.reader.wanted = lambda match_id: False
        self.overlay.reader.player_at = lambda match_id, row: (
            {"player_id": ids[row], "hero": "STEEL", "role": "OFFLANE"} if row in ids else None
        )
        self.step(2)

    def press_tab(self):
        self.tracker.tab_held = True
        self.step(2)

    def test_no_reading_retry_while_a_real_card_covers_the_board(self):
        self.press_tab()
        self.step(10)
        self.overlay.reader.wanted = lambda match_id: True
        with mock.patch.object(self.overlay, "_read_scoreboard") as read:
            self.step(5)
        read.assert_not_called()

    def test_the_reading_skeleton_does_not_hold_the_reading_back(self):
        self.overlay.reader.wanted = lambda match_id: True
        self.overlay.reader.player_at = lambda match_id, row: None
        with mock.patch.object(self.overlay, "_read_scoreboard") as read:
            self.press_tab()
            self.step(10)
        read.assert_called()

    def test_the_table_comes_in_when_the_card_first_opens(self):
        self.press_tab()
        self.assertIsNotNone(self.overlay.card_reveal)
        self.paint_now()
        self.assertLessEqual(paint.TABLE_TIME, 0.35)

    def test_flying_to_another_row_does_not_replay_the_table(self):
        self.press_tab()
        self.step(10)
        revealed = self.overlay.card_reveal
        self.cursor[0] = self.at("ally", 1)
        self.step(2)
        self.assertEqual(self.overlay.card_reveal, revealed)
        self.assertIsNotNone(self.overlay.card_prev)

    def test_only_the_next_tab_press_opens_it_again(self):
        self.press_tab()
        self.step(10)
        first = self.overlay.card_reveal
        self.tracker.tab_held = False
        self.step(10)
        self.press_tab()
        self.assertGreater(self.overlay.card_reveal, first)

    def test_the_other_team_takes_an_eased_flight_and_the_bar_changes_with_it(self):
        self.press_tab()
        self.step(10)
        self.cursor[0] = self.at("enemy", 0)
        self.step(1)
        self.assertEqual(self.overlay.card_move[2], window.CARD_CROSS)
        self.assertIs(self.overlay.card_move[3], window._EXIT)
        self.assertEqual(self.overlay.card_bar[0], "ally")
        self.paint_now()
        self.step(10)
        self.assertIsNone(self.overlay.card_bar)
        self.assertIsNone(self.overlay.card_move)

    def test_a_different_height_resizes_smoothly(self):
        self.press_tab()
        self.step(10)
        taller = dataclasses.replace(self.card_view.allies[1], seasons=())
        view = dataclasses.replace(
            self.card_view,
            allies=(self.card_view.allies[0], taller) + self.card_view.allies[2:],
            version=2,
        )
        self.tracker.snapshot = view
        self.cursor[0] = self.at("ally", 1)
        heights = []
        for _ in range(10):
            self.overlay._frame_step()
            self.now[0] += 0.016
            self.overlay.tick()
            heights.append(self.overlay.height())
        self.assertEqual(heights, sorted(heights, reverse=True))
        self.assertGreater(len(set(heights)), 2)


class PaintTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        self.image = QImage(700, 700, QImage.Format.Format_ARGB32)
        self.image.fill(0)
        self.painter = QPainter(self.image)
        self.addCleanup(self.painter.end)

    def test_a_table_mid_reveal_paints_and_leaves_opacity_alone(self):
        from predoverlay.overlay.rows import row

        view = window.preview_view("draft", 0.0, 1)
        data = row(view.allies[0])
        self.painter.setOpacity(0.5)
        paint.paint_card(self.painter, data, paint.Motion(), reveal=0.05)
        self.assertAlmostEqual(self.painter.opacity(), 0.5)

    def test_a_ladder_fills_from_the_left(self):
        paint._ladder(self.painter, 10, 10, "Gold II", "Platinum I", fill=0.2)
        self.painter.end()
        lit = self.image.pixelColor(10 + 1, 10 + 10)
        dark = self.image.pixelColor(10 + paint._pip_x(7) + 1, 10 + 10)
        self.assertGreater(lit.alpha(), dark.alpha())
        self.painter.begin(self.image)

    def test_a_rule_in_flight_carries_a_tip(self):
        paint.gold_rule(self.painter, 0, 600, 20, 0.5)
        self.painter.end()
        tip = self.image.pixelColor(450 - 2, 20)
        self.assertGreater(tip.red(), 150)
        self.painter.begin(self.image)

    def test_mix_times_cover_every_one_shot(self):
        self.assertEqual(paint.mix_time("strike"), paint.STRIKE)
        self.assertEqual(paint.mix_time("glint"), paint.GLINT)
        self.assertEqual(paint.mix_time("vp"), paint.SWAP_DONE)
        self.assertEqual(paint.mix_time("name"), paint.SWAP_IN_AT + paint.SWAP_IN)

    def test_easing_ends(self):
        self.assertEqual(motion.out_quart(1.0), 1.0)
        self.assertEqual(motion.clip(-1), 0.0)


if __name__ == "__main__":
    unittest.main()
