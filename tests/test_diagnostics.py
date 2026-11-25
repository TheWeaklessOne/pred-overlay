"""--diagnostics: what goes into the zip, and what never does."""

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from predoverlay import app, diagnostics

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl"  # public-ok: fake


class ZipTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.home, self.out = root / "home", root / "out"
        (self.home / "logs").mkdir(parents=True)
        self.out.mkdir()
        patch = mock.patch.dict(os.environ, {"PRED_OVERLAY_HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        logs = self.home / "logs"
        (logs / "overlay.log").write_text(
            f"starting\nsent Authorization: Bearer {JWT}\nraw {JWT}\n", encoding="utf-8"
        )
        (logs / "overlay.log.1").write_text("older\n", encoding="utf-8")
        (logs / "update.log").write_text("update v0.1.15 applied\n", encoding="utf-8")
        (logs / "crash.log").write_text("--- started\n", encoding="utf-8")
        (logs / "other.txt").write_text("not ours to send\n", encoding="utf-8")
        (self.home / "settings.json").write_text('{"hotkey": "F8"}', encoding="utf-8")
        (self.home / "session.json").write_text(f'{{"token": "{JWT}"}}', encoding="utf-8")

    def build(self):
        checks = [{"check": "steam", "status": "ok", "detail": "running"}]
        with mock.patch("predoverlay.doctor.run", return_value=checks):
            path = diagnostics.build(self.out, events=lambda: "no crash reports", now=0)
        with zipfile.ZipFile(path) as archive:
            return path, {name: archive.read(name).decode("utf-8") for name in archive.namelist()}

    def test_the_zip_holds_logs_settings_and_checks(self):
        path, files = self.build()
        self.assertEqual(path.parent, self.out)
        self.assertTrue(path.name.startswith("PredOverlay-diagnostics-"))
        self.assertEqual(
            set(files),
            {
                "logs/overlay.log",
                "logs/overlay.log.1",
                "logs/update.log",
                "logs/crash.log",
                "settings.json",
                "check.json",
                "about.txt",
                "windows-errors.txt",
            },
        )
        self.assertEqual(json.loads(files["check.json"])[0]["check"], "steam")
        self.assertEqual(files["windows-errors.txt"], "no crash reports")
        self.assertFalse(list(self.out.glob("*.tmp")))

    def test_no_session_tokens_get_in(self):
        _, files = self.build()
        everything = "\n".join(files.values())
        self.assertNotIn(JWT, everything)
        self.assertNotIn("not ours to send", everything)
        self.assertIn("Bearer [redacted]", files["logs/overlay.log"])

    def test_the_profile_path_is_replaced(self):
        profile = str(Path.home())
        text = diagnostics.scrub(f"game at {profile}/Games and {profile.upper()}/x")
        self.assertNotIn(profile, text)
        self.assertEqual(text.count("%USERPROFILE%"), 2)

    def test_the_cli_flag_prints_where_the_zip_is(self):
        with (
            mock.patch.object(diagnostics, "build", return_value=Path("C:/x/diag.zip")) as built,
            mock.patch.object(diagnostics, "_screens_app", return_value=None),
            mock.patch("builtins.print") as printed,
        ):
            self.assertEqual(app.main(["--diagnostics"]), 0)
        built.assert_called_once()
        self.assertIn("diag.zip", printed.call_args_list[0].args[0])


class StartLineTests(unittest.TestCase):
    def test_flags_only_from_the_command_line(self):
        self.assertEqual(diagnostics.launch_flags(["--updated", "x.txt"]), "--updated")
        self.assertEqual(diagnostics.launch_flags([]), "none")

    def test_screens_with_size_scale_and_primary(self):
        def screen(name, width, height, ratio):
            fake = mock.Mock()
            fake.name.return_value = name
            fake.geometry.return_value.width.return_value = width
            fake.geometry.return_value.height.return_value = height
            fake.devicePixelRatio.return_value = ratio
            return fake

        main, side = screen("A", 2560, 1440, 1.25), screen("B", 1920, 1080, 1.0)
        qt = mock.Mock()
        qt.screens.return_value = [main, side]
        qt.primaryScreen.return_value = main
        self.assertEqual(
            diagnostics.screens_text(qt), "2 screens: 2560x1440@1.25 primary, 1920x1080@1"
        )

    def test_a_broken_screen_list_never_fails_the_start(self):
        qt = mock.Mock()
        qt.primaryScreen.side_effect = RuntimeError("gone")
        self.assertIn("screens unknown", diagnostics.screens_text(qt))

    def test_start_details_name_pid_platform_and_qt(self):
        qt = mock.Mock()
        qt.screens.return_value = []
        qt.primaryScreen.return_value = None
        text = diagnostics.start_details(qt, ["--autostart"])
        self.assertIn(f"pid {os.getpid()}", text)
        self.assertIn("flags --autostart", text)
        self.assertIn("0 screens", text)

    def test_event_log_off_windows(self):
        if os.name == "nt":
            self.skipTest("reads the real event log")
        self.assertIn("not Windows", diagnostics.windows_errors())


if __name__ == "__main__":
    unittest.main()
