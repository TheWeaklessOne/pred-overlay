"""``--diagnostics``: one zip on the desktop with what a bug report needs.

It holds the logs, the settings, the ``--check`` results, version and screen
details and Windows' crash reports for this app. Every text goes through the
transport's redaction plus a swap of the user's profile path. The session file
never goes in: the zip is meant to be sent to someone.
"""

import json
import os
import platform
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

from . import __version__
from .config import log_dir, settings_path, windows_powershell
from .transport import redact

LOGS = ("overlay.log", "overlay.log.1", "overlay.log.2", "overlay.log.3", "update.log", "crash.log")
EVENT_DAYS = 14
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

# Application Error (1000) and Windows Error Reporting (1001) entries about this app.
EVENTS_SCRIPT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$since = (Get-Date).AddDays(-[int]$env:PO_DAYS)
$found = Get-WinEvent -FilterHashtable @{LogName = 'Application'; Id = 1000, 1001; StartTime = $since} -ErrorAction SilentlyContinue |
  Where-Object { $_.Message -like '*PredOverlay*' } | Select-Object -First 20
if (-not $found) { "no crash reports for PredOverlay in the last $env:PO_DAYS days"; return }
foreach ($e in $found) {
  "--- $($e.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss')) event $($e.Id) ($($e.ProviderName))"
  $e.Message
}
"""


def platform_text():
    """``Windows 10.0.26200`` (the build tells Windows 10 from 11), else the platform."""
    if sys.platform == "win32":
        return f"Windows {platform.version()}"
    return platform.platform()


def qt_text():
    try:
        import PySide6
        from PySide6.QtCore import qVersion

        return f"PySide6 {PySide6.__version__} (Qt {qVersion()})"
    except Exception as exc:
        return f"PySide6 unavailable ({type(exc).__name__})"


def screens_text(app):
    """``2 screens: 2560x1440@1.25 primary, 1920x1080@1``: sizes in Qt's logical pixels."""
    try:
        primary = app.primaryScreen()
        main = primary.name() if primary is not None else None
        shown = []
        for screen in app.screens():
            size = screen.geometry()
            text = f"{size.width()}x{size.height()}@{screen.devicePixelRatio():g}"
            shown.append(text + (" primary" if screen.name() == main else ""))
    except Exception as exc:  # the start line must never fail
        return f"screens unknown ({type(exc).__name__})"
    count = len(shown)
    return f"{count} screen{'' if count == 1 else 's'}: {', '.join(shown) or 'none'}"


def launch_flags(argv):
    """The command-line flags only; nothing else from the command line."""
    flags = [arg for arg in argv if arg.startswith("--")]
    return " ".join(flags) if flags else "none"


def start_details(app, argv):
    """Everything the start line adds after the version."""
    return (
        f"pid {os.getpid()}, {platform_text()}, Python {platform.python_version()}, "
        f"{qt_text()}, {screens_text(app)}, flags {launch_flags(argv)}"
    )


def scrub(text):
    """Tokens out (transport.redact), and the user's profile path, which names them."""
    text = redact(text)
    for profile in {str(Path.home()), _short_path(Path.home())}:
        if profile and len(profile) > 3:
            # As written, with forward slashes, and with the backslashes doubled once (an
            # error's repr) or twice (that repr inside JSON): the longest form first.
            doubled = profile.replace("\\", "\\\\")
            forms = {profile, profile.replace("\\", "/"), doubled, doubled.replace("\\", "\\\\")}
            for form in sorted(forms, key=len, reverse=True):
                text = re.sub(re.escape(form), "%USERPROFILE%", text, flags=re.IGNORECASE)
    return text


def _short_path(path):
    """The 8.3 form %TEMP% and older tools print; None elsewhere.

    ``C:\\Users\\JOHNSM~1`` (an invented user folder: public-ok)."""
    if sys.platform != "win32":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(260)
    if ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 260):
        return buffer.value
    return None


def desktop():
    """The user's desktop folder (also when OneDrive moved it), else the home folder."""
    if sys.platform == "win32":
        import ctypes

        buffer = ctypes.create_unicode_buffer(260)
        # SHGetFolderPathW(CSIDL_DESKTOPDIRECTORY): follows a redirected desktop
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer) == 0:
            if buffer.value and Path(buffer.value).is_dir():
                return Path(buffer.value)
    folder = Path.home() / "Desktop"
    return folder if folder.is_dir() else Path.home()


def windows_errors(days=EVENT_DAYS):
    """Windows' own crash entries for PredOverlay.exe from the Application log."""
    if sys.platform != "win32":
        return "not Windows: no event log"
    try:
        result = subprocess.run(
            [windows_powershell(), "-NoProfile", "-NonInteractive", "-Command", EVENTS_SCRIPT],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            creationflags=NO_WINDOW,
            env=dict(os.environ, PO_DAYS=str(int(days))),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"event log not read: {type(exc).__name__}: {exc}"
    return (result.stdout + result.stderr).strip() or "event log not read: no output"


def check_json():
    from . import doctor

    try:
        return json.dumps(doctor.run(), indent=2)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


def about(app=None):
    lines = [
        f"Pred Overlay {__version__}",
        f"built: {'yes' if getattr(sys, 'frozen', False) else 'no, from source'}",
        platform_text(),
        f"Python {platform.python_version()}",
        qt_text(),
        f"created: {time.strftime('%Y-%m-%d %H:%M:%S %z')}",
    ]
    if app is not None:
        lines.append(screens_text(app))
    return "\n".join(lines) + "\n"


def _screens_app():
    """A Qt GUI application for the screen list; nothing is shown."""
    try:
        from PySide6.QtGui import QGuiApplication

        return QGuiApplication.instance() or QGuiApplication(["pred-overlay-diagnostics"])
    except Exception:
        return None


def _read(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def contents(app=None, events=windows_errors):
    """{name in the zip: text}; every text already scrubbed."""
    files = {}
    for name in LOGS:
        text = _read(log_dir() / name)
        if text is not None:
            files[f"logs/{name}"] = text
    settings = _read(settings_path())
    if settings is not None:
        files["settings.json"] = settings
    files["check.json"] = check_json()
    files["about.txt"] = about(app)
    files["windows-errors.txt"] = events()
    return {name: scrub(text) for name, text in files.items()}


def build(folder=None, app=None, events=windows_errors, now=None):
    """Write the zip; returns its path."""
    folder = Path(folder) if folder else desktop()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    target = folder / f"PredOverlay-diagnostics-{stamp}.zip"
    partial = target.with_suffix(".tmp")
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in contents(app, events).items():
            archive.writestr(name, text)
    os.replace(partial, target)
    return target


def run():
    """The CLI command: build the zip and say where it is."""
    path = build(app=_screens_app())
    print(f"Diagnostics saved to {path}")
    print("It holds logs and settings, no session or tokens; look through it before sharing.")
    return 0
