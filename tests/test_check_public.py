"""tools/check_public.py: what it catches, and that the repository passes it."""

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_public", ROOT / "tools" / "check_public.py")
check_public = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_public)


class CheckPublicTests(unittest.TestCase):
    def found(self, text, name="sample.py"):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / name
        path.write_text(text, encoding="utf-8")
        return [what for _, what in check_public.problems(path)]

    def test_catches_what_must_not_be_published(self):
        cases = {
            'log = "C:\\\\Users\\\\someone\\\\AppData"': "a Windows user folder",
            "see /Users/someone/Projects": "a macOS or Linux home folder",
            "host = 'DESKTOP-AB12CD3'": "a PC name",
            "https://game.live.predecessor.omeda.pragmaengine.com": "Omeda's backend",
            "token = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2ln'": "a JSON Web Token",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz": "a bearer token",
            "client_secret = 'abcdefgh12345678'": "a client secret",
            "mail = 'someone@gmail.com'": "an e-mail address",
        }
        for text, what in cases.items():
            with self.subTest(text=text):
                self.assertIn(what, self.found(text))

    def test_leaves_alone_what_is_meant_to_be_there(self):
        for text in (
            "%LOCALAPPDATA%\\PredOverlay\\logs",
            "Co-Authored-By: x <41053514+someone@users.noreply.github.com>",
            "mail = 'player@example.com'",
            'path = "C:\\\\Users\\\\JOHNSM~1"  # public-ok: invented',
            "C:\\Users\\Public\\Desktop",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.found(text), [])

    def test_binary_files_are_not_read(self):
        self.assertEqual(self.found("someone@gmail.com", "picture.png"), [])

    def test_the_repository_passes(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        try:
            files = check_public.tracked()
        except Exception as exc:  # not a git checkout (a source zip)
            self.skipTest(f"not a git checkout: {exc}")
        bad = [(f, line, what) for f in files for line, what in check_public.problems(f)]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
