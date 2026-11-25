"""Serial I/O lanes: each runs commands on its own thread and posts events back.

One lane per provider keeps request spacing honest (the transports have no
locks) and lets a slow provider not block the other. Transports use short
timeouts and no internal retries; the core reschedules failed work.
"""

import queue
import threading

from ..api import Client
from ..auth import AuthError
from ..catalog import CatalogClient
from ..errors import AuthenticationError, PayloadError, PredError, TransportError, describe
from ..predgg import Session
from . import lookup
from .cards import hero_icons, hero_names
from .model import Failure

TIMEOUT = 8
RECENT = 20  # my matches read per match: "met before" and the day so far
RESULTS = {"search": "found", "profiles": "profiles", "recent": "recent", "match": "match"}


def failure_kind(exc):
    """ "auth", "network", "payload" or "other": what the core and the log need to know.

    An unreadable session file (AuthError) is a sign-in problem like a refused
    token, not an outage. An HTTP 4xx other than 401/403/429 is a refusal of the
    request, not the provider being down.
    """
    if isinstance(exc, (AuthenticationError, AuthError)):
        # 403 is a refusal of this request (a filter, a rate limit), not of the session.
        return "other" if getattr(exc, "status", None) == 403 else "auth"
    if isinstance(exc, TransportError):
        status = exc.status
        return "network" if status is None or status == 429 or status >= 500 else "other"
    if isinstance(exc, OSError):  # urllib's URLError and timeouts outside a transport
        return "network"
    if isinstance(exc, (PayloadError, ValueError, KeyError, TypeError)):
        return "payload"
    return "other"


def failure(command, error):
    """Events that report a failed command: a log line, the core's retry signal, health."""
    kind = command[0]
    events = [("lane_error", kind, error)]
    if kind in RESULTS:
        events.append((RESULTS[kind], command[1], None))
    elif kind == "display":
        events.append(("display", None))
    if error.kind == "auth":
        events.append(("auth", False))
    elif error.kind == "network":
        events.append(("reach", False))
    return events


class Lane(threading.Thread):
    def __init__(self, name, handler, out):
        super().__init__(daemon=True, name=name)
        self.handler, self.out = handler, out
        self.inbox = queue.Queue()
        self.stop = threading.Event()

    def submit(self, command):
        self.inbox.put(command)

    def discard_pending(self):
        """Forget the commands not started yet and return them; the one running, if any,
        finishes."""
        dropped = []
        while True:
            try:
                dropped.append(self.inbox.get_nowait())
            except queue.Empty:
                return dropped

    def run(self):
        while not self.stop.is_set():
            try:
                command = self.inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                for event in self.handler(command) or ():
                    self.out.put(event)
            except Exception as exc:  # a provider failure must not kill the lane
                problem = Failure(failure_kind(exc), describe(exc))
                for event in failure(command, problem):
                    self.out.put(event)


def search_text(versions):
    """What to search for: the reading the core settled on (the first), without the
    ellipsis of a name the screen cut off."""
    text = versions[0] if versions else ""
    return text.rstrip().removesuffix("...").removesuffix("…").strip()


class PredggIO:
    """Pred.gg, one request at a time: the hero catalog without an account, and the
    player reads with the user's own (predgg.Session)."""

    def __init__(self, session_path, client=None, public=None, store=None, salt=None):
        self.store, self.salt, self.learned, self.pending = store, salt, {}, []
        self.writing = threading.Lock()
        self.rating = None  # the running season's ID, for the points each match moved
        self.client = client or Client(
            timeout=TIMEOUT, delay=0.6, retries=0, token=Session(session_path).token
        )
        self.public = public or Client(timeout=TIMEOUT, delay=0.6, retries=0)

    def handle(self, command):
        kind = command[0]
        if kind == "display":
            heroes = CatalogClient(self.public).heroes()
            return [("reach", True), ("display", hero_names(heroes)), ("icons", hero_icons(heroes))]
        if kind == "search":
            slots = command[2]
            asked = [(slot, text) for slot in slots for text in slots[slot]]
            found = self.client.query(*lookup.search_query([search_text((t,)) for _, t in asked]))
            picked = {slot: None for slot in slots}
            for i, (slot, text) in enumerate(asked):
                # Each spelling is one the screens agreed on; the first that is exactly a
                # name on Pred.gg wins, in the core's order (draft, board, loading).
                if picked[slot] is None:
                    results = (found.get(f"s{i}") or {}).get("results")
                    picked[slot] = lookup.pick(text, results)
            return [("auth", True), ("reach", True), ("found", command[1], picked)]
        if kind == "profiles":
            ids = list(command[2])
            found = self.client.query(*lookup.profile_query(ids), tolerate=lookup.PLAY_FIELDS)
            hit = getattr(self.client, "tolerated", ())
            hit = hit if isinstance(hit, tuple) else ()  # a stand-in client has none
            nulled = [
                pid for i, pid in enumerate(ids) if f"p{i}" in hit and found.get(f"p{i}") is None
            ]
            if nulled:  # an error in their statistics took all of them: ranks alone
                try:
                    again = self.client.query(*lookup.profile_query(nulled, plays=False))
                except (AuthenticationError, TransportError):
                    raise
                except PredError:
                    again = {}  # Pred.gg refuses them: no profile, the others stand
                for i, pid in enumerate(nulled):
                    found[f"p{ids.index(pid)}"] = again.get(f"p{i}")
            return [
                ("auth", True),
                ("reach", True),
                ("profiles", command[1], lookup.profiles(ids, found)),
            ]
        if kind == "match":
            found = self.client.query(*lookup.match_query(command[1]))
            details = lookup.match_details(found.get("match"))
            return [("auth", True), ("reach", True), ("match", command[1], details)]
        if kind in ("remember", "pending"):
            with self.writing:  # the lane and a quitting app may both be at it
                return self._store(kind, command)
        if kind == "recent":
            if self.rating is None:
                try:
                    self.rating = lookup.running_season(self.public.query(lookup.SEASONS_QUERY))
                except PredError:
                    pass  # the day's points stay out; the rest works without them
            found = self.client.query(*lookup.recent_query(command[2], RECENT, self.rating))
            return [
                ("auth", True),
                ("reach", True),
                ("recent", command[1], lookup.my_matches(found)),
            ]
        return []

    def _store(self, kind, command):
        if kind == "remember":
            self.learned.update(command[1])
            for name in [k for k, v in command[1].items() if v is None]:
                self.learned.pop(name, None)  # learned wrong: forgotten
        else:
            self.pending = list(command[1])
        if self.store is not None:
            try:
                self.store.save(self.salt, self.learned, self.pending)
            except OSError as exc:
                return [("note", f"known players not saved: {type(exc).__name__}")]
        return []
