"""The shell around the roster on a stranger's PC: commands that fail, an update that
does not start, shortcuts that cannot be made, a profile path in a report."""

import argparse
import io
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from predoverlay import app, autostart, diagnostics, install, predgg
from predoverlay.errors import PredError, TransportError


class OneShotErrors(unittest.TestCase):
    """A command that prints and exits says an error as a line: a message box would wait
    for a click nobody is there to make (a script, a build server)."""

    def run_failing(self, argv, error):
        with (
            mock.patch.object(app, "_console"),
            mock.patch.object(app, "_fatal") as box,
            mock.patch.object(app, "quit_running", side_effect=error),
            mock.patch.object(app, "self_test", side_effect=error),
            mock.patch("sys.stderr", new=io.StringIO()) as printed,
        ):
            try:
                code = app.main(argv)
            except Exception as exc:  # an unexpected one is raised on, with its traceback
                code = exc
        return code, box, printed.getvalue()

    def test_a_known_error_is_one_line_for_every_such_command(self):
        for argv in (["--quit"], ["--self-test"]):
            code, box, printed = self.run_failing(argv, PredError("nope"))
            self.assertEqual((code, printed.strip()), (1, "error: nope"), argv)
            box.assert_not_called()

    def test_an_unexpected_error_is_raised_without_a_box(self):
        for argv in (["--quit"], ["--self-test"]):
            code, box, _ = self.run_failing(argv, RuntimeError("boom"))
            self.assertIsInstance(code, RuntimeError, argv)
            box.assert_not_called()

    def test_without_a_console_an_unexpected_error_is_an_exit_code(self):
        # The setup runs the windowed exe with no console: a traceback would be a box
        # waiting for a click. 2 also tells the setup "failed", not "still running" (1).
        with (
            mock.patch.object(app, "_console"),
            mock.patch.object(app, "_fatal") as box,
            mock.patch.object(app, "quit_running", side_effect=RuntimeError("boom")),
            mock.patch("sys.stderr", new=None),
        ):
            self.assertEqual(app.main(["--quit"]), 2)
        box.assert_not_called()

    def test_every_one_shot_flag_counts(self):
        flags = app.parser().parse_args([])
        for name in (
            "check",
            "sign_out",
            "sign_in",
            "self_test",
            "uninstall",
            "diagnostics",
            "quit",
        ):
            args = argparse.Namespace(**{**vars(flags), name: True})
            self.assertTrue(app._one_shot(args), name)
        self.assertFalse(app._one_shot(flags))


class UpdateLeftovers(unittest.TestCase):
    def test_the_previous_version_stays_until_the_new_one_has_started(self):
        # The update script puts the old folder back when the new version does not
        # start; the new version used to delete that folder first thing.
        with mock.patch("predoverlay.updater.cleanup") as cleanup:
            app._clean_leftovers(updated=True)
            cleanup.assert_not_called()
            app._clean_leftovers(updated=False)
            cleanup.assert_called_once_with()

    def test_the_script_stops_a_new_version_that_hangs_before_it_restores(self):
        from predoverlay import updater

        script = updater.SWAP_SCRIPT
        started = script.index("-PassThru")
        stop = script.index("Stop-Process -InputObject $started")
        restore = script.index("Move-Item -LiteralPath $app -Destination $failed")
        self.assertLess(started, stop)
        self.assertLess(stop, restore)


class Shortcuts(unittest.TestCase):
    def test_output_python_cannot_decode_does_not_raise(self):
        # PowerShell answers in the console's code page; a byte the ANSI one lacks left
        # stdout as None, and None + str raised in the tray's constructor.
        for stdout, stderr in ((None, None), ("made", None), (None, "refused")):
            result = mock.Mock(returncode=1, stdout=stdout, stderr=stderr)
            with mock.patch.object(install.subprocess, "run", return_value=result) as run:
                ok, output = install.powershell("exit 1")
            self.assertEqual((ok, output), (False, (stdout or "") + (stderr or "")))
            self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
            self.assertEqual(run.call_args.kwargs["errors"], "replace")
            self.assertTrue(os.path.isabs(run.call_args.args[0][0]))  # not found by name

    def test_nothing_in_the_first_run_setup_stops_the_start(self):
        logged, settings = [], {}
        with (
            mock.patch.object(install, "supported", return_value=True),
            mock.patch.object(install, "_start_menu_shortcut", return_value=Path("nowhere.lnk")),
            mock.patch.object(install, "create_shortcuts", side_effect=TypeError("None + str")),
            mock.patch.object(install.autostart, "enabled", side_effect=ValueError("odd value")),
        ):
            self.assertEqual(install.integrate(settings, logged.append), [])
        self.assertTrue(settings["integrated"])
        self.assertEqual(len(logged), 2)

    def test_lost_settings_do_not_undo_the_users_choices(self):
        # Settings gone, the Start menu shortcut still there: it was set up before. The
        # autostart the user switched off stays off, a deleted desktop shortcut stays gone.
        with tempfile.TemporaryDirectory() as folder:
            shortcut = Path(folder) / install.SHORTCUT
            shortcut.write_bytes(b"lnk")
            settings = {}
            with (
                mock.patch.object(install, "supported", return_value=True),
                mock.patch.object(install, "_start_menu_shortcut", return_value=shortcut),
                mock.patch.object(install, "create_shortcuts") as create,
                mock.patch.object(install.autostart, "set_enabled") as enable,
            ):
                self.assertEqual(install.integrate(settings, lambda line: None), [])
            create.assert_not_called()
            enable.assert_not_called()
            self.assertTrue(settings["integrated"])


class Autostart(unittest.TestCase):
    def test_an_empty_or_broken_run_value_is_repaired(self):
        for value in ("", '"', '"C:\\nowhere\\PredOverlay.exe" --autostart'):
            with (
                mock.patch.object(autostart, "supported", return_value=True),
                mock.patch.object(autostart, "_registered", return_value=value),
                mock.patch.object(autostart, "_write") as repair,
                mock.patch.object(autostart, "set_enabled") as switch,
            ):
                autostart.refresh()
            repair.assert_called_once_with(True)
            # Only the value: set_enabled would also clear Task Manager's "disabled" mark,
            # and a moved folder is no reason to undo that choice.
            switch.assert_not_called()

    def test_a_value_that_names_an_existing_program_is_left_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            program = Path(folder) / "PredOverlay.exe"
            program.write_bytes(b"")
            with (
                mock.patch.object(autostart, "supported", return_value=True),
                mock.patch.object(
                    autostart, "_registered", return_value=f'"{program}" --autostart'
                ),
                mock.patch.object(autostart, "set_enabled") as repair,
            ):
                autostart.refresh()
            repair.assert_not_called()


class Scrub(unittest.TestCase):
    def test_the_profile_path_goes_however_its_backslashes_are_written(self):
        profile = str(Path.home())
        if "\\" not in profile:
            self.skipTest("no backslashes in this profile path")
        once = profile.replace("\\", "\\\\")  # an error's repr
        twice = once.replace("\\", "\\\\")  # that repr inside JSON
        for written in (profile, profile.replace("\\", "/"), once, twice):
            text = diagnostics.scrub(f"FileNotFoundError: '{written}\\x' is missing")
            self.assertNotIn(Path.home().name, text, written)
            self.assertIn("%USERPROFILE%", text)


class SignInCause(unittest.TestCase):
    def test_a_network_failure_at_the_token_endpoint_names_its_kind(self):
        failure = urllib.error.URLError(ConnectionResetError("secret text never shown"))
        opener = mock.Mock()
        opener.open.side_effect = failure
        with mock.patch.object(predgg.urllib.request, "urlopen", side_effect=failure):
            with self.assertRaises(TransportError) as caught:
                predgg._post_form({"grant_type": "refresh_token"}, opener=opener)
        self.assertEqual(caught.exception.cause, "ConnectionResetError")
        self.assertIn("ConnectionResetError", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))


class TrayUpdates(unittest.TestCase):
    def setUp(self):
        try:
            from predoverlay import tray
        except ImportError:
            self.skipTest("PySide6 is not installed")
        self.tray = tray

    def fake(self, **fields):
        release = {"tag": "v9.9.9"}
        updater_state = mock.Mock(state="ready", release=release, skip=None, busy=False, error=None)
        updater_state.due.return_value = False
        values = dict(
            updater=updater_state,
            settings={"auto_update": True},
            was_busy=False,
            manual_check=False,
            notified={("ready", "v9.9.9")},
            install_when_ready=False,
            signing_in=False,
        )
        values.update(fields)
        return mock.Mock(**values)

    def test_an_update_does_not_restart_the_app_in_the_middle_of_a_sign_in(self):
        view = mock.Mock(phase="closed")
        waiting = self.fake(signing_in=True)
        self.tray.Tray._sync_updates(waiting, view)
        waiting._install.assert_not_called()
        idle = self.fake()
        self.tray.Tray._sync_updates(idle, view)
        idle._install.assert_called_once_with()

    def test_an_install_the_user_asked_for_goes_through_during_a_sign_in(self):
        asked = self.fake(signing_in=True, install_when_ready=True)
        self.tray.Tray._sync_updates(asked, mock.Mock(phase="draft"))
        asked._install.assert_called_once_with()

    def test_a_download_the_user_asked_for_that_fails_says_so(self):
        # Through the real updater: a failed download goes back to "available", not to
        # "error", and the wish to install must not survive it (the next download, an
        # hour later, would then install in the middle of a match).
        from predoverlay import updater

        real = updater.Updater(lambda line: None)
        real.release, real.state = {"tag": "v9.9.9"}, "downloading"
        real.due = lambda: False
        with mock.patch.object(updater, "download", side_effect=OSError("checksum mismatch")):
            real._download()
        self.assertEqual(real.state, "available")
        failed = self.fake(install_when_ready=True, was_busy=True, updater=real, notified=set())
        self.tray.Tray._sync_updates(failed, mock.Mock(phase="menu"))
        self.assertIn("checksum mismatch", failed.notify.call_args_list[0].args[1])
        self.assertFalse(failed.install_when_ready)
        failed._install.assert_not_called()
        real.state = "ready"  # the next hourly download, during a match
        self.tray.Tray._sync_updates(failed, mock.Mock(phase="loading"))
        failed._install.assert_not_called()

    def test_clicking_install_marks_the_download_as_running(self):
        clicked = self.fake(install_when_ready=False)
        clicked.updater.state = "available"
        with mock.patch.object(self.tray.updater, "installable", return_value=True):
            self.tray.Tray._update_clicked(clicked)
        self.assertTrue(clicked.install_when_ready and clicked.was_busy)
        clicked.updater.fetch.assert_called_once_with()

    def test_a_sign_in_from_the_console_is_seen_at_the_next_look(self):
        with tempfile.TemporaryDirectory() as folder:
            session = Path(folder) / "session.json"
            fake = mock.Mock(
                session_ok=False,
                session_checked=0.0,
                session_stamp=None,
                signing_in=False,
            )
            view = mock.Mock(health="ok", phase="closed")
            signed = mock.Mock()
            with (
                mock.patch.object(self.tray, "default_session", return_value=session),
                mock.patch.object(self.tray.predgg, "Session", return_value=signed),
                mock.patch.object(self.tray.time, "monotonic", return_value=1.0),
            ):
                signed.signed_in.return_value = False
                self.assertEqual(self.tray.Tray._health(fake, view), "login")
                looks = signed.signed_in.call_count
                self.assertEqual(self.tray.Tray._health(fake, view), "login")
                self.assertEqual(signed.signed_in.call_count, looks)  # nothing changed
                session.write_text("{}", encoding="utf-8")  # PredOverlayCLI --sign-in
                signed.signed_in.return_value = True
                self.assertEqual(self.tray.Tray._health(fake, view), "ok")


if __name__ == "__main__":
    unittest.main()
