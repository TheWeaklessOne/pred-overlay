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

import math
import time
import traceback

from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QEasingCurve,
    QIODevice,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QCursor, QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from .. import modes
from . import paint, scoreboard, win32
from .model import Encounter, PlayerView, ViewModel
from .motion import Badge, Seam
from .rows import (
    OMEDA_RANKS,
    ROLES,
    changed_groups,
    row,
    shown_name,
    team_balance,
    team_rank,
    team_winrate,
)
from .visibility import LOADING_SCREEN, Visibility, on_loading_screen

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

# Design-pixel anchors taken from the mocks in data/screens/mock/.
STACK_W = 640
STACK_RIGHT, STACK_BOTTOM = 54, 96
STACK_LEFT, STACK_TOP = 64, 200
CARD_GAP = 32  # between the cursor and the card
CARD_MOVE = 0.14  # the card flies to another row of its team
CARD_CROSS = 0.20  # ... or across to the other team
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


def _sample_seasons():
    """A short history for the sample card."""

    def season(name, final, peak, percentile, date, ranking=None, peak_ranking=None):
        return {
            "name": name,
            "final": final,
            "peak": peak,
            "ranking": ranking,
            "peak_ranking": peak_ranking,
            "percentile": percentile,
            "peak_date": date,
            "unranked": False,
        }

    return (
        season("Season 1 - Split 5", "Platinum III", "Platinum I", 22, "OCT 26", 8120, 4310),
        season("Season 1 - Split 4", "Gold I", "Platinum III", 31, "MAY 26"),
        season("Season 1 - Split 3", "Diamond III", "Diamond II", 6, "FEB 26", 1980, 1120),
        season("Season 1 - Split 2", "Gold II", "Gold I", 40, "OCT 25"),
    )


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
        seasons=_sample_seasons(),
        main={"hero": heroes[0], "role": ROLES[len(name) % len(ROLES)]},
        encounters=tuple(encounters),
        today={"form": ("L", "W", "W", "L", "W"), "vp": 41, "kda": 3.2} if me else None,
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
            encounters=(Encounter(1, "Riktor", "against", False, kda=(9, 4, 12)),),
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
            encounters=(
                Encounter(1, "Sparrow", "with", True, kda=(7, 2, 5)),
                Encounter(6, "Sparrow", "against", False, kda=(3, 6, 4)),
                Encounter(9, "Murdock", "with", True, kda=(11, 4, 3)),
            ),
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
        self.failures = {}  # tick, frame, paint -> the last failure logged
        self.log = log or (lambda message: None)
        self.tab_since = None
        self.card = None  # (row dict, {"hero", "role"}, (team, index)) under the cursor
        self.strip, self.badge = paint.new_strip(), Badge()  # the menu row's motion
        self.menu_widths = {}
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
        self.card_prev = None  # (row, seen, swapped at): the card content being replaced
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
        message = f"{type(exc).__name__}: {exc}"
        if self.failures.get(where) != message:
            self.failures[where] = message
            self.log(f"overlay {where} failed: {message}\n{traceback.format_exc()}")

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
                    duration, curve = (CARD_MOVE, _OUT_CUBIC) if same_team else (CARD_CROSS, _EXIT)
                    self.card_move = (self.geometry(), now, duration, curve)
                    if not same_team:
                        self.card_bar = (self.card[2][0], now, duration)
                    anchor = self.card[3] if same_team else QCursor.pos().x()
                    if self.card[0] is None:  # the skeleton gave way: the first real card
                        self._reveal_table(card, now + paint.SWAP_IN_AT)
                    self.card = card + (anchor,)
                else:
                    self.card = card + (QCursor.pos().x(),)  # anchored where it was entered
                    self.card_prev = self.card_move = self.card_bar = None
                    self.revealed_at = now
                    self._reveal_table(card, now)
            elif card[:2] != self.card[:2]:
                if self.card[0] is None or card[0] is None or card[1] != self.card[1]:
                    self.card_prev = (self.card[0], self.card[1], now)  # skeleton -> player
                    if self.card[0] is None:
                        self._reveal_table(card, now + paint.SWAP_IN_AT)
                self.card = card + self.card[3:]
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
                    self.strip.replay(now + 0.06)  # the drawer opens again on a fresh reveal
                    if self.layout_name == "menu":
                        self.badge_at = now + BADGE_STRIKE_AFTER
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
        if self.card_bar and now - self.card_bar[1] >= self.card_bar[2]:
            self.card_bar = None
        if self._waiting(now):
            self.update()  # the gentle pulse needs no 60 fps: the 20 Hz tick will do
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
        # A real card may cover rows: a retry would read the card, not the board.
        covered = self.layout_name == "card" and self.card and self.card[0] is not None
        if held >= READ_AFTER and not covered and self.reader.wanted(view.match_id):
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
        role = entry["role"] if modes.has_lanes(view.mode_name) else None
        return row(player), {"hero": entry["hero"], "role": role}, hit

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
        # The set: a re-sort of the same players is no new content.
        return family, self.view and self.view.match_id, frozenset(self.roster_ids)

    def _reveal_table(self, card, at):
        """The season table comes in once per TAB press, on the first real card."""
        if card[0] is not None and self.card_tab != self.tab_since:
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
            paint.Motion(rule=0.0, frame=self.layout_name),
            mode=self._mode_label(),
            source=self.view.stats_source,
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
        self.allies = [row(p) for p in view.allies]
        self.enemies = [row(p) for p in view.enemies]
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
        predgg = self.view.stats_source == "predgg"
        balance = None if predgg else team_balance(self.allies, self.enemies)
        for team, rows in (("ally", self.allies), ("enemy", self.enemies)):
            average = team_winrate(rows) if predgg else team_rank(rows)
            said = (average, balance["text"] if balance and team == "enemy" else None)
            if said != self.heads.get(team, (None, None)):
                if live and average is not None:
                    self.head_at[team] = max(now, self.wave_free)
                self.heads[team] = said

    def _frame_step(self):
        """Every 16 ms while animating: run the seam, move the flying card, repaint."""
        try:
            now = self.clock()
            self._advance_seam(now)
            if self.card_move and self.layout_name == "card":
                self._slide_card(now)
            self.update()
            self.failures.pop("frame", None)
        except Exception as exc:
            self._failed("frame", exc)

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
            return bool(self.card and self.card[0] is None)
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
            frame="loading" if loud else "draft",
        )

    def _animating(self, now):
        if not self.isVisible():
            return False
        if self.fade_started and now - self.fade_started < self.fade_duration:
            return True  # including its first frames, where progress is still 0 or 1
        if self.seam:
            return self.seam.stopped is None
        if self.layout_name == "menu":  # settles; no idle frames
            busy = max(self.strip.busy_until(), self.badge.busy_until())
            if self.badge_at and now < self.badge_at + paint.STRIKE:
                return True
            return now < busy or now - self.revealed_at < RULE_TIME
        if self.layout_name == "card":
            swap = paint.SWAP_IN_AT + paint.SWAP_IN
            swapping = self.card_prev and now - self.card_prev[2] < swap
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
                self.strip.frame(now),
                self.badge.frame(now),
                pulse=1.0,  # a steady badge dot: no frames needed to keep it alive
                rule=motion.rule,
                scale=self.scale,
                strike=strike,
            )
        elif self.layout_name == "card" and self.card:
            self._paint_card(painter, motion, now)
        elif not (self.allies or self.enemies):
            paint.paint_status(painter, self.design_size[0], self.view.status, motion)
        else:
            height, status, status_alpha = None, None, 0.0
            if self.grow:
                start_h, started, status, text_alpha = self.grow
                t = (now - started) / GROW
                if t >= 1.0:
                    self.grow = None
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
                source=self.view.stats_source,
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
        data, seen = self.card[0], self.card[1]
        team = self.card[2][0]
        source = self.view.stats_source
        width = self._card_size()[0]
        height = self.height() / self.scale  # the window's current (maybe resizing) height
        was, bar = None, 1.0
        if self.card_bar:
            was = self.card_bar[0]
            bar = _ease(_EXIT, (now - self.card_bar[1]) / self.card_bar[2])
        paint.card_chrome(painter, width, height, team, motion.rule, was, bar)
        if data is None:
            paint.paint_card_reading(painter, team, motion, chrome=False)
            return
        reveal = None
        if self.card_reveal is not None and now - self.card_reveal < paint.TABLE_TIME:
            reveal = max(0.0, now - self.card_reveal)
        prev = self.card_prev
        if prev is None or now - prev[2] >= paint.SWAP_IN_AT + paint.SWAP_IN:
            paint.paint_card(painter, data, motion, source, seen, chrome=False, reveal=reveal)
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
            paint.paint_card(painter, data, motion, source, seen, chrome=False, reveal=reveal)
            painter.restore()
