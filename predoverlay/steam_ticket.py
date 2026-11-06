"""Steam Web API auth ticket from the locally running Steam client (Windows only).

Loads the Steamworks DLL that ships with Predecessor into this process and asks
the logged-in Steam client for a web-API auth ticket for the game's app ID,
which Omeda's backend validates server-side. The game process is never touched.
While the Steam API is initialised this process shows as running the app in
Steam; ``steam_web_ticket`` shuts it down on exit.
"""

import ctypes
import glob
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import PredError

APP_ID = "961200"
# The identity string the game uses for its Steam web-API ticket; Omeda's
# backend checks the ticket against it.
IDENTITY = os.environ.get("PRED_STEAM_IDENTITY", "predepiconlineservices")
_USER_INTERFACE = b"SteamUser023"
_WEBAPI_SLOT = 0x70  # ISteamUser023::GetAuthTicketForWebApi
_WEBAPI_RESPONSE = 168  # GetTicketForWebApiResponse_t (k_iSteamUserCallbacks + 68)
_RESULT_OK = 1
_MAX_TICKET = 2560
_STEAM_ROOTS = (
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Steam"),
)


class _CallbackMsg(ctypes.Structure):
    _fields_ = [
        ("user", ctypes.c_int32),
        ("callback", ctypes.c_int),
        ("param", ctypes.c_void_p),
        ("size", ctypes.c_int),
    ]


class _WebApiResponse(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_uint32),
        ("result", ctypes.c_int),
        ("size", ctypes.c_int),
        ("ticket", ctypes.c_ubyte * _MAX_TICKET),
    ]


def _registry_roots():
    """Steam's install folder as the Steam client records it in the registry."""
    try:
        import winreg
    except ImportError:
        return []
    found = []
    for hive, key, value in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, key) as handle:
                found.append(str(winreg.QueryValueEx(handle, value)[0]))
        except OSError:
            continue
    return found


def library_paths(vdf_text):
    """Library folders listed in Steam's ``libraryfolders.vdf``."""
    return [m.replace("\\\\", "\\") for m in re.findall(r'"path"\s+"([^"]+)"', vdf_text)]


def steam_libraries():
    """Every Steam library folder on this PC, the main install first."""
    roots = [*_registry_roots(), *_STEAM_ROOTS]
    libraries = []
    for root in roots:
        libraries.append(root)
        try:
            text = Path(root, "steamapps", "libraryfolders.vdf").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue
        libraries += library_paths(text)
    unique = {}
    for path in libraries:
        unique.setdefault(os.path.normcase(os.path.normpath(path)), path)
    return list(unique.values())


def find_game():
    """Predecessor's install folder in any Steam library, or None."""
    for library in steam_libraries():
        folder = Path(library, "steamapps", "common", "Predecessor")
        if folder.is_dir():
            return folder
    return None


def find_steam_api():
    """steam_api64.dll shipped with Predecessor, or PRED_STEAM_API if set."""
    if os.environ.get("PRED_STEAM_API"):
        return Path(os.environ["PRED_STEAM_API"])
    for library in steam_libraries():
        pattern = os.path.join(
            library,
            "steamapps",
            "common",
            "Predecessor",
            "Engine",
            "Binaries",
            "ThirdParty",
            "Steamworks",
            "*",
            "Win64",
            "steam_api64.dll",
        )
        found = sorted(glob.glob(pattern))
        if found:
            return Path(found[-1])
    raise PredError(
        "Predecessor (Steam) was not found in any Steam library; "
        "set PRED_STEAM_API to the game's steam_api64.dll."
    )


def _bind(api):
    """Resolve entry points, accepting both the shipped and a newer Steamworks DLL."""
    api.SteamAPI_IsSteamRunning.restype = ctypes.c_bool
    api.SteamAPI_GetHSteamPipe.restype = ctypes.c_int32
    if hasattr(api, "SteamAPI_InitFlat"):
        api.SteamAPI_InitFlat.argtypes = [ctypes.c_char_p]
        api.SteamAPI_InitFlat.restype = ctypes.c_int

        def init():
            error = ctypes.create_string_buffer(1024)
            code = api.SteamAPI_InitFlat(error)
            if code:
                raise PredError(
                    "Steam API did not start (ESteamAPIInitResult %d: %s)."
                    % (code, error.value.decode(errors="replace") or "no detail")
                )
            return True

    else:
        api.SteamAPI_Init.restype = ctypes.c_bool
        init = api.SteamAPI_Init
    if hasattr(api, "SteamAPI_SteamUser_v023"):
        steam_user = api.SteamAPI_SteamUser_v023
        steam_user.restype = ctypes.c_void_p
    elif hasattr(api, "SteamInternal_FindOrCreateUserInterface"):
        # The DLL shipped with the game predates the flat v023 accessor, but the
        # Steam client still hands out the interface the game itself requests.
        api.SteamAPI_GetHSteamUser.restype = ctypes.c_int
        find = api.SteamInternal_FindOrCreateUserInterface
        find.argtypes = [ctypes.c_int, ctypes.c_char_p]
        find.restype = ctypes.c_void_p

        def steam_user():
            return find(api.SteamAPI_GetHSteamUser(), _USER_INTERFACE)

    else:
        raise PredError(
            "This Steamworks build exposes no SteamUser023 interface (needs SDK 1.57+)."
        )
    api.SteamAPI_ISteamUser_BLoggedOn.argtypes = [ctypes.c_void_p]
    api.SteamAPI_ISteamUser_BLoggedOn.restype = ctypes.c_bool
    api.SteamAPI_ISteamUser_CancelAuthTicket.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    api.SteamAPI_ManualDispatch_RunFrame.argtypes = [ctypes.c_int32]
    api.SteamAPI_ManualDispatch_GetNextCallback.argtypes = [
        ctypes.c_int32,
        ctypes.POINTER(_CallbackMsg),
    ]
    api.SteamAPI_ManualDispatch_GetNextCallback.restype = ctypes.c_bool
    api.SteamAPI_ManualDispatch_FreeLastCallback.argtypes = [ctypes.c_int32]
    return init, steam_user


def _webapi_ticket(api, user, identity):
    """HAuthTicket ISteamUser023::GetAuthTicketForWebApi(const char *pchIdentity)."""
    arg = identity.encode() if identity else None
    if hasattr(api, "SteamAPI_ISteamUser_GetAuthTicketForWebApi"):
        ticket = api.SteamAPI_ISteamUser_GetAuthTicketForWebApi
        ticket.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        ticket.restype = ctypes.c_uint32
        return ticket(user, arg)
    vtable = ctypes.cast(user, ctypes.POINTER(ctypes.c_void_p))[0]
    slot = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[_WEBAPI_SLOT // 8]
    call = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_void_p, ctypes.c_char_p)(slot)
    return call(user, arg)


def _wait_for_ticket(api, pipe, handle, timeout):
    """The web-API ticket arrives only in its callback, which carries the bytes."""
    deadline = time.monotonic() + timeout
    message = _CallbackMsg()
    while time.monotonic() < deadline:
        api.SteamAPI_ManualDispatch_RunFrame(pipe)
        while api.SteamAPI_ManualDispatch_GetNextCallback(pipe, ctypes.byref(message)):
            try:
                if message.callback == _WEBAPI_RESPONSE and message.param:
                    data = ctypes.cast(message.param, ctypes.POINTER(_WebApiResponse)).contents
                    if data.handle == handle:
                        if data.result != _RESULT_OK:
                            raise PredError("Steam rejected the ticket (EResult %d)." % data.result)
                        return bytes(data.ticket[: data.size]).hex().upper()
            finally:
                api.SteamAPI_ManualDispatch_FreeLastCallback(pipe)
        time.sleep(0.05)
    raise PredError("Steam did not return a web-API ticket in time.")


@contextmanager
def _start(api, init):
    """Start the Steam API, or say why it would not.

    IsSteamRunning is no proof either way: it reads ActiveProcess in the registry,
    which stayed 0 on a running, signed-in Steam (seen on 6 November 2026, after the
    game had logged in with another Steam account). Starting the API is the real test.
    """
    running = api.SteamAPI_IsSteamRunning()
    try:
        started = init()
    except PredError:
        if running:
            raise
        started = False
    if not started and not running:
        raise PredError("Steam is not running.")
    if not started:
        raise PredError("Steam API did not start; the logged-in account must own Predecessor.")


def steam_web_ticket(dll=None, *, identity=IDENTITY, timeout=10):
    """Yield a hex Steam web-API auth ticket for Predecessor; cancel it on exit."""
    if os.name != "nt":
        raise PredError("Steam ticket login is implemented for Windows only.")
    os.environ.setdefault("SteamAppId", APP_ID)
    os.environ.setdefault("SteamGameId", APP_ID)
    api = ctypes.CDLL(str(dll or find_steam_api()))
    init, steam_user = _bind(api)
    _start(api, init)
    handle = 0
    user = None
    try:
        api.SteamAPI_ManualDispatch_Init()
        pipe = api.SteamAPI_GetHSteamPipe()
        user = steam_user()
        if not user or not api.SteamAPI_ISteamUser_BLoggedOn(user):
            raise PredError("Steam is not logged in.")
        handle = _webapi_ticket(api, user, identity)
        if not handle:
            raise PredError("Steam did not issue a web-API ticket.")
        yield _wait_for_ticket(api, pipe, handle, timeout)
    finally:
        if handle and user:
            api.SteamAPI_ISteamUser_CancelAuthTicket(user, handle)
        api.SteamAPI_Shutdown()
