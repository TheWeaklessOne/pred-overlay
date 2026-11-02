"""The tray icon: status, on/off, region, sign-in, autostart, logs, quit."""

import os
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QIcon
from PySide6.QtWidgets import QMenu, QMessageBox, QSystemTrayIcon

from . import __version__, autostart, install, updater
from . import settings as settings_module
from .config import log_dir

ASSETS = Path(__file__).with_name("assets")
PROJECT_URL = "https://github.com/TheWeaklessOne/pred-overlay"
PREVIEW_SECONDS = 29  # one full cycle of the preview: draft, loading, TAB, menu
PHASE_TEXT = {
    "menu": "Ready · waiting for a match",
    "accepted": "Match found",
    "draft": "In draft",
    "loading": "In match",
}
REGION_TEXT = {
    "EUROPE": "Europe",
    "NA": "North America",
    "SA": "South America",
    "ASIA": "Asia",
    "OCE": "Oceania",
    "MENA": "Middle East",
}


def app_icon():
    return QIcon(str(ASSETS / "icon.ico"))


def status_text(view, enabled):
    if not enabled:
        return "Paused"
    if view.health == "login":
        return "Sign-in needed: start Steam"
    if view.health == "offline":
        return "Omeda not answering, retrying"
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
        self.icon = app_icon()
        fresh = not settings["welcomed"]
        integrated = install.integrate(settings, log)  # once: shortcuts (and autostart if new)
        self.paused_icon = QIcon(self.icon.pixmap(64, QIcon.Mode.Disabled))
        self.tray = QSystemTrayIcon(self.icon)
        self.tray.activated.connect(self._activated)
        self.preview = None
        self.health = None
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
        regions = menu.addMenu("Queue region")
        group = QActionGroup(regions)
        for code in settings_module.REGIONS:
            action = QAction(REGION_TEXT[code], regions, checkable=True)
            action.setChecked(code == self.settings["region"])
            action.triggered.connect(lambda _=False, c=code: self._set_region(c))
            group.addAction(action)
            regions.addAction(action)
        self.sign_in_action = menu.addAction("Sign in with Steam again")
        self.sign_in_action.triggered.connect(self._sign_in)
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
        menu.addAction("Quit").triggered.connect(self.quit)
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

    def _set_region(self, code):
        self.settings["region"] = code
        self._save()
        self.runtime.set_region(code)

    def _sign_in(self):
        self.log("sign-in requested from the tray")
        self.runtime.sign_in()
        self.notify("Signing in", "Asking Steam for a new session…")

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
        QTimer.singleShot(500, self.quit)

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
            elif u.state == "error":
                self.notify("Could not check for updates", u.error or "")
        if u.state == "ready" and (self.install_when_ready or (auto and view.phase == "menu")):
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
            "Reads the game's log file and the game backend with your own account; "
            "never touches the game process.<br><br>"
            "Not affiliated with or endorsed by Omeda Studios.<br><br>"
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

    def _activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_preview()

    # --- status ----------------------------------------------------------------

    def refresh(self):
        view = self.runtime.snapshot
        self._sync_updates(view)
        text = status_text(view, self.settings["enabled"])
        self.status_action.setText(text)
        self.tray.setToolTip(f"Pred Overlay {__version__}\n{text}")
        quiet = view.health != "ok" or not self.settings["enabled"]
        self.tray.setIcon(self.paused_icon if quiet else self.icon)
        if view.health != self.health:
            if view.health == "login":
                self.notify(
                    "Sign-in needed",
                    "Start Steam with the account you play Predecessor on. "
                    "The overlay retries by itself, or use “Sign in with Steam again”.",
                )
            elif view.health == "ok" and self.health == "login":
                self.notify("Signed in", "The overlay is ready.")
            self.health = view.health

    def notify(self, title, message):
        self.log(f"notice: {title}")
        self.tray.showMessage(title, message, self.icon, 8000)

    def already_running(self):
        self.notify("Pred Overlay is already running", "Its icon is in the tray.")

    def quit(self):
        self.timer.stop()
        self._close_preview()
        self.runtime.stop()
        self.tray.hide()
        self.app.quit()
