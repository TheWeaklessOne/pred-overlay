import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from predoverlay import app
from predoverlay.overlay import capture


def view(phase="draft", phase_at=100.0, ended_at=0.0):
    return SimpleNamespace(phase=phase, phase_at=phase_at, ended_at=ended_at)


def moment(v, tab_since=None, foreground=True, loading=False, in_match=False):
    return capture.moment(
        v,
        tab_since=tab_since,
        foreground=foreground,
        on_loading_screen=loading,
        in_match=in_match,
    )


class MomentTests(unittest.TestCase):
    def test_draft_loading_tab_and_end(self):
        self.assertEqual(moment(view()), ("draft", ("draft", 100.0), 100.0))
        self.assertEqual(moment(view("loading"), loading=True)[0], "loading")
        self.assertIsNone(moment(view("loading"), in_match=True))  # no TAB, no end
        self.assertEqual(moment(view("loading"), tab_since=150.0, in_match=True)[2], 150.0)
        self.assertEqual(moment(view("loading", ended_at=900.0), in_match=True)[0], "end")

    def test_nothing_while_the_game_is_not_in_front_or_in_the_menu(self):
        self.assertIsNone(moment(view(), foreground=False))
        self.assertIsNone(moment(view("menu")))


class PlannerTests(unittest.TestCase):
    def test_first_after_a_delay_then_spaced_and_capped(self):
        planner = capture.Planner()
        first, every, most = capture.MOMENTS["draft"]
        key = ("draft", 0.0)
        self.assertFalse(planner.due("draft", key, 0.0, first - 0.1))
        taken = [t for t in range(0, 400) if planner.due("draft", key, 0.0, float(t))]
        self.assertEqual(len(taken), most)
        self.assertGreaterEqual(min(b - a for a, b in zip(taken, taken[1:])), every)

    def test_a_new_occurrence_starts_over(self):
        planner = capture.Planner()
        self.assertTrue(planner.due("end", ("end", 1.0), 1.0, 5.0))
        self.assertTrue(planner.due("end", ("end", 2.0), 2.0, 5.0))


class PruneTests(unittest.TestCase):
    def test_keeps_the_newest(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            for n in range(5):
                (folder / f"2026-{n}.png").write_bytes(b"x")
            capture.prune(folder, keep=2)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["2026-3.png", "2026-4.png"])


class FlagTests(unittest.TestCase):
    def test_only_a_hidden_flag_turns_it_on(self):
        self.assertFalse(app.parser().parse_args([]).capture)
        self.assertTrue(app.parser().parse_args(["--capture"]).capture)
        self.assertNotIn("--capture", app.parser().format_help())


if __name__ == "__main__":
    unittest.main()
