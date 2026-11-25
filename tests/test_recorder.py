"""Matches recorded locally and replayed through the core; the reading ledger; --quit."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_core import DRAFT_NAMES, ME, T0, draft_texts, loading_texts

from predoverlay.overlay import core, recorder, replay, tracker

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TOP = ["Alpha1", "Bravo2", "Charlie3", "Delta4", "Echo5"]
BOTTOM = ["wanzogoduke_", "hulgarventas4", "BrenK141", "SilverFalcon", "UrVak"]


class CodecTests(unittest.TestCase):
    def test_the_core_values_come_back_as_they_were(self):
        value = {
            ("ally", 0): ["A", "A"],
            "set": {("x", 1), ("y", 2)},
            "tuple": (1, "two", None),
            "nested": [{"k": ({1, 2},)}],
        }
        again = recorder.decode(json.loads(json.dumps(recorder.encode(value))))
        self.assertEqual(again, value)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


class RecordedBase(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.clock = Clock()
        self.rec = recorder.Recorder(self.folder)
        self.tracker = tracker.Tracker(
            {}, clock=self.clock, log=lambda m: None, game_running=lambda: True, recorder=self.rec
        )
        self.tracker.state.game_running = True

    def tearDown(self):
        self.rec.close()

    def feed(self, *event, dt=0.5):
        self.clock.now += dt
        self.tracker.handle(event)

    def play_match(self):
        self.feed("display", {"Steel": "Steel"})
        self.feed("log", {"phase": "accepted", "at_ts": self.clock.now})
        self.feed(
            "log",
            {
                "phase": "draft",
                "at_ts": self.clock.now,
                "user_id": ME,
                "user_name": "SilverFalcon",
            },
        )
        self.feed(
            "screen",
            "draft",
            self.tracker.state.phase_at,
            draft_texts(DRAFT_NAMES),
            {"ms": 70.0, "n": 1},
        )
        self.feed(
            "found", self.tracker.state.epoch, {"a1": {"player_id": "p1", "name": "hulgarventas4"}}
        )
        self.feed(
            "tick",
        )
        self.feed("log", {"phase": "loading", "at_ts": self.clock.now, "map": "TwilightRC1"})
        self.feed(
            "screen",
            "loading",
            self.tracker.state.phase_at,
            loading_texts(TOP, BOTTOM),
            {"ms": 300.0, "n": 1},
        )
        self.feed("profiles", self.tracker.state.epoch, {ME: {"name": "SilverFalcon", "stats": {}}})


class RecordedMatchTests(RecordedBase):
    def test_one_folder_per_match_and_the_replay_gives_the_same_commands(self):
        self.play_match()
        (match,) = list(self.folder.iterdir())
        rows = list(replay.run(match))
        self.assertTrue(rows)
        for _, event, recorded, replayed, _ in rows:
            self.assertEqual(list(recorded), list(replayed), event[0])
        final = rows[-1][-1]
        self.assertEqual(core.view(self.tracker.state).allies, final.allies)
        self.assertEqual(len(final.enemies), 5)

    def test_a_payload_the_core_changes_is_recorded_as_it_came(self):
        self.play_match()
        (match,) = list(self.folder.iterdir())
        profiles = [e for _, e, _ in recorder.read(match)[1] if e[0] == "profiles"]
        self.assertNotIn("at", profiles[-1][2][ME]["stats"])  # the core stamps it later

    def test_the_menu_idles_unrecorded_and_old_matches_go(self):
        self.rec.keep = 2
        for _ in range(3):
            self.play_match()
            self.feed("log", {"phase": "menu", "at_ts": self.clock.now})
            for _ in range(5):
                self.feed(
                    "tick",
                )
            self.clock.now += 2  # one folder a second at most
        self.assertEqual(len(list(self.folder.iterdir())), 2)
        last = sorted(self.folder.iterdir())[-1]
        events = [e for _, e, _ in recorder.read(last)[1]]
        self.assertEqual(events[-1][0], "log")  # the menu's ticks were left out


class ChecksTests(RecordedBase):
    def test_a_good_match_passes_and_a_missed_loading_screen_fails(self):
        self.play_match()
        (match,) = list(self.folder.iterdir())
        measured = replay.measure(match)
        self.assertEqual(measured["differ"], 0)
        self.assertIsNotNone(measured["seated_after"])
        self.assertTrue(all(passed for _, passed, _ in replay.checks(measured)))
        self.assertTrue(measured["same_build"])
        late = dict(measured, seated_after=None)  # the loading screen gave nobody
        failed = [name for name, passed, _ in replay.checks(late) if not passed]
        self.assertEqual(failed, ["every player seated soon after the loading screen"])

    def test_a_core_that_takes_no_reading_fails_on_any_recording(self):
        # The gate must be able to fail for the build it gates: a recording of another
        # version gave only notes, so a core that seated nobody passed.
        import json
        from unittest import mock

        self.play_match()
        (match,) = list(self.folder.iterdir())
        header = json.loads((match / "state.json").read_text(encoding="utf-8"))
        header["version"] = "0.0.1"
        (match / "state.json").write_text(json.dumps(header), encoding="utf-8")
        self.assertEqual(replay.suite(self.folder)[1], 0)
        with mock.patch.object(core, "_on_screen", lambda state, kind, key, texts: False):
            lines, failures = replay.suite(self.folder)
        self.assertEqual(failures, 1)
        self.assertTrue([line for line in lines if line.startswith("FAIL") and replay.FULL in line])

    def test_the_retries_of_an_outage_are_not_counted_as_searches(self):
        self.play_match()
        (match,) = list(self.folder.iterdir())
        counted = replay.measure(match)["searches"]
        with mock.patch.object(core, "gave_up", lambda state, key: True):
            self.assertEqual(replay.measure(match)["searches"], 0)
        self.assertGreater(counted, 0)

    def test_a_full_reading_is_what_the_hard_check_judges(self):
        self.play_match()
        (match,) = list(self.folder.iterdir())
        measured = replay.measure(match)
        self.assertEqual(measured["given"], 10)
        self.assertEqual(measured["seated"], (5, 5))
        names = [name for name, _, _ in replay.checks(measured)]
        self.assertIn(replay.FULL, names)
        self.assertNotIn(replay.FULL, [n for n, _, _ in replay.checks(dict(measured, given=9))])

    def test_the_suite_runs_every_recorded_match(self):
        self.play_match()
        lines, failures = replay.suite(self.folder)
        self.assertEqual(failures, 0)
        self.assertEqual(len(lines), 1)


class RecorderEdgeTests(RecordedBase):
    def test_two_matches_begun_within_a_second_get_two_folders(self):
        self.play_match()
        self.feed("log", {"phase": "menu", "at_ts": self.clock.now}, dt=0.1)
        self.play_match()  # the same second, by the clock
        self.assertEqual(len(list(self.folder.iterdir())), 2)

    def test_a_match_of_another_version_is_a_note_not_a_failure(self):
        import json

        self.play_match()
        (match,) = list(self.folder.iterdir())
        header = json.loads((match / "state.json").read_text(encoding="utf-8"))
        header["version"] = "0.0.1"
        (match / "state.json").write_text(json.dumps(header), encoding="utf-8")
        events = (match / "events.jsonl").read_text(encoding="utf-8").splitlines()
        screens = [i for i, line in enumerate(events) if '"screen"' in line]
        for i in screens:  # as if no loading screen had been read: a seats check fails
            events[i] = ""
        (match / "events.jsonl").write_text("\n".join(events), encoding="utf-8")
        lines, failures = replay.suite(self.folder)
        self.assertEqual(failures, 0)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.state = core.State()
        core.reduce(self.state, ("display", {"Steel": "Steel"}), T0)
        core.reduce(self.state, ("log", {"phase": "accepted", "at_ts": T0}), T0)
        core.reduce(
            self.state,
            ("log", {"phase": "draft", "at_ts": T0, "user_id": ME, "user_name": "SilverFalcon"}),
            T0,
        )
        self.state.notes.clear()

    def test_a_reading_says_what_it_gave_and_never_a_name(self):
        core.reduce(
            self.state, ("screen", "draft", T0, draft_texts(DRAFT_NAMES), {"ms": 71.4, "n": 3}), T0
        )
        (line,) = self.state.notes
        self.assertEqual(line, "read draft #3: 5 names, 5 sure, 71 ms -> taken")
        for name in DRAFT_NAMES:
            self.assertNotIn(name, line)

    def test_a_reading_turned_down_says_why(self):
        core.reduce(self.state, ("screen", "draft", T0 - 99, draft_texts(DRAFT_NAMES)), T0)
        self.assertEqual(
            self.state.notes, ["read draft: 5 names, 5 sure -> dropped (an earlier phase)"]
        )

    def test_the_same_draft_reading_is_not_repeated(self):
        for _ in range(3):
            core.reduce(self.state, ("screen", "draft", T0, draft_texts(DRAFT_NAMES)), T0)
        self.assertEqual(len([n for n in self.state.notes if n.startswith("read draft")]), 2)


class QuitTests(unittest.TestCase):
    def test_a_quit_message_reaches_the_running_copy(self):
        from PySide6.QtCore import QCoreApplication

        from predoverlay import single_instance

        _app = QCoreApplication.instance() or QCoreApplication([])  # noqa: F841
        with mock.patch.object(single_instance, "_name", lambda: "PredOverlayTest-quit"):
            first = single_instance.Guard()
            self.assertTrue(first.listen())
            got = []
            first.connect(got.append)
            import threading

            sent = []
            thread = threading.Thread(
                target=lambda: sent.append(single_instance.Guard.notify_running(b"quit"))
            )
            thread.start()
            self.assertTrue(first.server.waitForNewConnection(3000))
            first._accept()  # what the newConnection signal does in the running app
            thread.join(3)
            first.server.close()
        self.assertEqual(sent, [True])
        self.assertEqual(got, [b"quit"])


if __name__ == "__main__":
    unittest.main()
