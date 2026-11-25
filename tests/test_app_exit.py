"""How the tray app leaves, and the one-shot sign-in."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from predoverlay import app


class Entry(unittest.TestCase):
    def test_sign_in_runs_the_browser_sign_in_only(self):
        with (
            mock.patch("predoverlay.predgg.sign_in", return_value="Me") as signed,
            mock.patch.object(app, "run_tray") as tray,
            mock.patch.object(app, "_console"),
            mock.patch("builtins.print"),
        ):
            self.assertEqual(app.main(["--sign-in"]), 0)
        signed.assert_called_once()
        tray.assert_not_called()


class Leaving(unittest.TestCase):
    def test_exit_flushes_the_log_and_skips_the_interpreter_teardown(self):
        with (
            mock.patch("logging.shutdown") as flushed,
            mock.patch("os._exit") as exited,
        ):
            app._exit_now(0)
        flushed.assert_called_once()
        exited.assert_called_once_with(0)

    def test_no_injected_modules_are_reported_off_windows(self):
        with mock.patch.object(app.sys, "platform", "linux"):
            self.assertEqual(app._injected(), "")

    def test_the_crash_file_starts_each_run_and_stays_small(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "crash.log"
            path.write_text("x" * 300_000, encoding="utf-8")
            with (
                mock.patch("predoverlay.config.log_dir", return_value=Path(folder)),
                mock.patch("faulthandler.enable") as enabled,
            ):
                stream = app._crash_file("9.9.9")
            stream.close()
            text = path.read_text(encoding="utf-8")
            self.assertLess(len(text), 1000)  # an oversized file starts over
            self.assertIn("Pred Overlay 9.9.9 started", text)
            enabled.assert_called_once()
            self.assertTrue(enabled.call_args.kwargs["all_threads"])


class Starting(unittest.TestCase):
    def test_an_updated_launch_waits_for_the_old_copy_to_let_go(self):
        guard = mock.Mock()
        guard.acquire.side_effect = [False, False, True]
        naps = []
        self.assertTrue(app._acquire(guard, patient=True, sleep=naps.append))
        self.assertEqual(len(naps), 2)

    def test_a_normal_second_launch_leaves_at_once(self):
        guard = mock.Mock()
        guard.acquire.return_value = False
        naps = []
        self.assertFalse(app._acquire(guard, patient=False, sleep=naps.append))
        self.assertEqual((guard.acquire.call_count, naps), (1, []))

    def test_patience_runs_out(self):
        guard = mock.Mock()
        guard.acquire.return_value = False
        with mock.patch("time.monotonic", side_effect=[0, 0, 4, 11]):
            self.assertFalse(app._acquire(guard, patient=True, wait=10, sleep=lambda _: None))
        self.assertEqual(guard.acquire.call_count, 3)

    def test_the_update_marker_is_written_and_logged(self):
        lines = []
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict("os.environ", {"PRED_OVERLAY_HOME": folder}):
                app._mark_started(lines.append)
                self.assertTrue((Path(folder) / "updates" / "started").exists())
        self.assertEqual(lines, ["updated version is up; told the update script"])

    def test_the_old_match_cache_is_removed_once_and_said(self):
        lines = []
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "match-cache"
            cache.mkdir()
            (cache / "1.json").write_text('{"players": []}', encoding="utf-8")
            with mock.patch.dict("os.environ", {"PRED_OVERLAY_HOME": folder}):
                app._drop_match_cache(lines.append)
                self.assertFalse(cache.exists())
                app._drop_match_cache(lines.append)  # nothing left: nothing said
        self.assertEqual(lines, ["removed the match cache older versions kept"])

    def test_a_match_cache_that_will_not_go_is_left_quietly(self):
        lines = []
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "match-cache").mkdir()
            with (
                mock.patch.dict("os.environ", {"PRED_OVERLAY_HOME": folder}),
                mock.patch("shutil.rmtree") as removed,
            ):
                app._drop_match_cache(lines.append)
        removed.assert_called_once()
        self.assertTrue(removed.call_args.kwargs["ignore_errors"])
        self.assertEqual(lines, [])

    def test_a_marker_that_cannot_be_written_is_logged_not_raised(self):
        lines = []
        with mock.patch("predoverlay.updater.mark_started", side_effect=OSError("denied")):
            app._mark_started(lines.append)
        self.assertEqual(lines, ["update marker not written: denied"])

    def test_the_mutex_handle_is_closed_when_another_copy_owns_it(self):
        from predoverlay import single_instance

        kernel32 = mock.Mock()
        kernel32.CreateMutexW.return_value = 1234
        with (
            mock.patch.object(single_instance.sys, "platform", "win32"),
            mock.patch.object(single_instance.ctypes, "WinDLL", return_value=kernel32, create=True),
            mock.patch.object(
                single_instance.ctypes,
                "get_last_error",
                return_value=single_instance.ERROR_ALREADY_EXISTS,
                create=True,
            ),
        ):
            guard = single_instance.Guard()
            self.assertFalse(guard.acquire())
        kernel32.CloseHandle.assert_called_once()
        self.assertIsNone(guard.handle)


class Ending(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        patch = mock.patch.dict("os.environ", {"PRED_OVERLAY_HOME": self.folder.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.lines = []

    def ending(self, reason=None):
        from predoverlay import heartbeat

        heartbeat.mark_running(started=1000)
        return app.Ending(self.lines.append, 1000, lambda: reason)

    def stopped(self):
        return [line for line in self.lines if line.startswith("Pred Overlay stopped")]

    def test_the_end_of_a_windows_session_is_a_clean_stop(self):
        from predoverlay import heartbeat

        ending = self.ending()
        ending.session_ending()
        ending.finish()  # from aboutToQuit: the process is ended right after it
        self.assertEqual(self.lines[0], "Windows session ending")
        self.assertIn("at stop: up", self.lines[1])
        self.assertIn("Windows ended the session", self.stopped()[0])
        self.assertIsNone(heartbeat.previous_run())

    def test_the_last_lines_are_written_once(self):
        ending = self.ending("asked to by --quit")
        ending.finish()  # from aboutToQuit
        ending.finish()  # after the loop
        self.assertEqual(len(self.stopped()), 1)
        self.assertIn("asked to by --quit", self.stopped()[0])

    def test_the_apps_own_reason_wins_and_no_reason_is_from_outside(self):
        ending = self.ending("update")
        ending.session_ending()
        ending.finish()
        self.assertIn("stopped: update", self.stopped()[0])
        self.lines.clear()
        self.ending().finish()
        self.assertIn("closed from outside", self.stopped()[0])

    def test_the_session_window_exists_and_is_never_shown(self):
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            self.skipTest("PySide6 is not installed")
        _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])  # noqa: F841
        window = app._session_window()
        self.assertIsNotNone(window.windowHandle())  # the native window is there
        self.assertFalse(window.isVisible())


class QuitReason(unittest.TestCase):
    def test_the_tray_names_why_it_quits(self):
        try:
            from predoverlay import tray
        except ImportError:
            self.skipTest("PySide6 is not installed")
        fake = mock.Mock(spec=["timer", "_close_preview", "runtime", "tray", "app"])
        tray.Tray.quit(fake, "update")
        self.assertEqual(fake.quit_reason, "update")
        fake.app.quit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
