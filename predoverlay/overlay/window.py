"""The overlay window: a click-through, never-focused panel sized to its content.

It polls the tracker's immutable snapshot and the keyboard every 50 ms, picks a
layout for the current screen (menu badge, draft, loading screen) and animates
the panel in and out. In the match nothing shows by itself: while TAB is held,
the scoreboard row under the cursor brings up that player's card, which uses
a quick 90/140 ms fade to keep up with the game's instant scoreboard.

Motion has one idea: the panel enters and leaves through its gold rule. Moving
from the draft to the loading screen, the body folds into the rule, the rule
draws in to its diamond and the same thing opens at the new place (motion.Seam);
the match start closes it the same way. Other shows and hides are plain fades.
Data that lands runs through the rows as a wave; a rank landing strikes once.
Everything is one-shot and nothing repaints while nothing moves: waiting cells
breathe for three cycles and then hold still.

The window never takes focus or input. Keys are polled with GetAsyncKeyState and
the game is recognised by its foreground window class and title only; nothing
here opens a handle to the game process.
"""

import dataclasses
import math
import queue
import time

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QEasingCurve,
    QIODevice,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QCursor, QGuiApplication, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from .. import modes
from ..errors import describe, stack
from . import capture as captures
from . import namereader, paint, roster, scoreboard, screens, win32
from .motion import Badge, Seam
from .preview import PreviewTracker, preview_view  # noqa: F401  (app, tray, tests use these)
from .rows import (
    changed_groups,
    row,
    shown_name,
    team_balance,
    team_rank,
)
from .visibility import Visibility, on_loading_screen

_OUT_CUBIC = QEasingCurve(QEasingCurve.Type.OutCubic)
_IN_CUBIC = QEasingCurve(QEasingCurve.Type.InCubic)
# Fade-outs ease in and out: InCubic ends at full speed, a visible cut on the last frame.
_EXIT = QEasingCurve(QEasingCurve.Type.InOutCubic)
_OUT_QUART = QEasingCurve(QEasingCurve.Type.OutQuart)

ENTER, EXIT, TAB_ENTER, TAB_EXIT = 0.18, 0.14, 0.09, 0.14
ROW_STAGGER, ROW_TIME, RULE_TIME = 0.03, 0.18, 0.30
PULSE, PULSES = 1.6, 3  # a waiting cell breathes this long, then holds still
RISE_DRAFT, RISE_LOADING = 4, 8  # in the draft the player is reading the game's screen
WAVE_STEP, WAVE_MAX, WAVE_OVERLAP = 0.035, 0.35, 0.10  # data landing on several rows
GLINT_AFTER, GLINTS = 0.08, 2  # a high peak shines once the wave has passed, twice at most
GLINT_TIERS = ("master", "paragon")
BADGE_STRIKE_AFTER = 0.06

# The plate while a phase has no names yet: what comes, not the phase's bare name.
EMPTY_STATUS = {"draft": "team shows as roles are picked"}

# Frame pacing (ms): the screen's refresh while animating, within these bounds; 30 fps
# for the waiting pulse. WARM_AFTER: the off-screen warm-up paint after start.
FRAME_MS, FRAME_MS_MIN, PULSE_MS, WARM_AFTER = 16, 8, 33, 3000

# Design-pixel anchors (2560x1440), measured on the game's draft and loading screens.
STACK_W = 640
STACK_RIGHT, STACK_BOTTOM = 54, 96
STACK_LEFT, STACK_TOP = 64, 200
CARD_GAP = 32  # between the cursor and the card
ACTIONS_GAP = 12  # between the game's strip by the hovered row and the card
CARD_MOVE = 0.14  # the card flies to another row of its team
CARD_CROSS = 0.20  # ... or across to the other team
HOVER_GRACE = 0.3  # the card stays while the cursor crosses the gap between rows
GROW = 0.24  # the panel grows out of the status plate
GROW_ROWS_AFTER = 0.12  # rows start once the panel has some room
STATUS_OUT = 0.10
SHORTEST_FADE = 0.35  # a reversed fade takes at least this share of its full time
READ_AFTER = 0.25  # seconds of TAB before the scoreboard is read (it must be drawn)
END_READ_AFTER = 2.0  # seconds after the match ended before its screen is read
LOADING_FRAME_AT = 3.0  # seconds into the loading screen when its frame is kept (recorder)
PROBE_EVERY = 0.5  # seconds between looks for the loading screen's pixels
ANIMATION_BOUND = 0.3  # seconds a screen reading waits for the panel's motion, at most
# The menu badge sits in the empty right end of the sub-menu row, under the
# profile block (design pixels from the 2560x1440 main menu).
BADGE_RIGHT, BADGE_TOP = 16, 98


def choose_layout(phase):
    """Where the panel sits: menu badge, draft panel or loading-screen panel.

    In the match the panel shows only when pinned with F8, in the loading
    place; the per-player card is chosen by the window, not here.
    """
    if phase == "menu":
        return "menu"
    if phase == "draft" or phase == "accepted":
        return "draft"
    return "loading"


def panel_teams(view, layout):
    """The players the panel lists: (allies, enemies).

    The draft lists only your own team (no enemy rows and no enemy header): the
    overlay adds nothing about the other team before the match is set. The
    loading screen and the TAB card show both teams.
    """
    if layout == "draft":
        return view.allies, ()
    return view.allies, view.enemies


def image_bytes(image):
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "BMP")  # Windows OCR decodes it alike; PNG cost 130 ms a TAB reading
    return bytes(data)


def versions(part, cuts=roster.CUTS["loading"], narrow=None):
    """One name box for OCR: as it is (twice the size), then dark text on white at each
    brightness cut (three times the size), then (``narrow``) its left part alone. Image
    bytes each."""
    smooth = Qt.TransformationMode.SmoothTransformation
    keep = Qt.AspectRatioMode.KeepAspectRatio
    found = [image_bytes(part.scaled(part.width() * 2, part.height() * 2, keep, smooth))]
    gray = part.convertToFormat(QImage.Format.Format_Grayscale8)
    width, height, line = gray.width(), gray.height(), gray.bytesPerLine()
    data = bytes(gray.constBits())[: line * height]
    for cut in cuts:
        table = bytes(0 if value >= cut else 255 for value in range(256))
        cut_image = QImage(
            data.translate(table), width, height, line, QImage.Format.Format_Grayscale8
        )
        cut_image = cut_image.copy()  # own the pixels: the bytes above go away
        found.append(image_bytes(cut_image.scaled(width * 3, height * 3, keep, smooth)))
    if narrow and narrow < part.width():
        left = part.copy(0, 0, narrow, part.height())
        found.append(image_bytes(left.scaled(left.width() * 2, left.height() * 2, keep, smooth)))
    return found


@dataclasses.dataclass(frozen=True)
class Card:
    """What the TAB card shows: the scoreboard row under the cursor and who is in it.

    ``data`` is the player's panel row, or None while the board is still being read
    (the card is then a skeleton); ``seen`` is what the board says they play in this
    match ({"hero", "role"}); ``hit`` is the row's (team, index). ``anchor`` is the
    screen x where the card was entered: it stays beside that point on its team.
    """

    data: dict | None
    seen: dict | None
    hit: tuple
    anchor: int | None = None
    dy: int = 0  # the board's rows sit this much lower (the end-of-match screen)
    others: tuple | None = None  # a row that cannot be told: its team's console players

    @property
    def team(self):
        return self.hit[0]


def _ease(curve, value):
    return curve.valueForProgress(max(0.0, min(1.0, value)))


# Rows a side on the scoreboard. The boards read are 5v5 ones, and how many seats the
# other screens have given so far says nothing about the board: none (the app started in
# the middle of a match) or six (a misread not yet retired) is when it is needed most.
SIDE = roster.TEAM


def _pick_screen(index):
    screens = QGuiApplication.screens()
    if not screens:
        return None
    primary = QGuiApplication.primaryScreen()
    ordered = [primary] + [screen for screen in screens if screen is not primary]
    return ordered[index] if 0 <= index < len(ordered) else ordered[0]


class Overlay(QWidget):
    """A panel that draws ``tracker.snapshot`` and never takes focus or clicks."""

    def __init__(
        self,
        tracker,
        *,
        hotkey="F8",
        force_foreground=False,
        screen_index=0,
        clock=time.time,
        log=None,
        recorder=None,
        capture=None,
        names=None,
    ):
        """``capture``: a capture.Capture that saves frames (capture mode), or None.
        ``names``: the overlay's model (namereader.READER), or None for Windows OCR alone."""
        if QGuiApplication.instance() is None:
            raise RuntimeError("Create a QApplication before the overlay window.")
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setWindowTitle("Pred Overlay")
        self.tracker = tracker
        self.hotkey = win32.VK[hotkey.upper()]
        self.force_foreground = force_foreground
        # The preview: sample data over whatever program is in front. Nothing of the
        # screen is read for it, and it points at a board row by itself.
        self.preview = isinstance(tracker, PreviewTracker)
        self.clock = clock
        self.screen_index = screen_index
        self.target_screen = None
        self._choose_screen()
        self.visibility = Visibility()
        self.view = None
        self.layout_name = None
        self.allies, self.enemies = [], []
        self.roster_ids = ()
        self.design_size = (STACK_W, 44)
        self.scale = 1.0
        self.rows_started = 0.0
        self.shown = False  # target state
        self.fade_from, self.fade_started, self.fade_duration = 0.0, 0.0, ENTER
        self.revealed_at = 0.0
        self.ticks = 0
        self._drawn = None
        # One reader per board: the TAB scoreboard and the end-of-match screen.
        self.readers = {
            "tab": scoreboard.Reader(names=names),
            "end": scoreboard.Reader(patient=True, names=names),
        }
        self.names = names  # the overlay's model, read first (namereader); None: OCR alone
        self.notes = {}  # reader -> its last note logged
        self.board = "tab"
        self.reader_error = self.cursor_logged = None
        self.reader_logged = self.hit_logged = self.spent_logged = None
        self.failures = {}  # tick, frame, paint -> the last failure logged
        self.log = log or (lambda message: None)
        self.recorder = recorder  # recorder.Recorder: frames of the match go with its events
        self.capture = capture
        self.roster_reader = roster.Reader(names=names)
        self.roster_key, self.roster_last, self.roster_count = None, None, 0
        self.roster_settled = 0  # readings since the panel had every name
        self.roster_error = None
        # The loading screen of this phase: readings started, why a tick read nothing,
        # whether its frame was kept (see _loading_over, _loading_frame)
        self.loading_phase, self.loading_reads, self.loading_skips = None, 0, {}
        self.loading_framed = self.loading_said = False
        # What the pixels say (screens.py): the loading screen is read while it shows,
        # whatever the log's phase says; each change is logged and recorded
        self.probe_at, self.probe_seen = 0.0, None
        self.held_since = None  # when a due reading began to wait for the panel's motion
        self.board_posted = None
        self.tab_since = None
        self.card = None  # a Card while the TAB card shows or fades
        self.badge = Badge()  # the menu row's motion
        self.revealed_key = None  # what was last revealed: no replay for the same content
        self.frozen_at = None  # row/rule motion stops while hiding
        self.rows_memory = {}  # player -> last painted row, to see which fields changed
        self.mixes = {}  # player -> {field group: (old row, changed at)}
        self.landed = set()  # players whose rank has arrived: it strikes only once
        self.peaks_seen, self.glints = set(), 0  # a peak shines once, GLINTS per panel
        self.wave_free = 0.0  # when the data wave under way has reached its last row
        self.heads, self.head_at = {}, {}  # team -> what its header says, when that arrived
        self.grow = None  # (from height, started, old status, its alpha) while growing
        self.rows_reverse = False  # rows arrive bottom-up: the panel grew from the bottom
        self.seam, self.seam_shot = None, None  # the close/open and a picture of the panel
        self.seam_moved = False
        self.badge_at = None  # when the menu appeared: its diamond strikes once
        self.card_prev = None  # (old Card, swapped at): the card content being replaced
        self.card_move = None  # (from QRect, started, duration, curve): the card in flight
        self.card_bar = None  # (old team, started, duration) while the bar changes colour
        self.card_lost = None  # when the cursor left the card's row (TAB still held)
        self.card_reveal, self.card_tab = None, None  # the table's entrance, once per TAB
        app = QGuiApplication.instance()
        # Monitors come and go (display mode switches, a streaming display, sleep):
        # pick the screen again instead of holding a deleted one.
        app.screenAdded.connect(self._screens_changed)
        app.screenRemoved.connect(self._screens_changed)
        app.primaryScreenChanged.connect(self._screens_changed)
        # Precise timers: Windows' coarse ones fire late (the 50 ms poll every 62 ms).
        self.poll = QTimer(self)  # coarse is enough: motion has its own frame timer
        self.poll.setInterval(50)
        self.poll.timeout.connect(self.tick)
        self.frame = QTimer(self)
        self.frame.setTimerType(Qt.TimerType.PreciseTimer)
        self.frame.setInterval(FRAME_MS)
        self.frame.timeout.connect(self._frame_step)
        self.poll.start()
        QTimer.singleShot(WARM_AFTER, self.warm)

    def set_paused(self, paused):
        """The tray switch: stop reading and drawing entirely, or start again."""
        if paused:
            self.poll.stop()
            self.frame.stop()
            self.shown, self.fade_started, self.card = False, 0.0, None
            self.seam = self.seam_shot = None
            self.visibility = Visibility()  # an F8 pin does not survive a pause
            self.hide()
        elif not self.poll.isActive():
            self.poll.start()

    # --- state ---------------------------------------------------------------

    def _progress(self, now):
        """Current panel opacity progress 0..1 including the in-flight fade."""
        if self.fade_started == 0.0:
            return 1.0 if self.shown else 0.0
        t = (now - self.fade_started) / self.fade_duration
        if self.shown:
            value = self.fade_from + (1 - self.fade_from) * _ease(_OUT_CUBIC, t)
        else:
            value = self.fade_from * (1 - _ease(_EXIT, t))
        return max(0.0, min(1.0, value))

    def tick(self):
        """The 50 ms poll. A failure is logged once per kind and the next tick tries
        again: one broken frame must not flood the log or stop the panel for good."""
        try:
            self._tick()
            self.failures.pop("tick", None)
        except Exception as exc:
            self._failed("tick", exc)

    def _failed(self, where, exc):
        """A step that raised: no 60 fps while it is broken, and one log line per failure."""
        self.frame.stop()
        message = describe(exc)
        if self.failures.get(where) != message:
            self.failures[where] = message
            self.log(f"overlay {where} failed: {message}\n{stack(exc)}")

    def _tick(self):
        now = self.clock()
        self._advance_seam(now)
        view = self.tracker.snapshot
        foreground = self.force_foreground or win32.game_in_front()
        tab = getattr(self.tracker, "tab_held", None)
        if tab is None:
            tab = win32.key_down(win32.VK["TAB"])
        age = now - view.phase_at if view.phase_at else 1e9
        started_age = now - view.started_at if view.started_at else None
        visible = self.visibility.update(
            phase=view.phase,
            phase_age=age,
            tab_held=tab,
            f8_down=win32.key_down(self.hotkey),
            foreground=foreground,
            started_age=started_age,
        )
        layout = choose_layout(view.phase)
        in_match = view.phase == "loading" and not on_loading_screen(view.phase, age, started_age)
        if tab and self.tab_since is None:
            self.tab_since = now
        elif not tab:
            self.tab_since = None
        # In the match (panel not pinned), TAB + pointing at a row shows that card.
        # The end-of-match screen shows the same rows, lower, with the cursor free.
        ended = in_match and bool(view.ended_at)
        self.board = "end" if ended else "tab"
        hovering = (tab or ended) and foreground and in_match and not visible
        if self.capture is not None:
            self._capture(view, now, foreground, in_match, age, started_age)
        self._read_roster(view, now, foreground, age, started_age)
        self._loading_over(view, in_match, now)
        self._probe(view, now, foreground, age)
        card = self._hover(view, now) if hovering else None
        moved = False
        shown_card = self.layout_name == "card" and self.card and self._progress(now) > 0
        if card is None and hovering and shown_card and self.shown:
            # Between rows (or a moment off the board): hold the card a little, so
            # moving to the next player flies the card there instead of blinking.
            self.card_lost = self.card_lost or now
            point = self._cursor_design()
            board = point and scoreboard.on_board(*point, SIDE, self.board_dy)
            if board or now - self.card_lost < HOVER_GRACE:
                card = self.card
        elif card is not None:
            self.card_lost = None
        if card:
            layout, visible = "card", True
            moved = self.card is None or card.hit != self.card.hit
            if moved:
                if shown_card:  # another row: fly there from where it is, swapping the content
                    same_team = card.team == self.card.team
                    self.card_prev = (self.card, now)
                    duration, curve = (CARD_MOVE, _OUT_CUBIC) if same_team else (CARD_CROSS, _EXIT)
                    self.card_move = (self.geometry(), now, duration, curve)
                    if not same_team:
                        self.card_bar = (self.card.team, now, duration)
                    anchor = self.card.anchor if same_team else self._pointer().x()
                    if self.card.data is None:  # the skeleton gave way: the first real card
                        self._reveal_table(card, now + paint.SWAP_IN_AT)
                    self.card = dataclasses.replace(card, anchor=anchor)
                else:  # anchored where it was entered
                    self.card = dataclasses.replace(card, anchor=self._pointer().x())
                    self.card_prev = self.card_move = self.card_bar = None
                    self.revealed_at = now
                    self._reveal_table(card, now)
            elif (card.data, card.seen, card.others) != (
                self.card.data,
                self.card.seen,
                self.card.others,
            ):
                old = self.card
                if old.data is None or card.data is None or card.seen != old.seen:
                    self.card_prev = (old, now)  # skeleton -> player
                    if old.data is None:
                        self._reveal_table(card, now + paint.SWAP_IN_AT)
                self.card = dataclasses.replace(card, anchor=old.anchor)
                moved = True  # its size may change
        elif self.card is not None and not (self.layout_name == "card" and self._progress(now) > 0):
            self.card = None
        layout, visible = self._seam_gate(now, layout, visible, foreground, in_match)
        if self.layout_name not in (None, layout) and self._progress(now) > 0:
            layout, visible = self.layout_name, False  # leave where it is, then change
        if view is not self.view or layout != self.layout_name or moved:
            self._apply(view, layout, now)
        if visible != self.shown:
            self.fade_from = self._progress(now)
            self.fade_started = now
            # Fixed for this fade: the layout may change before it ends.
            card_fade = self.layout_name == "card"
            full = (
                (TAB_ENTER if visible else TAB_EXIT) if card_fade else (ENTER if visible else EXIT)
            )
            distance = abs((1.0 if visible else 0.0) - self.fade_from)
            self.fade_duration = full * max(SHORTEST_FADE, distance)
            self.shown = visible
            self.frozen_at = None if visible else now
            if visible:
                key = self._reveal_key()
                if self.fade_from < 0.05 and key != self.revealed_key:
                    self.revealed_key = key
                    self.revealed_at = now
                    self.rows_started = now
                    self.rows_reverse = False
                    if self.layout_name == "menu":
                        self.badge_at = now + BADGE_STRIKE_AFTER
                if not self.isVisible():
                    self.show()
        if self.layout_name == "menu":  # the badge follows the data every tick
            revealed = self.shown and self._progress(now) > 0
            self.badge.sync(view.health, now, revealed=revealed)
        if self.isVisible() and not self.shown and self._progress(now) <= 0.0:
            self.hide()
        if self.card_move and self.layout_name == "card":
            self._slide_card(now)
        if self.grow and now - self.grow[1] >= GROW:
            self.grow = None
        if self.card_bar and now - self.card_bar[1] >= self.card_bar[2]:
            self.card_bar = None
        self.ticks += 1
        if self.isVisible() and self.ticks % 40 == 0:
            win32.keep_on_top(int(self.winId()))
        # Frames: at the screen's pace (up to 125 a second) while something moves, at
        # 30 while only the waiting pulse breathes, none at rest.
        if self._animating(now):
            interval = self._frame_ms()
        elif self._waiting(now):
            interval = PULSE_MS
        else:
            interval = None
        if interval is None:
            if self.frame.isActive():
                self.frame.stop()
                self.update()
        else:
            if self.frame.interval() != interval:
                self.frame.setInterval(interval)
            if not self.frame.isActive():
                self.frame.start()
        if view is not self._drawn:
            self.update()

    def _read_roster(self, view, now, foreground, age, started_age):
        """Read the names on the draft or loading screen now and then (roster.py); the
        tracker gets each reading as a ("screen", ...) event."""
        events = getattr(self.tracker, "events", None)  # the preview has none
        if not isinstance(events, queue.Queue):
            return
        before_match = view.phase in ("accepted", "draft", "loading") and not view.started_at
        seen = bool(self.probe_seen) and before_match  # the pixels show the loading screen
        loading = seen or (
            view.phase != "draft" and on_loading_screen(view.phase, age, started_age)
        )
        if loading and self.loading_phase != view.phase_at:
            self.loading_phase, self.loading_reads, self.loading_skips = view.phase_at, 0, {}
            self.loading_framed = self.loading_said = False
        if loading and not self.loading_framed and age >= LOADING_FRAME_AT and foreground:
            self.loading_framed = True  # never another program's pixels on disk
            self._loading_frame()
        # The panel's motion holds a reading back a moment (the grab costs a frame), never
        # for long: the loading screen shows for half a minute, once.
        moving = self._animating(now)
        if not moving:
            self.held_since = None
        elif self.held_since is None:
            self.held_since = now
        held = moving and now - self.held_since < ANIMATION_BOUND
        if seen and not foreground:
            seen = self._loading_shows()  # the last look may predate an alt-tab
        skip = (
            "game not in front"
            if not foreground and not seen  # the loading screen's pixels show the game
            else "a reading still running"
            if self.roster_reader.busy
            else "the panel moving"
            if held
            else None
        )
        if skip:
            if loading:
                if skip == "game not in front":
                    skip = f"in front: {win32.front_kind()}"  # a class, never a title
                self.loading_skips[skip] = self.loading_skips.get(skip, 0) + 1
            return
        if loading:
            kind = "loading"  # by the pixels, or by the phase while they are not looked at
        elif view.phase == "draft":
            kind = "draft"
        else:
            return
        if (kind, view.phase_at) != self.roster_key:
            self.roster_key, self.roster_last, self.roster_count = (kind, view.phase_at), None, 0
            self.roster_settled = 0
        full = len(view.allies) == roster.TEAM and (
            kind == "draft" or len(view.enemies) == roster.TEAM
        )
        # The draft keeps reading: plates come one by one, then the pick screen and the
        # heroes picked; only the loading screen is done once every name is in.
        settled = self.roster_settled if full and kind == "loading" else 0
        if not roster.due(kind, view.phase_at, self.roster_last, self.roster_count, now, settled):
            return
        if self.roster_reader.error and self.roster_reader.error != self.roster_error:
            self.roster_error = self.roster_reader.error
            self.log(f"roster reading failed: {self.roster_error}")
        self._note("roster", self.roster_reader)
        narrow = roster.NARROW if kind == "draft" else None
        boxes = roster.boxes(kind)

        def encode(key, part):
            # The model reads names; the screens' headers are Windows OCR's alone.
            ocr = lambda: versions(part, roster.CUTS[kind], narrow)  # noqa: E731
            if key[0] in ("anchor", "roles"):
                return scoreboard.Box(None, ocr)
            if key[0] == "hero":  # the hero picked: a mixed-case line, read whole
                return scoreboard.Box(part, ocr)
            if key[0] == "role":
                inside = roster.plate(part)
                if inside is None:
                    return None  # no plate there: nobody picked it (or not the role screen)
                # The plate's inside alone (its border reads as a mark); white mixed-case
                # names on a dark plate, read as the loading screen's are.
                part = roster.plate_line(part, inside)
                cuts = roster.CUTS["loading"]
                return scoreboard.Box(part, lambda: versions(part, cuts))
            line = namereader.line(part, kind, boxes[key][3])
            trusted = kind != "loading" or line.height() >= scoreboard.SMALL
            return scoreboard.Box(line, ocr, trusted)

        crops = self._grab_boxes(boxes, encode)
        if crops is None:
            if kind == "loading":
                self.loading_skips["no screen"] = self.loading_skips.get("no screen", 0) + 1
            return
        if kind == "loading":
            self.loading_reads += 1
        self.held_since = None
        self.roster_last, self.roster_count = now, self.roster_count + 1
        self.roster_settled = self.roster_settled + 1 if full else 0
        key = view.phase_at
        # Names this screen may show, as it spells them: mine, and those read before
        # (the draft's allies on the loading screen). Hidden players have none.
        known = [view.my_name] + [
            p.name or p.read_as for p in view.allies + view.enemies if not p.tag
        ]
        # The pick screen writes names in capitals, the role screen's plates as they are;
        # the hero picked is written under a name.
        known = [v for n in known if n for v in ((n, n.upper()) if kind == "draft" else (n,))]
        if kind == "draft":  # a hero line holds a hero, as the catalog spells it
            known = {k: list(view.heroes) if k[0] == "hero" else known for k in boxes}
        reader, number = self.roster_reader, self.roster_count
        self.roster_reader.start(
            crops,
            lambda texts: events.put(("screen", kind, key, texts, {"ms": reader.ms, "n": number})),
            known=known,
        )

    def _loading_over(self, view, in_match, now):
        """Once the loading screen is over, one line: how long, how many readings, the
        players it gave, and the ticks that read nothing by reason."""
        if not in_match or self.loading_phase != view.phase_at or self.loading_said:
            return
        skipped = ", ".join(f"{k} {v}x" for k, v in sorted(self.loading_skips.items()))
        seats = f"{len(view.allies)}+{len(view.enemies)}"
        self.log(
            f"loading over: {now - view.phase_at:.0f} s, {self.loading_reads} readings,"
            f" players {seats}" + (f", skipped: {skipped}" if skipped else "")
        )
        self.loading_said = True  # said once for this phase

    def _probe(self, view, now, foreground, age):
        """Twice a second from the accept until the match starts: whether the loading
        screen's pixels show (it is then read, see _read_roster). Each change is logged
        with the phase and its age, and goes to the match's record ("seen" events)."""
        events = getattr(self.tracker, "events", None)
        before_match = view.phase in ("accepted", "draft", "loading") and not view.started_at
        # Not only with the game in front: while it loads a map Windows may count another
        # window as in front. The look scores a small box and keeps nothing; only the
        # loading screen's own pixels make a reading follow (see _read_roster).
        if not isinstance(events, queue.Queue):
            return
        if not before_match:  # phases only while it runs
            self.probe_seen = None  # nothing seen carries over to the next match
            return
        if now - self.probe_at < PROBE_EVERY:
            return
        self.probe_at = now
        score = self._loading_score()
        if score is None:
            return
        seen = score >= screens.LOADING
        if seen != self.probe_seen:
            self.probe_seen = seen
            state = "shown" if seen else "not shown"
            self.log(f"screen: loading {state} ({view.phase} +{age:.0f} s, score {score:.2f})")
            events.put(("seen", "loading", seen, round(score, 3)))

    def _loading_score(self):
        """screens.loading_score of the screen now (a small grab, kept nowhere), or None."""
        screen = self._screen()
        if screen is None:
            return None
        left, top, scale = self._frame()
        origin = self._geometry()
        x, y, w, h = screens.REGION
        region = screen.grabWindow(
            0,
            round(left - origin.x() + x * scale),
            round(top - origin.y() + y * scale),
            round(w * scale),
            round(h * scale),
        ).toImage()
        return screens.loading_score(region)

    def _loading_shows(self):
        score = self._loading_score()
        return score is not None and score >= screens.LOADING

    def _loading_frame(self):
        """One frame of the loading screen for the match's record, whatever the reading
        gates say: what was in front of the game is the evidence when it went unread."""
        screen = self._screen()
        if self.recorder is None or screen is None:
            return
        try:
            self.recorder.frame("loading", screen.grabWindow(0).toImage())
        except Exception as exc:  # evidence only: never in the way of the panel
            self.log(f"loading frame not kept: {type(exc).__name__}: {exc}")

    def _note(self, name, reader):
        """Log once what a reader says about Windows OCR (it failed, the model read alone)."""
        note = getattr(reader, "note", None)
        if note != self.notes.get(name):
            self.notes[name] = note
            if note:
                self.log(f"{name}: Windows OCR did not read ({note}); names by the model alone")
        failure = getattr(self.names, "failure", None)  # the model loaded, then a reading raised
        if failure and failure != self.notes.get("model"):
            self.notes["model"] = failure
            self.log(f"name model failed ({failure}); names by Windows OCR alone")

    def _grab_boxes(self, boxes, encode):
        """One grab of the area around ``boxes`` (design pixels) on the UI thread; returns
        a function that cuts and encodes them on a worker: {key: encode(key, QImage)},
        leaving out the boxes ``encode`` turns down (None)."""
        screen = self._screen()
        if screen is None:
            return None
        left, top, scale = self._frame()
        origin = self._geometry()
        x0 = min(b[0] for b in boxes.values())
        y0 = min(b[1] for b in boxes.values())
        x1 = max(b[0] + b[2] for b in boxes.values())
        y1 = max(b[1] + b[3] for b in boxes.values())
        grab = screen.grabWindow(
            0,
            round(left - origin.x() + x0 * scale),
            round(top - origin.y() + y0 * scale),
            round((x1 - x0) * scale),
            round((y1 - y0) * scale),
        ).toImage()
        pixels = grab.width() / max((x1 - x0) * scale, 1) * scale  # image pixels per design px

        def crops():
            found = {
                key: encode(
                    key,
                    grab.copy(
                        round((x - x0) * pixels),
                        round((y - y0) * pixels),
                        round(w * pixels),
                        round(h * pixels),
                    ),
                )
                for key, (x, y, w, h) in boxes.items()
            }
            return {key: box for key, box in found.items() if box is not None}  # None: skip

        return crops

    def _capture(self, view, now, foreground, in_match, age, started_age):
        """A full-screen frame when one is due (capture mode only)."""
        due = captures.moment(
            view,
            tab_since=self.tab_since,
            foreground=foreground,
            on_loading_screen=on_loading_screen(view.phase, age, started_age),
            in_match=in_match,
        )
        screen = self._screen()
        if due and screen is not None and self.capture.due(*due, now):
            self.capture.save(screen.grabWindow(0).toImage(), due[0])

    @property
    def reader(self):
        return self.readers[self.board]

    @property
    def board_dy(self):
        return scoreboard.END_DY if self.board == "end" else 0

    def _hover(self, view, now):
        """The card for the scoreboard row under the cursor, reading the board once."""
        if self.preview:
            return self._sample_card(view)
        if self.board == "end":
            held, wait = now - view.ended_at, END_READ_AFTER
        else:
            held, wait = (now - self.tab_since if self.tab_since else 0.0), READ_AFTER
        # A real card may cover rows: a retry would read the card, not the board.
        covered = (
            self.layout_name == "card"
            and self.card
            and (self.card.data is not None or self.card.others is not None)
        )
        players = [p.player_id for p in view.allies + view.enemies]
        if held >= wait and not covered and self.reader.wanted(view.match_id, players):
            self._read_scoreboard(view)
        if self.reader.result and self.reader.result != self.reader_logged:
            self.reader_logged = self.reader.result  # counts only, never the names
            self.log("scoreboard: %d rows read, %d of %d players matched" % self.reader.result)
            self._note("scoreboard", self.reader)
        rows = self.reader.read_rows if self.reader.match_id == view.match_id else None
        events = getattr(self.tracker, "events", None)
        posted = (view.match_id, self.board, rows)
        if rows and posted != self.board_posted and isinstance(events, queue.Queue):
            # Every row the board showed, with hero and role: a source of the roster like
            # the draft and the loading screen (players it lacks get seats from it).
            self.board_posted = posted
            events.put(("board_read", view.match_id, self.board, dict(rows)))
        failed = self.reader.failed(view.match_id)
        if failed and self.reader_error != (view.match_id, self.board):
            self.reader_error = (view.match_id, self.board)  # logged once a match and board
            self.log(f"scoreboard reading failed: {failed}")
        spent = self.reader.spent(view.match_id) and not self.reader.busy
        if spent and self.spent_logged != view.match_id:
            self.spent_logged = view.match_id  # a board this reader does not handle, likely
            self.log(f"scoreboard: no more readings this match ({scoreboard.MAX_READINGS} done)")
        if self.board == "end" and not self.reader.saw(view.match_id):
            return None  # the victory or defeat cinematic still runs: no board to point at
        point = self._cursor_design()
        if point and self.cursor_logged != view.match_id and held >= wait:
            # Once per match: where the cursor is while TAB is held (a game that
            # keeps it captured at the centre would never point at a row).
            self.cursor_logged = view.match_id
            self.log(f"tab: cursor at design ({point[0]:.0f}, {point[1]:.0f})")
        hit = point and scoreboard.row_at(*point, SIDE, self.board_dy)
        entry = hit and self.reader.player_at(view.match_id, hit)
        if hit and self.hit_logged != view.match_id:
            self.hit_logged = view.match_id  # once per match: the first row pointed at
            known = "known" if entry else "not matched yet"
            self.log(f"tab: pointing at {hit[0]} row {hit[1] + 1} ({known})")
        if not entry:
            # Players the reader has had no names for (the board's own rows gave them
            # their seats a moment ago) are still to be placed: a skeleton, not "could
            # not be read", which would also cover the rows and hold the reading back.
            reading = self.reader.busy or (
                (
                    self.reader.pending(view.match_id, players)
                    or self.reader.wanted(view.match_id, players)
                )
                and not self.reader.failed(view.match_id)
            )
            if not hit:
                return None
            if reading:
                return Card(None, None, hit, dy=self.board_dy)  # a skeleton while reading
            return Card(None, None, hit, dy=self.board_dy, others=self._unmatched(view, hit[0]))
        player = next(
            (p for p in view.allies + view.enemies if p.player_id == entry["player_id"]), None
        )
        if player is None:
            return None
        role = entry["role"] if modes.has_lanes(view.mode_name) else None
        # The hero of this reading, else the one the core holds for the seat: a reading
        # taken with the overlay's own skeleton over the row loses its hero line.
        hero = entry["hero"] or (player.hero or "").upper() or None
        return Card(row(player), {"hero": hero, "role": role}, hit, dy=self.board_dy)

    def _sample_card(self, view):
        """The preview's card: the row its tracker points at, from the sample teams."""
        point = self._cursor_design()
        hit = point and scoreboard.row_at(*point, SIDE, self.board_dy)
        entry = hit and self.tracker.board_row(hit)
        player = entry and next(
            (p for p in view.allies + view.enemies if p.player_id == entry["player_id"]), None
        )
        if not player:
            return None
        seen = {"hero": entry["hero"], "role": entry["role"]}
        return Card(row(player), seen, hit, dy=self.board_dy)

    def _unmatched(self, view, team):
        """The team's console players no row was matched to, as panel rows."""
        matched = (
            {e["player_id"] for e in self.reader.rows.values()}
            if self.reader.match_id == view.match_id
            else set()
        )
        players = view.allies if team == "ally" else view.enemies
        return tuple(
            row(p)
            for p in players
            if (p.profile or {}).get("console") and p.player_id not in matched
        )

    def _choose_screen(self):
        old = self.target_screen
        if old is not None:
            try:
                old.geometryChanged.disconnect(self._screen_changed)
            except (RuntimeError, TypeError):  # already gone with its screen
                pass
        self.target_screen = _pick_screen(self.screen_index)
        if self.target_screen is not None:
            self.target_screen.geometryChanged.connect(self._screen_changed)

    def _screens_changed(self, *_):
        # Deferred: during screenRemoved the leaving screen may still be listed.
        QTimer.singleShot(0, self._rescreen)

    def _rescreen(self):
        self._choose_screen()
        self._screen_changed()

    def _screen(self):
        """The target screen, picked again if Qt has deleted the one held."""
        screen = self.target_screen
        if screen is not None:
            try:
                screen.geometry()
            except RuntimeError:  # its C++ object is gone
                self._choose_screen()
                screen = self.target_screen
        return screen

    def _geometry(self):
        screen = self._screen()
        return QRect(0, 0, paint.DESIGN_W, paint.DESIGN_H) if screen is None else screen.geometry()

    def _frame(self):
        """(left, top, scale) of the 16:9 design area, centred on wider or taller screens.

        Centring is an assumption for non-16:9 screens; 16:9 is what was measured.
        """
        geometry = self._geometry()
        scale = min(geometry.width() / paint.DESIGN_W, geometry.height() / paint.DESIGN_H)
        left = geometry.x() + (geometry.width() - paint.DESIGN_W * scale) / 2
        top = geometry.y() + (geometry.height() - paint.DESIGN_H * scale) / 2
        return left, top, scale

    def _pointer(self):
        """Where the user points on the screen: the cursor; in the preview, the row its
        tracker points at (the real cursor is at the tray, where Show preview was)."""
        point = self.tracker.pointing if self.preview else None
        if point is None:
            return QCursor.pos()
        left, top, scale = self._frame()
        return QPoint(round(left + point[0] * scale), round(top + point[1] * scale))

    def _cursor_design(self):
        left, top, scale = self._frame()
        pos = self._pointer()
        if scale <= 0 or not self._geometry().contains(pos):
            return None
        return (pos.x() - left) / scale, (pos.y() - top) / scale

    def _read_scoreboard(self, view):
        """Grab the ten name/hero crops now (UI thread) and OCR them on a worker."""
        if self.reader.failed(view.match_id):
            return  # neither reader works: the next match tries again
        screen = self._screen()
        # A console player's shown name is a fixed label, not what the board says:
        # the reader places them by elimination, never by name.
        teams = {"ally": view.allies, "enemy": view.enemies}
        # The board shows the same names as the loading screen: match rows by those.
        names = {
            team: {p.player_id: p.read_as or shown_name(p) or "" for p in players}
            for team, players in teams.items()
        }
        consoles = {}
        if screen is None or not all(n for team in names.values() for n in team.values()):
            return  # every name first, or the reading is spent on nothing
        left, top, scale = self._frame()
        origin = self._geometry()
        # One grab of the area holding every name (screen-local logical coordinates);
        # cutting it into rows and encoding happen on the reader's thread.
        boxes = {
            (team, index): scoreboard.text_box(team, index, self.board_dy)
            for team in ("ally", "enemy")
            for index in range(SIDE)
        }
        x0 = min(b[0] for b in boxes.values())
        y0 = min(b[1] for b in boxes.values())
        x1 = max(b[0] + b[2] for b in boxes.values())
        y1 = max(b[1] + b[3] for b in boxes.values())
        grab = screen.grabWindow(
            0,
            round(left - origin.x() + x0 * scale),
            round(top - origin.y() + y0 * scale),
            round((x1 - x0) * scale),
            round((y1 - y0) * scale),
        ).toImage()
        pixels = grab.width() / max((x1 - x0) * scale, 1) * scale  # image pixels per design px
        board = self.board  # read here: the crops are cut on the reader's thread

        def ocr(part):
            # Windows OCR reads the name and hero lines at twice the size.
            return [
                image_bytes(
                    part.scaled(
                        part.width() * 2,
                        part.height() * 2,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
            ]

        def crops():
            found = {}
            for key, (x, y, w, h) in boxes.items():
                part = grab.copy(
                    round((x - x0) * pixels),
                    round((y - y0) * pixels),
                    round(w * pixels),
                    round(h * pixels),
                )
                line = namereader.line(part, board, h)
                found[key] = scoreboard.Box(line, lambda part=part: ocr(part))
            return found

        self.reader.start(view.match_id, crops, names, view.heroes, consoles=consoles, size=SIDE)

    def _reveal_key(self):
        family = "panel" if self.layout_name in ("draft", "loading") else self.layout_name
        # The set: a re-sort of the same players is no new content.
        return family, self.view and self.view.match_id, frozenset(self.roster_ids)

    def _reveal_table(self, card, at):
        """The season table comes in once per TAB press, on the first real card."""
        if card.data is not None and self.card_tab != self.tab_since:
            self.card_tab = self.tab_since
            self.card_reveal = at

    # --- the seam ------------------------------------------------------------

    def _seam_gate(self, now, layout, visible, foreground, in_match):
        """Start, hold or abort the seam; returns the layout and visibility to act on."""
        seam = self.seam
        if seam is None:
            settled = (
                self.shown
                and self.isVisible()
                and self._progress(now) >= 0.99
                and bool(self.allies or self.enemies)
            )
            if settled and visible and self.layout_name == "draft" and layout == "loading":
                self._start_seam(now, opens=True)
            elif (
                settled
                and not visible
                and foreground
                and in_match
                and layout == self.layout_name == "loading"
                and self.visibility.forced is None
            ):
                self._start_seam(now, opens=False)
            else:
                return layout, visible
            return self.layout_name, True
        if seam.stopped is not None:  # aborted: frozen where it was while a fade takes over
            if self._progress(now) <= 0 or (visible and layout == self.layout_name):
                self.seam = self.seam_shot = None
            return layout, visible
        if not seam.opens:
            return self.layout_name, True
        if not visible or layout not in ("draft", "loading"):
            seam.stop(now)  # hidden or gone: the normal fade carries on from here
            return layout, visible
        return self.layout_name, True

    def _start_seam(self, now, opens):
        self.grow = None
        self.mixes, self.head_at = {}, {}  # whatever was about to play is simply there now
        self.wave_free = 0.0
        self.seam = Seam(now, opens)
        self.seam_moved = False
        self.seam_shot = self._snapshot()

    def _advance_seam(self, now):
        seam = self.seam
        if seam is None or seam.stopped is not None:
            return
        state = seam.frame(now)
        if state.moved and not self.seam_moved:
            self.seam_moved = True
            self._apply(self.view, "loading", now)
            self.seam_shot = self._snapshot()
        if state.done:
            self.seam = self.seam_shot = None
            if not seam.opens:  # the match started: closed, nothing left to fade
                self.shown, self.fade_started, self.frozen_at = False, 0.0, now
                self.hide()
            self.update()

    def _snapshot(self):
        """The panel as a picture, without its rules: a seam only clips and blits it."""
        ratio = self.devicePixelRatioF()
        shot = QPixmap(QSize(round(self.width() * ratio), round(self.height() * ratio)))
        shot.setDevicePixelRatio(ratio)
        shot.fill(Qt.GlobalColor.transparent)
        painter = QPainter(shot)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        painter.scale(self.scale, self.scale)
        paint.paint_stack(
            painter,
            self.design_size[0],
            self.allies,
            self.enemies,
            paint.Motion(rule=0.0),
            mode=self._mode_label(),
        )
        painter.end()
        return shot

    def _mode_label(self):
        return " \u00b7 ".join(m for m in (self.view.mode_name, self.view.mode) if m)

    def _screen_changed(self, *_):
        if self.view is not None:
            self._place()
            if self.seam_shot is not None:
                self.seam_shot = self._snapshot()  # the picture was made at the old scale

    def _apply(self, view, layout, now):
        had_rows = bool(self.allies or self.enemies)
        old_status = self.view.status if self.view else None
        if self.view is None or view.art != self.view.art:
            paint.look_for_art()
        self.view = view
        self.layout_name = layout
        allies, enemies = panel_teams(view, layout)
        self.allies = [row(p) for p in allies]
        self.enemies = [row(p) for p in enemies]
        ids = tuple(p.player_id for p in view.allies + view.enemies)
        if ids and ids != self.roster_ids and set(ids) == set(self.roster_ids):
            # The same players re-sorted (their ranks arrived): rows stay where they
            # are and each slot crossfades, field by field, to its new player.
            slots = [self.rows_memory.get(pid) for pid in self.roster_ids]
            self.rows_memory = {pid: old for pid, old in zip(ids, slots) if old is not None}
            self.mixes = {}
            self.roster_ids = ids
        elif ids != self.roster_ids:
            self.seam = self.seam_shot = None  # another roster: nothing to hand over
            self.rows_memory, self.mixes, self.landed = {}, {}, set()
            self.peaks_seen, self.glints, self.wave_free = set(), 0, 0.0
            self.heads, self.head_at = {}, {}
        live = self.shown and self._progress(now) > 0 and self.seam is None
        if ids != self.roster_ids:
            if live and not had_rows and ids and layout in ("draft", "loading"):
                # The status plate grows into the roster; rows start once there is room,
                # from the end it grows from.
                text_alpha = paint.status_alpha(self._pulse(now))
                self.grow = (44.0, now, old_status, text_alpha)
                self.rows_started = now + GROW_ROWS_AFTER
                self.rows_reverse = layout == "draft"
            else:
                self.rows_started = now
                self.rows_reverse = False
                self.revealed_at = now
            self.roster_ids = ids
            self.revealed_key = self._reveal_key() if live else self.revealed_key
        self._remember_rows(now, live)
        self._note_heads(now, live)
        self._place()
        self.update()

    def _remember_rows(self, now, live):
        """Note which field groups of each row changed, to swap them in place.

        Several rows changing together run as a wave; a rank arriving strikes once, a
        Master or Paragon peak shines once, only if the panel was there to see it.
        """
        self.mixes = {
            pid: {g: m for g, m in groups.items() if now - m[1] < paint.mix_time(g)}
            for pid, groups in self.mixes.items()
        }
        changes = []
        for index, data in enumerate(self.allies + self.enemies):
            pid = data["player_id"]
            old = self.rows_memory.get(pid)
            arrived = data["state"] != "loading" and pid not in self.landed
            strike = bool(arrived and old is not None and old["state"] == "loading")
            if arrived:
                self.landed.add(pid)
            shining = data["peak"] and pid not in self.peaks_seen
            if shining:
                self.peaks_seen.add(pid)
            groups = changed_groups(old, data) if old is not None else set()
            if groups and live:
                glint = bool(shining and data["peak"]["tier"] in GLINT_TIERS)
                changes.append((index, data, old, groups, strike, glint))
            self.rows_memory[pid] = data
        if not changes:
            return
        start = max(now, self.wave_free - WAVE_OVERLAP)
        last = start
        for order, (index, data, old, groups, strike, glint) in enumerate(changes):
            at = start + min(order * WAVE_STEP, WAVE_MAX)
            entering = self.rows_started + index * ROW_STAGGER + ROW_TIME
            if now < entering:  # a row still arriving takes its data as it settles
                at = max(at, entering)
            last = max(last, at)
            mix = self.mixes.setdefault(data["player_id"], {})
            for group in groups:
                mix[group] = (old, at)
            if strike and data["tier"] != "none":
                mix["strike"] = (None, at)
        self.wave_free = last
        for _, data, _, _, _, glint in changes:
            if glint and self.glints < GLINTS:
                self.glints += 1
                self.mixes[data["player_id"]]["glint"] = (None, last + GLINT_AFTER)

    def _note_heads(self, now, live):
        """A team's average arrives after the wave, and the header makes room for it."""
        balance = team_balance(self.allies, self.enemies)
        for team, rows in (("ally", self.allies), ("enemy", self.enemies)):
            average = team_rank(rows)
            said = (average, balance["text"] if balance and team == "enemy" else None)
            if said != self.heads.get(team, (None, None)):
                if live and average is not None:
                    self.head_at[team] = max(now, self.wave_free)
                self.heads[team] = said

    def _frame_step(self):
        """Every frame while animating (a whole number of refreshes, see _frame_ms): run the
        seam, move the flying card, repaint."""
        try:
            now = self.clock()
            self._advance_seam(now)
            if self.card_move and self.layout_name == "card":
                self._slide_card(now)
            self.update()
            self.failures.pop("frame", None)
        except Exception as exc:
            self._failed("frame", exc)

    def _frame_ms(self):
        """Milliseconds between animation frames: one refresh of the overlay's screen,
        8 ms at least (a full panel takes 3 to 14 ms to paint)."""
        screen = self._screen()
        rate = screen.refreshRate() if screen is not None else 60.0
        if rate <= 1:
            return FRAME_MS
        period = 1000.0 / rate  # a whole number of refreshes: no beat (144 Hz: 2 = 14 ms)
        return min(FRAME_MS, max(1, round(math.ceil(FRAME_MS_MIN / period - 1e-6) * period)))

    def warm(self):
        """Paint a sample panel and card once, off screen: the first real frame then
        does not pay for loading fonts and glyphs (46 ms against 7, measured 2026-11-19)
        and the entrance does not skip its first steps."""
        try:
            view = preview_view("loading")
            allies = [row(p) for p in view.allies]
            enemies = [row(p) for p in view.enemies]
            image = QImage(640, 1000, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            painter.setRenderHints(
                QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
            )
            paint.paint_stack(painter, 640, allies, enemies, paint.Motion())
            if allies:
                paint.paint_card(painter, allies[0], paint.Motion())
            painter.end()
        except Exception as exc:  # a warm-up only: the real frames still paint
            self.log(f"overlay warm-up failed: {type(exc).__name__}: {exc}")

    def _slide_card(self, now):
        start_rect, started, duration, curve = self.card_move
        t = _ease(curve, (now - started) / duration)
        target = self._card_rect()
        rect = QRect(
            round(start_rect.x() + (target.x() - start_rect.x()) * t),
            round(start_rect.y() + (target.y() - start_rect.y()) * t),
            target.width(),
            round(start_rect.height() + (target.height() - start_rect.height()) * t),
        )
        self.setGeometry(rect)
        if t >= 1.0:
            self.card_move = None

    def _place(self):
        if self.layout_name == "menu":
            width, height = paint.BADGE_W, paint.BADGE_H
        elif self.layout_name == "card" and self.card:
            width, height = self._card_size()
        elif not (self.allies or self.enemies):
            width, height = STACK_W, 44
        else:
            width = STACK_W
            height = paint.stack_size(len(self.allies), len(self.enemies), width)[1]
        geometry = self._geometry()
        self.scale = max(
            0.1,
            min(geometry.width() / paint.DESIGN_W, geometry.height() / paint.DESIGN_H),
        )
        width_px = round(width * self.scale)
        height_px = round(height * self.scale)
        if self.layout_name == "menu":
            x = geometry.x() + geometry.width() - width_px - round(BADGE_RIGHT * self.scale)
            y = geometry.y() + round(BADGE_TOP * self.scale)
        elif self.layout_name == "card" and self.card:
            self.design_size = (width, height)
            target = self._card_rect()
            if self.card_move:
                return
            if self.shown and self.isVisible() and self.geometry().size() != target.size():
                # A different height: resize smoothly.
                self.card_move = (self.geometry(), self.clock(), CARD_MOVE, _OUT_CUBIC)
                return
            self.setGeometry(target)
            return
        elif self.layout_name == "draft":
            x = geometry.x() + geometry.width() - width_px - round(STACK_RIGHT * self.scale)
            y = geometry.y() + geometry.height() - height_px - round(STACK_BOTTOM * self.scale)
        elif self.layout_name == "loading":
            x = geometry.x() + round(STACK_LEFT * self.scale)
            y = geometry.y() + round(STACK_TOP * self.scale)
        else:
            x = geometry.x() + round(STACK_LEFT * self.scale)
            y = geometry.y() + round(STACK_TOP * self.scale)
        x = max(geometry.x(), min(x, geometry.x() + geometry.width() - width_px))
        y = max(geometry.y(), min(y, geometry.y() + geometry.height() - height_px))
        self.design_size = (width, height)
        self.setGeometry(QRect(x, y, width_px, height_px))

    def _card_size(self):
        if self.card.data is None and self.card.others is not None:
            return paint.unknown_card_size(self.card.others)
        if self.card.data is None:
            return paint.CARD_W, 92  # the reading skeleton
        return paint.card_size(self.card.data, self.card.seen)

    @staticmethod
    def _paint_placeholder(painter, card, motion):
        """A card without a player: the reading skeleton, or a row that cannot be told."""
        if card.others is not None:
            paint.paint_card_unknown(painter, card.team, card.others, motion, chrome=False)
        else:
            paint.paint_card_reading(painter, card.team, motion, chrome=False)

    def _card_rect(self):
        """Beside where the row was entered, towards the centre; level with the row."""
        width, height = self._card_size()
        width_px, height_px = round(width * self.scale), round(height * self.scale)
        team, index = self.card.hit
        anchor = self.card.anchor if self.card.anchor is not None else self._pointer().x()
        gap = round(CARD_GAP * self.scale)
        x = anchor + gap if team == "ally" else anchor - gap - width_px
        # Clear of the game's mute / report strip by the hovered row: over the other
        # team's rows if need be (nothing there is needed while pointing at this one).
        left = self._frame()[0]
        if team == "ally":
            x = max(x, round(left + (scoreboard.ACTIONS["ally"][1] + ACTIONS_GAP) * self.scale))
        else:
            edge = scoreboard.ACTIONS["enemy"][0] - ACTIONS_GAP
            x = min(x, round(left + edge * self.scale) - width_px)
        row_middle = scoreboard.ROW_TOPS[index] + self.card.dy + scoreboard.ROW_H / 2
        y = round(self._frame()[1] + row_middle * self.scale) - height_px // 2
        geometry = self._geometry()
        x = max(geometry.x(), min(x, geometry.x() + geometry.width() - width_px))
        y = max(geometry.y(), min(y, geometry.y() + geometry.height() - height_px))
        return QRect(x, y, width_px, height_px)

    def _pulse(self, now):
        """The waiting pulse: three breaths from full, then it stays at full. A status
        that states a problem does not breathe at all."""
        if self.view is not None and self.view.status_note:
            return 1.0
        t = now - self.revealed_at
        if t >= PULSE * PULSES:
            return 1.0
        return 0.5 + 0.5 * math.cos(t * 2 * math.pi / PULSE)

    def _waiting(self, now):
        """Something on screen is breathing, so the tick repaints."""
        if not self.isVisible() or self.frozen_at or self.seam:
            return False
        if now - self.revealed_at >= PULSE * PULSES:
            return False
        if self.layout_name == "card":
            return bool(self.card and self.card.data is None)
        if self.layout_name in ("draft", "loading"):
            rows = self.allies + self.enemies
            if not rows:
                return not (self.view and self.view.status_note)
            return any(r["state"] == "loading" for r in rows)
        return False

    def _motion(self, now):
        now = min(now, self.frozen_at) if self.frozen_at else now
        count = len(self.allies) + len(self.enemies)

        def rows(index):
            order = count - 1 - index if self.rows_reverse else index
            start = self.rows_started + order * ROW_STAGGER
            return _ease(_OUT_CUBIC, (now - start) / ROW_TIME)

        loud = self.layout_name != "draft"
        return paint.Motion(
            panel=self._progress(now),
            rule=_ease(_OUT_QUART, (now - self.revealed_at) / RULE_TIME),
            pulse=self._pulse(now),
            rows=rows,
            rise=RISE_LOADING if loud else RISE_DRAFT,
            loud=loud,
        )

    def _animating(self, now):
        if not self.isVisible():
            return False
        if self.fade_started and now - self.fade_started < self.fade_duration:
            return True  # including its first frames, where progress is still 0 or 1
        if self.seam:
            return self.seam.stopped is None
        if self.layout_name == "menu":  # settles; no idle frames
            busy = self.badge.busy_until()
            if self.badge_at and now < self.badge_at + paint.STRIKE:
                return True
            return now < busy or now - self.revealed_at < RULE_TIME
        if self.layout_name == "card":
            swap = paint.SWAP_IN_AT + paint.SWAP_IN
            swapping = self.card_prev and now - self.card_prev[1] < swap
            revealing = self.card_reveal and now - self.card_reveal < paint.TABLE_TIME
            flying = self.card_move or self.card_bar or swapping or revealing
            return bool(flying) or now - self.revealed_at < RULE_TIME
        if self.frozen_at:
            return False  # hiding: only the fade above moves
        count = len(self.allies) + len(self.enemies)
        if now - self.rows_started < ROW_STAGGER * max(count, 1) + ROW_TIME:
            return True
        if now - self.revealed_at < RULE_TIME:
            return True
        if self.grow and now - self.grow[1] < GROW:
            return True
        ends = [
            at + paint.mix_time(group)
            for groups in self.mixes.values()
            for group, (_, at) in groups.items()
        ]
        ends += [at + paint.HEAD_TIME for at in self.head_at.values()]
        return now < max(ends, default=0.0)

    # --- painting ------------------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        win32.make_inert(int(self.winId()))

    def paintEvent(self, event):
        if self.view is None:
            return
        now = self.clock()
        progress = self._progress(now)
        if progress <= 0.0:
            return
        painter = QPainter(self)
        try:
            self._paint(painter, now, progress)
            self._drawn = self.view
            self.failures.pop("paint", None)
        except Exception as exc:
            self._failed("paint", exc)
        finally:
            painter.end()

    def _paint(self, painter, now, progress):
        """Draw the state as it is at ``now`` and change none of it: Qt may paint at
        any time and any number of times, so what ends is ended by the tick."""
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        painter.scale(self.scale, self.scale)
        painter.setOpacity(progress)
        motion = self._motion(now)
        if self.seam:
            self._paint_seam(painter, now)
        elif self.layout_name == "menu":
            strike = None
            if self.badge_at and 0 <= now - self.badge_at < paint.STRIKE:
                strike = now - self.badge_at
            paint.paint_menu(
                painter,
                self.design_size[0],
                self.badge.frame(now),
                pulse=1.0,  # a steady badge dot: no frames needed to keep it alive
                rule=motion.rule,
                scale=self.scale,
                strike=strike,
            )
        elif self.layout_name == "card" and self.card:
            self._paint_card(painter, motion, now)
        elif not (self.allies or self.enemies):
            status = self.view.status
            if not self.view.status_note:
                status = EMPTY_STATUS.get(self.view.phase, status)
            paint.paint_status(painter, self.design_size[0], status, motion)
        else:
            height, status, status_alpha = None, None, 0.0
            if self.grow:
                start_h, started, status, text_alpha = self.grow
                t = (now - started) / GROW  # past 1 it draws full size until the tick ends it
                full = self.design_size[1]
                height = start_h + (full - start_h) * _ease(_OUT_CUBIC, t)
                status_alpha = text_alpha * (1.0 - _ease(_IN_CUBIC, (now - started) / STATUS_OUT))
            mixes = {
                pid: {g: (old, now - at) for g, (old, at) in groups.items()}
                for pid, groups in self.mixes.items()
            }
            heads = {
                team: now - at for team, at in self.head_at.items() if now - at < paint.HEAD_TIME
            }
            paint.paint_stack(
                painter,
                self.design_size[0],
                self.allies,
                self.enemies,
                motion,
                mode=self._mode_label(),
                mixes=mixes,
                height=height,
                from_bottom=self.layout_name == "draft",
                status=status,
                status_alpha=status_alpha,
                heads=heads,
            )

    def _paint_seam(self, painter, now):
        """The panel's picture clipped to the body, with the rule at its top and foot."""
        state = self.seam.frame(now)
        width, height = self.design_size
        shown = height * state.body
        if self.seam_shot is not None and shown > 0:
            painter.save()
            painter.resetTransform()
            painter.setClipRect(QRectF(0, 0, self.width(), shown * self.scale))
            painter.drawPixmap(0, 0, self.seam_shot)
            painter.restore()
            paint.gold_rule(painter, 0, width, shown - 1, state.rule, center_diamond=False)
        paint.gold_rule(painter, 0, width, 0, state.rule)

    def _paint_card(self, painter, motion, now):
        """The card's chrome once, its content swapping out then in when it changes."""
        data, seen, team = self.card.data, self.card.seen, self.card.team
        width = self._card_size()[0]
        height = self.height() / self.scale  # the window's current (maybe resizing) height
        was, bar = None, 1.0
        if self.card_bar:
            was = self.card_bar[0]
            bar = _ease(_EXIT, (now - self.card_bar[1]) / self.card_bar[2])
        paint.card_chrome(painter, width, height, team, motion.rule, was, bar)
        if data is None:
            self._paint_placeholder(painter, self.card, motion)
            return
        reveal = None
        if self.card_reveal is not None and now - self.card_reveal < paint.TABLE_TIME:
            reveal = max(0.0, now - self.card_reveal)
        if self.card_prev is None or now - self.card_prev[1] >= paint.SWAP_IN_AT + paint.SWAP_IN:
            paint.paint_card(painter, data, motion, seen, chrome=False, reveal=reveal)
            return
        prev, swapped = self.card_prev
        dt = now - swapped
        base = painter.opacity()
        old_alpha = 1 - _ease(_IN_CUBIC, dt / paint.SWAP_OUT)
        new_alpha = _ease(_OUT_CUBIC, (dt - paint.SWAP_IN_AT) / paint.SWAP_IN)
        if old_alpha > 0:
            painter.save()
            painter.setOpacity(base * old_alpha)
            if prev.data is None:  # the reading skeleton gives way to the player
                self._paint_placeholder(painter, prev, motion)
            else:
                paint.paint_card(painter, prev.data, motion, prev.seen, chrome=False)
            painter.restore()
        if new_alpha > 0:
            painter.save()
            painter.setOpacity(base * new_alpha)
            painter.translate(0, paint.snap(painter, (1 - new_alpha) * 3))
            paint.paint_card(painter, data, motion, seen, chrome=False, reveal=reveal)
            painter.restore()
