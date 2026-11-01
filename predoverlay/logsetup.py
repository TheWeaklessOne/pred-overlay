"""A rotating log file, so a tray app without a console still leaves a trail.

Lines hold phases, counts and errors; never tokens (the transports redact
them) and never other players' names.
"""

import logging
import logging.handlers
import sys
import threading

from .config import log_dir

LOG_NAME = "overlay.log"


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


def setup(console=False):
    """Configure logging; returns ``log(message)`` for the overlay's components."""
    logging.raiseExceptions = False  # a failed write or rotation is dropped, never raised
    folder = log_dir()
    folder.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("predoverlay")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.handlers.RotatingFileHandler(
        folder / LOG_NAME, maxBytes=1_000_000, backupCount=3, encoding="utf-8", delay=True
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    if console and sys.stderr is not None:
        echo = logging.StreamHandler(sys.stderr)
        echo.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        logger.addHandler(echo)
    if sys.stdout is None:
        sys.stdout = _Stream(logger)
    if sys.stderr is None:
        sys.stderr = _Stream(logger)

    def crashed(kind, value, trace):
        logger.error("unhandled error", exc_info=(kind, value, trace))

    sys.excepthook = crashed
    threading.excepthook = lambda args: crashed(args.exc_type, args.exc_value, args.exc_traceback)
    return logger.info
