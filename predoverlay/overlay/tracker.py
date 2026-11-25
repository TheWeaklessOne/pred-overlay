"""Run the core on one thread: log phases in, lane results in, snapshot out."""

import copy
import queue
import threading
import time

from .. import predgg
from ..errors import describe
from . import core, lanes, phases, recorder, win32
from .model import Failure

GAME_CHECK = 5.0  # seconds between looks at the process list for the game client

STORE = ("remember", "pending")  # writes of the known-players file: no session needed
ROUTES = {
    "display": "predgg",
    "search": "predgg",
    "profiles": "predgg",
    "recent": "predgg",
    "match": "predgg",
    "remember": "predgg",
    "pending": "predgg",
    "portraits": "assets",
}


def log_event(event):
    """A LogFollower event with its UTC time as a unix timestamp."""
    return ("log", dict(event, at_ts=phases.event_time(event).timestamp()))


def answered(event):
    """The command a lane result reports as done, or None for a failure or a non-result."""
    kind = event[0]
    if kind == "display":
        return "display" if event[1] is not None else None
    if kind in ("found", "profiles", "recent", "match"):
        if event[2] is None:
            return None
        return "search" if kind == "found" else kind
    return None


def _duration(seconds):
    """45s, 3m, 1h 5m."""
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


class Tracker(threading.Thread):
    """Owns the match state; publishes ``snapshot``."""

    def __init__(
        self,
        lanes,
        *,
        follower=None,
        clock=time.time,
        log=print,
        game_running=win32.game_running,
        session_path=None,
        on_game=None,
        recorder=None,
    ):
        super().__init__(daemon=True, name="pred-overlay-tracker")
        self.on_game = on_game  # called when the game starts (the name model loads then)
        self.recorder = recorder  # recorder.Recorder: each match's events, kept locally
        self.lanes = lanes  # {"predgg": Lane, "assets": Lane}
        self.follower = follower or phases.LogFollower()
        self.clock = clock
        self.log = log
        self.game_running = game_running  # True, False, or None when it cannot tell
        self.next_game_check = 0.0
        self.session_path = session_path  # watched while a sign-in is needed
        self.session_seen = None
        self.events = queue.Queue()
        self.state = core.State(game_running=False)  # nothing before the check
        self.snapshot = core.view(self.state)  # "closed" from the start, not a default menu
        self.stop = threading.Event()
        self.last_error = None
        self.traced = None
        self.health_line = None
        self.failing = {}  # command -> the message last logged for it
        self.outage = None  # [since, failures] while Pred.gg cannot be reached
        for lane in lanes.values():
            lane.out = self.events

    def dispatch(self, commands):
        for command in commands:
            lane = self.lanes.get(ROUTES.get(command[0]))
            if lane:
                lane.submit(command)

    def handle(self, event):
        now = self.clock()
        if event == ("auth", False) and self.state.auth != "login" and "predgg" in self.lanes:
            # Refused: what is queued would be refused too, each with a token refresh. The
            # core hears of each as a failure, so it asks again after a sign-in; what needs
            # no session (the known-players file) goes back in the queue.
            lane = self.lanes["predgg"]
            for command in lane.discard_pending() or ():
                if command[0] in STORE:
                    lane.submit(command)
                else:
                    gone = Failure("auth", "dropped: the session was refused")
                    for result in lanes.failure(command, gone)[1:]:
                        if result[0] not in ("auth",):
                            self.events.put(result)
        if event[0] == "lane_error":
            self._failed(event[1], event[2], now)
        elif event[0] == "note":
            self.log(event[1])
        else:
            self._answered(event, now)
        # The state before an event that can begin a match: a recording starts from it.
        record = self.recorder is not None and not self.recorder.broken
        before = as_taken = None
        if record:
            try:  # the record never stops the tracker
                if event[0] in ("log", "game"):
                    before = copy.deepcopy(self.state)
                as_taken = recorder.encode(event)  # before the core changes it
            except Exception as exc:
                record = False
                self._report(exc)
        try:
            commands = core.reduce(self.state, event, now)
        except Exception:
            core.drop_result(self.state, event, now)  # retried later, not on the next tick
            raise
        if record:
            self.recorder.after(self.state, as_taken, commands, now, before)
        self.dispatch(commands)
        while self.state.notes:
            self.log(self.state.notes.pop(0))
        line = "session: " + ("sign-in needed" if self.state.auth == "login" else "ok")
        if line != self.health_line:
            self.health_line = line
            self.log(line)
        if self.state.version != self.snapshot.version:
            self.snapshot = core.view(self.state)
            self._trace(self.snapshot)

    def check_game(self):
        """Look at the process list (names only) and tell the core when the game came or went."""
        now = self.clock()
        if now < self.next_game_check:
            return
        self.next_game_check = now + GAME_CHECK
        running = self.game_running()
        if running is None or running == self.state.game_running:
            return  # an unreadable list changes nothing
        self.log("game started" if running else "game closed")
        if running and self.on_game is not None:
            try:
                self.on_game()
            except Exception as exc:  # the name model is optional: never stop the tracker
                self.log(f"game start hook failed: {type(exc).__name__}: {exc}")
        if not running:
            for lane in self.lanes.values():
                for command in lane.discard_pending() or ():  # requests not sent yet are not sent
                    if command[0] in STORE:
                        lane.submit(command)  # but what is learned is still written down
        self.handle(("game", running))

    def _failed(self, command, failure, now):
        """Log a failure once while it repeats; a Pred.gg outage as one line when it starts."""
        if getattr(failure, "kind", None) == "network" and ROUTES.get(command) == "predgg":
            if self.outage is None:
                self.outage = [now, 0]
                since = time.strftime("%H:%M", time.localtime(now))
                self.log(f"Pred.gg offline since {since} ({command}: {failure})")
            self.outage[1] += 1
            return
        message = str(failure)
        if self.failing.get(command) != message:
            self.failing[command] = message
            self.log(f"{command} failed: {message}")

    def _answered(self, event, now):
        """A lane result that is not a failure ends that command's repeat and any outage."""
        command = answered(event)
        if command is None:
            return
        self.failing.pop(command, None)
        if self.outage is not None and ROUTES.get(command) == "predgg":
            since, failures = self.outage
            self.outage = None
            self.log(f"Pred.gg back after {_duration(now - since)}, {failures} failures")

    def _trace(self, view):
        """One log line whenever what the panel shows changes in substance."""
        players = view.allies + view.enemies
        summary = (
            view.phase,
            view.status,
            " ".join(m for m in (view.mode_name, view.mode) if m) or None,
            sum(p.state != "loading" for p in players),
            sum(p.state == "ready" for p in players),
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
            line += f" · {mode} · looked up {cards}/{n} · found {names}/{n} · met before {met}"
        self.log(line)

    def start_lanes(self):
        for lane in self.lanes.values():
            if not lane.is_alive():
                lane.start()

    def run(self):
        self.start_lanes()
        try:
            self.check_game()  # before the log: a replayed match is taken only if the game runs
        except Exception as exc:
            self._report(exc)
        try:
            events = self.follower.replay()
            logins = [event for event in events if event["phase"] == "login"]
            if logins:
                self.handle(log_event(logins[-1]))  # who plays, before any match says so
            hint = phases.current(events)
            if hint["phase"] != "menu" and hint.get("at"):
                self.handle(log_event(hint))  # where the game is now
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

    def check_session(self):
        """While a sign-in is needed, a changed session file (a sign-in from the console,
        or one the tray reported late) counts as one."""
        if self.session_path is None:
            return
        try:
            stamp = self.session_path.stat().st_mtime_ns
        except OSError:
            stamp = None
        if self.state.auth != "login" or stamp == predgg.own_write(self.session_path):
            self.session_seen = stamp  # the app's own refresh is no sign-in
        elif stamp is not None and stamp != self.session_seen:
            self.session_seen = stamp
            self.log("a new session file: reading again")
            self.handle(("sign_in",))

    def step(self):
        """One loop turn: at most one lane result, the game's process, new log lines, a tick."""
        try:
            self.handle(self.events.get(timeout=0.25))
        except queue.Empty:
            pass
        try:
            self.check_game()
        except Exception as exc:  # the list unreadable once: the last answer stands
            self._report(exc)
        try:
            events = self.follower.poll()
        except Exception as exc:  # e.g. the log being rotated; ticks must go on
            self._report(exc)
            events = []
        for event in events:
            try:
                self.handle(log_event(event))
            except Exception as exc:  # the lines after it still count
                self._report(exc)
        try:
            self.check_session()
        except Exception as exc:
            self._report(exc)
        self.handle(("tick",))

    def _report(self, exc):
        message = f"tracker: {describe(exc)}"
        if message != self.last_error:  # a repeating failure is logged once
            self.last_error = message
            self.log(message)

    def shutdown(self):
        self.stop.set()
        for lane in self.lanes.values():
            lane.stop.set()
            for command in lane.discard_pending() or ():
                if command[0] in STORE:  # written now: the lane stops before it gets to them
                    try:
                        for event in lane.handler(command) or ():
                            if event[0] == "note":
                                self.log(event[1])
                    except Exception as exc:
                        self._report(exc)
        if self.is_alive():
            self.join(2)
