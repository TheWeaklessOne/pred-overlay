"""The preview's TAB stage: a sample card, and nothing of the screen read.

The preview runs over whatever program is in front (the tray's Show preview, --preview).
Its TAB stage used to run the real board reader: it grabbed the name areas of that
program's window, ran OCR over them and showed "could not be read" instead of a card.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class PreviewTab(unittest.TestCase):
    def setUp(self):
        from predoverlay.overlay import window

        self.window = window
        self.app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        self.now = [50_000.0]
        for patcher in (
            mock.patch.object(window.win32, "game_in_front", lambda: False),
            mock.patch.object(window.win32, "key_down", lambda code: False),
            mock.patch.object(window.win32, "keep_on_top", lambda hwnd: None),
            mock.patch.object(window.win32, "make_inert", lambda hwnd: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_tab_stage_shows_a_sample_card_without_reading_the_screen(self):
        window = self.window
        tracker = window.PreviewTracker(clock=lambda: self.now[0])
        overlay = window.Overlay(tracker, clock=lambda: self.now[0], force_foreground=True)
        self.addCleanup(overlay.close)
        grabs, readings, cards = [], [], []
        screen = type(overlay._screen())
        with (
            mock.patch.object(
                screen, "grabWindow", lambda *args, **more: grabs.append(args) or 1 / 0
            ),
            mock.patch.object(
                window.scoreboard.Reader, "start", lambda *args, **more: readings.append(args)
            ),
        ):
            for _ in range(int(4 * tracker.CYCLE / 0.05)):  # the whole cycle, once
                self.now[0] += 0.05
                overlay.tick()
                if tracker.tab_held and overlay.layout_name == "card" and overlay.card:
                    cards.append(overlay.card)
        self.assertEqual(overlay.failures, {})
        self.assertEqual((grabs, readings), ([], []))
        self.assertTrue(cards)
        self.assertTrue(all(card.data is not None for card in cards))  # a player, no skeleton
        self.assertEqual({card.hit for card in cards}, {("enemy", 1)})
        shown = tracker.board_row(("enemy", 1))
        self.assertEqual(cards[-1].seen, {"hero": shown["hero"], "role": shown["role"]})

    def test_every_cycle_shows_every_stage(self):
        # --preview runs until Ctrl+C: each stage's time must restart with its cycle, or
        # the loading screen of a late cycle is "long over" and shows nothing.
        from predoverlay.overlay.visibility import LOADING_SCREEN

        tracker = self.window.PreviewTracker(clock=lambda: self.now[0])
        cycle = 4 * tracker.CYCLE
        for turn in (0, 1, 9):
            self.now[0] = tracker.started + turn * cycle + tracker.CYCLE + 1  # the loading stage
            view = tracker.snapshot
            self.assertEqual(view.phase, "loading")
            self.assertLess(self.now[0] - view.phase_at, LOADING_SCREEN, turn)

    def test_the_trays_preview_is_one_cycle_long(self):
        from predoverlay import tray

        self.assertEqual(tray.PREVIEW_SECONDS, 4 * self.window.PreviewTracker.CYCLE)

    def test_outside_the_tab_stage_the_preview_points_nowhere(self):
        tracker = self.window.PreviewTracker(clock=lambda: self.now[0])
        tracker.snapshot  # the draft stage
        self.assertIsNone(tracker.pointing)
        self.assertIsNone(tracker.board_row(("enemy", 9)))


if __name__ == "__main__":
    unittest.main()
