"""Safe, structured errors shared by providers, services and presentation."""

import traceback
from pathlib import Path


def describe(exc):
    """An exception for the log, without text that could hold a player's name.

    The app's own errors (PredError) carry messages written to be logged; an OSError
    names a system failure (a file, WinRT, the network) and a RuntimeError one of Qt,
    WinRT or the app's own ("No Windows OCR language"). Any other message may quote
    data, such as a name read from the screen in a KeyError or a ValueError, so it is
    left out and the place it was raised is given instead.
    """
    if exc is None:
        return "an unknown error"
    if isinstance(exc, (PredError, OSError, RuntimeError)):
        return f"{type(exc).__name__}: {exc}"
    frames = traceback.extract_tb(getattr(exc, "__traceback__", None))
    # The app's own line that led there, not the library function that raised.
    ours = [f for f in frames if _PACKAGE in Path(f.filename).resolve().parents] or frames
    where = f" at {Path(ours[-1].filename).name}:{ours[-1].lineno}" if ours else ""
    return f"{type(exc).__name__}{where}"


def stack(exc):
    """The traceback's frames for the log, without the messages (``describe`` gives the
    first line), and those of the exceptions that led to it."""
    parts, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if parts:
            parts.append(f"caused by {describe(exc)}\n")
        parts.extend(traceback.format_tb(getattr(exc, "__traceback__", None)))
        cause = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        exc = cause
    return "".join(parts)


_PACKAGE = Path(__file__).resolve().parent


class PredError(Exception):
    code = "operation_failed"

    def __init__(self, message, *, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    def as_dict(self):
        data = {"error": str(self), "code": self.code}
        if self.status is not None:
            data["status"] = self.status
        if self.retry_after is not None:
            data["retry_after"] = self.retry_after
        return data


class AuthenticationError(PredError):
    code = "authentication_required"


class TransportError(PredError):
    code = "transport_error"


class RateLimitError(TransportError):
    code = "rate_limited"


class PayloadError(PredError):
    code = "invalid_response"


class AmbiguousPlayerError(PredError):
    code = "ambiguous_player"


class CapabilityError(PredError):
    code = "unsupported_capability"
