"""The draft lists only your own team; the loading screen and the TAB card show both."""

import unittest
from unittest import mock

try:
    from test_motion import WindowCase

    from predoverlay.overlay import paint, window
except ImportError:  # no PySide6
    WindowCase = None


@unittest.skipIf(WindowCase is None, "PySide6 is not installed")
class PanelTeamsTests(unittest.TestCase):
    def test_only_the_draft_drops_the_enemy_team(self):
        view = window.preview_view("draft", 0.0, 1)
        both = (view.allies, view.enemies)
        for phase in ("draft", "accepted"):
            layout = window.choose_layout(phase)
            self.assertEqual(window.panel_teams(view, layout), (view.allies, ()))
        for layout in ("loading", "card", "menu"):
            self.assertEqual(window.panel_teams(view, layout), both)

    def test_a_team_without_rows_takes_no_room_and_gets_no_header(self):
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtWidgets import QApplication

        from predoverlay.overlay.paint import panel

        # A QApplication, not a QGuiApplication: later tests make widgets in this process.
        _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        one = paint.STACK_HDR + 5 * paint.STACK_ROW_H + 4 * paint.STACK_GAP
        _, both = paint.stack_size(5, 5)
        _, alone = paint.stack_size(5, 0)
        self.assertEqual(alone, paint.PAD_TOP + one + paint.PAD_BOTTOM)
        self.assertEqual(both, paint.PAD_TOP + 2 * one + paint.TEAM_GAP + paint.PAD_BOTTOM)
        view = window.preview_view("draft", 0.0, 1)
        rows = [window.row(p) for p in view.allies]
        image = QImage(640, alone, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        try:
            with mock.patch.object(panel, "team_header") as header:
                paint.paint_stack(painter, 640, rows, [], paint.Motion(), "5v5")
        finally:
            painter.end()
        self.assertEqual([c.args[3] for c in header.call_args_list], ["ally"])
        self.assertIsNone(header.call_args.args[6])  # no balance against a hidden team


@unittest.skipIf(WindowCase is None, "PySide6 is not installed")
class DraftWindowTests(WindowCase if WindowCase else unittest.TestCase):
    """The draft panel in the real window."""

    def test_the_draft_lists_only_your_team_and_is_shorter(self):
        self.settle_in_draft()
        self.assertEqual((len(self.overlay.allies), len(self.overlay.enemies)), (5, 0))
        self.assertEqual(self.overlay.design_size[1], paint.stack_size(5, 0)[1])
        self.assertTrue(all(r["team"] == "ally" for r in self.overlay.allies))
        self.paint_now()

    def test_the_loading_screen_shows_both_teams_again(self):
        self.settle_in_draft()
        short = self.overlay.height()
        self.tracker.snapshot = self.view("loading")
        self.step(20)
        self.paint_now()
        self.assertIsNone(self.overlay.seam)
        self.assertEqual(self.overlay.layout_name, "loading")
        self.assertEqual((len(self.overlay.allies), len(self.overlay.enemies)), (5, 5))
        self.assertGreater(self.overlay.height(), short)


if __name__ == "__main__":
    unittest.main()
