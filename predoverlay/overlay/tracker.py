"""Run the core on one thread: log phases in, lane results in, snapshot out."""

import queue
import threading
import time

from . import core, phases
from .cards import build
from .model import ViewModel

ROUTES = {
    "poll_instance": "omeda",
    "identities": "omeda",
    "profiles": "omeda",
    "recent": "omeda",
    "refresh_tokens": "omeda",
    "queues": "omeda",
    "me": "omeda",
    "relogin": "omeda",
    "card": "predgg",
    "display": "predgg",
    "peaks": "predgg",
}


def log_event(event):
    """A LogFollower event with its UTC time as a unix timestamp."""
    return ("log", dict(event, at_ts=phases.event_time(event).timestamp()))


class Tracker(threading.Thread):
    """Owns the match state; publishes ``snapshot``."""

    def __init__(
        self,
        lanes,
        *,
        follower=None,
        cards="omeda",
        region="EUROPE",
        clock=time.time,
        log=print,
    ):
        super().__init__(daemon=True, name="pred-overlay-tracker")
        self.lanes = lanes  # {"omeda": Lane, "predgg": Lane}
        self.follower = follower or phases.LogFollower()
        self.clock = clock
        self.log = log
        self.events = queue.Queue()
        self.state = core.State(stats_source=cards, region=region)
        self.snapshot = ViewModel()
        self.stop = threading.Event()
        self.last_error = None
        self.traced = None
        for lane in lanes.values():
            lane.out = self.events

    def dispatch(self, commands):
        for command in commands:
            lane = self.lanes.get(ROUTES.get(command[0]))
            if lane:
                lane.submit(command)

    def handle(self, event):
        if event[0] == "card_raw":
            event = self._card(event)
        if event[0] == "lane_error":
            self.log(f"{event[1]} failed: {event[2]}")
        elif event[0] == "note":
            self.log(event[1])
        self.dispatch(core.reduce(self.state, event, self.clock()))
        if self.state.version != self.snapshot.version:
            self.snapshot = core.view(self.state)
            self._trace(self.snapshot)

    def _trace(self, view):
        """One log line whenever what the panel shows changes in substance."""
        players = view.allies + view.enemies
        summary = (
            view.phase,
            view.status,
            " ".join(m for m in (view.mode_name, view.mode) if m) or None,
            sum(p.state != "loading" for p in players),
            sum(bool(p.name) for p in players),
            sum(bool(p.encounters) for p in players),
            bool(view.started_at),
        )
        if summary == self.traced:
            return
        self.traced = summary
        phase, status, mode, cards, names, met, started = summary
        line = f"{phase}: {status}" + (" (started)" if started and phase == "loading" else "")
        if players:
            n = len(players)
            line += f" · {mode} · cards {cards}/{n} · names {names}/{n} · met before {met}"
        self.log(line)

    def _card(self, event):
        _, pid, raw = event
        if raw is None:
            return ("card", pid, None, None)  # no Pred.gg profile
        try:
            card = build(raw, display=self.state.display)
        except Exception as exc:  # one bad profile must not stop the tracker
            return ("card", pid, None, f"{type(exc).__name__}: {exc}")
        return ("card", pid, card, None)

    def start_lanes(self):
        for lane in self.lanes.values():
            if not lane.is_alive():
                lane.start()

    def run(self):
        self.start_lanes()
        try:
            events = self.follower.replay()
            hint = phases.current(events)
            if hint["phase"] != "menu" and hint.get("at"):
                self.handle(log_event(hint))  # a hint; the game instance confirms it
                # Markers after it (map joined, match started) when starting mid-match.
                last = max(i for i, e in enumerate(events) if e["phase"] not in phases.MARKERS)
                for event in events[last + 1 :]:
                    self.handle(log_event(event))
        except Exception as exc:
            self._report(exc)
        while not self.stop.is_set():
            try:
                self.step()
            except Exception as exc:  # e.g. the log being rotated; keep going
                self._report(exc)
                self.stop.wait(1.0)

    def step(self):
        """One loop turn: at most one lane result, new log lines, then a tick."""
        try:
            self.handle(self.events.get(timeout=0.25))
        except queue.Empty:
            pass
        try:
            events = self.follower.poll()
        except Exception as exc:  # e.g. the log being rotated; ticks must go on
            self._report(exc)
            events = []
        for event in events:
            self.handle(log_event(event))
        self.handle(("tick",))

    def _report(self, exc):
        message = f"tracker: {type(exc).__name__}: {exc}"
        if message != self.last_error:  # a repeating failure is logged once
            self.last_error = message
            self.log(message)

    def shutdown(self):
        self.stop.set()
        for lane in self.lanes.values():
            lane.stop.set()
        self.join(2)
