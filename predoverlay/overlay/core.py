"""Pure match logic: ``reduce(state, event, now) -> commands``; ``view(state)`` -> ViewModel.

The roster is what the game shows: names read off the draft and loading screens
(roster.py). Pred.gg says who each name is and how they play; the game log says
the phase and who plays here. Nothing comes from Omeda's servers.

Events (tuples, first item is the kind); ``None`` as the payload means the
lookup failed and is retried with a backoff:
    ("log", phase_event)                 from phases.parse / LogFollower
    ("screen", kind, key, {(row, i): [text, ...]} | None[, details])  a reading of the
                                         draft ("draft", rows "ally") or loading ("loading",
                                         rows "top"/"bottom") screen, see roster.boxes;
                                         ``key`` is the phase's start (phase_at); details:
                                         {"ms": reading time, "n": reading of this phase}
    ("found", epoch, {seat: {"player_id", "name"} | None} | None)  Pred.gg's search
    ("board_read", match_id, board, {(team, i): {"text", "hero", "role"}})  every row the
                                         TAB board ("tab") or end board ("end") showed
    ("profiles", epoch, {pid: profile} | None)  ranks, seasons, main (lookup.profiles)
    ("recent", epoch, (summaries, details) | None)  my latest matches (lookup.my_matches)
    ("display", {internal: display} | None)  hero display names
    ("icons", {internal: icon})          portrait names of all heroes
    ("portraits", stored, failed icons)  a download round is over
    ("game", running)                    the game client started or quit (process list)
    ("sign_in",)                         the user signed in from the tray
    ("match", match_id, details | None)  a finished match, to learn who was where
    ("aliases", salt, {digest: pid}, pending)  what earlier matches taught (aliases.Store)
    ("auth", ok)                         a Pred.gg read was refused (False) or went through
    ("reach", ok)                        Pred.gg could not be reached (False), or could
    ("tick",)

Commands returned for the I/O lanes:
    ("display",)  ("search", epoch, {seat: (spelling, ...)})  ("profiles", epoch, ids)
    ("recent", epoch, my_id)  ("portraits", icons)  ("match", match_id)
    ("remember", {digest: pid | None})  ("pending", learns)  (the known-players file)

Seats are the players (seats.py): "a0".. your team, "e0".. the other, each with what
every screen read of the name; who that name is on Pred.gg is its identity
(identity.py). A seat's reading counts once two versions of it agree (versions of one
reading or of several, see roster.py); it is then looked up once.
While the game client is not running nothing is asked for at all.
"""

import dataclasses
from dataclasses import dataclass, field
from urllib.parse import unquote_plus

from ..modes import experience_label, has_lanes, is_pve, roster_mode
from . import aliases, identity, lookup, roster
from . import seats as seating
from .model import Encounter, PlayerView, ViewModel
from .rows import ROLES, rank_points, rank_step
from .scoreboard import assign, similarity
from .seats import REPEATS, TEAM  # noqa: F401  (REPEATS: the tests' threshold)

SPELLINGS = identity.SPELLINGS
CARD_RETRY = (5, 15, 60, 180)  # backoff for every failed lookup
RECENT = 20  # my matches read per match: "met before" and the day so far
MISSED = 3  # full pick-screen readings without a seat's name before it counts as a misread
# Where the panel lists a seat seen first on: the pick screen's row (0..4), the loading
# screen's column (+5), a role plate (+10), the TAB board's row (+20)
LOADING_ORDER, ROLE_INDEX, BOARD_ORDER = 5, 10, 20
PENDING = 5  # finished matches waiting to be learned from
SESSION_GAP = 6 * 60 * 60  # a break this long ends a day of play; midnight does not
PORTRAIT_TRIES, PORTRAIT_RETRY = 3, 5 * 60.0  # failed downloads, per start of the app
ACCEPT_TIMEOUT = 100.0  # an accept with no draft by then fell through
MIN_MATCH = 120.0  # a presence change this soon after the start is still the start
PER_EPOCH = ("search", "profiles", "recent")
# A finished match is listed on Pred.gg some time after it ends (the log says how
# long, see "listed"): the first look waits a minute, later ones back off, then it is
# left. A new match accepted meanwhile asks at once: what it teaches is wanted now.
LEARN_AFTER, LEARN_RETRY = 60.0, (60, 120, 300, 600, 1200)
OFFLINE = "Pred.gg offline"
NEEDS_LOGIN = "sign-in needed"
WAITING = "waiting for Predecessor"
STATUS = {
    "menu": "waiting for a match",
    "accepted": "match found",
    "draft": "draft",
    "loading": "match",
}


@dataclass
class State:
    # The tracker starts with False and its process check sets it; True here keeps
    # the reducer usable on its own (and in tests) as if the game were running.
    game_running: bool = True
    me: str | None = None  # my player ID, from the game log
    my_name: str | None = None  # my name, from the game log
    auth: str = "ok"  # "login" once Pred.gg refused the session (or there is none)
    offline: bool = False  # the last Pred.gg read could not reach it
    phase: str = "menu"
    phase_at: float = 0.0
    epoch: int = 0
    seats: dict = field(default_factory=dict)  # seat id -> seats.new: what screens read
    ids: dict = field(default_factory=dict)  # seat id -> identity.new: who it is on Pred.gg
    profiles: dict = field(default_factory=dict)  # pid -> lookup.profiles entry
    asked: set = field(default_factory=set)  # pids whose profile was asked for this epoch
    missing: set = field(default_factory=set)  # asked, and Pred.gg had no profile
    strength: dict = field(default_factory=dict)  # seat -> place in its team, set once
    encounters: dict = field(default_factory=dict)  # pid -> tuple[Encounter]
    matches: list = field(default_factory=list)  # my latest match details, newest first
    known: dict = field(default_factory=dict)  # name key -> pid, from my recent matches
    salt: str | None = None  # this PC's key for name digests and labels (aliases.py)
    aliases: dict = field(default_factory=dict)  # name digest -> pid, learned after matches
    learns: list = field(default_factory=list)  # finished matches to learn from (_learning)
    today: dict | None = None  # see today_summary; None: not read yet
    last_finished: str | None = None  # the match ID the log named last
    display: dict = field(default_factory=dict)
    icons: dict = field(default_factory=dict)  # internal hero name -> portrait
    art: int = 0  # grows with every round that brought new portraits
    portraits_failed: tuple = ()
    portrait_tries: int = 0
    portrait_retry: float = 0.0
    inflight: set = field(default_factory=set)
    failures: dict = field(default_factory=dict)  # lookup key -> (count, retry_at)
    loaded: set = field(default_factory=set)  # per-epoch lookups done
    unanchored: bool = False  # a draft reading without the pick screen's header was logged
    joined: bool = False  # the client is on the match map (loading phase)
    started_at: float = 0.0  # when the match itself started; 0 while on the loading screen
    ended_at: float = 0.0  # when the match ended: the end-of-match screen is up until the menu
    note: str | None = None  # overrides the phase status (unsupported mode)
    mode_name: str | None = None  # "RANKED", from the map URL once the match loads
    notes: list = field(default_factory=list)  # lines for the log; the tracker takes them
    why: str | None = None  # why the screen reading being taken was turned down
    ledger: tuple | None = None  # the last draft reading's ledger line, unrepeated
    version: int = 0


def _new_epoch(state):
    state.epoch += 1
    state.seats, state.ids, state.profiles = {}, {}, {}
    state.asked, state.missing = set(), set()
    state.strength, state.encounters, state.matches = {}, {}, []
    state.ended_at, state.note = 0.0, None
    state.loaded, state.unanchored = set(), False
    # Per-epoch lookups are moot now: their keys go, and a late answer is dropped by epoch.
    state.inflight = {key for key in state.inflight if key[0] not in PER_EPOCH}
    state.failures = {key: v for key, v in state.failures.items() if len(key) == 1}
    state.version += 1


def _to_menu(state, at):
    state.phase, state.phase_at = "menu", at
    state.offline = False  # nothing is read in the menu: nothing would ever clear it
    state.mode_name = None
    state.joined, state.started_at = False, 0.0
    _new_epoch(state)


def _backoff(failures, key, now):
    count = failures.get(key, (0, 0.0))[0] + 1
    failures[key] = (count, now + CARD_RETRY[min(count, len(CARD_RETRY)) - 1])


def _due(state, key, now):
    return key not in state.inflight and now >= state.failures.get(key, (0, 0.0))[1]


def gave_up(state, key):
    """This lookup has failed through every step of the backoff: its rows say so, and it
    is tried again at the last step's pace."""
    return state.failures.get(key, (0, 0.0))[0] >= len(CARD_RETRY)


_gave_up = gave_up


_agreed = seating.agreed  # the tests' name for it
_versions = seating.versions


def _mine(state, seat):
    return identity.mine(state, seat)


def _spellings(seat):
    """Other screens' spellings of a seat's name, best first (the board's capitals are
    the cleanest, then the loading screen's)."""
    return tuple(seat["spelled"][g] for g in ("board", "loading", "draft") if g in seat["spelled"])


def _candidates(state, sid):
    seat = state.seats[sid]
    own = [t for _, t in seat["evidence"].get(seat["primary"], ())]
    return identity.candidates(seat["reading"], _spellings(seat), own, state.ids[sid])


def _on_screen(state, kind, key, texts):
    """A reading of the draft or loading screen; returns whether anything changed."""
    if kind == "draft":
        return _on_draft(state, texts)
    return _on_loading(state, texts)


def _on_roles(state, texts):
    """The role screen before the pick screen: a plate with a name under each role
    a player picked. It only adds names: who has not picked yet is not on it, so
    nobody goes for lack of a plate (the pick screen settles who is there)."""
    rows = {i: v for (row, i), v in texts.items() if row == "role" and _versions(v)}
    if not rows:
        return False
    anchor = texts.get(("roles", 0)) or ()
    header = any(lookup.name_key(t) == lookup.name_key(roster.ROLE_ANCHOR_TEXT) for t in anchor)
    me = state.my_name and any(
        similarity(t, state.my_name) >= 0.9 for v in rows.values() for t in v if t
    )
    if not (header or me):
        return False
    entries = {
        i: {"texts": v, "role": _plate_role(i), "order": ROLE_INDEX + i} for i, v in rows.items()
    }
    _, changed = seating.place(state, "draft", "ally", entries)
    return changed


def _plate_role(index):
    """The role of a plate's column (roster.ROLE_X: OFFLANE .. CARRY, then FILL)."""
    column = index // roster.ROLE_STACK
    order = ("OFFLANE", "JUNGLE", "MIDLANE", "SUPPORT", "CARRY")
    return order[column] if column < len(order) else None


def _on_draft(state, texts):
    """The pick screen: my team's names, one per row.

    Rows are matched to seats by name, not by place: players swap rows when they
    trade roles. Only the pick screen counts (its ALLY header is in the reading):
    the role screen before it shows other text where the names will be.
    """
    roles = _on_roles(state, texts)
    anchor = texts.get(("anchor", 0)) or ()
    rows = {i: v for (row, i), v in texts.items() if row == "ally" and _versions(v)}
    header = any(lookup.name_key(t) == lookup.name_key(roster.ANCHOR_TEXT) for t in anchor)
    # A game in another language has no ALLY header; my own name on a row says the
    # same: the role screen never shows it in these boxes.
    me = state.my_name and any(
        similarity(t, state.my_name) >= 0.9 for v in rows.values() for t in v if t
    )
    if not (header or me):
        if rows and not roles and not state.unanchored:
            state.unanchored = True
            state.notes.append("draft: names read, but no pick-screen header or own name")
        if rows and not roles:
            state.why = "no pick-screen header or own name"
        return roles
    before = set(seating.live(state, "ally"))
    entries = {i: {"texts": v, "role": None, "order": i} for i, v in rows.items()}
    placed, changed = seating.place(state, "draft", "ally", entries, cap=seating.MAX_SEATS)
    if len(rows) >= TEAM:
        # A full reading without a row for a seat: three of those and it was a misread.
        for sid in before:
            seat = state.seats[sid]
            if sid in placed.values():
                seat["missed"] = 0
            else:
                seat["missed"] += 1
                if seat["missed"] >= MISSED and not seat["gone"]:
                    seat["gone"] = changed = True
    for index, sid in placed.items():
        seat = state.seats[sid]
        seat["order"] = index  # where it is now: the panel lists in this order
        hero = _picked(state, texts.get(("hero", index)))
        if hero is not KEEP and hero != seat["hero"]:
            seat["hero"], changed = hero, True
    return changed or roles


KEEP = object()  # _picked: this reading tells nothing new
HERO_WAITING = ("WAITING", "PICKING")  # "Waiting...", "Picking...": nothing picked yet


def _placeholder(text, heroes):
    """Whether a hero line reads as "Waiting..." rather than any hero (folded keys)."""
    from difflib import SequenceMatcher

    from .scoreboard import _key

    key = _key(text)
    if not key:
        return False
    near = max(SequenceMatcher(None, key, _key(w)).ratio() for w in HERO_WAITING)
    best = max((SequenceMatcher(None, key, _key(h)).ratio() for h in heroes), default=0.0)
    return near >= 0.5 and near >= best


def _picked(state, texts):
    """The hero a pick-screen row shows under the name: a hero, None when it says
    "Waiting..." (any version: one misread of it must not pick a hero), or KEEP when
    the reading is empty or its versions do not agree on one hero."""
    from .scoreboard import snap_hero

    texts = [t for t in texts or () if t and t.strip()]
    heroes = sorted(set((state.display or {}).values()))
    if any(_placeholder(t, heroes) for t in texts):
        return None
    found = [h for h in (snap_hero(t, heroes) for t in texts) if h]
    best = max(sorted(set(found)), key=found.count) if found else None
    return best if best and found.count(best) >= 2 else KEEP  # a sure reading is two


def _on_loading(state, texts):
    """The loading screen: both teams, one column per role. My team's seats are matched
    by name (the draft's rows are the pick order, these columns the roles); a column
    whose name matches none takes the seat of its role, or a new one."""
    rows = {row: {i: v for (r, i), v in texts.items() if r == row} for row in ("top", "bottom")}
    mine = _my_row(state, rows)
    if mine is None:
        state.why = "neither row known as mine"
        return False  # neither row is known to be mine yet: wait for my name
    changed = False
    for team, row in (("ally", mine), ("enemy", "top" if mine == "bottom" else "bottom")):
        entries = {
            i: {"texts": v, "role": _role(i), "order": i + (LOADING_ORDER if team == "ally" else 0)}
            for i, v in rows[row].items()
        }
        placed, moved = seating.place(
            state, "loading", team, entries, cap=seating.MAX_SEATS, by_role=True
        )
        changed |= moved
        changed |= _retire_unplaced(state, team, entries, placed)
    return changed


def _retire_unplaced(state, team, entries, placed):
    """A screen that showed every player of a team (every column read) and placed them
    all: a live seat it did not place, beyond five, was a misread."""
    full = sum(1 for e in entries.values() if _versions(e["texts"])) >= TEAM
    seats = seating.live(state, team)
    if not full or len(placed) < TEAM or len(seats) <= TEAM:
        return False
    changed = False
    for sid in seats:
        if sid not in placed.values():
            state.seats[sid]["gone"] = changed = True
    return changed


def _on_board(state, board, rows):
    """Every row the TAB board showed: both teams, by role, with the hero each plays. The board is read all match, so a player the loading screen did
    not give (or gave misread) gets a seat and a name from it."""
    changed = False
    size = max([i + 1 for _, i in rows] or [0])
    for team in ("ally", "enemy"):
        entries = {
            i: {"texts": _board_texts(row), "role": row.get("role"), "order": i + BOARD_ORDER}
            for (side, i), row in rows.items()
            if side == team and row.get("text")
        }
        whole = size == len(ROLES)
        placed, moved = seating.place(state, "board", team, entries, by_role=whole, whole=whole)
        changed |= moved
        changed |= _retire_unplaced(state, team, entries, placed)
        for i, sid in placed.items():
            hero = (rows[(team, i)].get("hero") or "").upper()
            if hero and board == "tab" and not state.seats[sid]["hero"]:
                state.seats[sid]["hero"], changed = hero, True
    return changed


def _board_texts(row):
    """A board row's versions; when they do not agree, the reader's choice among them (the
    one nearer the roster, else Windows OCR's) counts as another: a player the loading
    screen missed must still get a name from the board."""
    texts = list(row.get("texts") or [row["text"]])
    if not seating.agreed(seating.versions(texts)) and row.get("text"):
        texts.append(row["text"])
    return texts


def _turned_down(state, screen, key, texts, current, on_screen):
    """Why a screen reading is not taken at all, or None."""
    if not texts:
        return "nothing read" if texts is not None else "reading failed"
    if not state.game_running:
        return "game not running"
    if state.note:
        return state.note
    if not on_screen:
        return "match started" if state.started_at else f"phase {state.phase}"
    if screen != current:
        return f"{screen} screen read in {current}"
    if key != state.phase_at:
        return "an earlier phase"
    return None


_NOT_NAMES = ("anchor", "roles", "hero")


def names_read(texts):
    """(boxes with text, sure ones) among a screen reading's name boxes: not its header,
    its role labels or its hero lines. Two equal versions are the reader's sign of a
    sure reading."""
    boxes = [v for k, v in (texts or {}).items() if k[0] not in _NOT_NAMES]
    text = sum(1 for v in boxes if any(t and t.strip() for t in v))
    sure = sum(1 for v in boxes if len(v) == 2 and v[0] == v[1] and v[0].strip())
    return text, sure


def _ledger(state, screen, texts, details, changed):
    """One log line per reading, never a name: boxes with text, sure ones, the verdict.
    A draft reading like the last one is not repeated (the draft is read to its end)."""
    text, sure = names_read(texts)
    verdict = f"dropped ({state.why})" if state.why else "taken" if changed else "nothing new"
    line = (screen, text, sure, verdict)
    if screen == "draft" and line == state.ledger:
        return
    state.ledger = line
    ms = details.get("ms")
    took = f", {ms:.0f} ms" if isinstance(ms, (int, float)) else ""
    number = f" #{details['n']}" if details.get("n") else ""
    state.notes.append(f"read {screen}{number}: {text} names, {sure} sure{took} -> {verdict}")


def _role(index):
    roles = roster.LOADING_ROLES
    return roles[index] if 0 <= index < len(roles) else None


def _learning(state, match_id, at):
    """What a finished match can teach: the loading screen's names by team and role."""
    if not has_lanes(state.mode_name) or not state.me:
        return None

    def reading(sid):  # mine as the game log spells it: my column must check out
        seat = state.seats[sid]
        mine = state.my_name and state.ids[sid]["pid"] == state.me
        return state.my_name if mine else seat["reading"]

    rows = [
        [
            s["team"],
            s["role"],
            aliases.digest(state.salt, lookup.name_key(reading(sid))),
            aliases.spelled(state.salt, lookup.strict_key(reading(sid))),
            s["role_from"],  # the board's roles are exact; the loading columns may flip
        ]
        for sid, s in state.seats.items()
        # my own column too: my name, which Pred.gg shows, tells the columns' order
        if s["role"] and s["role_from"] in ("loading", "board") and s["reading"] and not s["gone"]
    ]
    if not rows:
        return None
    return {"match_id": match_id, "rows": rows, "due": at + LEARN_AFTER, "tries": 0, "ended": at}


_FLIP = dict(zip(roster.LOADING_ROLES, reversed(roster.LOADING_ROLES)))


def _column(role, source, flip):
    """The role a learned row stands for: a loading column's flipped with the side."""
    return _FLIP.get(role, role) if flip and source != "board" else role


def _learn_from(state, learn, details):
    """Name digests of a match's loading screen -> the players Pred.gg lists there.

    A team is learned only when it checks out: every player whose name Pred.gg does
    show (PC players, me included) must be in the column of their role, by the same
    name digest, under one order of the columns (it depends on the side). One that
    is not means the roles do not match the columns (lanes swapped in play): nothing
    is learned for that team. With no PC player to tell the order, only the middle
    column is learned.
    """
    players = (details or {}).get("players") or []
    mine = next((p.get("team") for p in players if p.get("player_id") == state.me), None)
    if not mine:
        return {}
    teams = {}
    for team in ("ally", "enemy"):
        # (role, name digest, spelling digest or None: rows of matches before 0.5.3, and
        # where the role came from: "board" is exact, the loading screen's may flip)
        rows = [
            (row[1], row[2], row[3] if len(row) > 3 else None, row[4] if len(row) > 4 else None)
            for row in learn["rows"]
        ]
        rows = [row for row, raw in zip(rows, learn["rows"]) if raw[0] == team]
        by_role = {}
        for p in players:
            if (p.get("team") == mine) == (team == "ally"):
                by_role.setdefault(p.get("role"), []).append(p)
        teams[team] = rows, by_role

    def checks(flip, team):
        """For each PC player in a column: is the name Pred.gg shows the one read there?"""
        rows, by_role = teams[team]
        out = []
        for role, name, _, source in rows:
            found = by_role.get(_column(role, source, flip), ())
            if len(found) == 1 and found[0].get("name") not in (None, "", lookup.HIDDEN):
                keys = lookup.name_keys(found[0]["name"])
                out.append(name in {aliases.digest(state.salt, key) for key in keys})
        return out

    # The loading screen's columns run CARRY..OFFLANE on one side of the map and
    # OFFLANE..CARRY on the other (seen 2026-11-19). The PC players' names tell
    # which: the order under which every one of them stands in their role's column.
    def fit(names):
        return [
            flip
            for flip in (False, True)
            if any(len(checks(flip, t)) for t in names) and all(all(checks(flip, t)) for t in names)
        ]

    both = fit(teams)
    taught = {}
    for team in teams:
        rows, by_role = teams[team]
        fits = both if len(both) == 1 else fit([team])  # one team's lanes may be swapped
        if len(fits) == 1:
            flip = fits[0]
        elif checks(False, team) and not fits:
            continue  # its PC players stand in no order's columns: lanes swapped in play
        else:
            # Nothing tells the order: only the middle column, MIDLANE either way.
            flip = False
            rows = [row for row in rows if _FLIP.get(row[0]) == row[0] or row[3] == "board"]
        for role, name, spelled, source in rows:
            found = by_role.get(_column(role, source, flip), ())
            if len(found) != 1:
                continue
            pid = found[0]["player_id"]
            if pid == state.me:
                continue  # my column told the order; I am never looked up by name
            entries = {name: pid}
            if spelled:
                entries[spelled] = pid
                entries[aliases.checked(state.salt, name)] = pid
            taught.update({k: v for k, v in entries.items() if state.aliases.get(k) != v})
    state.aliases.update(taught)
    return taught


def _my_row(state, rows):
    """ "top" or "bottom": the row with my name, or the most of my team's draft names."""
    allies = [
        s["reading"]
        for s in state.seats.values()
        if s["team"] == "ally" and s["reading"] and not s["gone"]
    ]
    names = dict(enumerate(allies + ([state.my_name] if state.my_name else [])))
    scores = {}
    for row, found in rows.items():
        readings = {i: roster.best(v) for i, v in found.items()}
        mine = state.my_name and any(
            similarity(text, state.my_name) >= 0.9 for text in readings.values()
        )
        scores[row] = (bool(mine), len(assign(readings, names)) if names else 0)
    ranked = sorted(scores, key=scores.get, reverse=True)
    best = scores[ranked[0]]
    if best == (False, 0) or best == scores[ranked[1]]:
        return None
    return ranked[0]


def _lookups(state, now):
    """Pred.gg reads still missing for this roster: names, profiles, my recent matches."""
    if not state.game_running or state.auth == "login":
        return []
    wanted = []
    if not state.display:
        wanted.append((("display",), ("display",)))
    if state.phase in ("accepted", "draft", "loading"):
        if state.me and "recent" not in state.loaded:
            wanted.append((("recent", state.epoch), ("recent", state.epoch, state.me)))
        names = {}
        for sid, seat in sorted(state.seats.items()):
            ident = state.ids[sid]
            if seat["primary"] == "loading" and state.phase == "draft":
                continue  # the log says the mode with the loading screen: bots are no players
            # Never answered (ident["failed"]) is asked again, at the backoff's slowest pace.
            unasked = not ident["searched"] or (ident["failed"] and ident["searched"] is True)
            if seat["reading"] and not ident["pid"] and unasked and not seat["gone"]:
                spellings = _candidates(state, sid)
                if spellings:
                    names[sid] = spellings
                else:
                    ident["searched"] = True  # every spelling was asked for: not on Pred.gg
        if names:
            wanted.append((("search", state.epoch), ("search", state.epoch, names)))
        ids = tuple(
            dict.fromkeys(
                i["pid"]
                for _, i in sorted(state.ids.items())
                if i["pid"] and i["pid"] not in state.asked
            )
        )
        # Profiles wait for a search in flight or going out now (its players come in
        # the same request), not for one that is waiting out a backoff.
        searching = ("search", state.epoch) in state.inflight or (
            names and _due(state, ("search", state.epoch), now)
        )
        if ids and not searching:
            wanted.append((("profiles", state.epoch), ("profiles", state.epoch, ids)))
    commands = []
    for key, command in wanted:
        if _due(state, key, now):
            state.inflight.add(key)
            if command[0] == "search":
                for sid, spellings in command[2].items():
                    state.ids[sid]["searched"] = "asked"
                    state.ids[sid]["asking"] = spellings
            elif command[0] == "profiles":
                state.asked.update(command[2])
            commands.append(command)
    return commands


def _failed(state, key, now):
    """A per-epoch lookup failed: back off. After the last step of the backoff the rows
    say that Pred.gg did not answer, and the lookup goes on at that step's pace: an outage
    that ends during the match still gives the rows their data."""
    _backoff(state.failures, key, now)
    if key[0] == "search":
        for ident in state.ids.values():
            if ident["searched"] == "asked":
                ident["searched"] = _gave_up(state, key)  # True: shown without data
                ident["failed"] = ident["searched"] is True
    elif key[0] == "profiles":
        # Nobody of the failed request counts as asked: only an answer says who has no
        # profile (see "missing" where the profiles come).
        state.asked = {p for p in state.asked if p in state.profiles or p in state.missing}
    elif key[0] == "recent" and _gave_up(state, key):
        state.loaded.add("recent")  # optional: the panel works without it
    state.version += 1


def reduce(state, event, now):
    """Apply one event; return the commands it triggers."""
    kind = event[0]
    commands = []
    if kind == "log":
        commands += _on_log(state, event[1], now)
    elif kind == "game":
        commands += _on_game(state, bool(event[1]), now)
    elif kind == "sign_in":
        state.auth, state.offline = "ok", False
        state.failures = {k: v for k, v in state.failures.items() if k[0] == "display"}
        # Profiles given up on are asked for again too.
        state.asked = {p for p in state.asked if p in state.profiles or p in state.missing}
        for ident in state.ids.values():
            if ident["searched"] is True and not ident["pid"]:
                ident["searched"] = False  # refused before: ask again with the new session
        state.version += 1
        commands += _lookups(state, now)
    elif kind == "auth":
        # A read that went through says nothing once a sign-in is needed: it may have been
        # on its way when the user signed out. Only a sign-in ends "login".
        if not event[1] and state.auth != "login":
            state.auth = "login"
            state.version += 1
    elif kind == "reach":
        if state.offline != (not event[1]):
            state.offline = not event[1]
            state.version += 1
    elif kind == "screen":
        _, screen, key, texts, *details = event
        current = state.phase if state.phase == "draft" else "loading"
        if screen == "loading" and current == "draft":
            current = "loading"  # the pixels show it before the log says so
        on_screen = state.phase in ("draft", "loading") and not state.started_at
        # Not against bots: their names are no players to look up (see the loading line).
        state.why = _turned_down(state, screen, key, texts, current, on_screen)
        changed = False
        if state.why is None:
            changed = _on_screen(state, screen, key, texts)
            if changed:
                _met(state)
                commands += _lookups(state, now)
        _ledger(state, screen, texts, details[0] if details else {}, changed)
    elif kind == "found":
        _, epoch, found = event
        key = ("search", epoch)
        state.inflight.discard(key)
        if epoch == state.epoch:
            if found is None:
                _failed(state, key, now)
            else:
                state.failures.pop(key, None)
                for sid, ident in state.ids.items():
                    if ident["searched"] != "asked":
                        continue
                    ident["searched"], ident["failed"] = True, False
                    hit = found.get(sid)
                    if hit:
                        ident["pid"], ident["name"] = hit["player_id"], hit["name"]
                    else:
                        ident["tried"] |= {lookup.attempt_key(t) for t in ident["asking"]}
                    ident["asking"] = ()
                state.version += 1
                _met(state)
                commands += _lookups(state, now)
    elif kind == "profiles":
        _, epoch, found = event
        key = ("profiles", epoch)
        state.inflight.discard(key)
        if epoch == state.epoch:
            if found is None:
                _failed(state, key, now)
            else:
                state.failures.pop(key, None)
                for profile in found.values():  # "games today" counts back from now
                    if isinstance(profile, dict) and isinstance(profile.get("stats"), dict):
                        at = min(now, state.started_at or now)  # came mid-match: its start
                        profile["stats"]["at"] = profile["stats"].get("at") or at
                state.profiles.update(found)
                # Asked for, and Pred.gg has no profile: no data, not "loading" for good.
                state.missing |= set(state.asked) - set(state.profiles)
                for sid, ident in state.ids.items():
                    profile = found.get(ident["pid"])
                    if profile is not None:
                        commands += identity.check_alias(
                            state, state.seats[sid]["reading"], ident, profile
                        )
                order = _strength_order(state)
                for sid, ident in state.ids.items():
                    if ident["pid"] in found:
                        state.strength.setdefault(sid, order.get(sid))
                state.version += 1
                commands += _lookups(state, now)
    elif kind == "recent":
        _, epoch, found = event
        key = ("recent", epoch)
        state.inflight.discard(key)
        if epoch == state.epoch:
            if found is None:
                _failed(state, key, now)
            else:
                summaries, details = found
                state.matches = list(details)
                state.today = today_summary(summaries, now)
                state.known.update(lookup.known_names(details))
                ids = {m["match_id"] for m in details}
                # A match that just ended is listed a few minutes later: ask again.
                late = state.last_finished and state.last_finished not in ids
                if late and not _gave_up(state, key):
                    _backoff(state.failures, key, now)
                else:
                    state.loaded.add("recent")
                    state.last_finished = None  # listed, or never will be: not asked again
                for sid, seat in state.seats.items():
                    if seat["reading"]:
                        identity.name(state, seat, state.ids[sid])
                _met(state)
                commands += _lookups(state, now)
    elif kind == "board_read":
        # Every row the TAB board showed: a source like the others. Not the end board: it
        # is read through the victory cinematic, whose words ("VICTORY") pass for names.
        _, match_id, board, rows = event
        if (
            match_id == f"{state.epoch}"
            and state.game_running
            and not state.note
            and board == "tab"
        ):
            if _on_board(state, board, rows):
                _met(state)
                state.version += 1
                commands += _lookups(state, now)
    elif kind == "aliases":
        _, state.salt, learned, pending = event
        state.aliases = {**learned, **state.aliases}
        known = {learn["match_id"] for learn in state.learns}
        state.learns += [learn for learn in pending if learn["match_id"] not in known]
        state.version += 1
    elif kind == "match":
        _, match_id, details = event
        state.inflight.discard(("match",))
        learn = next((x for x in state.learns if x["match_id"] == match_id), None)
        players = (details or {}).get("players") or ()
        if details is not None and not any(p.get("player_id") == state.me for p in players):
            details = None  # listed without its players yet: ask again later
        if learn:
            if details is None:
                learn["tries"] += 1
                if learn["tries"] > len(LEARN_RETRY):
                    state.learns.remove(learn)  # Pred.gg never listed it
                else:
                    learn["due"] = now + LEARN_RETRY[learn["tries"] - 1]
            else:
                state.learns.remove(learn)
                taught = _learn_from(state, learn, details)
                if taught:
                    commands.append(("remember", taught))
                ended = learn.get("ended")
                listed = f", listed {round(now - ended)} s after it ended" if ended else ""
                players = len(set(taught.values()))
                state.notes.append(f"learned {players} players from a finished match{listed}")
            commands.append(("pending", [dict(x) for x in state.learns]))
    elif kind == "display":
        state.inflight.discard(("display",))
        if not event[1]:  # an empty catalog would be asked for again on every tick
            _backoff(state.failures, ("display",), now)
        else:
            state.display = dict(event[1])
            state.version += 1
    elif kind == "icons":
        state.icons = dict(event[1])
        state.version += 1
        if state.matches:
            _met(state)  # they came first: their portraits are known only now
        icons = tuple(sorted(set(state.icons.values())))
        if icons and state.game_running:
            state.portrait_tries = 1
            commands.append(("portraits", icons))
        elif icons:  # the catalog came back after the game quit: download once it runs
            state.portrait_tries, state.portraits_failed, state.portrait_retry = 0, icons, 0.0
    elif kind == "portraits":
        _, stored, failed = event
        if stored:
            state.art += 1
            state.version += 1
        state.portraits_failed = failed
        state.portrait_retry = now + PORTRAIT_RETRY
    elif kind == "tick":
        if not state.game_running:
            return commands
        if state.phase == "accepted" and now - state.phase_at > ACCEPT_TIMEOUT:
            _to_menu(state, now)  # the draft never came: a failed start or a declined accept
        if state.portraits_failed and now >= state.portrait_retry:
            if state.portrait_tries < PORTRAIT_TRIES:
                state.portrait_tries += 1
                commands.append(("portraits", state.portraits_failed))
            state.portraits_failed = ()
        due = [x for x in state.learns if now >= x["due"]]
        if due and ("match",) not in state.inflight and state.auth != "login":
            learn = due[0]
            state.inflight.add(("match",))
            learn["due"] = now + LEARN_RETRY[-1]  # an answer sets the real next look
            commands.append(("match", learn["match_id"]))
        commands += _lookups(state, now)
    return commands


def _on_log(state, log, now):
    pending = []
    phase = log["phase"]
    if log.get("user_id") and log["user_id"] != state.me:
        state.me, state.today = log["user_id"], None  # another account: not the old one's day
        state.version += 1
    if log.get("user_name"):
        state.my_name = unquote_plus(log["user_name"])
    if phase == "login" or not state.game_running:
        return []  # the game signed in, or an old log replayed before the process check
    at = log.get("at_ts", now)
    if phase == "joined" and log.get("map") != "Draft" and state.phase in ("accepted", "draft"):
        # A reconnect joins the match map with no map-load line: it is the match.
        state.phase, state.phase_at = "loading", at
        state.joined, state.started_at = True, 0.0
        state.version += 1
    elif phase in ("joined", "presence"):
        # Markers inside the loading phase: the map is in, then the match starts.
        if state.phase == "loading" and not state.started_at and log.get("map") != "Draft":
            if phase == "joined":
                state.joined = True
            elif state.joined:
                state.started_at = at
                state.version += 1
        elif (
            phase == "presence"
            and state.phase == "loading"
            and state.started_at
            and not state.ended_at
            and at - state.started_at >= MIN_MATCH
        ):
            # The game's presence changes again when the match is over: its
            # end-of-match screen stays until the player leaves for the menu.
            state.ended_at = at
            state.version += 1
    elif phase in ("finished", "menu"):
        if log.get("match_id") and log.get("reason") == "REASON_FINISHED":
            state.last_finished = log["match_id"]
            learn = _learning(state, log["match_id"], at)
            if learn and all(x["match_id"] != learn["match_id"] for x in state.learns):
                state.learns = (state.learns + [learn])[-PENDING:]
                pending = [("pending", [dict(x) for x in state.learns])]
        _to_menu(state, at)
    else:
        if phase == "accepted" or (phase == "draft" and state.phase not in ("accepted", "draft")):
            # A second accept is another match: the first fell through. So is a draft that
            # follows anything but its own accept (the lines between were missed).
            _new_epoch(state)
            state.mode_name = None
            for learn in state.learns:
                learn["due"] = min(learn["due"], at)  # wanted for this match: ask now
        if phase == "loading" and log.get("experience"):
            state.mode_name = experience_label(log["experience"])
            if is_pve(state.mode_name):
                state.seats = {k: v for k, v in state.seats.items() if v["team"] == "ally"}
                state.ids = {k: v for k, v in state.ids.items() if k in state.seats}
                state.note = "mode not supported"
        if phase != state.phase:
            state.joined, state.started_at, state.ended_at = False, 0.0, 0.0
        state.phase, state.phase_at = phase, at
        state.version += 1
    return pending + _lookups(state, now)


def _on_game(state, running, now):
    """The game client started or quit; only a change does anything."""
    if running == state.game_running:
        return []
    state.game_running = running
    state.version += 1
    if not running:
        _to_menu(state, now)  # the match is gone with the game; so is "offline"
        state.inflight = set()  # the tracker drops the lanes' pending commands
        return []
    state.phase, state.phase_at = "menu", now
    return _lookups(state, now)


def drop_result(state, event, now):
    """A lane result that made ``reduce`` raise counts as a failed lookup, backoff included.

    ``reduce`` takes the key out of flight first; without a backoff the next tick
    would send the same command again at once, and fail the same way, forever.
    """
    kind = event[0]
    key = {"found": "search", "profiles": "profiles", "recent": "recent"}.get(kind)
    if key:
        key = (key, event[1])
    elif kind == "display":
        key = ("display",)
    else:
        return
    state.inflight.discard(key)
    _backoff(state.failures, key, now)


def _met(state):
    pids = tuple(i["pid"] for i in state.ids.values() if i["pid"])
    state.encounters = encounters_for(pids, state.me, state.matches, state.display, state.icons)
    state.version += 1


def today_summary(matches, now):
    """The own row's day: {"form": ("L", "W", ...) oldest first, "vp": 24, "kda": 4.4}.

    A day ends at a long break, not at midnight: night sessions run past it. No games
    means an empty form; a number Pred.gg left out is None, not 0.
    """
    day, last = [], now
    for match in sorted(matches, key=lambda m: m["ended_at"], reverse=True):
        if last - match["ended_at"] > SESSION_GAP:
            break
        day.append(match)
        last = match["ended_at"]
    day.reverse()
    gained = [m["vp"] for m in day if m["vp"] is not None]
    lines = [m["kda"] for m in day if None not in m["kda"]]
    kills, deaths, assists = (sum(line[i] for line in lines) for i in range(3))
    return {
        "form": tuple("W" if m["won"] else "L" for m in day),
        "vp": sum(gained) if gained else None,
        "kda": round((kills + assists) / max(deaths, 1), 1) if lines else None,
    }


def encounters_for(roster_ids, me, matches, display=None, icons=None):
    """Players of this roster seen in my previous matches (newest first).

    ``matches`` keeps its positions: a ``None`` (not recorded yet) still counts
    as a match ago, so the numbering stays right.
    """
    display, icons = display or {}, icons or {}
    found = {}
    for ago, details in enumerate(matches, 1):
        if not details:
            continue
        players = {p["player_id"]: p for p in details.get("players", [])}
        mine = players.get(me, {}).get("team")
        if not mine:
            continue
        winner = details.get("winning_team")
        for pid in roster_ids:
            if pid == me or pid not in players:
                continue
            row = players[pid]
            found.setdefault(pid, []).append(
                Encounter(
                    matches_ago=ago,
                    hero=display.get(row.get("hero"), row.get("hero")),
                    side="with" if row.get("team") == mine else "against",
                    won=(winner == mine) if winner else None,
                    icon=icons.get(row.get("hero")),
                    kda=_whole_kda(row.get("kda")),
                )
            )
    return {pid: tuple(rows) for pid, rows in found.items()}


def _whole_kda(kda):
    """(5, 2, 8), or None unless all three are numbers."""
    numbers = tuple(kda or ())
    if len(numbers) != 3 or not all(isinstance(n, (int, float)) for n in numbers):
        return None
    return tuple(int(n) for n in numbers)


def shown_main(main, display, icons=None):
    """A main with its hero under the game's name; an internal name is never shown."""
    if not main:
        return None
    hero = main.get("hero")
    return {
        "hero": (display or {}).get(hero),
        "role": main.get("role"),
        "icon": (icons or {}).get(hero),
    }


def _strength_order(state):
    """Strongest first: points, then past peak; the unplaced go last.

    Taken once per player, when their profile lands, so that later data never moves a row.
    """

    def key(sid):
        profile = state.profiles.get(state.ids[sid]["pid"]) or {}
        points = rank_points(profile)
        step = rank_step((profile.get("peak") or {}).get("rank"))
        return (points is None, -(points or 0), -(-1 if step is None else step))

    return {k: place for place, k in enumerate(sorted(state.seats, key=key))}


def _team(state, team):
    seats = [
        (k, s)
        for k, s in state.seats.items()
        if s["team"] == team and s["reading"] and not s["gone"]
    ]
    seats.sort(key=lambda item: item[1]["order"])
    if state.strength:
        seats.sort(key=lambda item: state.strength.get(item[0], len(state.seats)))
    return seats


# Two players of a team are a party when Pred.gg counts many matches of theirs on one
# side (a random teammate comes back 5 to 7 times in a thousand matches: measured
# 2026-11-20), or, for a pair new to each other, when they were on one side in this
# many of their last ten matches each (two can be the same randoms queued again).
PARTY_GAMES, PARTY_SHARE, PARTY_SHARED = 10, 0.02, 3


def shown_stats(stats, display):
    """cards.play_stats with Pred.gg's internal hero names ("Wood") as the game shows
    them ("Mourn"): the scoreboard's hero and the card speak the game's names."""
    if not stats or not display:
        return stats
    from .cards import hero_key

    heroes = {}
    for entry in (stats.get("heroes") or {}).values():
        name = display.get(entry.get("name"), entry.get("name"))
        heroes[hero_key(name)] = dict(entry, name=name)
    played = [(display.get(h, h), t) for h, t in stats.get("played") or ()]
    return dict(stats, heroes=heroes, played=played)


def parties(members):
    """{key: {"team", "pid", "recent": {match ID: side}, "mates": {pid: (played, won)},
    "total"}} -> {key: {"size", "group", "with": keys, "games", "won"}} for those in a
    party with others of their team. ``games`` / ``won``: the most matches they played
    together by Pred.gg's count (``won`` None when only their last ten tell). Groups
    are numbered per team from 0, in key order."""
    keys = sorted(members)
    root = {k: k for k in keys}

    def find(k):
        while root[k] != k:
            root[k] = root[root[k]]
            k = root[k]
        return k

    def together(a, b):
        """(games, won or None) of a and b on one side, or None when that is chance."""
        one, other = members[a], members[b]
        counts = [
            x["mates"][y["pid"]]
            for x, y in ((one, other), (other, one))
            if y.get("pid") and y["pid"] in (x.get("mates") or {})
        ]
        if counts:
            played, won = max(counts)
            least = max(
                PARTY_GAMES, PARTY_SHARE * max(one.get("total") or 0, other.get("total") or 0)
            )
            if played >= least:
                return played, won
        seen_a, seen_b = one.get("recent") or {}, other.get("recent") or {}
        shared = sum(1 for match, side in seen_a.items() if seen_b.get(match) == side)
        return (shared, None) if shared >= PARTY_SHARED else None

    best = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            if members[a]["team"] != members[b]["team"]:
                continue
            if members[a]["pid"] and members[a]["pid"] == members[b]["pid"]:
                continue  # one player in two seats (a misread): no party with oneself
            found = together(a, b)
            if found:
                root[find(a)] = find(b)
                for k in (a, b):
                    if k not in best or found[0] > best[k][0]:
                        best[k] = found
    groups = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    out, numbers = {}, {}
    for group in sorted((g for g in groups.values() if len(g) > 1), key=lambda g: g[0]):
        team = members[group[0]]["team"]
        number = numbers.get(team, 0)
        numbers[team] = number + 1
        for k in group:
            out[k] = {
                "size": len(group),
                "group": number,
                "with": tuple(o for o in group if o != k),
                "games": best[k][0],
                "won": best[k][1],
            }
    return out


CONSOLE_PLAYER, UNKNOWN_PLAYER = "CONSOLE PLAYER", "UNKNOWN PLAYER"
LIKELY_CONSOLE = "CONSOLE PLAYER?"  # a clean reading Pred.gg does not know


def _why(state, seat, ident):
    """Why a player has no data (view state "error"): see PlayerView.why."""
    if state.auth == "login":
        return "login"  # nothing can be asked: whatever else is true comes after a sign-in
    if ident["pid"] and ident["pid"] in state.missing:
        return "noprofile"  # Pred.gg answered, with no profile for them
    if ident["pid"] or ident["failed"]:
        return "offline"  # found but the profile would not come, or the search never did
    if lookup.gap_key(seat["reading"]) in seat["sure"]:
        return "console"
    return "misread"


def _shown_name(state, seat, ident, profile, ready, why=None, mine=False):
    """(name, tag): Pred.gg's name for a PC player; for one whose name stays hidden, what
    they are and the overlay's own tag for them.

    Console players' names are hidden on Pred.gg, and so in the panel too: they read
    CONSOLE PLAYER, a player Pred.gg does not know UNKNOWN PLAYER, never as a name.
    The tag ("SILENT HAWK", aliases.label) comes from the name the screen shows, so
    the same player carries the same tag match after match on this PC; the panel
    draws it as a tag, not as a name.
    """
    if ready == "loading":
        if mine and state.my_name:
            return state.my_name, None  # the log names me: nothing to wait for
        return None, None  # "..." for the moment: no name before it is known whether to show it
    if why in ("offline", "noprofile", "login") and ident["name"]:
        # A PC player Pred.gg found, whose profile did not come; me, named by the log.
        return ident["name"], None
    if profile and not profile.get("console"):
        name = (
            ident["name"]
            or profile.get("name")
            or (state.my_name if ident["pid"] == state.me else None)
        )
        if name:
            return name, None
    tag = aliases.label(state.salt, lookup.name_key(seat["reading"]))
    if profile and profile.get("console"):
        return CONSOLE_PLAYER, tag
    return (LIKELY_CONSOLE if why == "console" else UNKNOWN_PLAYER), tag


def view(state):
    """The immutable snapshot the window draws."""
    if not state.game_running:
        status = NEEDS_LOGIN if state.auth == "login" else WAITING
    elif state.auth == "login":
        status = NEEDS_LOGIN
    elif state.offline:
        status = OFFLINE
    else:
        status = state.note or STATUS.get(state.phase, state.phase)
    health = "login" if state.auth == "login" else "offline" if state.offline else "ok"
    failed = _gave_up(state, ("profiles", state.epoch))

    def player(key, seat):
        ident = state.ids[key]
        pid = ident["pid"]
        profile = state.profiles.get(pid) if pid else None
        if profile and profile.get("stats"):  # heroes by the game's names
            profile = dict(profile, stats=shown_stats(profile["stats"], state.display))
        if profile is not None:
            ready = "ready"
        elif ((ident["searched"] is True or ident["failed"]) and not pid) or (
            pid and (failed or pid in state.missing)
        ):
            # Not found on Pred.gg, or its profile would not come; and it stays so while
            # a lookup that never got its answer is tried again.
            ready = "error"
        elif state.auth == "login":
            # Not signed in: nothing is asked, so nothing is on its way. The row says so
            # (the status plate does only before the first name is read): "..." would
            # promise data that cannot come (seen live on 2026-11-24).
            ready = "error"
        else:
            ready = "loading"
        mine = (pid is not None and pid == state.me) or _mine(state, seat)
        why = _why(state, seat, ident) if ready == "error" else None
        name, tag = _shown_name(state, seat, ident, profile, ready, why, mine)
        # What we know of them from earlier matches waits for their name: a row
        # comes together, never facts under "..."
        met = state.encounters.get(pid, ()) if pid and (mine or ready != "loading") else ()
        return PlayerView(
            player_id=key,  # stable for the match, while the Pred.gg ID may arrive later
            name=name,
            tag=tag,
            why=why,
            hero={h.upper(): h for h in state.display.values()}.get(seat["hero"], seat["hero"]),
            read_as=seat["reading"],
            team=seat["team"],
            is_me=mine,
            state=ready,
            profile=profile,
            peak=(profile or {}).get("peak"),
            seasons=(profile or {}).get("seasons"),
            main=shown_main((profile or {}).get("main"), state.display, state.icons),
            encounters=met,
            last_played=(profile or {}).get("last_played"),
            today=state.today if mine else None,
        )

    allies = tuple(player(k, s) for k, s in _team(state, "ally"))
    enemies = tuple(player(k, s) for k, s in _team(state, "enemy"))
    members = {}
    for key, seat in state.seats.items():
        pid = state.ids[key]["pid"]
        stats = (state.profiles.get(pid) or {}).get("stats") if pid else None
        if stats and not seat["gone"]:
            members[key] = {
                "team": seat["team"],
                "pid": pid,
                "recent": dict(stats.get("recent") or ()),
                "mates": stats.get("mates") or {},
                "total": stats.get("total") or 0,
            }
    found = parties(members)
    if found:
        shown = {p.player_id: (p.tag or p.name or "?") for p in allies + enemies}

        def grouped(p):
            party = found.get(p.player_id)
            if party is None:
                return p
            names = tuple(shown.get(k, "?") for k in party["with"])
            return dataclasses.replace(p, party=dict(party, **{"with": names}))

        allies = tuple(grouped(p) for p in allies)
        enemies = tuple(grouped(p) for p in enemies)
    size = max(len(allies), len(enemies))
    return ViewModel(
        version=state.version,
        phase=state.phase if state.game_running else "closed",
        phase_at=state.phase_at,
        started_at=state.started_at,
        ended_at=state.ended_at,
        status=status,
        status_note=health != "ok" or bool(state.note),
        health=health,
        # The screens read are 5v5 ones, whatever the seats count: five a side while a
        # name is still unread, and with a sixth seat a misread has opened.
        mode=roster_mode(2 * TEAM) if size else None,
        mode_name=state.mode_name,
        heroes=tuple(sorted(set(state.display.values()))),
        my_name=state.my_name,
        art=state.art,
        match_id=f"{state.epoch}",
        allies=allies,
        enemies=enemies,
    )
