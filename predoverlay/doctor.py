"""``--check``: what this PC has and lacks for the overlay. Local reads only:
no network, no sign-in, nothing written."""

import os
import re
import sys
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


def check_steam():
    if sys.platform != "win32":
        return FAIL, "Windows only"
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as k:
            user = winreg.QueryValueEx(k, "ActiveUser")[0]
    except OSError:
        return FAIL, "Steam is not installed for this Windows user"
    if not user:
        return FAIL, "Steam is not running or not signed in"
    return OK, "running and signed in"


def check_game():
    from .steam_ticket import find_game, find_steam_api

    folder = find_game()
    try:
        dll = find_steam_api()
    except Exception as exc:
        return FAIL, str(exc)
    if not dll.is_file():
        return FAIL, f"PRED_STEAM_API points to a missing file: {dll}"
    where = folder or dll
    return OK, f"Steam version at {where}"


def check_played():
    from .overlay.phases import default_log

    log = default_log()
    if log.exists():
        return OK, "the game has run on this PC"
    return FAIL, "no game log yet: play Predecessor once before signing in"


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


def check_ocr():
    try:
        from winrt.windows.media.ocr import OcrEngine
    except Exception:
        return WARN, "Windows OCR runtime missing: no TAB cards"
    languages = [lang.language_tag for lang in OcrEngine.available_recognizer_languages]
    if not languages:
        return WARN, "no Windows OCR language installed: no TAB cards (see README)"
    return OK, ", ".join(languages)


def check_session():
    from .config import default_session, token_for
    from .game import _fresh

    try:
        social = token_for("omeda", default_session())
        game = token_for("omeda_game", default_session())
    except Exception as exc:
        return WARN, f"session file unreadable: {exc}"
    if not (social and game):
        return WARN, "not signed in yet (the overlay signs in through Steam on start)"
    if _fresh(social, 0) and _fresh(game, 0):
        return OK, "signed in"
    return WARN, "session expired; renews through Steam automatically"


def check_gui():
    try:
        import PySide6

        return OK, f"PySide6 {PySide6.__version__}"
    except ImportError:
        return FAIL, "PySide6 is not installed"


CHECKS = (
    ("steam", check_steam),
    ("game", check_game),
    ("played", check_played),
    ("display", check_display),
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
