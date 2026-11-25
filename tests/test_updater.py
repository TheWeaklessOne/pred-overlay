import hashlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from predoverlay import updater

RELEASE = {
    "version": (0, 2, 0),
    "tag": "v0.2.0",
    "zip_name": "PredOverlay-v0.2.0-win64.zip",
    "zip_url": "https://example.invalid/PredOverlay-v0.2.0-win64.zip",
    "sums_url": "https://example.invalid/SHA256SUMS.txt",
}


def wait():
    for thread in list(updater.threading.enumerate()):
        if thread.name == "pred-update":
            thread.join(5)


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


class VersionTests(unittest.TestCase):
    def test_parse_and_compare(self):
        self.assertEqual(updater.parse_version("v0.1.10"), (0, 1, 10))
        self.assertGreater(updater.parse_version("v0.1.10"), updater.parse_version("0.1.9"))
        self.assertIsNone(updater.parse_version("v1.0-beta"))

    def test_checksum_line_for_the_zip(self):
        sums = "ABC123  PredOverlay-v0.2.0-win64.zip\ndef456  other.exe\n"
        self.assertEqual(updater.expected_hash(sums, RELEASE["zip_name"]), "abc123")
        self.assertIsNone(updater.expected_hash(sums, "missing.zip"))


class NoReleaseTests(unittest.TestCase):
    def test_no_release_on_github_is_not_an_error(self):
        import urllib.error

        missing = urllib.error.HTTPError(updater.LATEST, 404, "Not Found", None, None)
        with mock.patch.object(updater, "_get", side_effect=missing):
            self.assertIs(updater.latest_release(), updater.NO_RELEASE)
        refused = urllib.error.HTTPError(updater.LATEST, 403, "rate limit", None, None)
        with mock.patch.object(updater, "_get", side_effect=refused):
            with self.assertRaises(urllib.error.HTTPError):
                updater.latest_release()


class DownloadTests(unittest.TestCase):
    def fake_get(self, data, digest=None):
        sums = f"{digest or hashlib.sha256(data).hexdigest()}  {RELEASE['zip_name']}\n"
        responses = {RELEASE["sums_url"]: sums.encode(), RELEASE["zip_url"]: data}
        return mock.patch.object(updater, "_get", side_effect=lambda url, limit: responses[url])

    def test_verified_zip_is_unpacked_next_to_the_app(self):
        data = archive({"PredOverlay/PredOverlay.exe": b"exe", "PredOverlay/_internal/x": b"x"})
        with tempfile.TemporaryDirectory() as root, self.fake_get(data):
            app = fake_app(Path(root) / "PredOverlay")
            new = updater.download(RELEASE, app)
            self.assertEqual(new, (Path(root) / "PredOverlay.update" / "PredOverlay").resolve())
            self.assertEqual((new / "PredOverlay.exe").read_bytes(), b"exe")

    def test_a_mismatching_zip_is_refused(self):
        data = archive({"PredOverlay/PredOverlay.exe": b"exe"})
        with tempfile.TemporaryDirectory() as root, self.fake_get(data, digest="00" * 32):
            with self.assertRaises(OSError):
                updater.download(RELEASE, fake_app(Path(root) / "PredOverlay"))

    def test_a_folder_with_other_files_is_never_replaced(self):
        data = archive({"PredOverlay/PredOverlay.exe": b"exe"})
        with tempfile.TemporaryDirectory() as root, self.fake_get(data):
            app = fake_app(Path(root) / "Downloads")
            (app / "holiday.jpg").write_bytes(b"x")
            with self.assertRaises(OSError):
                updater.download(RELEASE, app)

    def test_archives_escaping_their_folder_or_without_the_app_are_refused(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(OSError):
                updater.unpack(archive({"../evil.txt": b"x"}), Path(root) / "a")
            with self.assertRaises(OSError):
                updater.unpack(archive({"PredOverlay/readme.txt": b"x"}), Path(root) / "b")
            self.assertFalse((Path(root) / "b").exists())  # no half-unpacked folder stays

    def test_empty_or_partial_staging_folders_are_replaced(self):
        data = archive({"PredOverlay/PredOverlay.exe": b"exe", "PredOverlay/_internal/x": b"x"})
        leftovers = {
            "empty": [],
            "empty app folder": ["PredOverlay/"],
            "exe blocked by Defender": ["PredOverlay/_internal/python314.dll"],
            "complete": ["PredOverlay/PredOverlay.exe", "PredOverlay/_internal/x"],
        }
        for case, files in leftovers.items():
            with self.subTest(case), tempfile.TemporaryDirectory() as root, self.fake_get(data):
                app = fake_app(Path(root) / "PredOverlay")
                staged = Path(root) / "PredOverlay.update"
                staged.mkdir()
                for name in files:
                    path = staged / name
                    if name.endswith("/"):
                        path.mkdir(parents=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b"old")
                new = updater.download(RELEASE, app)
                self.assertEqual((new / "PredOverlay.exe").read_bytes(), b"exe")
                self.assertFalse((new / "_internal" / "python314.dll").exists())

    def test_a_staging_folder_that_cannot_go_fails_before_the_download(self):
        data = archive({"PredOverlay/PredOverlay.exe": b"exe"})
        for case in ("foreign file", "locked"):
            with self.subTest(case), tempfile.TemporaryDirectory() as root:
                app = fake_app(Path(root) / "PredOverlay")
                staged = Path(root) / "PredOverlay.update"
                (staged / "PredOverlay").mkdir(parents=True)
                if case == "foreign file":
                    (staged / "notes.txt").write_text("mine")
                with (
                    self.fake_get(data) as fetched,
                    mock.patch.object(updater.shutil, "rmtree") as removed,
                ):
                    with self.assertRaises(OSError):
                        updater.download(RELEASE, app)
                fetched.assert_not_called()  # not even the checksum list
                if case == "foreign file":
                    removed.assert_not_called()
                self.assertTrue(staged.exists())


class CleanupTests(unittest.TestCase):
    def test_partial_leftovers_go_and_foreign_folders_stay(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = fake_app(root / "PredOverlay")
            (root / "PredOverlay.old" / "_internal").mkdir(parents=True)  # Remove-Item stopped
            (root / "PredOverlay.failed").mkdir()
            (root / "PredOverlay.update" / "PredOverlay").mkdir(parents=True)
            foreign = root / "PredOverlay.update" / "notes.txt"
            with (
                mock.patch.object(updater, "installable", return_value=True),
                mock.patch.object(updater, "app_dir", return_value=app),
            ):
                updater.cleanup()
                self.assertFalse((root / "PredOverlay.old").exists())
                self.assertFalse((root / "PredOverlay.failed").exists())
                self.assertFalse((root / "PredOverlay.update").exists())
                (root / "PredOverlay.update").mkdir()
                foreign.write_text("mine")
                updater.cleanup()
            self.assertTrue(foreign.exists())
            self.assertTrue((app / "PredOverlay.exe").exists())

    def test_only_app_files_never_matches_a_user_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertTrue(updater.only_app_files(root))  # empty
            (root / "PredOverlay.exe").write_bytes(b"x")
            self.assertTrue(updater.only_app_files(root))
            (root / "photo.jpg").write_bytes(b"x")
            self.assertFalse(updater.only_app_files(root))
            self.assertFalse(updater.only_app_files(root / "missing"))


def fake_app(folder, start=None):
    """A folder shaped like the app; ``start`` is a .cmd standing in for the exe."""
    (folder / "_internal").mkdir(parents=True)
    (folder / "PredOverlay.exe").write_bytes(b"exe")
    if start is not None:
        (folder / "start.cmd").write_text(start, encoding="ascii")
    return folder


# Real console windows open here, so only CI runs these, never a player's PC.
@unittest.skipUnless(sys.platform == "win32" and os.environ.get("CI"), "opens windows")
class SwapTests(unittest.TestCase):
    """SWAP_SCRIPT for real, in temporary folders; a .cmd plays the new exe."""

    WRITES_MARKER = '@echo started> "%PO_MARKER%"\r\n'

    def run_swap(self, root, new_start, waits="40", pid=999999, exit_wait=None):
        app = fake_app(root / "PredOverlay", start="@rem old\r\n")
        (app / "version.txt").write_text("old")
        new = fake_app(root / "PredOverlay.update" / "PredOverlay", start=new_start)
        (new / "version.txt").write_text("new")
        with mock.patch.dict(os.environ, {"PRED_OVERLAY_HOME": str(root / "home")}):
            (root / "home" / "logs").mkdir(parents=True)
            (root / "home" / "updates").mkdir()
            environment = updater.swap_environment(pid, app, new, "v9.9.9", exe="start.cmd")
        environment["PO_WAIT"] = waits
        if exit_wait is not None:
            environment["PO_EXIT_WAIT"] = exit_wait
        updater.run_swap(environment, wait=True)
        time.sleep(0.5)
        return app, (root / "home" / "logs" / "update.log").read_text(encoding="utf-8-sig")

    def test_swap_replaces_the_folder_once_the_new_version_starts(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            app, log = self.run_swap(root, self.WRITES_MARKER)
            self.assertEqual((app / "version.txt").read_text(), "new")
            self.assertFalse((root / "PredOverlay.old").exists())
            self.assertFalse((root / "PredOverlay.update").exists())
            self.assertIn("update v9.9.9 applied", log)

    def test_a_new_version_that_never_starts_is_rolled_back(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            app, log = self.run_swap(root, "@rem never starts\r\n", waits="4")
            self.assertEqual((app / "version.txt").read_text(), "old")
            self.assertIn("previous version restored", log)

    def test_a_new_version_that_hangs_is_stopped_and_rolled_back(self):
        # It runs, so it holds its folder (its working directory), and never says that
        # it started: the folder could not move until the script stops it.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            app, log = self.run_swap(root, "@set /p wait=\r\n", waits="4")
            self.assertEqual((app / "version.txt").read_text(), "old")
            self.assertIn("previous version restored", log)

    def test_an_old_process_that_lingers_is_stopped(self):
        lingering = subprocess.Popen(
            ["ping", "-n", "60", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            creationflags=updater.NO_WINDOW,
        )
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
                root = Path(folder)
                app, log = self.run_swap(
                    root, self.WRITES_MARKER, pid=lingering.pid, exit_wait="1000"
                )
                self.assertIsNotNone(lingering.poll())
                self.assertIn("stopping it", log)
                self.assertEqual((app / "version.txt").read_text(), "new")
        finally:
            lingering.kill()


class StuckDownloadTests(DownloadTests):
    """A download that would fail the same way next hour is tried once, not every hour:
    an app unzipped under Program Files downloaded the whole zip with every check."""

    def test_a_place_the_new_folder_cannot_be_made_in_fails_before_the_network(self):
        data = archive({"PredOverlay/PredOverlay.exe": b"exe", "PredOverlay/_internal/x": b"x"})
        with tempfile.TemporaryDirectory() as root, self.fake_get(data) as fetched:
            app = fake_app(Path(root) / "PredOverlay")
            refused = PermissionError(13, "Access is denied")
            with mock.patch.object(Path, "mkdir", side_effect=refused):
                with self.assertRaises(updater.Stuck):
                    updater.download(RELEASE, app)
            fetched.assert_not_called()

    def test_what_the_network_cannot_mend_is_stuck_and_what_it_can_is_not(self):
        data = archive({"PredOverlay/PredOverlay.exe": b"exe", "PredOverlay/_internal/x": b"x"})
        with tempfile.TemporaryDirectory() as root:
            app = fake_app(Path(root) / "PredOverlay")
            with self.fake_get(data, digest="00" * 32), self.assertRaises(updater.Stuck):
                updater.download(RELEASE, app)
            with self.fake_get(archive({"PredOverlay/readme.txt": b"x"})):
                with self.assertRaises(updater.Stuck):
                    updater.download(RELEASE, app)
            offline = mock.patch.object(updater, "_get", side_effect=TimeoutError("timed out"))
            with offline, self.assertRaises(OSError) as raised:
                updater.download(RELEASE, app)
            self.assertNotIsInstance(raised.exception, updater.Stuck)

    def test_a_stuck_release_is_offered_and_not_fetched_again_by_itself(self):
        u = updater.Updater(lambda message: None, clock=lambda: 1000.0, current="0.1.2")
        with (
            mock.patch.object(updater, "latest_release", return_value=RELEASE),
            mock.patch.object(updater, "installable", return_value=True),
            mock.patch.object(updater, "download", side_effect=updater.Stuck("no")) as fetched,
        ):
            for _ in range(3):  # three hourly checks
                u.next_check = 0
                u.check(download_too=True)
                wait()
                self.assertEqual(u.state, "available")
            self.assertEqual(fetched.call_count, 1)
            u.fetch()  # the user's own click tries once more
            wait()
            self.assertEqual(fetched.call_count, 2)

    def test_a_download_the_network_failed_is_tried_at_the_next_check(self):
        u = updater.Updater(lambda message: None, clock=lambda: 1000.0, current="0.1.2")
        with (
            mock.patch.object(updater, "latest_release", return_value=RELEASE),
            mock.patch.object(updater, "installable", return_value=True),
            mock.patch.object(updater, "download", side_effect=TimeoutError("x")) as fetched,
        ):
            for _ in range(2):
                u.next_check = 0
                u.check(download_too=True)
                wait()
            self.assertEqual(fetched.call_count, 2)
            self.assertIsNone(u.stuck)


class UpdaterTests(unittest.TestCase):
    def make(self, current="0.1.2"):
        return updater.Updater(lambda message: None, clock=lambda: 1000.0, current=current)

    def test_newer_release_downloads_when_asked(self):
        u = self.make()
        with (
            mock.patch.object(updater, "latest_release", return_value=RELEASE),
            mock.patch.object(updater, "installable", return_value=True),
            mock.patch.object(updater, "download", return_value="staged") as fetched,
        ):
            u.check(download_too=True)
            wait()
        self.assertEqual((u.state, u.staged), ("ready", "staged"))
        fetched.assert_called_once_with(RELEASE)

    def test_same_or_older_release_is_current(self):
        u = self.make(current="0.2.0")
        with mock.patch.object(updater, "latest_release", return_value=RELEASE):
            u.check(download_too=True)
            wait()
        self.assertEqual(u.state, "current")

    def test_source_checkout_only_reports_a_release(self):
        u = self.make()
        with (
            mock.patch.object(updater, "latest_release", return_value=RELEASE),
            mock.patch.object(updater, "installable", return_value=False),
            mock.patch.object(updater, "download") as fetched,
        ):
            u.check(download_too=True)
            wait()
        self.assertEqual(u.state, "available")
        fetched.assert_not_called()

    def test_failures_wait_for_the_next_check(self):
        u = self.make()
        with mock.patch.object(updater, "latest_release", side_effect=OSError("offline")):
            u.check(download_too=True)
            wait()
        self.assertEqual(u.state, "error")
        self.assertFalse(u.due())  # an hour until the next try
        with (
            mock.patch.object(updater, "latest_release", return_value=RELEASE),
            mock.patch.object(updater, "installable", return_value=True),
            mock.patch.object(updater, "download", side_effect=OSError("bad checksum")),
        ):
            u.check(download_too=True)
            wait()
        self.assertEqual(u.state, "available")  # found, but not downloaded


if __name__ == "__main__":
    unittest.main()
