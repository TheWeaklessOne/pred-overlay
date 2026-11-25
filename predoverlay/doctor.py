"""``--check``: what this PC has and lacks for the overlay. Local reads only:
no network, no sign-in, nothing written."""

import os
import re
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"


def game_settings_file():
    base = os.environ.get("LOCALAPPDATA", "")
    return Path(base, "Predecessor", "Saved", "Config", "WindowsClient", "GameUserSettings.ini")


def _ini_values(text, *keys):
    found = {}
    for key in keys:
        match = re.search(rf"^{key}=(\S+)", text, re.MULTILINE)
        found[key] = match.group(1) if match else None
    return found


def check_played():
    from .overlay.phases import default_log

    log = default_log()
    if log.exists():
        return OK, "the game has run on this PC"
    return FAIL, "no game log yet: play Predecessor once"


def check_display():
    path = game_settings_file()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return WARN, "game settings not found; use Windowed Fullscreen (borderless)"
    values = _ini_values(text, "FullscreenMode", "ResolutionSizeX", "ResolutionSizeY")
    mode = values["FullscreenMode"]
    notes = []
    status = OK
    if mode == "0":
        status = FAIL
        notes.append("exclusive Fullscreen hides the overlay: switch to Windowed Fullscreen")
    elif mode == "2":
        status = WARN
        notes.append("Windowed mode: the panels are placed for a full-screen game")
    elif mode == "1":
        notes.append("Windowed Fullscreen")
    else:
        status = WARN
        notes.append("display mode unknown; use Windowed Fullscreen (borderless)")
    try:
        width, height = int(values["ResolutionSizeX"]), int(values["ResolutionSizeY"])
    except (TypeError, ValueError):
        width = height = 0
    if width and height:
        notes.append(f"{width}x{height}")
        if abs(width / height - 16 / 9) > 0.02:
            status = WARN if status == OK else status
            notes.append("not 16:9: the TAB card may not line up")
    return status, ", ".join(notes)


def _model():
    """(loaded, why not) for the overlay's own name reader."""
    try:
        from .overlay import namereader
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return namereader.READER.available(), namereader.READER.error


def check_names():
    loaded, why = _model()
    if not loaded:
        return WARN, f"the overlay's name reader did not load ({why}): Windows OCR reads alone"
    return OK, "the overlay's name reader (Windows ML)"


def check_ocr():
    # Without Windows OCR the model still reads the names; OCR adds the TAB card's
    # hero line and a second opinion on names the model is unsure of.
    status = WARN if _model()[0] else FAIL
    alone = "names by the overlay's reader alone" if status == WARN else "no names can be read"
    try:
        from winrt.windows.media.ocr import OcrEngine
    except Exception:
        return status, f"Windows OCR runtime missing: {alone}"
    languages = [lang.language_tag for lang in OcrEngine.available_recognizer_languages]
    if not languages:
        return status, f"no Windows OCR language installed: {alone} (see README)"
    return OK, ", ".join(languages)


def check_session():
    from .auth import SessionStore
    from .config import default_session

    try:
        status = SessionStore(default_session()).status()
    except Exception as exc:
        return WARN, f"session file unreadable: {exc}"
    if not status["stored"]:
        return WARN, "not signed in yet: tray menu, Sign in with Pred.gg"
    return OK, "signed in (Pred.gg renews the session by itself)"


def check_gui():
    try:
        import PySide6

        return OK, f"PySide6 {PySide6.__version__}"
    except ImportError:
        return FAIL, "PySide6 is not installed"


CHECKS = (
    ("played", check_played),
    ("display", check_display),
    ("names", check_names),
    ("ocr", check_ocr),
    ("session", check_session),
    ("gui", check_gui),
)


def run():
    results = []
    for name, check in CHECKS:
        try:
            status, detail = check()
        except Exception as exc:  # a broken check reports itself, the rest still run
            status, detail = FAIL, f"{type(exc).__name__}: {exc}"
        results.append({"check": name, "status": status, "detail": detail})
    return results


def report(results):
    marks = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}
    return "\n".join(f"[{marks[r['status']]}] {r['check']:<8} {r['detail']}" for r in results)
