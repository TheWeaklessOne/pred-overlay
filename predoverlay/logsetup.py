"""A rotating log file, so a tray app without a console still leaves a trail.

Lines hold phases, counts and errors; never tokens (the transports redact
them) and never other players' names.
"""

import logging
import logging.handlers
import sys
import threading
import time

from .config import log_dir
from .errors import describe, stack

LOG_NAME = "overlay.log"
REPEAT_WINDOW = 10 * 60  # seconds an identical line stays silenced


class _Stream:
    """Stands in for stdout/stderr, which are None in a windowed (no console) app."""

    def __init__(self, logger):
        self.logger = logger
        self.busy = threading.local()

    def write(self, text):
        text = text.strip()
        if text and not getattr(self.busy, "on", False):  # a logging error must not loop
            self.busy.on = True
            try:
                self.logger.info(text)
            finally:
                self.busy.on = False

    def flush(self):
        pass


class _Seen:
    __slots__ = ("level", "text", "emitted", "count", "first", "last")

    def __init__(self, level, text, now):
        self.level, self.text, self.emitted = level, text, now
        self.count, self.first, self.last = 0, None, None


class Repeats(logging.Filter):
    """Silences a line logged again within ``window`` seconds, and counts it.

    One error every 2 s for 88 minutes once filled the whole rotation (4 x 1 MB)
    and erased the history before it; a Qt traceback came 2,375 times. The count
    comes out as "... repeated N× over M min" when another line gets through, or
    when the window is over and the line shows again.

    Sits on the logger, so it sees each record once whatever the handlers; the
    counts go straight to ``handlers`` so they never pass through here again.
    """

    def __init__(self, handlers, window=REPEAT_WINDOW, clock=time.monotonic):
        super().__init__()
        self.handlers, self.window, self.clock = handlers, window, clock
        self.seen = {}
        self.lock = threading.Lock()

    def filter(self, record):
        key = self._key(record)
        now = self.clock()
        with self.lock:
            entry = self.seen.get(key)
            if entry is not None and now - entry.emitted < self.window:
                entry.count += 1
                entry.first = entry.first if entry.first is not None else now
                entry.last = now
                return False
            pending = self._take_counts(now)
            self.seen[key] = _Seen(record.levelno, record.getMessage(), now)
        self._emit(pending, record)
        return True

    def flush(self):
        """Write out the counts still held back (at the end of a run)."""
        with self.lock:
            pending = self._take_counts(self.clock())
        self._emit(pending)

    @staticmethod
    def _key(record):
        if record.exc_info and not record.exc_text:
            # Cached on the record: the formatter reuses it instead of formatting again.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        return record.levelno, record.getMessage(), record.exc_text or ""

    def _take_counts(self, now):
        """Counts to write now; forgets lines whose window is over. Holds the lock."""
        pending = []
        for key, entry in list(self.seen.items()):
            if entry.count:
                pending.append(
                    (entry.level, summary(entry.text, entry.count, entry.last - entry.first))
                )
                entry.count, entry.first, entry.last = 0, None, None
            if now - entry.emitted >= self.window:
                del self.seen[key]
        return pending

    def _emit(self, pending, like=None):
        for level, text in pending:
            record = logging.LogRecord("predoverlay", level, __file__, 0, text, None, None)
            if like is not None:
                record.created = like.created  # keeps the file in time order
            for handler in self.handlers:
                if level >= handler.level:
                    handler.handle(record)


def summary(text, count, seconds):
    first = text.splitlines()[0] if text else ""
    if len(first) > 160:
        first = first[:157] + "..."
    minutes = max(1, round(seconds / 60))
    return f"{first} … repeated {count}× over {minutes} min"


_repeats = None


def flush():
    """Write out held-back repeat counts; the app calls it right before it exits."""
    if _repeats is not None:
        _repeats.flush()


def setup(console=False):
    """Configure logging; returns ``log(message)`` for the overlay's components."""
    global _repeats
    logging.raiseExceptions = False  # a failed write or rotation is dropped, never raised
    folder = log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("predoverlay")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    for old in list(logger.filters):
        logger.removeFilter(old)
    handler = logging.handlers.RotatingFileHandler(
        folder / LOG_NAME, maxBytes=1_000_000, backupCount=3, encoding="utf-8", delay=True
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    if console and sys.stderr is not None:
        echo = logging.StreamHandler(sys.stderr)
        echo.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        logger.addHandler(echo)
    _repeats = Repeats(logger.handlers)
    logger.addFilter(_repeats)
    if sys.stdout is None:
        sys.stdout = _Stream(logger)
    if sys.stderr is None:
        sys.stderr = _Stream(logger)

    def crashed(kind, value, trace):
        # Without the message unless it is safe (errors.describe): it may quote a name.
        logger.error("unhandled error: %s\n%s", describe(value), stack(value))

    sys.excepthook = crashed
    threading.excepthook = lambda args: crashed(args.exc_type, args.exc_value, args.exc_traceback)
    return logger.info


_QT_KINDS = {1: "warning", 2: "critical", 3: "fatal"}  # QtMsgType values; debug and info stay out
_qt_handler = None  # referenced here so Qt never calls a collected function


def qt_messages(log):
    """Route Qt's warnings into the log.

    Qt writes them to the C-level stderr, which goes nowhere in the windowed exe,
    so screen and plugin trouble left no trace. Install before QApplication to
    catch the platform plugin's own messages. Repeats are silenced like any line.
    """
    global _qt_handler
    from PySide6.QtCore import qInstallMessageHandler

    def handler(kind, context, message):
        try:
            name = _QT_KINDS.get(int(getattr(kind, "value", kind)))
            if name is None:
                return
            category = getattr(context, "category", None)
            where = f" [{category}]" if category and category != "default" else ""
            log(f"Qt {name}{where}: {message}")
            if name == "fatal":
                flush()  # Qt aborts right after this returns
                logging.shutdown()
        except Exception:
            pass  # a logging problem must never take Qt down with it

    _qt_handler = handler
    qInstallMessageHandler(handler)
    return handler
