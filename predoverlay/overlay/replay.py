"""Replay a recorded match (recorder.py) through the core: the same events at the same
times give the same commands and views, so a match that went wrong can be rerun after a
fix, and checks say what the panel would have shown and when.

Two kinds of check. Some judge this build's core on what the recording gave it, whoever
recorded it (HARD): a reading with every name in it must seat every player, and the
searches stay few. The others also depend on when the window that recorded the match read
each screen, so they fail only for a match this same version recorded and are a note for
any other.

    python -m predoverlay.overlay.replay <match folder>   a summary and the checks
    python -m predoverlay.overlay.replay --all            every recorded match, checks only
"""

import dataclasses
import sys
from pathlib import Path

from .. import modes
from . import core, recorder

LOADING_NAMED = 15.0  # seconds after the loading screen began: every player has a seat
SEARCHES_MOST = 12  # searches a match (a name read later gets its own; spellings retried)
FULL = "a reading with every name seats every player"
HARD = ("searches", FULL)  # judged on any recording, see the module's docstring


def start_state(fields):
    """The core's State from a recording's snapshot (fields a newer State lacks are left
    out; those it added take their defaults)."""
    names = {f.name for f in dataclasses.fields(core.State)}
    return core.State(**{k: v for k, v in fields.items() if k in names})


def _walk(folder):
    fields, lines = recorder.read(folder)
    state = start_state(fields)
    for at, event, commands in lines:
        state.notes.clear()
        replayed = core.reduce(state, event, at)
        yield at, event, commands, replayed, core.view(state), state


def run(folder):
    """Yield (time, event, recorded commands, replayed commands, view) per event."""
    for at, event, commands, replayed, view, _ in _walk(folder):
        yield at, event, commands, replayed, view


def _given(state, event):
    """How many names one reading gave the core, when the core had no reason to turn it
    down: the loading screen's boxes with text, the TAB board's rows with text. Else 0."""
    if event[0] == "screen" and event[1] == "loading" and state.why is None:
        return core.names_read(event[3])[0]
    if (
        event[0] == "board_read"
        and event[2] == "tab"
        and event[1] == f"{state.epoch}"
        and state.game_running
        and not state.note
    ):
        return sum(1 for row in event[3].values() if row.get("text"))
    return 0


def measure(folder):
    """What the panel did in a recorded match, as numbers the checks judge."""
    from .. import __version__

    out = {
        "same_build": recorder.version(folder) == __version__,  # recorded by this version
        "differ": 0,  # events whose replayed commands differ from the recording's
        "searches": 0,
        "profiles": 0,
        "loading_at": None,  # when the loading phase began
        "started_at": None,
        "seated_after": None,  # seconds from the loading phase to ten seats with a name read
        "seats_at_start": None,  # (allies, enemies) when the match started
        "first_board": None,  # (allies, enemies) after the first board reading
        "size": None,  # players a team in this mode (5v5: 5)
        "bots": False,  # a mode against bots: only the humans have seats
        "given": 0,  # the most names one reading gave (a loading screen's, a board's)
        "seated": (0, 0),  # the seats once a reading had given every name
    }
    for at, event, commands, replayed, view, state in _walk(folder):
        if list(commands) != list(replayed):
            out["differ"] += 1
        out["given"] = max(out["given"], _given(state, event))
        if out["given"] >= 2 * core.TEAM:
            now = (len(view.allies), len(view.enemies))
            out["seated"] = max(out["seated"], now, key=min)
        if not core.gave_up(state, ("search", state.epoch)):
            # Not the retries of an outage (one every three minutes while it lasts).
            out["searches"] += sum(1 for c in replayed if c[0] == "search")
        out["profiles"] += sum(1 for c in replayed if c[0] == "profiles")
        if view.mode:
            out["size"] = _size(view.mode)
        if view.mode_name and modes.is_pve(view.mode_name):
            out["bots"] = True
        if view.phase == "loading" and out["loading_at"] is None:
            out["loading_at"] = at
        seats = (len(view.allies), len(view.enemies))
        full = out["size"] and seats == (out["size"], out["size"])
        if full and out["loading_at"] is not None and out["seated_after"] is None:
            out["seated_after"] = at - out["loading_at"]
        if view.started_at and out["started_at"] is None:
            out["started_at"] = at
            out["seats_at_start"] = seats
        if event[0] == "board_read" and out["first_board"] is None:
            out["first_board"] = seats
    return out


def _size(mode):
    try:
        return int(mode.split("v")[0])
    except ValueError:
        return None


def checks(measured):
    """[(name, passed, detail)] for one recorded match; a check with nothing to judge in
    it (no loading screen, no board) is left out."""
    m, out = measured, []
    if m.get("same_build", True):
        out.append(("replays as recorded", m["differ"] == 0, f"{m['differ']} events differ"))
    out.append(
        ("searches", m["searches"] <= SEARCHES_MOST, f"{m['searches']} (at most {SEARCHES_MOST})")
    )
    if m.get("given", 0) >= 2 * core.TEAM and not m["bots"]:
        allies, enemies = m["seated"]
        out.append(
            (
                FULL,
                min(allies, enemies) >= core.TEAM,
                f"{allies}+{enemies} seats after a reading of {m['given']} names",
            )
        )
    if m["loading_at"] is not None and m["size"] and not m["bots"]:
        after = m["seated_after"]
        out.append(
            (
                "every player seated soon after the loading screen",
                after is not None and after <= LOADING_NAMED,
                "never" if after is None else f"after {after:.1f} s",
            )
        )
    if m["first_board"] is not None and m["size"] and not m["bots"]:
        out.append(
            (
                "every player seated at the first TAB",
                m["first_board"] == (m["size"], m["size"]),
                f"{m['first_board'][0]}+{m['first_board'][1]}",
            )
        )
    return out


def summary(folder):
    """Lines a person can read: each phase's players, then the checks."""
    out, last = [], None
    for at, _, _, _, view in run(folder):
        players = view.allies + view.enemies
        line = (
            view.phase,
            len(view.allies),
            len(view.enemies),
            sum(p.state != "loading" for p in players),
            bool(view.started_at),
        )
        if line != last:
            last = line
            phase, allies, enemies, looked, started = line
            out.append(
                f"{at:.1f} {phase}{' (started)' if started else ''}: players {allies}+{enemies},"
                f" looked up {looked}"
            )
    for name, passed, detail in checks(measure(folder)):
        out.append(f"{'ok  ' if passed else 'FAIL'} {name}: {detail}")
    return out


def suite(root=None):
    """The checks over every recorded match: (lines, failures). A match recorded by another
    version is not expected to replay as recorded: how many events differ is a note, and
    so is a check that also judges the window that recorded it. The HARD ones fail for
    any recording."""
    from ..config import home

    root = Path(root) if root else home() / "matches"
    lines, failures = [], 0
    for folder in sorted(p for p in root.iterdir() if (p / "events.jsonl").exists()):
        measured = measure(folder)
        for name, passed, detail in checks(measured):
            if passed:
                continue
            if measured["same_build"] or name in HARD:
                failures += 1
                lines.append(f"FAIL {folder.name}: {name}: {detail}")
            else:  # it judged the version that recorded it, not this one
                lines.append(f"note {folder.name}: {name}: {detail}")
        note = (
            ""
            if measured["same_build"]
            else f" (another version: {measured['differ']} events differ)"
        )
        lines.append(f"     {folder.name} checked{note}")
    return lines, failures


if __name__ == "__main__":
    if sys.argv[1:] == ["--all"]:
        found, failed = suite()
        print("\n".join(found))
        print(f"{failed} failed")
        sys.exit(1 if failed else 0)
    for item in summary(sys.argv[1]):
        print(item)
