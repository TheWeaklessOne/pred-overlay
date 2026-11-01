"""Entry point: the tray app by default, plus a few one-shot commands.

``PredOverlay.exe`` (no console) runs the tray app. ``PredOverlayCLI.exe`` is
the same program with a console, for ``--check`` and the other commands.
"""

import argparse
import json
import sys

from . import __version__
from .errors import PredError


def parser():
    p = argparse.ArgumentParser(
        prog="pred-overlay",
        description="Player cards over Predecessor's draft, loading screen and TAB scoreboard.",
    )
    p.add_argument("--version", action="version", version=f"Pred Overlay {__version__}")
    p.add_argument("--check", action="store_true", help="Check this PC's setup and exit")
    p.add_argument("--json", action="store_true", help="With --check: print JSON")
    p.add_argument("--preview", action="store_true", help="Show sample panels, no game needed")
    p.add_argument("--console", action="store_true", help="Also print the log to the console")
    p.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)
    p.add_argument(
        "--sign-in-browser",
        action="store_true",
        help="Sign in through Steam in a web browser (fallback when the Steam client "
        "sign-in cannot work); the session lasts about a day",
    )
    p.add_argument("--sign-out", action="store_true", help="Delete the saved session and exit")
    p.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    return p


def _console():
    """A windowed exe has no stdout: borrow the console it was started from."""
    if sys.stdout is not None or sys.platform != "win32":
        return
    import ctypes

    if ctypes.windll.kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
        sys.stdout = sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")


def _fatal(message):
    """Say why the tray app cannot start, in a box when there is no console."""
    print(f"error: {message}", file=sys.stderr)
    if not getattr(sys.stderr, "isatty", lambda: False)():
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            QApplication.instance() or QApplication(sys.argv[:1])
            QMessageBox.critical(None, "Pred Overlay", str(message))
        except Exception:
            pass


def main(argv=None):
    args = parser().parse_args(argv)
    if args.check or args.sign_out or args.sign_in_browser or args.self_test:
        _console()
    try:
        if args.check:
            return check(args.json)
        if args.self_test:
            return self_test()
        if args.sign_out:
            return sign_out()
        if args.sign_in_browser:
            return sign_in_browser()
        if args.preview:
            return preview()
        return run_tray(args)
    except PredError as exc:
        if args.check or args.sign_out or args.sign_in_browser or args.preview:
            print(f"error: {exc}", file=sys.stderr)
        else:
            _fatal(exc)
        return 1
    except Exception as exc:  # the tray app must never vanish without a word
        if args.check or args.sign_out or args.sign_in_browser or args.preview:
            raise
        _fatal(f"{type(exc).__name__}: {exc}")
        raise


def check(as_json):
    from . import doctor

    results = doctor.run()
    print(json.dumps(results, indent=2) if as_json else doctor.report(results))
    return 1 if any(r["status"] == doctor.FAIL for r in results) else 0


def self_test():
    """For CI: the frozen build can start Qt and load the OCR runtime."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtNetwork import QLocalServer  # noqa: F401  (the single-instance guard)
    from PySide6.QtWidgets import QApplication

    from . import single_instance, tray  # noqa: F401  (imports the tray and the panel)
    from .overlay import scoreboard, window  # noqa: F401

    app = QApplication.instance() or QApplication(["self-test"])
    from winrt.windows.media.ocr import OcrEngine  # noqa: F401

    tray.app_icon().pixmap(32)  # the icon is bundled

    print(f"Pred Overlay {__version__} self-test ok ({app.platformName()})")
    return 0


def sign_out():
    from .auth import SessionStore
    from .config import default_session

    removed = SessionStore(default_session()).clear()
    print("Signed out." if removed else "There was no saved session.")
    return 0


def sign_in_browser():
    from . import game
    from .auth import SessionStore, steam_login
    from .config import default_session

    result = steam_login(
        game.browser_exchange,
        on_ready=lambda url: print("Steam sign-in page: " + url, flush=True),
    )
    game.save_tokens(SessionStore(default_session()), result["tokens"])
    name = result["profile"].get("displayName", {}).get("displayName", "your account")
    print(f"Signed in as {name}. Start Pred Overlay now.")
    return 0


def _qt_app():
    from PySide6.QtWidgets import QApplication

    if sys.platform == "win32":
        import ctypes

        # Lets Windows show notifications under the app's own name and icon.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PredOverlay")
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Pred Overlay")
    app.setApplicationVersion(__version__)
    return app


def _keep_ctrl_c(app, on_quit):
    """Lets Ctrl+C in a console stop the Qt loop."""
    import signal

    from PySide6.QtCore import QTimer

    signal.signal(signal.SIGINT, lambda *_: on_quit())
    keepalive = QTimer(app)
    keepalive.timeout.connect(lambda: None)  # gives Python a moment to see SIGINT
    keepalive.start(200)
    return keepalive


def preview():
    from . import settings as settings_module
    from .overlay import window as overlay_window

    app = _qt_app()
    values = settings_module.load()
    panel = overlay_window.Overlay(
        overlay_window.PreviewTracker(),
        hotkey=values["hotkey"],
        force_foreground=True,
        screen_index=values["screen"],
    )
    panel.show()
    print("Preview running; press Ctrl+C to stop.", file=sys.stderr)
    _keep = _keep_ctrl_c(app, app.quit)  # noqa: F841
    return app.exec()


def run_tray(args):
    if sys.platform != "win32":
        raise PredError("The overlay runs on Windows only; --preview works anywhere.")
    from . import autostart, logsetup
    from . import settings as settings_module
    from .single_instance import Guard

    app = _qt_app()
    app.setQuitOnLastWindowClosed(False)
    guard = Guard()
    if not guard.acquire():
        Guard.notify_running()  # the first copy shows a notice; this one leaves quietly
        return 0
    guard.listen()
    log = logsetup.setup(console=args.console)
    log(f"Pred Overlay {__version__} starting")
    values = settings_module.load()
    try:
        autostart.refresh()
    except OSError as exc:
        log(f"autostart not refreshed: {exc}")

    from .runtime import Runtime
    from .tray import Tray, app_icon

    app.setWindowIcon(app_icon())
    runtime = Runtime(values, log)
    runtime.start()
    tray = Tray(app, runtime, values, log, quiet_start=args.autostart)
    guard.connect(tray.already_running)
    _keep = _keep_ctrl_c(app, tray.quit)  # noqa: F841
    app.aboutToQuit.connect(runtime.stop)
    code = app.exec()
    log("Pred Overlay stopped")
    return code
