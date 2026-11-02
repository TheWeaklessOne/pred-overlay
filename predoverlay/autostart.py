"""Start with Windows: one value under the user's Run key, no admin rights."""

import shlex
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE = "PredOverlay"


def command():
    """What Windows runs at sign-in: the windowed exe, or pythonw with this package."""
    if getattr(sys, "frozen", False):
        # Also from PredOverlayCLI.exe: sign-in must not open a console window.
        return f'"{Path(sys.executable).with_name("PredOverlay.exe")}" --autostart'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{runner}" -m predoverlay --autostart'


def supported():
    return sys.platform == "win32"


def _registered():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, VALUE)[0]
    except OSError:
        return None


def enabled():
    """On when our value exists and Task Manager has not disabled it."""
    if not supported() or _registered() is None:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            state = winreg.QueryValueEx(key, VALUE)[0]
    except OSError:
        return True
    # Task Manager writes 02... for enabled and an odd first byte (03...) for disabled.
    return not (isinstance(state, bytes) and state[:1] and state[0] & 1)


def disabled_in_task_manager():
    """True when the user switched our entry off in Task Manager's Startup apps."""
    if not supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            state = winreg.QueryValueEx(key, VALUE)[0]
    except OSError:
        return False
    return isinstance(state, bytes) and bool(state[:1]) and bool(state[0] & 1)


def set_enabled(on):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, VALUE, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, VALUE)
            except FileNotFoundError:
                pass
    try:  # a "disabled" mark left by Task Manager would keep it off
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE)
    except OSError:
        pass


def refresh():
    """Repoint the Run value only when the program it names no longer exists
    (the folder was moved); otherwise leave the user's choice alone."""
    if not supported():
        return
    current = _registered()
    if current is None:
        return
    try:
        program = shlex.split(current, posix=False)[0].strip('"')
    except (ValueError, IndexError):
        program = ""
    if not Path(program).exists():
        set_enabled(True)
