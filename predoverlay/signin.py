"""The sign-in flyout: one button that sends the browser to Pred.gg, and the answer.

It opens from the tray icon like Windows' own tray flyouts: a small dark panel
above the taskbar's corner that slides in, and steps aside when focus leaves.
Stepping aside does not stop a sign-in: the panel hides while the browser is in
front and comes back with the answer. Only Cancel stops one.

The user signs in on Pred.gg's own website in their default browser, where Pred.gg
usually remembers them; this panel never shows a password field. It says what
to check first, waits for Pred.gg's answer on a thread and shows the result.
"""

import sys
import threading
import time
import webbrowser

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

FONT = "'Segoe UI Variable Text', 'Segoe UI'"
STYLE = f"""
QWidget#signin {{ background: #2B2B2B; }}
QFrame#footer {{ background: #202020; border-top: 1px solid #1A1A1A; }}
QLabel {{ color: #CFCFCF; font-family: {FONT}; font-size: 14px; }}
QLabel#brand {{ color: #A0A0A0; font-size: 12px; }}
QLabel#title {{ color: #FFFFFF; font-family: 'Segoe UI Variable Display', 'Segoe UI';
    font-size: 20px; font-weight: 600; }}
QLabel#check {{ color: #A0A0A0; font-size: 12px; }}
QLabel#note {{ color: #7A7A7A; font-size: 11px; }}
QPushButton {{
    font-family: {FONT}; font-size: 14px; min-height: 30px; padding: 0 14px;
    color: #FFFFFF; background: #373737; border: 1px solid #434343; border-radius: 4px;
}}
QPushButton:hover {{ background: #3C3C3C; }}
QPushButton:pressed {{ background: #323232; color: #CFCFCF; }}
QPushButton#primary {{ color: #1A1405; background: #EDD01E; border: 1px solid #F2D94A; }}
QPushButton#primary:hover {{ background: #F2D94A; }}
QPushButton#primary:pressed {{ background: #D9BE1B; }}
"""

INTRO = (
    "Pred Overlay looks players up on Pred.gg with your own Pred.gg account. Your "
    "browser opens Pred.gg: sign in there, or create an account, and allow Pred Overlay."
)
CHECK = "Any Pred.gg account works: it only lets the overlay read what Pred.gg shows."
WAITING = (
    "Pred.gg is open in your browser. Once you allow Pred Overlay there, "
    "this panel comes back with the answer."
)
WIDTH = 360  # Windows' own tray flyouts are about this wide
MARGIN = 12  # from the screen edge and the taskbar
SLIDE, SLIDE_MS = 12, 170  # the panel rises this far into place
DONE_CLOSE = 4000  # ms a finished sign-in stays on screen
REOPEN_GAP = 0.4  # s: a click on the tray icon that just put the panel away does not reopen it


class SignInWindow(QWidget):
    """``finished(name, error)`` fires on the UI thread once an attempt ends."""

    finished = Signal(object, object)
    _answer = Signal(object, object)  # from the worker thread

    def __init__(self, sign_in, icon=None):
        """``sign_in(on_ready, cancel)`` signs in and returns the account's name."""
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        super().__init__(None, flags)
        self.sign_in = sign_in
        self.busy = False
        self.page = None  # the local link that leads to Pred.gg, while an attempt runs
        self.cancel = None
        self.anchor = None  # the tray icon's rectangle on screen, when Windows gives it
        self.animation = None
        self.hidden_at = 0.0  # monotonic time the panel last went away
        self.setObjectName("signin")
        self.setWindowTitle("Pred Overlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        if icon is not None:
            self.setWindowIcon(icon)
        self.setStyleSheet(STYLE)
        self.setFixedWidth(WIDTH)
        self._answer.connect(self._done)
        self._build(icon)
        self.show_intro()

    # --- layout -----------------------------------------------------------------

    def _build(self, icon):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QVBoxLayout()
        body.setContentsMargins(20, 16, 20, 18)
        body.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        if icon is not None:
            mark = QLabel()
            mark.setPixmap(icon.pixmap(16, 16))
            head.addWidget(mark)
        brand = QLabel("Pred Overlay")
        brand.setObjectName("brand")
        head.addWidget(brand)
        head.addStretch(1)
        body.addLayout(head)
        body.addSpacing(4)
        self.title = QLabel()
        self.title.setObjectName("title")
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.check = QLabel(CHECK)
        self.check.setObjectName("check")
        self.check.setWordWrap(True)
        self.note = QLabel(
            "You sign in on Pred.gg's website, never here. "
            "Not affiliated with Pred.gg or Omeda Studios."
        )
        self.note.setObjectName("note")
        self.note.setWordWrap(True)
        for widget in (self.title, self.body, self.check):
            body.addWidget(widget)
        body.addSpacing(2)
        body.addWidget(self.note)
        outer.addLayout(body)
        footer = QFrame()
        footer.setObjectName("footer")
        buttons = QHBoxLayout(footer)
        buttons.setContentsMargins(20, 14, 20, 14)
        buttons.setSpacing(8)
        self.primary = QPushButton()
        self.primary.setObjectName("primary")
        self.primary.setCursor(Qt.CursorShape.PointingHandCursor)
        self.primary.clicked.connect(self._primary)
        self.secondary = QPushButton()
        self.secondary.clicked.connect(self._secondary)
        buttons.addWidget(self.primary, 1)
        buttons.addWidget(self.secondary, 1)
        outer.addWidget(footer)

    def _show(self, state, title, body, primary, secondary, check=False):
        self.state = state
        self.title.setText(title)
        self.body.setText(body)
        self.check.setVisible(check)
        self.primary.setText(primary)
        self.primary.setVisible(bool(primary))
        self.secondary.setText(secondary)
        self.secondary.setVisible(bool(secondary))
        self.adjustSize()
        if self.isVisible():
            QTimer.singleShot(0, self._settle)  # once the labels have re-wrapped

    def _settle(self):
        """The height changed: keep the bottom edge where it was."""
        self.adjustSize()
        self.move(self._spot())

    def show_intro(self):
        self._show("intro", "Sign in with Pred.gg", INTRO, "Sign in on Pred.gg", "Not now", True)

    # --- actions ----------------------------------------------------------------

    def _primary(self):
        if self.state in ("intro", "error"):
            self.start()
        elif self.state == "waiting" and self.page:
            webbrowser.open(self.page)  # the tab was closed: the same attempt goes on
        elif self.state == "done":
            self.hide()

    def _secondary(self):
        if self.state == "waiting" and self.cancel is not None:
            self.cancel.set()  # Cancel: the attempt stops and the panel starts afresh
        self.hide()

    def start(self):
        if self.busy:
            return
        self.busy, self.page = True, None
        self.cancel = threading.Event()
        self._show("waiting", "Finish in your browser", WAITING, "Open the page again", "Cancel")
        threading.Thread(
            target=self._work, args=(self.cancel,), daemon=True, name="pred-sign-in"
        ).start()

    def _work(self, cancel):
        def ready(url):  # a plain attribute set; read on the UI thread only to reopen it
            self.page = url

        try:
            name = self.sign_in(ready, cancel)
        except Exception as exc:  # the messages carry no tokens (see auth.oauth_login)
            self._answer.emit(None, exc)
        else:
            self._answer.emit(name, None)

    def _done(self, name, error):
        self.busy, self.page = False, None
        cancelled = self.cancel is not None and self.cancel.is_set()
        self.finished.emit(name, error)
        if cancelled:
            self.show_intro()  # cancelled on purpose; a reopen starts afresh
            return
        if error is None:
            self._show(
                "done", "Signed in", f"Signed in as {name}. The overlay is ready.", "Done", ""
            )
            QTimer.singleShot(DONE_CLOSE, self._close_if_done)
        else:
            self._show(
                "error",
                "Sign-in did not finish",
                str(error) or type(error).__name__,
                "Try again",
                "Close",
                check=True,
            )
        from .overlay import win32

        if not win32.game_in_front():  # never pull focus out of a match
            self.bring_forward()

    def _close_if_done(self):
        if self.state == "done":
            self.hide()

    # --- the flyout ---------------------------------------------------------------

    def _spot(self):
        """Above the taskbar's corner, under the tray icon when Windows says where it is."""
        anchor = self.anchor
        point = anchor.center() if anchor is not None and anchor.isValid() else None
        screen = (QGuiApplication.screenAt(point) if point else None) or (
            QGuiApplication.primaryScreen()
        )
        area = screen.availableGeometry()
        width, height = self.width(), self.sizeHint().height()
        x = area.right() - width - MARGIN + 1
        if point is not None:
            x = min(max(point.x() - width // 2, area.left() + MARGIN), x)
        y = area.bottom() - height - MARGIN + 1
        if point is not None and point.y() < area.center().y():
            y = area.top() + MARGIN  # a taskbar at the top
        return QPoint(x, y)

    def bring_forward(self, anchor=None):
        """Slide in above the tray (or come back in front of the browser)."""
        if anchor is not None:
            self.anchor = anchor
        self.adjustSize()
        spot = self._spot()
        if not self.isVisible():
            self.move(spot + QPoint(0, SLIDE))
            self.setWindowOpacity(0.0)
            self.show()
            self._rise(spot)
        else:
            self.move(spot)
        self.raise_()
        self.activateWindow()
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.SetForegroundWindow(int(self.winId()))

    def _rise(self, spot):
        group = QParallelAnimationGroup(self)
        for prop, end in ((b"pos", spot), (b"windowOpacity", 1.0)):
            animation = QPropertyAnimation(self, prop, group)
            animation.setDuration(SLIDE_MS)
            animation.setEndValue(end)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            group.addAnimation(animation)
        self.animation = group
        group.start()

    def toggle(self, anchor=None):
        """A click on the tray icon: open, or put away if it is open."""
        if self.isVisible():
            self.hide()
        elif time.monotonic() - self.hidden_at > REOPEN_GAP:
            self.bring_forward(anchor)
        # else: the click on the icon took the focus and put the panel away; leave it

    def hideEvent(self, event):
        self.hidden_at = time.monotonic()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        _round_corners(int(self.winId()))

    def event(self, event):
        # Like Windows' tray flyouts: a click elsewhere puts the panel away. A running
        # sign-in goes on; the answer brings the panel back.
        if event.type() == QEvent.Type.WindowDeactivate:
            QTimer.singleShot(0, self.hide)
        return super().event(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        event.ignore()  # Alt+F4 puts the panel away like a click elsewhere; Cancel stops
        self.hide()


def _round_corners(hwnd):
    """Windows 11 corners and border for a frameless window; older Windows keeps square."""
    if sys.platform != "win32":
        return
    import ctypes

    dwm = ctypes.windll.dwmapi
    for attribute, value in (
        (20, 1),  # DWMWA_USE_IMMERSIVE_DARK_MODE
        (33, 2),  # DWMWA_WINDOW_CORNER_PREFERENCE: DWMWCP_ROUND
        (34, 0x003A3A3A),  # DWMWA_BORDER_COLOR (0x00BBGGRR)
    ):
        number = ctypes.c_int(value)
        dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(number), ctypes.sizeof(number))
