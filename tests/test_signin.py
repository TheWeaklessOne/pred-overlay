"""The sign-in window: intro, waiting for Pred.gg, then the result."""

import threading
import time
import unittest

try:
    from PySide6.QtWidgets import QApplication

    from predoverlay.signin import SignInWindow
except ImportError:  # no PySide6
    SignInWindow = None


@unittest.skipIf(SignInWindow is None, "PySide6 is not installed")
class SignInWindowTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])

    def settle(self, window, state, seconds=3.0):
        deadline = time.monotonic() + seconds
        while window.state != state and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(window.state, state)

    def window(self, sign_in):
        window = SignInWindow(sign_in)
        # The panel's states are under test, not Windows bringing it to the front: on
        # the build server's desktop that call ends the test process.
        window.bring_forward = lambda anchor=None: None
        self.addCleanup(window.deleteLater)
        results = []
        window.finished.connect(lambda name, error: results.append((name, error)))
        return window, results

    def test_starts_with_the_account_check_and_one_button(self):
        window, _ = self.window(lambda ready, cancel: "Me")
        self.assertEqual(window.state, "intro")
        self.assertIn("your own Pred.gg account", window.body.text())
        self.assertTrue(window.check.isVisibleTo(window))
        self.assertEqual(window.primary.text(), "Sign in on Pred.gg")

    def test_a_sign_in_ends_signed_in_with_the_name(self):
        window, results = self.window(lambda ready, cancel: "Me")
        window.start()
        self.settle(window, "done")
        self.assertIn("Signed in as Me", window.body.text())
        self.assertEqual(results, [("Me", None)])
        self.assertFalse(window.busy)

    def test_an_error_offers_another_try(self):
        def fail(ready, cancel):
            raise RuntimeError("Pred.gg did not finish this sign-in.")

        window, results = self.window(fail)
        window.start()
        self.settle(window, "error")
        self.assertIn("did not finish", window.body.text())
        self.assertEqual(window.primary.text(), "Try again")
        self.assertIsInstance(results[0][1], RuntimeError)

    def test_putting_the_panel_away_keeps_the_attempt(self):
        go = threading.Event()

        def wait(ready, cancel):
            ready("http://127.0.0.1:1/?start=x")
            if not go.wait(3):
                raise AssertionError("never let go")
            return "Me"

        window, results = self.window(wait)
        window.start()
        window.close()  # a click elsewhere or Alt+F4: the panel steps aside
        self.assertTrue(window.busy)
        self.assertFalse(window.cancel.is_set())
        go.set()
        self.settle(window, "done")
        self.assertEqual(results, [("Me", None)])

    def test_cancel_stops_the_attempt(self):
        seen = threading.Event()

        def wait(ready, cancel):
            ready("http://127.0.0.1:1/?start=x")
            seen.set()
            if not cancel.wait(3):
                raise AssertionError("not cancelled")
            raise RuntimeError("cancelled")

        window, results = self.window(wait)
        window.start()
        self.assertTrue(seen.wait(2))
        self.assertTrue(window.busy)
        self.assertEqual(window.secondary.text(), "Cancel")
        window.secondary.click()
        deadline = time.monotonic() + 3
        while window.busy and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertFalse(window.busy)
        self.assertEqual(window.state, "intro")  # no result screen; a reopen starts afresh
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
