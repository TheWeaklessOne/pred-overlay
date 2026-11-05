"""Follow the game client's log for match phases. Reads the file only.

Phases: ``menu`` → ``accepted`` → ``draft`` → ``loading`` (map load and the
match itself) → ``finished`` with the Pred.gg match ID, then ``menu`` again.
A ``menu`` event marks a return to the menu that carries no match ID.

Two markers are not phases. ``joined``: the client entered the match map
("Welcomed by server" on /Game/Maps/). ``presence``: a presence update, the
first of which after ``joined`` comes when the match itself starts, so the
loading screen is over.
The client recreates the log on launch, so the follower reopens it when the
file shrinks or is replaced.
"""

import os
import re
from pathlib import Path

_TIME = r"^\[(?P<at>\d{4}\.\d\d\.\d\d-\d\d\.\d\d\.\d\d):\d+\]"
MARKERS = ("joined", "presence")
_PATTERNS = (
    (
        "joined",
        re.compile(_TIME + r".*LogNet: Welcomed by server \(Level: /Game/Maps/(?P<map>[^,\s)]+)"),
    ),
    ("presence", re.compile(_TIME + r".*LogPredTask: Task Started: Pragma_PresenceChange\s*$")),
    ("accepted", re.compile(_TIME + r".*Task Started: Pragma_MatchmakingAccept")),
    (
        "finished",
        re.compile(
            _TIME
            + r".*Browse: /Game/\S*\?closed=(?P<reason>\w+)\?gameInstanceId=(?P<match_id>[0-9a-f-]{36})"
        ),
    ),
    # Back in the main menu without a match ID: a dropped connection (bare
    # ``?closed``), the menu map after launch (``OTA``) or plain ``Voyager``.
    (
        "menu",
        re.compile(_TIME + r".*Browse: \S*/Game/(?:Voyager/Voyager(?:\?closed)?\s*$|Maps/OTA\?)"),
    ),
    (
        "draft",
        re.compile(
            _TIME
            + r".*Browse: \S+/Game/Voyager/Voyager\?userId=(?P<user_id>[0-9a-f-]{36})\?userName=(?P<user_name>[^?\s]+)"
        ),
    ),
    (
        "loading",
        re.compile(
            _TIME
            + r".*Browse: \S+/Game/Maps/(?P<map>[^?\s]+)\?userId=(?P<user_id>[0-9a-f-]{36})\?userName=(?P<user_name>[^?\s]+)"
            # The mode the match is played in: ?Experience=BP_Experience_Game_Overclocked
            + r"(?:\S*?\?Experience=(?P<experience>[^?\s]+))?"
        ),
    ),
)


def default_log():
    return (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Predecessor"
        / "Saved"
        / "Logs"
        / "Predecessor.log"
    )


def parse(line):
    """One log line → phase event dict, or None."""
    for phase, pattern in _PATTERNS:
        found = pattern.search(line)
        if found:
            return {"phase": phase, **found.groupdict()}
    return None


class LogFollower:
    """Poll a growing log; ``poll()`` returns events appended since the last call."""

    def __init__(self, path=None):
        self.path = Path(path or default_log())
        self.offset = 0
        self.identity = None
        self.partial = b""

    def replay(self):
        """Events already in the current file, to learn the phase at startup."""
        self.offset, self.identity, self.partial = 0, None, b""
        return self.poll()

    def poll(self):
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return []
        identity = (info.st_ino, info.st_dev)
        if identity != self.identity or info.st_size < self.offset:
            self.identity, self.offset, self.partial = identity, 0, b""
        if info.st_size == self.offset:
            return []
        with open(self.path, "rb") as stream:
            stream.seek(self.offset)
            data = self.partial + stream.read(info.st_size - self.offset)
        self.offset = info.st_size
        *complete, self.partial = data.split(b"\n")
        events = []
        for raw in complete:
            event = parse(raw.decode("utf-8", errors="replace"))
            if event:
                events.append(event)
        return events


def current(events):
    """Phase after a sequence of events; ``finished`` returns to the menu."""
    state = {"phase": "menu"}
    for event in events:
        if event["phase"] in MARKERS:
            continue
        state = dict(event)
        if event["phase"] == "finished":
            state["phase"] = "menu"
            state["last_match_id"] = event["match_id"]
    return state


def event_time(event):
    """UTC datetime of an event; the client log stamps lines in UTC."""
    from datetime import datetime, timezone

    return datetime.strptime(event["at"], "%Y.%m.%d-%H.%M.%S").replace(tzinfo=timezone.utc)
