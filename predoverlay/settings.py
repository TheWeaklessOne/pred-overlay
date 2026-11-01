"""User settings in a small JSON file; unknown or invalid values fall back to defaults."""

import json
import os

from .config import settings_path

REGIONS = ("EUROPE", "NA", "SA", "ASIA", "OCE", "MENA")
HOTKEYS = tuple(f"F{n}" for n in range(1, 13))
DEFAULTS = {
    "region": "EUROPE",  # matchmaking region whose queues the main menu shows
    "hotkey": "F8",  # pins the panel in game
    "screen": 0,  # monitor index, 0 = primary
    "enabled": True,  # the tray's "Overlay enabled" switch
    "welcomed": False,  # the first-run notification was shown
}
_VALID = {
    "region": lambda v: v in REGIONS,
    "hotkey": lambda v: v in HOTKEYS,
    "screen": lambda v: isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 16,
    "enabled": lambda v: isinstance(v, bool),
    "welcomed": lambda v: isinstance(v, bool),
}


def load(path=None):
    path = path or settings_path()
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        found = {}
    if not isinstance(found, dict):
        found = {}
    values = dict(DEFAULTS)
    for key, check in _VALID.items():
        if key in found and check(found[key]):
            values[key] = found[key]
    return values


def save(values, path=None):
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    clean = {key: values[key] for key in DEFAULTS if key in values}
    temporary.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    os.replace(temporary, path)
