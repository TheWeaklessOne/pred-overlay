"""The heartbeat line, and telling a crashed run from a clean stop."""

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from predoverlay import heartbeat


class HomeTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        patch = mock.patch.dict(os.environ, {"PRED_OVERLAY_HOME": self.folder.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.home = Path(self.folder.name)
        (self.home / "logs").mkdir()
        self.crash = self.home / "logs" / "crash.log"


class LineTests(unittest.TestCase):
    def test_line_shows_uptime_counters_and_threads(self):
        counters = {"private": 150 * 2**20, "working_set": 90 * 2**20, "handles": 812}
        counters.update(gdi=64, user=31)
        with mock.patch.object(heartbeat, "counters", return_value=counters):
            text = heartbeat.line(started=1000, now=1000 + 2 * 3600 + 5 * 60)
        self.assertTrue(text.startswith("heartbeat: up 2h05m, private 150 MB, working set 90 MB"))
        self.assertIn("handles 812, gdi 64, user 31", text)
        self.assertIn("MainThread", text)

    def test_numbered_threads_are_folded(self):
        stop = threading.Event()
        workers = [threading.Thread(target=stop.wait, name=f"Thread-{n} (fetch)") for n in (7, 8)]
        for worker in workers:
            worker.start()
        try:
            self.assertIn("Thread ×2", heartbeat.threads_text())
        finally:
            stop.set()
            for worker in workers:
                worker.join()

    def test_off_windows_the_line_still_works(self):
        with mock.patch.object(heartbeat, "counters", return_value={}):
            text = heartbeat.line(started=0, now=59, label="at stop")
        self.assertTrue(text.startswith("at stop: up 0h00m, threads "))

    def test_counters_on_windows_are_numbers(self):
        found = heartbeat.counters()
        if os.name != "nt":
            self.assertEqual(found, {})
            return
        for key in ("private", "working_set", "handles", "gdi", "user"):
            self.assertIsInstance(found[key], int)
        self.assertGreater(found["private"], 0)


class MarkerTests(HomeTest):
    def test_a_clean_stop_leaves_nothing_to_report(self):
        heartbeat.mark_running(started=1000)
        self.assertTrue(heartbeat.marker_path().exists())
        heartbeat.mark_stopped()
        self.assertIsNone(heartbeat.previous_run())

    def test_an_abrupt_end_with_a_native_crash_names_the_first_new_line(self):
        self.crash.write_text("--- 2026-11-09 Pred Overlay 9.9.9 started\n", encoding="utf-8")
        heartbeat.mark_running(started=1000)
        with open(self.crash, "a", encoding="utf-8") as stream:
            stream.write("Windows fatal exception: access violation\n\nThread 0x1 ...\n")
        text = heartbeat.previous_run()
        self.assertIn("previous run ended abruptly", text)
        self.assertIn(f"pid {os.getpid()}", text)
        self.assertIn("crash.log grew by", text)
        self.assertIn("Windows fatal exception: access violation", text)

    def test_an_abrupt_end_without_a_python_stack(self):
        self.crash.write_text("--- header\n", encoding="utf-8")
        heartbeat.mark_running(started=1000)
        self.assertIn("crash.log did not grow", heartbeat.previous_run())

    def test_a_damaged_marker_still_counts(self):
        heartbeat.marker_path().write_text("{not json", encoding="utf-8")
        text = heartbeat.previous_run()
        self.assertIn("previous run ended abruptly (version ?", text)
        self.assertIn("crash.log not compared", text)

    def test_the_marker_holds_no_more_than_it_needs(self):
        heartbeat.mark_running(started=1000)
        record = json.loads(heartbeat.marker_path().read_text(encoding="utf-8"))
        self.assertEqual(set(record), {"version", "pid", "started", "crash_size", "boot"})

    def test_a_run_that_ended_with_windows_is_not_called_a_crash(self):
        self.crash.write_text("--- header\n", encoding="utf-8")
        with mock.patch.object(heartbeat, "boot_time", return_value=5000.0):
            heartbeat.mark_running(started=6000)
        with mock.patch.object(heartbeat, "boot_time", return_value=90000.0):
            text = heartbeat.previous_run()
        self.assertIn("previous run ended with Windows, not by itself", text)
        self.assertIn(f"pid {os.getpid()}", text)
        self.assertNotIn("abruptly", text)

    def test_the_same_windows_run_or_a_crash_stack_is_still_abrupt(self):
        self.crash.write_text("--- header\n", encoding="utf-8")
        with mock.patch.object(heartbeat, "boot_time", return_value=5000.0):
            heartbeat.mark_running(started=6000)
        for boot in (5000.0, 5000.0 + heartbeat.BOOT_SLACK, None):  # the clock was corrected
            with mock.patch.object(heartbeat, "boot_time", return_value=boot):
                self.assertIn("previous run ended abruptly", heartbeat.previous_run())
        with open(self.crash, "a", encoding="utf-8") as stream:
            stream.write("Windows fatal exception: access violation\n")
        with mock.patch.object(heartbeat, "boot_time", return_value=90000.0):
            text = heartbeat.previous_run()  # it crashed first, whatever Windows did later
        self.assertIn("previous run ended abruptly", text)
        self.assertIn("crash.log grew by", text)

    def test_a_marker_without_the_start_of_windows_is_abrupt(self):
        record = {"version": "0.0.1", "pid": 7, "started": 1000, "crash_size": 0}
        heartbeat.marker_path().write_text(json.dumps(record), encoding="utf-8")
        self.assertIn("previous run ended abruptly (version 0.0.1", heartbeat.previous_run())

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_windows_start_is_in_the_past_and_steady(self):
        first, second = heartbeat.boot_time(), heartbeat.boot_time()
        self.assertLess(first, heartbeat.time.time())
        self.assertLess(abs(first - second), 1.0)


if __name__ == "__main__":
    unittest.main()
