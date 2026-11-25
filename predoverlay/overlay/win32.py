"""Small Win32 helpers: polled input, the foreground window, a no-activate topmost window.

Nothing here opens a handle to the game process: the game is recognised by the
foreground window's class and title, or by its name in the system's process
list, and keys are polled, never hooked.
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


GAME_TITLE = "Predecessor"


def _front():
    """(class, title) of the foreground window, or None."""
    if _user32 is None:
        return None
    hwnd = _user32.GetForegroundWindow()
    title, cls = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256)
    _user32.GetWindowTextW(hwnd, title, 256)
    _user32.GetClassNameW(hwnd, cls, 256)
    return cls.value, title.value.strip()


def front_kind(front=None):
    """What the foreground window is, for the log: "game", "game not responding" (the
    ghost Windows puts in place of a window that stops answering for 5 s, as the game
    does while it loads a map), or the window's class alone (never another program's
    title)."""
    cls, title = front or _front() or ("", "")
    if cls == "UnrealWindow" and title == GAME_TITLE:
        return "game"
    if cls == "Ghost" and title.startswith(GAME_TITLE + " ("):
        return "game not responding"
    return cls or "nothing"


def game_in_front():
    """True when the foreground window is Predecessor's (class and title only), including
    the ghost of it while it does not respond: the screen still shows the game."""
    return front_kind() in ("game", "game not responding")


def make_inert(hwnd):
    """Never activate, never appear in Alt-Tab; Qt already makes it click-through."""
    if _user32 is None:
        return
    style = _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)


WDA_EXCLUDEFROMCAPTURE = 0x11


def exclude_from_capture(hwnd):
    """Leave this window out of screen captures (Windows 10 2004+); False if refused."""
    if _user32 is None:
        return False
    return bool(_user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))


def keep_on_top(hwnd):
    """Games sometimes push themselves above topmost windows; reassert without focus."""
    if _user32 is not None:
        _user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
        )


class _MemoryCounters(ctypes.Structure):
    """PROCESS_MEMORY_COUNTERS_EX: PrivateUsage is what Task Manager calls commit size."""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


GR_GDIOBJECTS, GR_USEROBJECTS = 0, 1


def process_counters():
    """This process's own private bytes, working set, handles, GDI and USER objects.

    A slow leak of any of them ends in a crash or blank windows hours later; the
    heartbeat logs them so the trend shows. Reads this process only, through its
    pseudo handle; empty off Windows.
    """
    if _user32 is None:
        return {}
    kernel32 = ctypes.windll.kernel32
    process = wintypes.HANDLE(-1)  # what GetCurrentProcess returns; never closed
    found = {}
    memory = _MemoryCounters()
    memory.cb = ctypes.sizeof(memory)
    if kernel32.K32GetProcessMemoryInfo(process, ctypes.byref(memory), memory.cb):
        found["private"] = memory.PrivateUsage
        found["working_set"] = memory.WorkingSetSize
    handles = wintypes.DWORD()
    if kernel32.GetProcessHandleCount(process, ctypes.byref(handles)):
        found["handles"] = handles.value
    found["gdi"] = _user32.GetGuiResources(process, GR_GDIOBJECTS)
    found["user"] = _user32.GetGuiResources(process, GR_USEROBJECTS)
    return found


# The game client itself: it writes the log the overlay follows. PredecessorLauncher.exe
# (Easy Anti-Cheat's starter) runs only seconds before it and is not needed.
GAME_EXE = "PredecessorClient-Win64-Shipping.exe"
TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class _ProcessEntry(ctypes.Structure):
    """PROCESSENTRY32W."""

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


# Its own instance: the argtypes set here must not change kernel32 for other modules.
_kernel32 = ctypes.WinDLL("kernel32") if hasattr(ctypes, "windll") else None
if _kernel32 is not None:
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def process_names():
    """Executable names of all running processes, lower case; None if the list cannot be read.

    A Toolhelp snapshot of the process list: names only, no handle to any
    process is opened. None off Windows as well.
    """
    if _kernel32 is None:
        return None
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == _INVALID_HANDLE:
        return None
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        names = set()
        found = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            names.add(entry.szExeFile.lower())
            found = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return names
    finally:
        _kernel32.CloseHandle(snapshot)


def game_running():
    """True while Predecessor's client runs, False when not, None when the list is unreadable."""
    names = process_names()
    return None if names is None else GAME_EXE.lower() in names
