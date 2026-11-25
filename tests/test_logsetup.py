"""The log keeps its history: repeated lines are counted, Qt's warnings get in."""

import itertools
import logging
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from predoverlay import logsetup


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


_names = itertools.count()


def make():
    """A logger with only the filter and a collecting handler."""
    logger = logging.getLogger(f"test-repeats-{next(_names)}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    sink = Collect()
    logger.addHandler(sink)
    clock = Clock()
    repeats = logsetup.Repeats(logger.handlers, clock=clock)
    logger.addFilter(repeats)
    return logger, sink, clock, repeats


class RepeatTests(unittest.TestCase):
    def test_a_storm_becomes_one_line_and_a_count(self):
        logger, sink, clock, _ = make()
        for _ in range(30):
            logger.info("queues failed: timed out")
            clock.now += 2
        logger.info("phase menu")
        self.assertEqual(
            sink.lines,
            [
                "queues failed: timed out",
                "queues failed: timed out … repeated 29× over 1 min",
                "phase menu",
            ],
        )

    def test_a_long_storm_shows_again_every_ten_minutes(self):
        logger, sink, clock, _ = make()
        for _ in range(int(88 * 60 / 2)):  # one error every 2 s for 88 minutes
            logger.info("display failed: no route")
            clock.now += 2
        self.assertLess(len(sink.lines), 20)
        self.assertEqual(sink.lines[1], "display failed: no route … repeated 299× over 10 min")

    def test_interleaved_storms_are_both_silenced(self):
        logger, sink, clock, repeats = make()
        for _ in range(50):
            logger.info("a failed")
            logger.info("b failed")
            clock.now += 1
        self.assertEqual(sink.lines, ["a failed", "b failed"])
        repeats.flush()
        self.assertIn("a failed … repeated 49× over 1 min", sink.lines)
        self.assertIn("b failed … repeated 49× over 1 min", sink.lines)

    def test_different_lines_all_pass(self):
        logger, sink, clock, _ = make()
        for n in range(5):
            logger.info(f"tab: pointing at row {n}")
        self.assertEqual(len(sink.lines), 5)

    def test_same_message_with_another_traceback_is_not_a_repeat(self):
        logger, sink, clock, _ = make()
        for error in (ValueError("x"), ValueError("x"), KeyError("y")):
            try:
                raise error
            except Exception:
                logger.error("unhandled error", exc_info=True)
        self.assertEqual(len(sink.lines), 3)
        self.assertIn("ValueError", sink.lines[0])
        self.assertEqual(sink.lines[1], "unhandled error … repeated 1× over 1 min")
        self.assertIn("KeyError", sink.lines[2])

    def test_flush_with_nothing_held_writes_nothing(self):
        logger, sink, clock, repeats = make()
        logger.info("once")
        repeats.flush()
        self.assertEqual(sink.lines, ["once"])


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        patch = mock.patch.dict(os.environ, {"PRED_OVERLAY_HOME": self.folder.name})
        patch.start()
        self.addCleanup(patch.stop)
        saved = sys.excepthook, threading.excepthook, sys.stdout, sys.stderr
        self.addCleanup(self._restore, saved)

    @staticmethod
    def _restore(saved):
        sys.excepthook, threading.excepthook, sys.stdout, sys.stderr = saved
        logger = logging.getLogger("predoverlay")
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
        for old in list(logger.filters):
            logger.removeFilter(old)
        logsetup._repeats = None

    def read(self):
        for handler in logging.getLogger("predoverlay").handlers:
            handler.flush()
        return (Path(self.folder.name) / "logs" / "overlay.log").read_text(encoding="utf-8")

    def test_the_excepthook_path_is_deduplicated_too(self):
        logsetup.setup()
        for _ in range(100):
            try:
                raise RuntimeError("QScreen is gone")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
        logsetup.flush()
        text = self.read()
        self.assertEqual(text.count("unhandled error: RuntimeError: QScreen is gone\n"), 1)
        self.assertIn("unhandled error: RuntimeError: QScreen is gone … repeated 99×", text)

    def test_setting_up_twice_keeps_one_filter(self):
        logsetup.setup()
        logsetup.setup()
        self.assertEqual(len(logging.getLogger("predoverlay").filters), 1)


class QtMessageTests(unittest.TestCase):
    def setUp(self):
        try:
            from PySide6.QtCore import qInstallMessageHandler
        except ImportError:
            self.skipTest("PySide6 is not installed")
        self.addCleanup(qInstallMessageHandler, None)

    def test_warnings_reach_the_log_and_debug_stays_out(self):
        from PySide6.QtCore import QMessageLogContext, QtMsgType

        lines = []
        handler = logsetup.qt_messages(lines.append)
        context = QMessageLogContext()
        handler(QtMsgType.QtDebugMsg, context, "chatter")
        handler(QtMsgType.QtWarningMsg, context, "QScreen(0x1) was removed")
        handler(QtMsgType.QtCriticalMsg, context, "no platform plugin")
        self.assertEqual(
            lines, ["Qt warning: QScreen(0x1) was removed", "Qt critical: no platform plugin"]
        )

    def test_qt_warning_goes_through_the_installed_handler(self):
        from PySide6.QtCore import qWarning

        lines = []
        logsetup.qt_messages(lines.append)
        qWarning("from Qt")
        self.assertEqual(lines, ["Qt warning: from Qt"])

    def test_a_failing_log_never_raises_into_qt(self):
        from PySide6.QtCore import QMessageLogContext, QtMsgType

        def broken(_):
            raise OSError("disk full")

        handler = logsetup.qt_messages(broken)
        handler(QtMsgType.QtWarningMsg, QMessageLogContext(), "x")  # no exception


if __name__ == "__main__":
    unittest.main()
