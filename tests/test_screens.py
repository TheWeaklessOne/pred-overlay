"""Which screen shows, from pixels: the loading screen's anchors (screens.py), and the
window's shadow probe that logs and records it."""

import os
import queue
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from predoverlay.overlay import screens  # noqa: E402

DATA = Path(__file__).parent / "data"


class AnchorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A QApplication, not a QGuiApplication: later tests in this file make widgets.
        cls.app = QApplication.instance() or QApplication([])

    def region(self, name, scale=1.0):
        image = QImage(str(DATA / f"screens_{name}.png"))
        self.assertFalse(image.isNull(), name)
        if scale != 1.0:
            image = image.scaled(round(image.width() * scale), round(image.height() * scale))
        return image

    def test_the_loading_screen_is_told_at_every_screen_size(self):
        for scale in (1.0, 0.75, 0.5):
            self.assertTrue(screens.is_loading(self.region("loading", scale)), scale)

    def test_the_draft_and_the_board_are_not_the_loading_screen(self):
        for name in ("draft", "tab"):
            for scale in (1.0, 0.5):
                self.assertLess(screens.loading_score(self.region(name, scale)), 0.15, name)

    def test_a_blank_grab_is_no_loading_screen(self):
        blank = QImage(200, 122, QImage.Format.Format_RGB32)
        blank.fill(0)
        self.assertEqual(screens.loading_score(blank), 0.0)


class ProbeTests(unittest.TestCase):
    def test_the_probe_records_each_change_once(self):
        import dataclasses
        from unittest import mock

        from PySide6.QtWidgets import QApplication

        from predoverlay.overlay import model, window

        _app = QApplication.instance() or QApplication([])  # noqa: F841
        now = [1000.0]
        patches = [
            mock.patch.object(window.win32, "game_in_front", lambda: True),
            mock.patch.object(window.win32, "key_down", lambda code: False),
            mock.patch.object(window.win32, "keep_on_top", lambda hwnd: None),
            mock.patch.object(window.win32, "make_inert", lambda hwnd: None),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        tracker = mock.Mock(snapshot=model.ViewModel(phase="menu", phase_at=now[0]), tab_held=False)
        tracker.events = queue.Queue()
        lines = []
        overlay = window.Overlay(tracker, clock=lambda: now[0], log=lines.append)
        self.addCleanup(overlay.close)
        draft = window.preview_view("draft", phase_at=now[0], version=2)
        tracker.snapshot = dataclasses.replace(draft, enemies=())
        for _ in range(60):  # three seconds of ticks: six looks
            now[0] += 0.05
            overlay.tick()
        seen = []
        while not tracker.events.empty():
            event = tracker.events.get()
            if event[0] == "seen":
                seen.append(event)
        self.assertEqual([e[:3] for e in seen], [("seen", "loading", False)])
        self.assertEqual(sum(line.startswith("screen: loading") for line in lines), 1)


class PixelReadingTests(unittest.TestCase):
    def setUp(self):
        import dataclasses
        from unittest import mock

        from PySide6.QtWidgets import QApplication

        from predoverlay.overlay import model, window

        self._app = QApplication.instance() or QApplication([])
        self.now = [1000.0]
        self.score = [0.0]
        patches = [
            mock.patch.object(window.win32, "game_in_front", lambda: True),
            mock.patch.object(window.win32, "key_down", lambda code: False),
            mock.patch.object(window.win32, "keep_on_top", lambda hwnd: None),
            mock.patch.object(window.win32, "make_inert", lambda hwnd: None),
            mock.patch.object(window.screens, "loading_score", lambda image: self.score[0]),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tracker = mock.Mock(snapshot=model.ViewModel(phase="menu", phase_at=self.now[0]))
        self.tracker.tab_held = False
        self.tracker.events = queue.Queue()
        self.overlay = window.Overlay(self.tracker, clock=lambda: self.now[0])
        self.addCleanup(self.overlay.close)
        self.kinds = []
        test = self

        class Reader:
            busy, error, note, ms = False, None, None, None

            def start(self, crops, done, known=()):
                test.kinds.append(test.overlay.roster_key[0])

        self.overlay.roster_reader = Reader()
        self.overlay._grab_boxes = lambda boxes, encode: lambda: {}
        draft = window.preview_view("draft", phase_at=self.now[0] - 30, version=2)
        self.tracker.snapshot = dataclasses.replace(draft, enemies=())

    def ticks(self, seconds):
        for _ in range(round(seconds / 0.05)):
            self.now[0] += 0.05
            self.overlay.tick()

    def test_the_loading_screen_is_read_when_its_pixels_show_in_the_draft_phase(self):
        self.ticks(4)
        self.assertEqual(set(self.kinds), {"draft"})
        self.score[0] = 0.9  # the loading screen is up, and the log has not said so yet
        self.kinds.clear()
        self.ticks(4)
        self.assertIn("loading", self.kinds)

    def test_the_panel_moving_holds_a_reading_back_only_a_moment(self):
        self.overlay._animating = lambda now: True  # a motion that never settles
        self.ticks(4)
        self.assertTrue(self.kinds)


class FinalReviewTests(PixelReadingTests):
    def test_a_loading_screen_past_its_fallback_never_breaks_the_tick(self):
        import dataclasses

        from predoverlay.overlay import window

        lines = []
        self.overlay.log = lines.append
        self.score[0] = 0.9
        loading = window.preview_view("loading", phase_at=self.now[0] - 200, version=3)
        self.tracker.snapshot = dataclasses.replace(loading, enemies=())
        self.overlay.roster_reader.busy = True  # every tick is skipped and counted
        self.ticks(2)
        self.assertEqual(self.overlay.failures, {})
        self.assertFalse([line for line in lines if "failed" in line])

    def test_another_window_in_front_is_looked_at_again_before_a_reading(self):
        from unittest import mock

        from predoverlay.overlay import window

        self.score[0] = 0.9
        self.ticks(1)  # the loading screen was seen
        self.overlay.probe_at = self.now[0] + 99  # no new look by the probe for a while
        self.score[0] = 0.0  # alt-tab: another program shows now
        self.kinds.clear()
        with mock.patch.object(window.win32, "game_in_front", lambda: False):
            self.ticks(3)
        self.assertEqual(self.kinds, [])


class GhostTests(unittest.TestCase):
    def test_only_the_games_own_ghost_counts_as_the_game(self):
        from predoverlay.overlay import win32

        self.assertEqual(win32.front_kind(("UnrealWindow", "Predecessor")), "game")
        ghost = ("Ghost", "Predecessor (Not Responding)")
        self.assertEqual(win32.front_kind(ghost), "game not responding")
        other = ("Ghost", "Predecessor Wiki - Browser (Not Responding)")
        self.assertEqual(win32.front_kind(other), "Ghost")


if __name__ == "__main__":
    unittest.main()
