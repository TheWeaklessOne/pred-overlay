"""The tray icon: status, on/off, preview, sign-in, autostart, updates, logs, quit."""

import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtGui import QCursor, QDesktopServices, QIcon
from PySide6.QtWidgets import QMenu, QMessageBox, QSystemTrayIcon

from . import __version__, autostart, install, predgg, updater
from . import settings as settings_module
from .config import default_session, log_dir

ASSETS = Path(__file__).with_name("assets")
PROJECT_URL = "https://github.com/TheWeaklessOne/pred-overlay"
PREVIEW_SECONDS = 28  # one full cycle of the preview: draft, loading, TAB, menu (7 s each)
PHASE_TEXT = {
    "closed": "Waiting for Predecessor",
    "menu": "Ready · waiting for a match",
    "accepted": "Match found",
    "draft": "In draft",
    "loading": "In match",
}


def app_icon():
    return QIcon(str(ASSETS / "icon.ico"))


SESSION_CHECK = 60  # seconds between local looks at the session file while the game is closed


def status_text(view, enabled, health=None):
    health = health or view.health
    if not enabled:
        return "Paused"
    if health == "login":
        return "Sign-in needed: Sign in with Pred.gg below"
    if health == "offline":
        return "Pred.gg not answering, retrying"
    return PHASE_TEXT.get(view.phase, view.phase)


class Tray(QObject):
    def __init__(
        self, app, runtime, settings, log, *, quiet_start=False, updated=False, failed=None
    ):
        super().__init__()
        self.app, self.runtime, self.settings, self.log = app, runtime, settings, log
        if failed:
            settings["skip_update"] = failed
        self.updater = updater.Updater(log, skip=settings["skip_update"] or None)
        self.notified = set()  # (state, tag) pairs already told to the user
        self.manual_check = False  # the user asked: report the answer whatever it is
        self.was_busy = False
        self.install_when_ready = False
        self.quit_reason = None  # set when the app itself quits; None means closed from outside
        self.icon = app_icon()
        fresh = not settings["welcomed"]
        integrated = install.integrate(settings, log)  # once: shortcuts (and autostart if new)
        try:  # like integrate(): nothing here may stop the app from starting
            if install.sync_setup_entry(__version__):
                log(f"Apps entry now says {__version__}")
        except Exception as exc:
            log(f"Apps entry not updated: {type(exc).__name__}")
        self.paused_icon = QIcon(self.icon.pixmap(64, QIcon.Mode.Disabled))
        self.tray = QSystemTrayIcon(self.icon)
        self.tray.activated.connect(self._activated)
        self.tray.messageClicked.connect(self._message_clicked)
        self.preview = None
        self.health = None
        self.signin = None  # the sign-in window, made when first needed
        self.announce_sign_in = True  # False: the sign-in window already said so
        self.session_ok, self.session_checked, self.session_stamp = True, -SESSION_CHECK, None
        first_sign_in = fresh and not default_session().exists()
        if first_sign_in:
            self.health = "login"  # known, and the sign-in below says what to do
        self._build_menu()
        self.tray.show()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        settings["welcomed"] = True
        self._save()
        self.refresh()
        where = []
        if "shortcuts" in integrated:
            where.append("is in the Start menu and on the desktop")
        if "autostart" in integrated:
            where.append("starts with Windows")
        extra = (" It " + " and ".join(where) + ".") if where else ""
        if failed:
            self.notify(
                f"Update {failed} did not install",
                "The previous version is back. Right-click the icon to try the update again.",
            )
        elif updated:
            self.notify(f"Updated to {__version__}", "Pred Overlay is running the new version.")
        elif fresh or where:
            self.notify(
                "Pred Overlay is running",
                "It appears in the draft, on the loading screen and while you hold TAB. "
                f"{settings['hotkey']} pins the panel in game.{extra} "
                "Right-click this icon for options.",
            )
        elif not quiet_start:
            self.notify("Pred Overlay is running", "Right-click the tray icon for options.")
        if first_sign_in:
            self._sign_in("first start")  # the sign-in window: one click to Pred.gg

    # --- menu ------------------------------------------------------------------

    def _build_menu(self):
        menu = QMenu()
        self.status_action = menu.addAction("Starting…")
        self.status_action.setEnabled(False)
        menu.addSeparator()
        self.enabled_action = menu.addAction("Overlay enabled")
        self.enabled_action.setCheckable(True)
        self.enabled_action.setChecked(self.settings["enabled"])
        self.enabled_action.toggled.connect(self._set_enabled)
        self.preview_action = menu.addAction("Show preview")
        self.preview_action.triggered.connect(self._show_preview)
        self.sign_in_action = menu.addAction("Sign in with Pred.gg\u2026")
        self.sign_in_action.triggered.connect(lambda: self._sign_in("tray menu"))
        menu.addAction("Sign out").triggered.connect(self._sign_out)
        menu.addSeparator()
        self.autostart_action = menu.addAction("Start with Windows")
        self.autostart_action.setCheckable(True)
        self.autostart_action.setEnabled(autostart.supported())
        self.autostart_action.setChecked(autostart.enabled())
        self.autostart_action.toggled.connect(self._set_autostart)
        self.update_action = menu.addAction("Check for updates")
        self.update_action.triggered.connect(self._update_clicked)
        self.auto_update_action = menu.addAction("Update automatically")
        self.auto_update_action.setCheckable(True)
        self.auto_update_action.setChecked(self.settings["auto_update"])
        self.auto_update_action.toggled.connect(self._set_auto_update)
        menu.addAction("Open log folder").triggered.connect(self._open_logs)
        menu.addAction("About").triggered.connect(self._about)
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(lambda: self.quit("tray menu"))
        menu.aboutToShow.connect(self._sync_autostart)
        self.menu = menu
        self.tray.setContextMenu(menu)

    def _save(self):
        try:
            settings_module.save(self.settings)
        except OSError as exc:
            self.log(f"settings not saved: {exc}")

    def _set_enabled(self, on):
        self.settings["enabled"] = on
        self._save()
        self.runtime.set_enabled(on)
        self.refresh()

    def _sign_out(self):
        from .auth import AuthError

        try:
            removed = predgg.sign_out(default_session())
        except AuthError as exc:
            self.notify("Could not sign out", str(exc))
            return
        self.log("signed out from the tray")
        self.session_checked = -SESSION_CHECK
        self.health = "login"  # said here: no "Pred.gg did not accept the session" after it
        self.runtime.signed_out()  # the view asks for a sign-in at once
        self.notify(
            "Signed out" if removed else "Not signed in",
            "The saved session is deleted. Sign in with Pred.gg to use the overlay again.",
        )

    @property
    def signing_in(self):
        return self.signin is not None and self.signin.busy

    def _sign_in(self, reason):
        """The sign-in window; its button sends the browser to Pred.gg's page."""
        from .signin import SignInWindow

        if self.signin is None:
            self.signin = SignInWindow(
                lambda ready, cancel: predgg.sign_in(
                    default_session(), on_ready=ready, cancel=cancel
                ),
                icon=self.icon,
            )
            self.signin.finished.connect(self._sign_in_done)
        if not self.signin.busy and self.signin.state != "intro":
            self.signin.show_intro()
        self.log(f"sign-in window opened ({reason})")
        self.signin.bring_forward(self.tray.geometry())

    def _sign_in_done(self, name, error):
        self.session_checked = -SESSION_CHECK  # look at the file again on the next refresh
        if error is not None:
            self.log(f"sign-in failed: {type(error).__name__}: {error}")
            return  # the window says it
        self.log("signed in from the browser")
        self.runtime.signed_in()
        self.announce_sign_in = False  # the window said it, with the name

    def _set_autostart(self, on):
        try:
            autostart.set_enabled(on)
        except OSError as exc:
            self.notify("Could not change autostart", str(exc))
        self._sync_autostart()

    def _sync_autostart(self):
        self.autostart_action.blockSignals(True)
        self.autostart_action.setChecked(autostart.enabled())
        self.autostart_action.blockSignals(False)

    def _set_auto_update(self, on):
        self.settings["auto_update"] = on
        self._save()

    def _update_clicked(self):
        state = self.updater.state
        if state in ("available", "ready") and not updater.installable():
            QDesktopServices.openUrl(QUrl(updater.RELEASES_PAGE))  # a source checkout
        elif state == "ready":
            self._install()
        elif state == "available":  # also a release whose earlier install failed
            self.install_when_ready = True
            self.was_busy = True  # its end is noticed even if it fails before the next look
            self.updater.fetch()
        else:
            self.manual_check = True
            self.was_busy = True
            self.updater.check(download_too=self.settings["auto_update"])
        self.refresh()

    def _install(self):
        tag = self.updater.release["tag"]
        try:
            updater.apply(self.updater.staged, tag)
        except OSError as exc:  # no PowerShell, or it may not run: do not try every second
            self.updater.give_up(f"update {tag} not started: {exc}")
            self.settings["skip_update"] = tag
            self._save()
            self.notify("Update failed", f"{tag} could not be installed: {exc}")
            return
        self.log(f"installing update {tag}")
        self.notify(f"Updating to {tag}", "Pred Overlay restarts in a few seconds.")
        QTimer.singleShot(500, lambda: self.quit("update"))

    def _sync_updates(self, view):
        """Schedule checks, say what was found, install when it is a good moment."""
        u = self.updater
        if u.due():
            u.check(download_too=self.settings["auto_update"])
        tag = u.release["tag"] if u.release else None
        text = {
            "checking": "Checking for updates\u2026",
            "downloading": f"Downloading update {tag}\u2026",
            "available": f"Install update {tag}" if updater.installable() else f"Get {tag}",
            "ready": f"Install update {tag} (restarts)",
        }.get(u.state, "Check for updates")
        self.update_action.setText(text)
        self.update_action.setEnabled(not u.busy)
        finished = self.was_busy and not u.busy
        self.was_busy = u.busy
        auto = self.settings["auto_update"] and tag != u.skip
        key = (u.state, tag)
        if u.state in ("available", "ready") and key not in self.notified:
            self.notified.add(key)
            if self.install_when_ready:
                pass  # the user just asked to install it
            elif u.state == "ready" and auto:
                self.notify(f"Update {tag} ready", "It installs when you are not in a match.")
            elif u.state == "available" or not auto:
                self.notify(f"Update {tag} available", "Right-click the tray icon to install it.")
        if finished and self.manual_check:
            self.manual_check = False
            if u.state == "current":
                self.notify("Pred Overlay is up to date", f"Version {__version__}.")
            elif u.state == "none":
                self.notify("No release found", "GitHub shows no Pred Overlay release to install.")
            elif u.state == "error":
                self.notify("Could not check for updates", u.error or "")
        if self.install_when_ready and finished and u.state != "ready":
            # The download the user asked for ended without a folder to install (the
            # updater is back at "available", or at "error"). Said once, and not wished
            # for any longer: the next download must wait for a good moment again.
            self.install_when_ready = False
            self.notify("Could not install the update", u.error or "The download failed.")
        # Not by itself during a sign-in: the restart would close the listener the
        # browser is about to answer.
        quiet = view.phase in ("menu", "closed") and not self.signing_in
        if u.state == "ready" and (self.install_when_ready or (auto and quiet)):
            self.install_when_ready = False
            self._install()

    def _open_logs(self):
        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(folder)

    def _about(self):
        QMessageBox.about(
            None,
            "Pred Overlay",
            f"<b>Pred Overlay {__version__}</b><br><br>"
            "Player cards over Predecessor's draft, loading screen and TAB scoreboard.<br>"
            "Reads the game's log file and its screen, and looks players up on "
            "Pred.gg with your own Pred.gg account; never touches the game process.<br><br>"
            "Not affiliated with or endorsed by Omeda Studios or Pred.gg.<br><br>"
            f'<a href="{PROJECT_URL}">{PROJECT_URL}</a>',
        )

    def _show_preview(self):
        if self.preview is not None:
            return
        from .overlay import window as overlay_window

        self.preview = overlay_window.Overlay(
            overlay_window.PreviewTracker(),
            hotkey=self.settings["hotkey"],
            force_foreground=True,
            screen_index=self.settings["screen"],
        )
        self.preview.show()
        self.preview_action.setEnabled(False)
        QTimer.singleShot(PREVIEW_SECONDS * 1000, self._close_preview)

    def _close_preview(self):
        if self.preview is not None:
            self.preview.set_paused(True)
            self.preview.deleteLater()
            self.preview = None
        self.preview_action.setEnabled(True)

    def _message_clicked(self):
        if getattr(self, "last_notice", None) == "Sign-in needed":
            self._sign_in("notification")  # "Sign-in needed" clicked

    def _activated(self, reason):
        """A left click: the sign-in panel while it is needed or running, else the menu."""
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return  # a double click is two clicks; it opens nothing more
        signin = self.signin
        if signin is not None and (signin.busy or signin.isVisible()):
            signin.toggle(self.tray.geometry())
        elif self.health == "login":
            self._sign_in("tray icon")
        else:
            self.menu.popup(QCursor.pos())
            _to_front(self.menu)

    # --- status ----------------------------------------------------------------

    def _health(self, view):
        """The view's health; with the game closed, also a missing session.

        The core reads nothing while the game is closed, so it cannot know; a local
        look at the file (no network) lets the tray ask for a sign-in before the
        game starts rather than over it. The file is looked at again when it changes
        (a sign-in or sign-out from the console), and once a minute anyway.
        """
        if view.health != "ok" or view.phase != "closed":
            return view.health
        now = time.monotonic()
        try:
            stamp = default_session().stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp != self.session_stamp or now - self.session_checked >= SESSION_CHECK:
            self.session_checked, self.session_stamp = now, stamp
            self.session_ok = predgg.Session(default_session()).signed_in()
        return "ok" if self.session_ok or self.signing_in else "login"

    def refresh(self):
        view = self.runtime.snapshot
        self._sync_updates(view)
        health = self._health(view)
        text = status_text(view, self.settings["enabled"], health)
        self.status_action.setText(text)
        self.tray.setToolTip(f"Pred Overlay {__version__}\n{text}")
        quiet = health != "ok" or not self.settings["enabled"]
        self.tray.setIcon(self.paused_icon if quiet else self.icon)
        if health != self.health:
            if health == "login" and self.signin is not None and self.signin.isVisible():
                pass  # the sign-in window is open: it says what to do
            elif health == "login":
                self.notify(
                    "Sign-in needed",
                    "Pred Overlay is not signed in on Pred.gg. Click here to sign in.",
                )
            elif health == "ok" and self.health == "login" and self.announce_sign_in:
                self.notify("Signed in", "The overlay is ready.")
            if health == "ok":
                self.announce_sign_in = True
            self.health = health

    def notify(self, title, message):
        self.last_notice = title
        self.log(f"notice: {title}")
        self.tray.showMessage(title, message, self.icon, 8000)

    def already_running(self):
        self.notify("Pred Overlay is already running", "Its icon is in the tray.")

    def quit(self, reason):
        self.quit_reason = reason
        self.timer.stop()
        self._close_preview()
        self.runtime.stop()
        self.tray.hide()
        self.app.quit()


def _to_front(widget):
    """Make a popup the foreground window, as Windows' own tray menus do.

    Opened from a click in the taskbar's hidden-icons flyout, a popup that is not in
    the foreground stays behind that flyout; once it is, the flyout closes itself.
    """
    widget.activateWindow()
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.SetForegroundWindow(int(widget.winId()))
