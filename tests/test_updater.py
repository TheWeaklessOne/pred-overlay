import hashlib
import io
import os
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

    def run_swap(self, root, new_start, waits="40"):
        app = fake_app(root / "PredOverlay", start="@rem old\r\n")
        (app / "version.txt").write_text("old")
        new = fake_app(root / "PredOverlay.update" / "PredOverlay", start=new_start)
        (new / "version.txt").write_text("new")
        with mock.patch.dict(os.environ, {"PRED_OVERLAY_HOME": str(root / "home")}):
            (root / "home" / "logs").mkdir(parents=True)
            (root / "home" / "updates").mkdir()
            environment = updater.swap_environment(999999, app, new, "v9.9.9", exe="start.cmd")
        environment["PO_WAIT"] = waits
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
        self.assertFalse(u.due())  # six hours until the next try
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
