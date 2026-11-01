"""Where the overlay keeps its files; nothing is written on import.

Everything lives under ``%LOCALAPPDATA%\\PredOverlay`` (or ``PRED_OVERLAY_HOME``):
the private session file, settings, logs and the finished-match cache.
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


def cache_dir():
    return home() / "match-cache"


def token_for(provider, session_path):
    variable = {
        "omeda": "OMEDA_SOCIAL_TOKEN",
        "omeda_game": "OMEDA_GAME_TOKEN",
        "predgg": "PRED_ACCESS_TOKEN",
    }[provider]
    if os.environ.get(variable):
        return os.environ[variable]
    if session_path is None:
        return None
    from .auth import SessionStore

    record = SessionStore(session_path).load()["providers"].get(provider, {})
    field = {"omeda": "social_token", "omeda_game": "game_token"}.get(provider, "access_token")
    return record.get(field)
