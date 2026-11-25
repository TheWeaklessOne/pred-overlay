"""Where the overlay keeps its files; nothing is written on import.

Everything lives under ``%LOCALAPPDATA%\\PredOverlay`` (or ``PRED_OVERLAY_HOME``):
the private session file, settings, logs, the players earlier matches taught
(``known-players.json``) and hero portraits.
"""

import os
from pathlib import Path


def home():
    if os.environ.get("PRED_OVERLAY_HOME"):
        return Path(os.environ["PRED_OVERLAY_HOME"]).expanduser()
    base = os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share"
    return Path(base) / "PredOverlay"


def default_session():
    if os.environ.get("PRED_SESSION_FILE"):
        return Path(os.environ["PRED_SESSION_FILE"]).expanduser()
    return home() / "session.json"


def settings_path():
    return home() / "settings.json"


def log_dir():
    return home() / "logs"


def windows_powershell():
    """Windows PowerShell by its full path. A bare ``powershell.exe`` is looked for in the
    program's own folder and the current directory before System32."""
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def capture_dir():
    return home() / "captures"


def portrait_dir():
    return home() / "portraits"
