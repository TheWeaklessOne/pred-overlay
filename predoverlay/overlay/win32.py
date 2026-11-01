"""Small Win32 helpers: polled input, the foreground window, a no-activate topmost window.

Nothing here opens a handle to the game process: the game is recognised by the
foreground window's class and title only, and keys are polled, never hooked.
"""

import ctypes
from ctypes import wintypes

VK = {"TAB": 0x09, **{f"F{n}": 0x6F + n for n in range(1, 13)}}
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
if _user32 is not None:
    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]


def key_down(code):
    return bool(_user32 and _user32.GetAsyncKeyState(code) & 0x8000)


def game_in_front():
    """True when the foreground window is Predecessor's (class and title only)."""
    if _user32 is None:
        return False
    hwnd = _user32.GetForegroundWindow()
    title, cls = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
    _user32.GetWindowTextW(hwnd, title, 256)
    _user32.GetClassNameW(hwnd, cls, 256)
    return cls.value == "UnrealWindow" and title.value.strip() == "Predecessor"


def make_inert(hwnd):
    """Never activate, never appear in Alt-Tab; Qt already makes it click-through."""
    if _user32 is None:
        return
    style = _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)


def keep_on_top(hwnd):
    """Games sometimes push themselves above topmost windows; reassert without focus."""
    if _user32 is not None:
        _user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
        )
