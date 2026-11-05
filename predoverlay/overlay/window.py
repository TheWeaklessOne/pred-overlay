"""The overlay window: a click-through, never-focused panel sized to its content.

It polls the tracker's immutable snapshot and the keyboard every 50 ms, picks a
layout for the current screen (menu badge, draft, loading screen) and animates
the panel in and out. In the match nothing shows by itself: while TAB is held,
the scoreboard row under the cursor brings up that player's card, which uses
a quick 90/80 ms fade to keep up with the game's instant scoreboard. Motion
follows the visual spec: 220 ms OutCubic fade with a 24 px slide from the
anchored edge, rows staggered by 30 ms, a gold rule drawn from the centre,
140 ms InCubic exit.

The window never takes focus or input. Keys are polled with GetAsyncKeyState and
the game is recognised by its foreground window class and title only; nothing
here opens a handle to the game process.
"""

import math
import time
import traceback

from PySide6.QtCore import QBuffer, QByteArray, QEasingCurve, QIODevice, QRect, Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication, QPainter
from PySide6.QtWidgets import QWidget

from . import paint, scoreboard, win32
from .model import Encounter, PlayerView, ViewModel
from .motion import Badge
from .rows import OMEDA_RANKS, changed_groups, row, shown_name
from .visibility import LOADING_SCREEN, Visibility, on_loading_screen

_OUT_CUBIC = QEasingCurve(QEasingCurve.Type.OutCubic)
_IN_CUBIC = QEasingCurve(QEasingCurve.Type.InCubic)
# Fade-outs ease in and out: InCubic ends at full speed, a visible cut on the last frame.
_EXIT = QEasingCurve(QEasingCurve.Type.InOutCubic)
_OUT_QUART = QEasingCurve(QEasingCurve.Type.OutQuart)

ENTER, EXIT, TAB_ENTER, TAB_EXIT = 0.22, 0.14, 0.09, 0.14
ROW_STAGGER, ROW_TIME, RULE_TIME = 0.03, 0.18, 0.30
PULSE = 1.6  # seconds per loading-row breathing cycle

# Design-pixel anchors taken from the mocks in data/screens/mock/.
STACK_W = 640
STACK_RIGHT, STACK_BOTTOM = 54, 104
STACK_LEFT, STACK_TOP = 64, 200
SLIDE = 24
CARD_GAP = 32  # between the cursor and the card
CARD_MOVE = 0.14  # the card flies to another row of its team
CARD_CROSS = 0.24  # ... or across to the other team
HOVER_GRACE = 0.3  # the card stays while the cursor crosses the gap between rows
GROW = 0.24  # the panel grows out of the status plate
GROW_ROWS_AFTER = 0.12  # rows start once the panel has some room
STATUS_OUT = 0.10
SHORTEST_FADE = 0.35  # a reversed fade takes at least this share of its full time
READ_AFTER = 0.25  # seconds of TAB before the scoreboard is read (it must be drawn)
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


def png_bytes(image):
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def _ease(curve, value):
    return curve.valueForProgress(max(0.0, min(1.0, value)))


def _pick_screen(index):
    screens = QGuiApplication.screens()
    if not screens:
        return None
    primary = QGuiApplication.primaryScreen()
    ordered = [primary] + [screen for screen in screens if screen is not primary]
    return ordered[index] if 0 <= index < len(ordered) else ordered[0]


def _sample_card(rank, winrate, games, kda, heroes, form, shared=0):
    return {
        "rating": {"rank": rank},
        "overall": {"games": games, "winrate": winrate, "kda": kda},
        "mains": [{"hero": hero} for hero in heroes],
        "form": form,
        "shared": {"with": shared} if shared else None,
    }


def _sample_profile(rank, games):
    """An official profile shaped like GameClient.public_profiles() for 'Gold II'."""
    name, _, step = rank.title().partition(" ")
    placed = name in OMEDA_RANKS
    steps = {"Iii": 1, "Ii": 2, "I": 3}  # tier 1 is the lowest step
    days = 25 if games < 20 else games * 3 + 30
    return {
        "placed": placed,
        "division": OMEDA_RANKS.index(name) + 1 if placed else 0,
        "tier": steps.get(step, 0),
        "vp": (games * 7) % 100,
        "level": 20 + games // 2,
        "created": time.time() - days * 86400,
    }


def _sample_peak(rank):
    """A past best for the sample players under Diamond (and the unplaced one)."""
    if rank and rank.startswith(("Diamond", "Paragon")):
        return None
    return {"rank": "Diamond II", "season": "Season 1 - Split 2"}


def _sample_player(
    player_id,
    name,
    team,
    *,
    me=False,
    rank,
    winrate,
    games,
    kda,
    heroes,
    form,
    shared=0,
    encounters=(),
):
    return PlayerView(
        player_id=player_id,
        name=name,
        team=team,
        is_me=me,
        state="ready",
        card=_sample_card(rank, winrate, games, kda, heroes, form, shared),
        profile=_sample_profile(rank, games),
        peak=_sample_peak(rank),
        encounters=tuple(encounters),
    )


def preview_view(phase="draft", phase_at=0.0, version=1):
    """A complete 5v5 roster for ``--preview``, without the game."""
    allies = (
        _sample_player(
            "preview-a1",
            "Orbitfox#1234",
            "ally",
            rank="Gold III",
            winrate=0.54,
            games=212,
            kda=3.4,
            heroes=("Riktor", "Murdock", "Grux"),
            form="WWLWL",
        ),
        _sample_player(
            "preview-a2",
            "Lanternjaw90#5678",
            "ally",
            rank="Silver I",
            winrate=0.48,
            games=87,
            kda=2.1,
            heroes=("Zinx", "Gideon", "Howitzer"),
            form="LWLWW",
            shared=3,
        ),
        _sample_player(
            "preview-a3",
            "SamplePlayer#9012",
            "ally",
            me=True,
            rank="Gold II",
            winrate=0.52,
            games=340,
            kda=3.9,
            heroes=("Narbash", "Mourn", "Muriel"),
            form="WLWWL",
        ),
        _sample_player(
            "preview-a4",
            "Quietstorm05#3456",
            "ally",
            rank="Platinum III",
            winrate=0.57,
            games=501,
            kda=4.2,
            heroes=("Khaimera", "Kallari", "Feng Mao"),
            form="WWWWL",
        ),
        _sample_player(
            "preview-a5",
            "N-Somebody#7890",
            "ally",
            rank="Bronze II",
            winrate=0.44,
            games=36,
            kda=1.6,
            heroes=("Crunch", "Steel", "Grux"),
            form="LLWLL",
        ),
    )
    enemies = (
        _sample_player(
            "preview-e1",
            "Rook_Atlas#1111",
            "enemy",
            rank="Gold I",
            winrate=0.55,
            games=403,
            kda=3.1,
            heroes=("Akeron", "Riktor", "Rampage"),
            form="WLWLW",
            encounters=(Encounter(1, "Riktor", "against", False),),
        ),
        _sample_player(
            "preview-e2",
            "Vellum_Ash#2222",
            "enemy",
            rank="Diamond II",
            winrate=0.61,
            games=820,
            kda=4.8,
            heroes=("Renna", "Scarlett", "Sparrow"),
            form="WWLWW",
        ),
        _sample_player(
            "preview-e3",
            "Kpr_0#3333",
            "enemy",
            rank="Silver II",
            winrate=0.47,
            games=129,
            kda=2.0,
            heroes=("Steel", "Muriel", "Dekker"),
            form="LWLWL",
        ),
        _sample_player(
            "preview-e4",
            "Lady Quill X#4444",
            "enemy",
            rank="Gold III",
            winrate=0.50,
            games=264,
            kda=2.9,
            heroes=("Twinblast", "Sparrow", "Murdock"),
            form="WLWWL",
            encounters=(Encounter(1, "Sparrow", "with", True),),
        ),
        _sample_player(
            "preview-e5",
            "Harrowfen#5555",
            "enemy",
            rank="UNRANKED",
            winrate=None,
            games=14,
            kda=1.2,
            heroes=("Eden",),
            form="LLL",
        ),
    )
    return ViewModel(
        version=version,
        phase=phase,
        phase_at=phase_at,
        status="preview · " + phase,
        mode="5v5",
        match_id="preview",
        allies=allies,
        enemies=enemies,
        queues={
            "RANKED": (42, 28),
            "TEAM_VS_TEAM": (26, 14),
            "OVERCLOCKED": (12, 9),
            "ARAM": (5, 5),
            "TEAM_VS_AI": (0, 0),
        },
        queues_at=time.time(),
    )


class PreviewTracker:
    """Stand-in for the live tracker: sample roster, cycles draft/loading/TAB/menu."""

    CYCLE = 7.0

    def __init__(self, clock=time.time):
        self.clock = clock
        self.started = clock()
        self.index = -1
        self.view = None
        self.tab = False

    @property
    def snapshot(self):
        elapsed = self.clock() - self.started
        index = int(elapsed // self.CYCLE) % 4
        if index != self.index:
            self.index = index
            start = self.started + index * self.CYCLE
            if index == 0:
                phase, phase_at, self.tab = "draft", start, False
            elif index == 1:
                phase, phase_at, self.tab = "loading", start, False
            elif index == 2:
                phase, phase_at, self.tab = (
                    "loading",
                    start - (LOADING_SCREEN + 10),
                    True,
                )
            else:
                phase, phase_at, self.tab = "menu", start, False
            self.view = preview_view(phase, phase_at, index + 1)
        return self.view

    @property
    def tab_held(self):
        return self.tab


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
    ):
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
        self.reader = scoreboard.Reader()
        self.reader_error = self.reader_match = self.cursor_logged = None
        self.reader_logged = self.hit_logged = None
        self.tick_error = None
        self.log = log or (lambda message: None)
        self.tab_since = None
        self.card = None  # (row dict, {"hero", "role"}, (team, index)) under the cursor
        self.strip, self.badge = paint.new_strip(), Badge()  # the menu row's motion
        self.menu_widths = {}
        self.revealed_key = None  # what was last revealed: no replay for the same content
        self.frozen_at = None  # row/rule motion stops while hiding
        self.rows_memory = {}  # player -> last painted row, to see which fields changed
        self.mixes = {}  # player -> {field group: (old row, changed at)}
        self.grow = None  # (from height, started, old status) while the panel grows
        self.card_prev = None  # (row, seen, swapped at): the card content being replaced
        self.card_move = None  # (from QRect, started, duration): the card flying to a row
        self.card_lost = None  # when the cursor left the card's row (TAB still held)
        app = QGuiApplication.instance()
        # Monitors come and go (display mode switches, a streaming display, sleep):
        # pick the screen again instead of holding a deleted one.
        app.screenAdded.connect(self._screens_changed)
        app.screenRemoved.connect(self._screens_changed)
        app.primaryScreenChanged.connect(self._screens_changed)
        self.poll = QTimer(self)
        self.poll.setInterval(50)
        self.poll.timeout.connect(self.tick)
        self.frame = QTimer(self)
        self.frame.setInterval(16)
        self.frame.timeout.connect(self._frame_step)
        self.poll.start()

    def set_paused(self, paused):
        """The tray switch: stop reading and drawing entirely, or start again."""
        if paused:
            self.poll.stop()
            self.frame.stop()
            self.shown, self.fade_started, self.card = False, 0.0, None
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
            self.tick_error = None
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if message != self.tick_error:
                self.tick_error = message
                self.log(f"overlay tick failed: {message}\n{traceback.format_exc()}")

    def _tick(self):
        now = self.clock()
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
        card = self._hover(view, now) if tab and foreground and in_match and not visible else None
        moved = False
        shown_card = self.layout_name == "card" and self.card and self._progress(now) > 0
        hovering = tab and foreground and in_match and not visible
        if card is None and hovering and shown_card and self.shown:
            # Between rows (or a moment off the board): hold the card a little, so
            # moving to the next player flies the card there instead of blinking.
            self.card_lost = self.card_lost or now
            point = self._cursor_design()
            board = point and scoreboard.on_board(*point, len(view.allies))
            if board or now - self.card_lost < HOVER_GRACE:
                card = self.card[:3]
        elif card is not None:
            self.card_lost = None
        if card:
            layout, visible = "card", True
            moved = self.card is None or card[2] != self.card[2]
            if moved:
                if shown_card:  # another row: fly there from where it is, swapping the content
                    same_team = card[2][0] == self.card[2][0]
                    self.card_prev = (self.card[0], self.card[1], now)
                    duration = CARD_MOVE if same_team else CARD_CROSS
                    self.card_move = (self.geometry(), now, duration)
                    anchor = self.card[3] if same_team else QCursor.pos().x()
                    self.card = card + (anchor,)
                else:
                    self.card = card + (QCursor.pos().x(),)  # anchored where it was entered
                    self.card_prev = self.card_move = None
                    self.revealed_at = now
            elif card[:2] != self.card[:2]:
                if self.card[0] is None or card[0] is None or card[1] != self.card[1]:
                    self.card_prev = (self.card[0], self.card[1], now)  # skeleton -> player
                self.card = card + self.card[3:]
                moved = True  # its size may change
        elif self.card is not None and not (self.layout_name == "card" and self._progress(now) > 0):
            self.card = None
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
                    self.strip.replay(now + 0.06)  # the drawer opens again on a fresh reveal
                if not self.isVisible():
                    self.show()
        if self.layout_name == "menu":  # the queue strip and badge follow the data every tick
            revealed = self.shown and self._progress(now) > 0
            self.strip.sync(view.queues, view.queues_at, view.region, now, revealed=revealed)
            self.badge.sync(view.health, now, revealed=revealed)
        if self.isVisible() and not self.shown and self._progress(now) <= 0.0:
            self.hide()
        if self.card_move and self.layout_name == "card":
            self._slide_card(now)
        if self.grow and now - self.grow[1] >= GROW:
            self.grow = None
        if self.isVisible() and self.layout_name in ("draft", "loading"):
            loading = any(r["state"] == "loading" for r in self.allies + self.enemies)
            if loading or not (self.allies or self.enemies):
                self.update()  # the gentle pulse needs no 60 fps: the 20 Hz tick will do
        if self.isVisible() and self.layout_name == "card" and self.card and self.card[0] is None:
            self.update()  # the reading skeleton breathes at the tick rate too
        self.ticks += 1
        if self.isVisible() and self.ticks % 40 == 0:
            win32.keep_on_top(int(self.winId()))
        if self._animating(now):
            if not self.frame.isActive():
                self.frame.start()
        elif self.frame.isActive():
            self.frame.stop()
            self.update()
        if view is not self._drawn:
            self.update()

    def _hover(self, view, now):
        """The card for the scoreboard row under the cursor, reading the board once."""
        if view.mode is None or len(view.allies) > len(scoreboard.ROW_TOPS):
            return None
        held = now - self.tab_since if self.tab_since else 0.0
        if held >= READ_AFTER and self.reader.wanted(view.match_id):
            self._read_scoreboard(view)
        if self.reader_match != view.match_id:  # a new match gets a fresh try
            self.reader_match, self.reader_error = view.match_id, None
        if self.reader.result and self.reader.result != self.reader_logged:
            self.reader_logged = self.reader.result  # counts only, never the names
            self.log("scoreboard: %d rows read, %d of %d players matched" % self.reader.result)
        if self.reader.error and self.reader.error != self.reader_error:
            self.reader_error = self.reader.error  # no more readings this match
            self.log(f"scoreboard reading failed: {self.reader_error}")
        point = self._cursor_design()
        if point and self.cursor_logged != view.match_id and held >= READ_AFTER:
            # Once per match: where the cursor is while TAB is held (a game that
            # keeps it captured at the centre would never point at a row).
            self.cursor_logged = view.match_id
            self.log(f"tab: cursor at design ({point[0]:.0f}, {point[1]:.0f})")
        hit = point and scoreboard.row_at(*point, len(view.allies))
        entry = hit and self.reader.player_at(view.match_id, hit)
        if hit and self.hit_logged != view.match_id:
            self.hit_logged = view.match_id  # once per match: the first row pointed at
            known = "known" if entry else "not matched yet"
            self.log(f"tab: pointing at {hit[0]} row {hit[1] + 1} ({known})")
        if not entry:
            reading = self.reader.busy or (
                self.reader.wanted(view.match_id) and not self.reader_error
            )
            return (None, None, hit) if hit and reading else None  # a skeleton while reading
        player = next(
            (p for p in view.allies + view.enemies if p.player_id == entry["player_id"]), None
        )
        if player is None:
            return None
        return row(player), {"hero": entry["hero"], "role": entry["role"]}, hit

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

    def _cursor_design(self):
        left, top, scale = self._frame()
        pos = QCursor.pos()
        if scale <= 0 or not self._geometry().contains(pos):
            return None
        return (pos.x() - left) / scale, (pos.y() - top) / scale

    def _read_scoreboard(self, view):
        """Grab the ten name/hero crops now (UI thread) and OCR them on a worker."""
        if self.reader_error:
            return
        screen = self._screen()
        names = {
            "ally": {p.player_id: shown_name(p) or "" for p in view.allies},
            "enemy": {p.player_id: shown_name(p) or "" for p in view.enemies},
        }
        if screen is None or not all(n for team in names.values() for n in team.values()):
            return  # every name first, or the reading is spent on nothing
        left, top, scale = self._frame()
        origin = self._geometry()
        # One grab of the area holding every name (screen-local logical coordinates);
        # cutting it into rows and encoding happen on the reader's thread.
        boxes = {
            (team, index): scoreboard.text_box(team, index)
            for team in ("ally", "enemy")
            for index in range(len(view.allies if team == "ally" else view.enemies))
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

        def crops():
            found = {}
            for key, (x, y, w, h) in boxes.items():
                part = grab.copy(
                    round((x - x0) * pixels),
                    round((y - y0) * pixels),
                    round(w * pixels),
                    round(h * pixels),
                )
                part = part.scaled(
                    part.width() * 2,
                    part.height() * 2,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                found[key] = png_bytes(part)
            return found

        self.reader.start(view.match_id, crops, names, view.heroes)

    def _reveal_key(self):
        family = "panel" if self.layout_name in ("draft", "loading") else self.layout_name
        return family, self.view and self.view.match_id, self.roster_ids

    def _screen_changed(self, *_):
        if self.view is not None:
            self._place()

    def _apply(self, view, layout, now):
        changed = layout != self.layout_name
        had_rows = bool(self.allies or self.enemies)
        old_status = self.view.status if self.view else None
        self.view = view
        self.layout_name = layout
        self.allies = [row(p) for p in view.allies]
        self.enemies = [row(p) for p in view.enemies]
        ids = tuple(p.player_id for p in view.allies + view.enemies)
        live = self.shown and self._progress(now) > 0
        if ids != self.roster_ids:
            self.rows_memory, self.mixes = {}, {}
            if live and not had_rows and ids and layout in ("draft", "loading"):
                # The status plate grows into the roster; rows start once there is room.
                self.grow = (44.0, now, old_status)
                self.rows_started = now + GROW_ROWS_AFTER
            elif ids != self.roster_ids or changed:
                self.rows_started = now
                self.revealed_at = now
            self.roster_ids = ids
            self.revealed_key = self._reveal_key() if live else self.revealed_key
        self._remember_rows(now, live)
        self._place()
        self.update()

    def _remember_rows(self, now, live):
        """Note which field groups of each row changed, to swap them in place."""
        self.mixes = {
            pid: {g: m for g, m in groups.items() if now - m[1] < paint.SWAP_DONE}
            for pid, groups in self.mixes.items()
        }
        for data in self.allies + self.enemies:
            pid = data["player_id"]
            old = self.rows_memory.get(pid)
            if old is not None and live:
                for group in changed_groups(old, data):
                    self.mixes.setdefault(pid, {})[group] = (old, now)
            self.rows_memory[pid] = data

    def _frame_step(self):
        """Every 16 ms while animating: move the flying card, then repaint."""
        if self.card_move and self.layout_name == "card":
            self._slide_card(self.clock())
        self.update()

    def _slide_card(self, now):
        start_rect, started, duration = self.card_move
        t = _ease(_OUT_CUBIC, (now - started) / duration)
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
            region = self.view.region
            if region not in self.menu_widths:
                self.menu_widths[region] = paint.menu_width(region)
            width, height = self.menu_widths[region], paint.BADGE_H
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
                self.card_move = (self.geometry(), self.clock(), CARD_MOVE)  # resize smoothly
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
        if self.card[0] is None:
            return paint.CARD_W, 92  # the reading skeleton
        return paint.card_size(self.card[0], self.view.stats_source, self.card[1])

    def _card_rect(self):
        """Beside where the row was entered, towards the centre; level with the row."""
        width, height = self._card_size()
        width_px, height_px = round(width * self.scale), round(height * self.scale)
        team, index = self.card[2]
        anchor = self.card[3] if len(self.card) > 3 else QCursor.pos().x()
        gap = round(CARD_GAP * self.scale)
        x = anchor + gap if team == "ally" else anchor - gap - width_px
        row_middle = scoreboard.ROW_TOPS[index] + scoreboard.ROW_H / 2
        y = round(self._frame()[1] + row_middle * self.scale) - height_px // 2
        geometry = self._geometry()
        x = max(geometry.x(), min(x, geometry.x() + geometry.width() - width_px))
        y = max(geometry.y(), min(y, geometry.y() + geometry.height() - height_px))
        return QRect(x, y, width_px, height_px)

    def _motion(self, now):
        now = min(now, self.frozen_at) if self.frozen_at else now

        def rows(index):
            start = self.rows_started + index * ROW_STAGGER
            return _ease(_OUT_CUBIC, (now - start) / ROW_TIME)

        return paint.Motion(
            panel=self._progress(now),
            rule=_ease(_OUT_QUART, (now - self.revealed_at) / RULE_TIME),
            pulse=0.5 + 0.5 * math.sin((now - self.revealed_at) * 2 * math.pi / PULSE),
            rows=rows,
        )

    def _animating(self, now):
        if not self.isVisible():
            return False
        if self.fade_started and now - self.fade_started < self.fade_duration:
            return True  # including its first frames, where progress is still 0 or 1
        if self.layout_name == "menu":  # settles; no idle frames
            busy = max(self.strip.busy_until(), self.badge.busy_until())
            return now < busy or now - self.revealed_at < RULE_TIME
        if self.layout_name == "card":
            swap = paint.SWAP_IN_AT + paint.SWAP_IN
            swapping = self.card_prev and now - self.card_prev[2] < swap
            return bool(self.card_move or swapping) or now - self.revealed_at < RULE_TIME
        if self.frozen_at:
            return False  # hiding: only the fade above moves
        count = len(self.allies) + len(self.enemies)
        if now - self.rows_started < ROW_STAGGER * max(count, 1) + ROW_TIME:
            return True
        if now - self.revealed_at < RULE_TIME:
            return True
        if self.grow and now - self.grow[1] < GROW:
            return True
        swap = paint.SWAP_IN_AT + paint.SWAP_IN
        ends = [
            at + (paint.SWAP_DONE if group == "vp" else swap)
            for groups in self.mixes.values()
            for group, (_, at) in groups.items()
        ]
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
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        painter.scale(self.scale, self.scale)
        painter.setOpacity(progress)
        if self.layout_name in ("draft", "loading"):
            direction = 1 if self.layout_name == "draft" else -1
            painter.translate(direction * (1.0 - progress) * SLIDE, 0)
        motion = self._motion(now)
        if self.layout_name == "menu":
            paint.paint_menu(
                painter,
                self.design_size[0],
                self.strip.frame(now),
                self.badge.frame(now),
                pulse=1.0,  # a steady badge dot: no frames needed to keep it alive
                rule=motion.rule,
                scale=self.scale,
            )
        elif self.layout_name == "card" and self.card:
            self._paint_card(painter, motion, now)
        elif not (self.allies or self.enemies):
            paint.paint_status(painter, self.design_size[0], self.view.status, motion)
        else:
            height, status, status_alpha = None, None, 0.0
            if self.grow:
                start_h, started, status = self.grow
                t = (now - started) / GROW
                if t >= 1.0:
                    self.grow = None
                full = self.design_size[1]
                height = start_h + (full - start_h) * _ease(_OUT_CUBIC, t)
                status_alpha = 1.0 - _ease(_IN_CUBIC, (now - started) / STATUS_OUT)
            mixes = {
                pid: {g: (old, now - at) for g, (old, at) in groups.items()}
                for pid, groups in self.mixes.items()
            }
            paint.paint_stack(
                painter,
                self.design_size[0],
                self.allies,
                self.enemies,
                motion,
                mode=" \u00b7 ".join(m for m in (self.view.mode_name, self.view.mode) if m),
                source=self.view.stats_source,
                mixes=mixes,
                height=height,
                from_bottom=self.layout_name == "draft",
                status=status,
                status_alpha=status_alpha,
            )
        painter.end()
        self._drawn = self.view

    def _paint_card(self, painter, motion, now):
        """The card's chrome once, its content swapping out then in when it changes."""
        data, seen = self.card[0], self.card[1]
        team = self.card[2][0]
        source = self.view.stats_source
        width = self._card_size()[0]
        height = self.height() / self.scale  # the window's current (maybe resizing) height
        if data is None:
            paint.card_chrome(painter, width, height, team, motion.rule)
            paint.paint_card_reading(painter, team, motion, chrome=False)
            return
        paint.card_chrome(painter, width, height, team, motion.rule)
        prev = self.card_prev
        if prev is None or now - prev[2] >= paint.SWAP_IN_AT + paint.SWAP_IN:
            paint.paint_card(painter, data, motion, source, seen, chrome=False)
            return
        dt = now - prev[2]
        base = painter.opacity()
        old_alpha = 1 - _ease(_IN_CUBIC, dt / paint.SWAP_OUT)
        new_alpha = _ease(_OUT_CUBIC, (dt - paint.SWAP_IN_AT) / paint.SWAP_IN)
        if old_alpha > 0:
            painter.save()
            painter.setOpacity(base * old_alpha)
            if prev[0] is None:  # the reading skeleton gives way to the player
                paint.paint_card_reading(painter, team, motion, chrome=False)
            else:
                paint.paint_card(painter, prev[0], motion, source, prev[1], chrome=False)
            painter.restore()
        if new_alpha > 0:
            painter.save()
            painter.setOpacity(base * new_alpha)
            painter.translate(0, (1 - new_alpha) * 3)
            paint.paint_card(painter, data, motion, source, seen, chrome=False)
            painter.restore()
