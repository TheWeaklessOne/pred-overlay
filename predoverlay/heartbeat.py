"""A periodic line on the app's own health, and a marker that tells a crash from a quit.

The tray app logs uptime, memory, handles, GDI/USER objects and its threads
every 30 minutes and once more when it stops, so a slow leak shows as a trend
before it ends in a crash. While it runs, ``running.json`` sits in the app's
home; a clean stop removes it. Found at the next start, it means the last run
ended without a word (a native crash, a kill, a power cut), and the start line
says so together with what crash.log got since. The marker also holds when
Windows started: if Windows has started again since and crash.log got nothing,
the run ended with Windows (a blue screen, a power cut, a forced shutdown) and
the line says that instead of "abruptly".
"""

import collections
import json
import os
import re
import threading
import time

from . import __version__
from .config import home, log_dir

EVERY = 30 * 60  # seconds between two heartbeat lines
MARKER = "running.json"
BOOT_SLACK = 120  # seconds two readings of Windows' start may differ (clock corrections)


def _mb(value):
    return f"{value / (1024 * 1024):.0f} MB"


def threads_text():
    """Live threads by name, numbered ones folded: ``pred-roster, Thread ×2``."""
    names = collections.Counter(
        re.sub(r"([-_ ]?\d+)+(\s*\(.*\))?$", "", t.name) or t.name for t in threading.enumerate()
    )
    return ", ".join(name if n == 1 else f"{name} ×{n}" for name, n in sorted(names.items()))


def counters():
    try:
        from .overlay.win32 import process_counters

        return process_counters()
    except Exception:  # a heartbeat must never fail the app
        return {}


def line(started, now=None, label="heartbeat"):
    """One log line: uptime, memory, handles, GDI/USER objects, threads."""
    now = time.time() if now is None else now
    hours, rest = divmod(int(max(0, now - started)), 3600)
    parts = [f"{label}: up {hours}h{rest // 60:02d}m"]
    found = counters()
    if "private" in found:
        parts.append(f"private {_mb(found['private'])}, working set {_mb(found['working_set'])}")
    for key in ("handles", "gdi", "user"):
        if key in found:
            parts.append(f"{key} {found[key]}")
    parts.append(f"threads {threading.active_count()}: {threads_text()}")
    return ", ".join(parts)


def marker_path():
    return home() / MARKER


def boot_time(now=None):
    """When Windows started, in seconds since the epoch; None where that is unknown."""
    if os.name != "nt":
        return None
    try:
        import ctypes

        ticks = ctypes.windll.kernel32.GetTickCount64
        ticks.restype = ctypes.c_uint64
        return (time.time() if now is None else now) - ticks() / 1000.0
    except Exception:  # a marker must never fail the app
        return None


def _crash_size():
    try:
        return (log_dir() / "crash.log").stat().st_size
    except OSError:
        return 0


def previous_run():
    """What the last run left if it did not stop cleanly: a log line, or None.

    Call before this run writes its own header to crash.log.
    """
    path = marker_path()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    when = record.get("started")
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(when)) if when else "an unknown time"
    who = f"version {record.get('version', '?')}, pid {record.get('pid', '?')}, started {since}"
    size_then, booted_then, booted = record.get("crash_size"), record.get("boot"), boot_time()
    grew = isinstance(size_then, int) and _crash_size() > size_then
    restarted = (
        isinstance(booted_then, (int, float))
        and booted is not None
        and booted > booted_then + BOOT_SLACK
    )
    if restarted and not grew:
        again = time.strftime("%Y-%m-%d %H:%M", time.localtime(booted))
        return (
            f"previous run ended with Windows, not by itself ({who}): Windows went down "
            f"without closing it and started again {again} (a blue screen, a power cut, "
            f"a reset or a forced shutdown)"
        )
    return f"previous run ended abruptly ({who}); {_crash_news(size_then)}"


def _crash_news(size_then):
    size_now = _crash_size()
    if not isinstance(size_then, int):
        return "crash.log not compared"
    if size_now <= size_then:
        return "crash.log did not grow (no Python stack: killed, or a crash outside Python)"
    first = ""
    try:
        with open(log_dir() / "crash.log", encoding="utf-8", errors="replace") as stream:
            stream.seek(size_then)
            first = next((ln.strip() for ln in stream if ln.strip()), "")
    except OSError:
        pass
    news = f"crash.log grew by {size_now - size_then} bytes"
    return f"{news}: {first[:200]}" if first else news


def mark_running(started):
    """Write the marker; call after this run's crash.log header is written."""
    path = marker_path()
    record = {
        "version": __version__,
        "pid": os.getpid(),
        "started": int(started),
        "crash_size": _crash_size(),
        "boot": boot_time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
    except OSError:
        pass


def mark_stopped():
    try:
        marker_path().unlink()
    except OSError:
        pass
