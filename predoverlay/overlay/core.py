"""Pure match logic: ``reduce(state, event, now) -> commands``; ``view(state)`` -> ViewModel.

Events (tuples, first item is the kind); ``None`` as the payload means the
lookup failed and is retried with a backoff:
    ("log", phase_event)                 from phases.parse / LogFollower
    ("me", player_id | None[, error])    the account the stored tokens belong to
    ("relogin", player_id | None[, error])  tokens re-issued for the Steam account signed in
    ("display", {internal: display} | None)  hero display names
    ("instance", current | None[, error])  GameClient.current_game()
    ("identities", epoch, {pid: name} | None)
    ("profiles", epoch, {pid: profile} | None)  official rank, level, account age
    ("peaks", epoch, {pid: {"peak", "seasons"}} | None)  past seasons (Pred.gg)
    ("card", pid, card | None, error)    card None = no Pred.gg profile
    ("recent", epoch, [details | None, ...] | None)  my previous matches, newest
                                         first; None marks one not recorded yet
    ("queues", {region: {queue: (players, parties)}} | None)  matchmaking, in the menu
    ("set_region", region)               the user picked another matchmaking region
    ("sign_in",)                         the user asked to sign in through Steam again
    ("tick",)

Commands returned for the I/O lanes:
    ("poll_instance",)  ("me",)  ("relogin",)  ("display",)  ("identities", epoch, ids)
    ("profiles", epoch, ids)  ("peaks", epoch, ids)
    ("card", pid)  ("recent", epoch, match_id)
    ("refresh_tokens",)

The epoch identifies one match roster; results tagged with an older epoch are
dropped. A roster is accepted only when it has 4, 6 or 10 players (2v2, 3v3,
5v5), contains my UUID and the same list was read twice in a row. A list that
grows later was partial and is read again. Against bots the instance lists only
the humans, so a PvE match (known from the loading line) is never shown.

``me`` is who plays, from the game log once it names a user; ``token_account``
is whom the stored tokens belong to. A player may switch Steam accounts, so
when the two differ the tokens are re-issued via Steam.

Cards (win rate, mains, form) come only from Pred.gg with a signed-in account
(``stats_source == "predgg"``); without one there are none, and a player is
ready once the official profile is in.
"""

from dataclasses import dataclass, field

from ..modes import experience_label, is_pve, roster_mode
from .model import Encounter, PlayerView, ViewModel
from .rows import rank_points, rank_step

SUPPORTED = (4, 6, 10)  # players: 2v2 Crucible, 3v3, 5v5

POLL_FAST = 2.0  # seconds between game-instance reads until the roster is accepted
POLL_SLOW = 10.0  # afterwards, to notice a dodge or a new match
CARD_TTL = 30 * 60
CARD_RETRY = (5, 15, 60, 180)  # backoff for every failed lookup, not only cards
REFRESH_EVERY = 20 * 60  # renew tokens in the menu, never mid-match
QUEUES_EVERY = 30.0  # matchmaking counts, read only while in the menu
QUEUES_STALE = 90.0  # older counts are not shown
ACTIVE = ("accepted", "draft", "loading")
PER_EPOCH = ("identities", "profiles", "recent", "peaks")
# The log does not always say when a lobby or match is gone (a declined accept,
# a crash); the game backend does. Draft and match have an instance from the
# first second; after an accept it takes up to ~20 s to appear.
MISSES_TO_MENU = 3
ACCEPT_GRACE = 45.0
MATCH_GRACE = 60.0
OFFLINE = "Omeda offline"
NEEDS_LOGIN = "sign-in needed"
STATUS = {
    "menu": "waiting for a match",
    "accepted": "match found",
    "draft": "draft",
    "loading": "match",
}


@dataclass
class State:
    me: str | None = None
    token_account: str | None = None
    phase: str = "menu"
    phase_at: float = 0.0
    epoch: int = 0
    match_id: str | None = None
    roster: tuple = ()
    candidate: tuple = ()
    names: dict = field(default_factory=dict)
    profiles: dict = field(default_factory=dict)  # pid -> official public profile
    peaks: dict = field(default_factory=dict)  # pid -> {"peak", "seasons"} (Pred.gg)
    strength: dict = field(default_factory=dict)  # pid -> place in its team, set once
    stats_source: str = "omeda"  # "predgg" when a signed-in Pred.gg account gives cards
    region: str = "EUROPE"  # matchmaking region whose queues the menu shows
    queues: dict = field(default_factory=dict)  # {region: {queue: (players, parties)}}
    queues_at: float = 0.0
    next_queues: float = 0.0
    cards: dict = field(default_factory=dict)  # pid -> (fetched_at, card | None)
    card_failures: dict = field(default_factory=dict)  # pid -> (count, retry_at)
    encounters: dict = field(default_factory=dict)  # pid -> tuple[Encounter]
    display: dict = field(default_factory=dict)
    inflight: set = field(default_factory=set)
    failures: dict = field(default_factory=dict)  # lookup key -> (count, retry_at)
    loaded: set = field(default_factory=set)  # per-epoch lookups done (PER_EPOCH)
    joined: bool = False  # the client is on the match map (loading phase)
    started_at: float = 0.0  # when the match itself started; 0 while on the loading screen
    misses: int = 0  # game-instance reads in a row that found no match
    missing_since: float = 0.0
    next_poll: float = 0.0
    next_refresh: float = 0.0
    note: str | None = None  # overrides the phase status (unsupported mode, not my match)
    mode_name: str | None = None  # "OVERCLOCKED", from the map URL once the match loads
    signed_out: bool = False  # no session, and Steam could not give one
    offline: bool = False  # the last game-instance read failed
    version: int = 0


def _new_epoch(state, match_id=None):
    state.epoch += 1
    state.match_id = match_id
    state.roster = state.candidate = ()
    state.names, state.encounters = {}, {}
    state.profiles = {}
    state.peaks = {}
    state.strength = {}
    state.loaded = set()
    # Per-epoch lookups are moot now; a read already in flight stays marked so
    # that it is not sent twice.
    state.inflight = {key for key in state.inflight if key[0] not in PER_EPOCH}
    state.failures = {key: v for key, v in state.failures.items() if len(key) == 1}
    state.note = None
    state.version += 1


def _to_menu(state, at):
    state.phase, state.phase_at, state.misses = "menu", at, 0
    state.mode_name = None
    _new_epoch(state)


def _backoff(failures, key, now):
    count = failures.get(key, (0, 0.0))[0] + 1
    failures[key] = (count, now + CARD_RETRY[min(count, len(CARD_RETRY)) - 1])


def _due(state, key, now):
    return key not in state.inflight and now >= state.failures.get(key, (0, 0.0))[1]


def _lookups(state, now):
    """Own UUID, hero names and the roster's names and history, until they arrive."""
    wanted = []
    if state.token_account is None:
        wanted.append((("me",), ("me",)))
    elif state.me and state.me != state.token_account and state.phase in ACTIVE:
        # Only while a match is on: a user ID replayed from an old log must not
        # trigger Steam logins from the menu.
        wanted.append((("relogin",), ("relogin",)))
    if not state.display:
        wanted.append((("display",), ("display",)))
    if state.roster:
        lookups = (
            ("identities", state.roster),
            ("profiles", state.roster),
            ("peaks", state.roster),
            ("recent", state.match_id),
        )
        for kind, payload in lookups:
            if kind not in state.loaded:
                wanted.append(((kind, state.epoch), (kind, state.epoch, payload)))
    commands = []
    for key, command in wanted:
        if _due(state, key, now):
            state.inflight.add(key)
            commands.append(command)
    return commands


def _card_commands(state, now):
    if state.stats_source != "predgg":
        return []
    commands = []
    for pid in state.roster:
        cached = state.cards.get(pid)
        if cached and now - cached[0] < CARD_TTL:
            continue
        _, retry_at = state.card_failures.get(pid, (0, 0.0))
        if ("card", pid) in state.inflight or now < retry_at:
            continue
        state.inflight.add(("card", pid))
        commands.append(("card", pid))
    return commands


def reduce(state, event, now):
    """Apply one event; return the commands it triggers."""
    kind = event[0]
    commands = []
    if kind == "log":
        log = event[1]
        phase = log["phase"]
        if log.get("user_id") and log["user_id"] != state.me:
            if state.me:  # another account: cards count history from the old one's side
                state.cards, state.card_failures = {}, {}
            state.me = log["user_id"]  # the log says who is playing
            state.version += 1
        if phase == "joined" and log.get("map") != "Draft" and state.phase in ("accepted", "draft"):
            # A reconnect joins the match map with no map-load line: it is the match.
            state.phase, state.phase_at = "loading", log.get("at_ts", now)
            state.joined, state.started_at = True, 0.0
            state.next_poll = now
            state.version += 1
        elif phase in ("joined", "presence"):
            # Markers inside the loading phase: the map is in, then the match starts.
            if state.phase == "loading" and not state.started_at and log.get("map") != "Draft":
                if phase == "joined":
                    state.joined = True
                elif state.joined:
                    state.started_at = log.get("at_ts", now)
                    state.version += 1
        elif phase in ("finished", "menu"):
            _to_menu(state, log.get("at_ts", now))
        else:
            if phase == "accepted" and state.phase != "accepted":
                _new_epoch(state)
                state.mode_name = None
            if phase == "loading" and log.get("experience"):
                state.mode_name = experience_label(log["experience"])
                if is_pve(state.mode_name) and state.roster:
                    state.roster, state.candidate = (), ()  # bots are not in the list
                    state.note = "mode not supported"
            if phase != state.phase:
                state.joined, state.started_at = False, 0.0
            state.phase, state.phase_at = phase, log.get("at_ts", now)
            state.misses = 0
            state.next_poll = now
            state.version += 1
    elif kind in ("me", "relogin"):
        state.inflight.discard((kind,))
        account = event[1]
        if account is None:
            _backoff(state.failures, (kind,), now)
            error = event[2] if len(event) > 2 else None
            if _auth_error(error) and not state.signed_out:
                state.signed_out = True
                state.version += 1
        else:
            state.signed_out = False
            state.token_account = account
            state.me = state.me or account
            if kind == "relogin" and account != state.me:
                _backoff(state.failures, ("relogin",), now)  # Steam is on another account
            else:
                state.failures.pop((kind,), None)
            state.version += 1
        commands += _lookups(state, now)
    elif kind == "sign_in":
        if ("relogin",) not in state.inflight:
            state.inflight.add(("relogin",))
            state.failures.pop(("relogin",), None)
            state.failures.pop(("me",), None)
            commands.append(("relogin",))
    elif kind == "set_region":
        if event[1] != state.region:
            state.region = event[1]
            state.version += 1
    elif kind == "queues":
        state.inflight.discard(("queues",))
        if event[1] is not None:
            state.queues, state.queues_at = event[1], now
            state.version += 1
    elif kind == "display":
        state.inflight.discard(("display",))
        if not event[1]:  # an empty catalog would be asked for again on every tick
            _backoff(state.failures, ("display",), now)
        else:
            state.display = dict(event[1])
            state.version += 1
    elif kind == "instance":
        state.inflight.discard(("instance",))
        commands += _on_instance(state, event[1], now, event[2] if len(event) > 2 else None)
    elif kind in ("identities", "profiles", "peaks"):
        _, epoch, found = event
        state.inflight.discard((kind, epoch))
        if epoch == state.epoch:
            if found is None:
                _backoff(state.failures, (kind, epoch), now)
                gave_up = state.failures[(kind, epoch)][0] >= len(CARD_RETRY)
                if kind == "peaks" and gave_up:
                    state.loaded.add(kind)  # optional: the panel works without it
            else:
                target = {"identities": state.names, "profiles": state.profiles}
                target.get(kind, state.peaks).update(found)
                state.loaded.add(kind)
                if kind == "profiles" and not state.strength:
                    state.strength = _strength_order(state)
                state.version += 1
    elif kind == "card":
        _, pid, card, error = event
        state.inflight.discard(("card", pid))
        if error:
            _backoff(state.card_failures, pid, now)
        else:
            state.cards[pid] = (now, card)
            state.card_failures.pop(pid, None)
        state.version += 1
    elif kind == "recent":
        _, epoch, matches = event
        key = ("recent", epoch)
        state.inflight.discard(key)
        if epoch == state.epoch:
            if matches is None:
                _backoff(state.failures, key, now)
            else:
                state.encounters = encounters_for(state.roster, state.me, matches, state.display)
                state.version += 1
                # A match that just ended is not recorded for a minute or so: ask again.
                if None in matches and state.failures.get(key, (0, 0.0))[0] < len(CARD_RETRY):
                    _backoff(state.failures, key, now)
                else:
                    state.loaded.add("recent")
    elif kind == "tick":
        if state.phase in ACTIVE and now >= state.next_poll and ("instance",) not in state.inflight:
            state.inflight.add(("instance",))
            state.next_poll = now + (POLL_SLOW if state.roster else POLL_FAST)
            commands.append(("poll_instance",))
        if state.phase == "menu" and now >= state.next_queues and ("queues",) not in state.inflight:
            state.inflight.add(("queues",))
            state.next_queues = now + QUEUES_EVERY
            commands.append(("queues",))
        if state.phase == "menu" and now >= state.next_refresh:
            state.next_refresh = now + REFRESH_EVERY
            commands.append(("refresh_tokens",))
        commands += _lookups(state, now)
        commands += _card_commands(state, now)
    return commands


def _auth_error(error):
    """Lane errors are ``"ExceptionName: message"``; sign-in problems are AuthenticationError."""
    return bool(error) and str(error).startswith("AuthenticationError")


def _on_instance(state, current, now, error=None):
    if current is None:
        if _auth_error(error):
            state.signed_out = True
        else:
            state.offline = True
        state.version += 1
        return []
    if state.offline or state.signed_out:  # the backend answers with a valid session
        state.offline = state.signed_out = False
        state.version += 1
    if not current.get("in_game"):
        # One empty read is not proof (a backend hiccup): keep the roster, read
        # again soon, and leave only when the miss repeats.
        if state.misses == 0:
            state.missing_since = now
        state.misses += 1
        state.next_poll = min(state.next_poll, now + POLL_FAST)
        if state.phase == "accepted":
            gone = now - state.phase_at > ACCEPT_GRACE
        elif state.phase == "loading":  # a live match: be slow to give it up
            gone = state.misses >= MISSES_TO_MENU and now - state.missing_since >= MATCH_GRACE
        else:
            gone = state.misses >= MISSES_TO_MENU
        if gone:
            _to_menu(state, now)  # dodged lobby, crash, or the match is over
        return []
    state.misses = 0
    if current["match_id"] != state.match_id:
        _new_epoch(state, current["match_id"])
    if state.roster:
        if len(current["player_ids"]) <= len(state.roster):
            return []
        _new_epoch(state, current["match_id"])  # the list grew: it was partial
    ids = tuple(current["player_ids"])
    if ids != state.candidate:
        state.candidate = ids
        state.next_poll = now + POLL_FAST
        return []
    if len(ids) not in SUPPORTED or is_pve(state.mode_name):
        note = "mode not supported"
    elif state.me is None:
        note = None  # own UUID still loading; teams cannot be told apart yet
    elif state.me not in ids:
        note = "not your match"
    else:
        note = False
    if note is not False:
        if note != state.note:
            state.note = note
            state.version += 1
        return []
    state.roster = ids
    state.note = None
    state.version += 1
    return [*_lookups(state, now), *_card_commands(state, now)]


def encounters_for(roster, me, matches, display=None):
    """Players of this roster seen in my previous matches (newest first).

    ``matches`` keeps its positions: a ``None`` (not recorded yet) still counts
    as a match ago, so the numbering stays right.
    """
    display = display or {}
    found = {}
    for ago, details in enumerate(matches, 1):
        if not details:
            continue
        players = {p["player_id"]: p for p in details.get("players", [])}
        mine = players.get(me, {}).get("team")
        if not mine:
            continue
        winner = details.get("winning_team")
        for pid in roster:
            if pid == me or pid not in players:
                continue
            row = players[pid]
            found.setdefault(pid, []).append(
                Encounter(
                    matches_ago=ago,
                    hero=display.get(row.get("hero"), row.get("hero")),
                    side="with" if row.get("team") == mine else "against",
                    won=(winner == mine) if winner else None,
                )
            )
    return {pid: tuple(rows) for pid, rows in found.items()}


def teams(roster, me):
    """Split the API order into (allies, enemies): two blocks, mine holds my UUID."""
    half = len(roster) // 2
    first, second = roster[:half], roster[half:]
    return (second, first) if me in second else (first, second)


def _strength_order(state):
    """Strongest first: points, then past peak, then level; the unplaced go last.

    Taken once, when profiles land, so that later data never moves a row.
    """

    def key(pid):
        profile = state.profiles.get(pid) or {}
        peak = (state.peaks.get(pid) or {}).get("peak") or {}
        points = rank_points(profile)
        step = rank_step(peak.get("rank"))
        return (
            points is None,
            -(points or 0),
            -(-1 if step is None else step),
            -(profile.get("level") or 0),
        )

    return {pid: place for place, pid in enumerate(sorted(state.roster, key=key))}


def view(state):
    """The immutable snapshot the window draws."""
    allies, enemies = teams(state.roster, state.me) if state.roster else ((), ())
    if state.strength:
        place = state.strength.get
        allies = sorted(allies, key=lambda pid: place(pid, len(state.roster)))
        enemies = sorted(enemies, key=lambda pid: place(pid, len(state.roster)))

    key = ("profiles", state.epoch)
    if "profiles" in state.loaded:
        official = "ready"
    elif state.failures.get(key, (0, 0.0))[0] >= len(CARD_RETRY):
        official = "error"
    else:
        official = "loading"

    def player(pid, team):
        cached = state.cards.get(pid)
        card = cached[1] if cached else None
        if state.stats_source != "predgg":
            stats_state = "off"
        elif cached:
            stats_state = "ready" if card else "missing"
        else:
            failed = state.card_failures.get(pid, (0, 0.0))[0] >= len(CARD_RETRY)
            stats_state = "error" if failed else "loading"
        return PlayerView(
            player_id=pid,
            name=state.names.get(pid),
            team=team,
            is_me=pid == state.me,
            state=official,
            card=card,
            stats_state=stats_state,
            profile=state.profiles.get(pid),
            peak=(state.peaks.get(pid) or {}).get("peak"),
            seasons=(state.peaks.get(pid) or {}).get("seasons"),
            encounters=state.encounters.get(pid, ()),
        )

    return ViewModel(
        version=state.version,
        phase=state.phase,
        phase_at=state.phase_at,
        started_at=state.started_at,
        status=(
            NEEDS_LOGIN
            if state.signed_out
            else OFFLINE
            if state.offline
            else state.note or STATUS.get(state.phase, state.phase)
        ),
        status_note=bool(state.signed_out or state.offline or state.note),
        health="login" if state.signed_out else "offline" if state.offline else "ok",
        mode=roster_mode(len(state.roster)),
        mode_name=state.mode_name,
        stats_source=state.stats_source,
        heroes=tuple(sorted(set(state.display.values()))),
        region=state.region,
        queues=state.queues.get(state.region, {}),
        queues_at=state.queues_at,
        match_id=state.match_id,
        allies=tuple(player(pid, "ally") for pid in allies),
        enemies=tuple(player(pid, "enemy") for pid in enemies),
    )
