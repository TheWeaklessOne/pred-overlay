"""The players of this match as the screens show them: one seat per player, with what each
screen read of their name. Who the name is on Pred.gg is identity.py's part.

Every screen's reading goes through ``place``: the draft (role plates, then the pick
screen's rows), the loading screen and the TAB board. An entry joins the seat whose name
it matches (``scoreboard.assign``); an entry with no name to match (a console player, a
misread) joins the seat of its role when the screen tells roles (the loading screen's
columns, the board's rows); else it opens a seat while the team has room.

A seat is a dict:
    team      "ally" | "enemy"
    order     where the panel lists it (the pick screen's row; loading and board after)
    evidence  {group: [(version, text), ...]}: "draft" (plates and pick rows), "loading",
              "board"; version 0 is the overlay's model, the others Windows OCR's
    primary   the group whose agreed text is the seat's reading (the first that agreed)
    reading   that agreed text, or ""
    spelled   {group: agreed text} of the other groups: more spellings to search by
    sure      gap keys the model read with no doubt (two equal versions)
    role      the role the loading screen or the board gave, or a plate's
    role_from "plate" | "loading" | "board" | None
    hero      the hero shown with it (the pick line, or the board), or None
    gone      a misread: the screen no longer shows it
    missed    full pick-screen readings in a row without it
"""

from . import identity, lookup, roster
from .scoreboard import assign, similarity

TEAM = 5  # players a team
MAX_SEATS = 8  # seats one team may open on the draft: a misread or two on top of five
AGREE = 2  # versions (image treatments) that must agree on a name before it is looked up
REPEATS = 3  # or: readings of one version that must give the same name
KEEP_TEXTS = 36  # texts kept per group (several versions per reading)
GROUPS = ("draft", "loading", "board")  # a seat's reading comes from the first that agrees
# What the game writes where a name will be before the player has loaded: no name.
PLACEHOLDERS = tuple(lookup.name_key(t) for t in ("LOADING IN PLAYER", "PICKING", "WAITING"))


def new(team, order):
    return {
        "team": team,
        "order": order,
        "evidence": {},
        "primary": None,
        "reading": "",
        "spelled": {},
        "sure": set(),
        "role": None,
        "role_from": None,
        "hero": None,
        "gone": False,
        "missed": 0,
    }


def versions(texts):
    """A reading's texts as [(version, text), ...], empty ones and placeholders left out."""
    return [
        (i, t)
        for i, t in enumerate(texts or ())
        if lookup.name_key(t) and not lookup.name_key(t).startswith(PLACEHOLDERS)
    ]


def agreed(texts):
    """The reading at least AGREE different versions share, else "".

    ``texts`` is [(version, text), ...] over every reading. A static screen read again
    gives the same misread in the same version every time: only versions that process
    the image differently count as independent evidence. Version 0 is the overlay's
    model (roster.Reader), whose spelling wins a tie.
    """
    groups = {}
    for version, text in texts:
        key = lookup.name_key(text)
        if key:
            group = groups.setdefault(
                key, {"versions": set(), "count": 0, "texts": [], "model": set()}
            )
            group["versions"].add(version)
            group["count"] += 1
            group["texts"].append(text)
            if version == 0:
                group["model"].add(text)
    if not groups:
        return ""
    key = max(
        groups,
        key=lambda k: (
            len(groups[k]["versions"]),
            groups[k]["count"],
            0 in groups[k]["versions"],
            len(k),
        ),
    )
    group = groups[key]
    # Weaker: one version, the same in several readings. A misread repeats too, so it
    # must be long enough not to exact-match a stranger on Pred.gg by chance.
    weak = group["count"] >= REPEATS and len(key) >= 4
    if len(group["versions"]) < AGREE and not weak:
        return ""
    # Only the model writes GAP: where it saw an emoji or a symbol, the spellings without
    # one (Windows OCR drops them) name another player, or nobody. Then the model's own
    # spelling: its slips ("l" for "I") are what the search spellings undo, OCR's ("-"
    # for "_", "0" for "o", "1" for "l", spaces) mostly are not (real frames, review of
    # 2026-11-19). Then: OCR puts spaces into a name more often than it drops them, so
    # the fewest spaces, then the most common.
    return min(
        set(group["texts"]),
        key=lambda t: (
            lookup.GAP not in t,
            t not in group["model"],
            t.count(" "),
            -group["texts"].count(t),
            t,
        ),
    )


def rough(seat):
    """What a seat's readings say so far, agreed or not: to tell which seat a row is."""
    if seat["reading"]:
        return seat["reading"]
    texts = [t for group in seat["evidence"].values() for _, t in group]
    return roster.best(texts)


def all_texts(seat):
    return [t for group in GROUPS for _, t in seat["evidence"].get(group, ())]


def take(state, sid, group, texts):
    """One reading's versions of a seat's name from ``group``; True when the seat's
    reading changed (whoever it was found to be is then unknown again) or another
    screen's spelling gives the search a new try."""
    seat, ident = state.seats[sid], state.ids[sid]
    if len(texts or ()) == 2 and texts[0] == texts[1] and lookup.name_key(texts[0]):
        seat["sure"].add(lookup.gap_key(texts[0]))  # roster's sign of a sure model reading
    kept = seat["evidence"].setdefault(group, [])
    kept.extend(versions(texts))
    del kept[:-KEEP_TEXTS]
    said = agreed(kept)
    if not said:
        return False
    if seat["primary"] not in (None, group):
        # Another screen's own spelling: searched as well when the name is not found.
        if lookup.name_key(said) and len(lookup.name_key(said)) >= 2:
            new_text = seat["spelled"].get(group) != said
            seat["spelled"][group] = said
            if new_text and lookup.attempt_key(said) != lookup.attempt_key(seat["reading"]):
                return identity.another_spelling(ident, said)
        return False
    seat["primary"] = group
    # A found (or asked) player stays while the reading only changes spelling ("NAME_1" to
    # OCR's "NAME-1"); a GAP that appears or goes is another name, searched again, and so
    # is the model's spelling when it is not the found name's ("Name_1" over "Name-1").
    model = any(version == 0 and text == said for version, text in kept)
    same = (
        (ident["pid"] or ident["searched"] == "asked")
        and lookup.gap_key(said) == lookup.gap_key(seat["reading"])
        and not (model and ident["name"] and not lookup.same_name(said, ident["name"]))
    )
    if same or lookup.attempt_key(said) == lookup.attempt_key(seat["reading"]):
        return False
    seat["reading"] = said
    identity.forget(ident)
    identity.name(state, seat, ident)
    return True


def live(state, team):
    return {sid: s for sid, s in state.seats.items() if s["team"] == team and not s["gone"]}


def open_seat(state, team, order):
    sid = f"{team[0]}{sum(1 for s in state.seats.values() if s['team'] == team)}"
    state.seats[sid] = new(team, order)
    state.ids[sid] = identity.new()
    return sid


ROLE_BY_NAME = 0.8  # a name match this close lets a screen's role overwrite a seat's


def place(state, group, team, entries, *, cap=TEAM, by_role=False, whole=False):
    """Put one screen reading's entries on this team's seats.

    ``entries``: {key: {"texts": [...], "role": role or None, "order": n}}. Each entry
    joins the seat its name matches; with ``by_role``, an entry left over joins the seat
    this same screen gave the same role (a loading column's role may be mirrored, the
    board's is exact: they are never mixed); with ``whole`` (the screen lists the team
    row by row, as the board does), the one entry left of a full reading joins the one
    seat left of a full team; one still left opens a seat while the team has fewer than
    ``cap`` live seats. A role is taken from an entry placed by role or as the last one,
    or by a name at least ROLE_BY_NAME alike. Returns ({key: seat id}, changed)."""
    seats = live(state, team)
    known = {sid: rough(s) for sid, s in seats.items()}
    readings = {key: roster.best(e["texts"]) for key, e in entries.items() if versions(e["texts"])}
    placed = assign(readings, known) if known else {}
    trusted = {
        k for k, sid in placed.items() if similarity(readings[k], known[sid]) >= ROLE_BY_NAME
    }
    taken = set(placed.values())
    changed = False
    if by_role:
        for key in sorted(readings):
            role = entries[key].get("role")
            if key in placed or not role:
                continue
            same = [
                sid
                for sid, s in seats.items()
                if s["role"] == role and s["role_from"] == group and sid not in taken
            ]
            if len(same) == 1:
                placed[key] = same[0]
                taken.add(same[0])
                trusted.add(key)
    if whole and len(readings) == cap == len(seats):
        # Every row read and every other row placed: the row left is the player left,
        # however unlike the name the seat has (a short name the loading screen misread).
        # Which of the two readings the seat keeps is take()'s business, as for any row.
        rows_left = [key for key in sorted(readings) if key not in placed]
        seats_left = [sid for sid in seats if sid not in taken]
        if len(rows_left) == 1 and len(seats_left) == 1:
            placed[rows_left[0]] = seats_left[0]
            taken.add(seats_left[0])
            trusted.add(rows_left[0])
    for key in sorted(readings):
        if key in placed:
            continue
        if len(live(state, team)) >= cap:
            continue  # the team is full: a name no seat matches is a misread or noise
        sid = open_seat(state, team, entries[key]["order"])
        placed[key] = sid
        trusted.add(key)
        changed = True
    for key, sid in placed.items():
        entry, seat = entries[key], state.seats[sid]
        changed |= take(state, sid, group, entry["texts"])
        role = entry.get("role")
        exact = group in ("loading", "board") and key in trusted
        if role and (seat["role_from"] in (None, "plate") or exact):
            seat["role"], seat["role_from"] = role, "plate" if group == "draft" else group
    return placed, changed
