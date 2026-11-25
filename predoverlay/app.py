"""Entry point: the tray app by default, plus a few one-shot commands.

``PredOverlay.exe`` (no console) runs the tray app. ``PredOverlayCLI.exe`` is
the same program with a console, for ``--check`` and the other commands.
"""

import argparse
import json
import sys

from . import __version__
from .errors import PredError, describe


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
    p.add_argument("--updated", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--update-failed", metavar="TAG", help=argparse.SUPPRESS)
    p.add_argument(
        "--sign-in",
        action="store_true",
        help="Sign in on Pred.gg's website in your browser (the tray menu does the same)",
    )
    p.add_argument("--sign-out", action="store_true", help="Delete the saved session and exit")
    p.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the shortcuts and autostart; then delete the folders it names",
    )
    p.add_argument(
        "--diagnostics",
        action="store_true",
        help="Save a zip with logs, settings and setup checks on the desktop (no session)",
    )
    p.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    # Installs: ask the running copy to quit as its tray menu does, and wait for it.
    p.add_argument("--quit", action="store_true", help="Quit the running overlay and exit")
    # Debugging: save full-screen frames for building screen readers (overlay/capture.py).
    p.add_argument("--capture", action="store_true", help=argparse.SUPPRESS)
    # Debugging: keep each match's events to replay them (overlay/recorder.py). The files
    # hold other players' names, so nothing is recorded unless this is given.
    p.add_argument("--record", action="store_true", help=argparse.SUPPRESS)
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


def _one_shot(args):
    """Commands that print an answer and exit, as opposed to the tray app."""
    return bool(
        args.check
        or args.sign_out
        or args.sign_in
        or args.self_test
        or args.uninstall
        or args.diagnostics
        or args.quit
    )


def main(argv=None):
    args = parser().parse_args(argv)
    if _one_shot(args):
        _console()
    try:
        if args.check:
            return check(args.json)
        if args.diagnostics:
            from . import diagnostics

            return diagnostics.run()
        if args.self_test:
            return self_test()
        if args.sign_out:
            return sign_out()
        if args.quit:
            return quit_running()
        if args.uninstall:
            from .install import uninstall

            print(uninstall())
            return 0
        if args.sign_in:
            return sign_in()
        if args.preview:
            return preview()
        return run_tray(args)
    except PredError as exc:
        if _one_shot(args) or args.preview:
            print(f"error: {exc}", file=sys.stderr)
        else:
            _fatal(exc)
        return 1
    except Exception as exc:  # the tray app must never vanish without a word
        if _one_shot(args) or args.preview:
            if sys.stderr is None:
                # The windowed exe run without a console (by the setup): a traceback
                # would become a box that waits for a click. 2: failed, not "still running".
                return 2
            raise  # a command run from a console or a script: no box to wait on
        _fatal(describe(exc))
        raise


def quit_running(wait=15.0):
    """Ask the running copy to quit (it ends as from its tray menu) and wait until it has:
    0 when none runs any more, 1 when it is still there."""
    import time

    from .single_instance import Guard

    if sys.platform != "win32":
        return 0
    from PySide6.QtCore import QCoreApplication

    _app = QCoreApplication.instance() or QCoreApplication([])  # noqa: F841  (for the socket)
    if not Guard.notify_running(b"quit"):
        guard = Guard()
        if guard.acquire():
            guard.release()
            print("not running")
            return 0
        print("running, but not answering", file=sys.stderr)
        return 1
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        guard = Guard()
        if guard.acquire():  # the mutex is free: it has quit
            guard.release()
            print("quit")
            return 0
        time.sleep(0.2)
    print("still running", file=sys.stderr)
    return 1


def check(as_json):
    from . import doctor

    results = doctor.run()
    print(json.dumps(results, indent=2) if as_json else doctor.report(results))
    return 1 if any(r["status"] == doctor.FAIL for r in results) else 0


def self_test():
    """For CI: the frozen build can start Qt, read portraits and load the OCR runtime."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QImageReader
    from PySide6.QtNetwork import QLocalServer  # noqa: F401  (the single-instance guard)
    from PySide6.QtWidgets import QApplication

    from . import single_instance, tray  # noqa: F401  (imports the tray and the panel)
    from .overlay import scoreboard, window  # noqa: F401

    app = QApplication.instance() or QApplication(["self-test"])
    from winrt.windows.ai.machinelearning import LearningModel  # noqa: F401  (the name model)
    from winrt.windows.media.ocr import OcrEngine  # noqa: F401

    from .overlay import namereader
    from .transport import tls_context, tls_fallback

    tls_context()
    if sys.platform == "win32" and tls_fallback():
        raise PredError(f"Windows would not verify HTTPS in this build ({tls_fallback()}).")
    if not os.path.isfile(namereader.MODEL):
        raise PredError("The name model is missing from the build (assets, namereader.onnx).")

    tray.app_icon().pixmap(32)  # the icon is bundled
    if b"webp" not in [bytes(name) for name in QImageReader.supportedImageFormats()]:
        raise PredError("The WebP plugin is missing: portraits would stay blank.")

    print(f"Pred Overlay {__version__} self-test ok ({app.platformName()})")
    return 0


def sign_out():
    from . import predgg
    from .config import default_session

    removed = predgg.sign_out(default_session())
    print("Signed out." if removed else "There was no saved session.")
    return 0


def sign_in():
    from . import predgg
    from .config import default_session

    predgg.sign_in(
        default_session(),
        on_ready=lambda url: print("Pred.gg sign-in page: " + url, flush=True),
    )
    print("Signed in. A running Pred Overlay picks it up on its own.")
    return 0


def _qt_app():
    from PySide6.QtWidgets import QApplication

    if sys.platform == "win32":
        import ctypes

        # Lets Windows show notifications under the app's own name and icon.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PredOverlay")
    # Worker threads (name model, Windows OCR, Pred.gg answers) hand the GIL back to the
    # GUI thread every 0.5 ms instead of 5: animation frames stop waiting on them
    # (measured 2026-11-19: worst frame 48 ms -> 22 ms during a name reading).
    sys.setswitchinterval(0.0005)
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


STARTED_AFTER = 5  # seconds of a running event loop before an update counts as started
ACQUIRE_PATIENCE = 10  # seconds an updated launch waits for the old copy to let go


def _acquire(guard, patient, wait=ACQUIRE_PATIENCE, sleep=None):
    """Take the single-instance mutex; after an update, wait for the old copy to go.

    The swap script starts the new version right after the old process exits,
    and Windows may hold the old mutex a moment longer.
    """
    import time

    sleep = sleep or time.sleep
    deadline = time.monotonic() + (wait if patient else 0)
    while True:
        if guard.acquire():
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(0.5)


def _clean_leftovers(updated):
    """Remove what an earlier update left next to the program folder. Not on the first
    start after an update: the folder with the previous version is what the update script
    puts back if this start fails, and the script removes it itself once it has not."""
    if updated:
        return
    from .updater import cleanup

    cleanup()


def _mark_started(log):
    from .updater import mark_started

    try:
        mark_started()  # tells the update script that this version runs
    except OSError as exc:
        log(f"update marker not written: {exc}")
        return
    log("updated version is up; told the update script")


def run_tray(args):
    if sys.platform != "win32":
        raise PredError("The overlay runs on Windows only; --preview works anywhere.")
    import time

    from . import autostart, diagnostics, heartbeat, logsetup
    from . import settings as settings_module
    from .single_instance import Guard

    # Logging first: Qt's own warnings, and a second launch, leave a line too.
    log = logsetup.setup(console=args.console)
    logsetup.qt_messages(log)
    app = _qt_app()
    app.setQuitOnLastWindowClosed(False)
    guard = Guard()
    if not _acquire(guard, patient=bool(args.updated or args.update_failed)):
        log(f"Pred Overlay {__version__} launched again; the running copy says so")
        Guard.notify_running()  # the first copy shows a notice; this one leaves quietly
        return 0
    if not guard.listen():
        log("single-instance listener not started: --quit and a second launch go unheard")
    started = time.time()
    abrupt = heartbeat.previous_run()  # before this run's header lands in crash.log
    details = diagnostics.start_details(app, sys.argv[1:])
    log(f"Pred Overlay {__version__} starting: {details}{_injected()}")
    if abrupt:
        log(abrupt)
    _crash = _crash_file(__version__)  # noqa: F841  (kept open until the process ends)
    heartbeat.mark_running(started)
    from .transport import tls_context, tls_fallback

    tls_context()
    if tls_fallback():  # a source checkout without the package: a fresh PC may lack roots
        log(f"HTTPS is verified with Python's own roots, not by Windows: {tls_fallback()}")
    _clean_leftovers(updated=bool(args.updated))
    _drop_match_cache(log)
    values = settings_module.load()
    try:
        autostart.refresh()
    except OSError as exc:
        log(f"autostart not refreshed: {exc}")

    from .runtime import Runtime
    from .tray import Tray, app_icon

    app.setWindowIcon(app_icon())
    runtime = Runtime(values, log, capture=args.capture, record=args.record)
    runtime.start()
    tray = Tray(
        app,
        runtime,
        values,
        log,
        quiet_start=args.autostart,
        updated=args.updated,
        failed=args.update_failed,
    )
    guard.connect(
        lambda message: (
            tray.quit("asked to by --quit") if message == b"quit" else tray.already_running()
        )
    )
    from PySide6.QtCore import QTimer

    if args.updated:  # only once the tray is up and the loop runs: see mark_started
        QTimer.singleShot(STARTED_AFTER * 1000, lambda: _mark_started(log))
    beat = QTimer(app)
    beat.timeout.connect(lambda: log(heartbeat.line(started)))
    beat.start(heartbeat.EVERY * 1000)
    _keep = _keep_ctrl_c(app, lambda: tray.quit("Ctrl+C"))  # noqa: F841
    ending = Ending(log, started, lambda: tray.quit_reason)
    _session = _session_window()  # noqa: F841  (kept for as long as the app runs)
    app.commitDataRequest.connect(ending.session_ending)
    app.aboutToQuit.connect(ending.finish)  # first: Windows gives little time after it
    app.aboutToQuit.connect(runtime.stop)
    code = app.exec()
    ending.finish()
    _exit_now(code)


class Ending:
    """The run's last lines and its marker, written once however the run ends.

    When the app quits (tray menu, --quit, an update), Qt emits aboutToQuit and
    app.exec() returns. When Windows ends the session (shut down, restart, sign out),
    Qt emits commitDataRequest and then aboutToQuit from inside WM_ENDSESSION, and
    Windows ends the process as soon as that returns: app.exec() never does. Written
    after exec(), as they used to be, the last lines never came: a shutdown left the marker
    behind and the next start said "ended abruptly" (seen 2026-11-22 21:29).
    """

    def __init__(self, log, started, quit_reason):
        self.log, self.started, self.quit_reason = log, started, quit_reason
        self.session = False  # Windows said the session is ending
        self.done = False

    def session_ending(self, *_):
        self.session = True
        self.log("Windows session ending")

    def finish(self):
        if self.done:
            return
        self.done = True
        from . import heartbeat, logsetup

        reason = self.quit_reason()
        if not reason and self.session:
            reason = "Windows ended the session (shut down, restart or sign out)"
        reason = reason or "closed from outside (Windows or another program)"
        self.log(heartbeat.line(self.started, label="at stop"))
        self.log(f"Pred Overlay stopped: {reason}{_injected()}")
        heartbeat.mark_stopped()
        logsetup.flush()


def _session_window():
    """A native window that is never shown, so that Windows says when the session ends.

    Windows asks top-level windows only, and until the game first runs the app has none
    of its own: the process was then ended without a word (2026-11-23 10:13; measured
    2026-11-24: without a window Qt gets neither commitDataRequest nor the end of the
    session, with this one both). Keep the returned widget alive.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    window = QWidget(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
    window.winId()  # creates the native window without showing it
    return window


def _drop_match_cache(log):
    """Delete the match-cache folder older versions kept: it holds other players' IDs,
    and nothing reads or writes it any more. Errors are left for the next start."""
    import shutil

    from .config import home

    folder = home() / "match-cache"
    if not folder.is_dir():
        return
    shutil.rmtree(folder, ignore_errors=True)
    if not folder.exists():
        log("removed the match cache older versions kept")


def _crash_file(version):
    """Python stacks of every thread land in crash.log if the process dies natively."""
    import faulthandler
    import time

    from .config import log_dir

    path = log_dir() / "crash.log"
    try:
        mode = "w" if path.exists() and path.stat().st_size > 200_000 else "a"
        stream = open(path, mode, encoding="utf-8")
        stream.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} Pred Overlay {version} started\n")
        stream.flush()
        faulthandler.enable(stream, all_threads=True)
        return stream
    except OSError:
        return None


# DLLs that other programs inject into processes (overlays, recorders); listed only
# to say in the log when one got into this one. The app loads none of them itself.
_INJECTED = (
    "gameoverlayrenderer64.dll",
    "steam_api64.dll",
    "discord_hook64.dll",
    "RTSSHooks64.dll",
)


def _injected():
    """Foreign DLLs known to hook processes, if any got into this one."""
    if sys.platform != "win32":
        return ""
    import ctypes

    found = [name for name in _INJECTED if ctypes.windll.kernel32.GetModuleHandleW(name)]
    return f" (loaded: {', '.join(found)})" if found else ""


def _exit_now(code):
    """Leave without the interpreter's teardown.

    Tearing down winrt's OCR objects after Qt crashed the process on the way out
    (access violation in _winrt.pyd, 2 to 9 November 2026). Everything that
    matters is already done: the runtime stopped, the session and settings are
    written as they change; the log is flushed here.
    """
    import logging
    import os

    from . import logsetup

    logsetup.flush()  # repeat counts still held back
    logging.shutdown()
    if sys.stdout is not None:
        sys.stdout.flush()
    os._exit(code)
