"""The tray icon: status, on/off, region, sign-in, autostart, logs, quit."""

import os
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QAction, QActionGroup, QIcon
from PySide6.QtWidgets import QMenu, QMessageBox, QSystemTrayIcon

from . import __version__, autostart
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
    def __init__(self, app, runtime, settings, log, *, quiet_start=False):
        super().__init__()
        self.app, self.runtime, self.settings, self.log = app, runtime, settings, log
        self.icon = app_icon()
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
        self.refresh()
        if not settings["welcomed"]:
            self.notify(
                "Pred Overlay is running",
                "It appears in the draft, on the loading screen and while you hold TAB. "
                f"{settings['hotkey']} pins the panel in game. Right-click this icon for options.",
            )
            settings["welcomed"] = True
            self._save()
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
