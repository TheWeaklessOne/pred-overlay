"""Serial I/O lanes: each runs commands on its own thread and posts events back.

One lane per provider keeps request spacing honest (the transports have no
locks) and lets a slow provider not block the other. Transports use short
timeouts and no internal retries; the core reschedules failed work.
"""

import json
import os
import queue
import threading
import time
from pathlib import Path

from ..api import Client
from ..catalog import CatalogClient
from ..errors import AuthenticationError, PredError
from ..game import GameClient, full_name, stored_tokens
from ..official import OfficialClient
from .cards import CARD_QUERY, best_peak, hero_names, peak_query, season_history

TIMEOUT = 8
MENU_RENEW = 2 * 60 * 60  # in the menu, renew tokens that expire within two hours
RENEW_BACKOFF = 10 * 60  # after a failed Steam refresh, do not try again for this long
STEAM = threading.Lock()  # one Steam API session in this process at a time


def failure(command, error):
    """Events that report a failed command: a log line, then the core's retry signal."""
    kind = command[0]
    events = [("lane_error", kind, error)]
    if kind == "poll_instance":
        events.append(("instance", None, error))
    elif kind == "card":
        events.append(("card", command[1], None, error))
    elif kind in ("identities", "profiles", "recent", "peaks"):
        events.append((kind, command[1], None))
    elif kind in ("me", "relogin", "display", "queues"):
        events.append((kind, None, error))
    return events


class Lane(threading.Thread):
    def __init__(self, name, handler, out):
        super().__init__(daemon=True, name=name)
        self.handler, self.out = handler, out
        self.inbox = queue.Queue()
        self.stop = threading.Event()

    def submit(self, command):
        self.inbox.put(command)

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
                for event in failure(command, f"{type(exc).__name__}: {exc}"):
                    self.out.put(event)


class OmedaIO:
    """Game backend and official identities, with tokens renewed off the UI thread."""

    def __init__(self, session_path, cache_dir, clock=time.time, renew=True):
        self.session_path = session_path
        self.cache_dir = Path(cache_dir)
        self.clock = clock
        self.renew = renew  # False: never log in via Steam (another lane does)
        self.tokens = None
        self.game = self.official = None
        self.renew_after = 0.0
        self.renewed = False
        self.announce = False  # new tokens: tell the core whose they are

    def clients(self, margin=0):
        """Clients for the stored tokens. A Steam refresh runs only when they expire
        within ``margin`` seconds, and at most once per RENEW_BACKOFF. Tokens that
        cannot be checked (no expiry, or an env variable shadowing the session) keep
        the last refreshed pair for that long instead of logging in on every call."""
        try:
            tokens = stored_tokens(self.session_path, renew=False, margin=margin)
        except AuthenticationError:
            if not self.renew:
                raise
            if self.clock() < self.renew_after:
                if self.renewed:
                    return self.game, self.official
                raise
            self.renew_after, self.renewed = self.clock() + RENEW_BACKOFF, False
            with STEAM:
                tokens = stored_tokens(self.session_path, margin=margin)
            self.renewed = True
        if tokens != self.tokens:
            # A refresh signs in whichever Steam account runs now, which may not
            # be the one before; the core learns the account from the next event.
            self.announce = self.tokens is not None
            self.tokens = tokens
            self.game = GameClient(tokens["game_token"], timeout=TIMEOUT, retries=0)
            self.official = OfficialClient(tokens["social_token"], timeout=TIMEOUT, retries=0)
        return self.game, self.official

    def relogin(self):
        """New tokens for the Steam account signed in now; returns its player ID."""
        from ..auth import SessionStore
        from ..game import save_tokens, steam_client_login

        try:
            with STEAM:
                tokens = steam_client_login()
        except PredError as exc:  # Steam closed, game never run: a sign-in problem
            raise AuthenticationError(f"Could not sign in through Steam: {exc}") from None
        save_tokens(SessionStore(self.session_path), tokens)
        self.renew_after, self.renewed = 0.0, False
        _, official = self.clients()
        self.announce = False
        return self.account(official)

    @staticmethod
    def account(official):
        identities = official.me()["gameIdentities"]
        return identities[0]["pragmaPlayerId"] if identities else None

    def details(self, game, match_id):
        """Finished-match details, cached on disk (they never change)."""
        path = self.cache_dir / f"{match_id}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except ValueError:
            path.unlink(missing_ok=True)  # a damaged entry is fetched again
        found = game.match_details(match_id)
        if found:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(found), encoding="utf-8")
            os.replace(temporary, path)
        return found

    def handle(self, command):
        if command[0] == "relogin":
            return [("relogin", self.relogin())]
        events = self.run(command)
        if self.announce:  # tokens changed under us: whose are they now?
            self.announce = False
            events.insert(0, ("me", self.account(self.official)))
        return events

    def run(self, command):
        kind = command[0]
        if kind == "refresh_tokens":
            self.clients(margin=MENU_RENEW)
            return []
        game, official = self.clients()
        if kind == "poll_instance":
            return [("instance", game.current_game())]
        if kind == "me":
            self.announce = False
            return [("me", self.account(official))]
        if kind == "identities":
            rows = official.identities(list(command[2]))["results"]
            names = {}
            for row in rows:  # a row without a name (deleted account) loses only itself
                display = row.get("pragmaDisplayName") or {}
                if row.get("pragmaPlayerId") and display.get("displayName"):
                    names[row["pragmaPlayerId"]] = full_name(display)
            return [("identities", command[1], names)]
        if kind == "profiles":
            return [("profiles", command[1], game.public_profiles(command[2]))]
        if kind == "queues":
            return [("queues", game.queues())]
        if kind == "recent":
            previous = [m for m in game.match_list(4) if m["match_id"] != command[2]][:3]
            # Keep positions: None (not recorded yet) still counts as a match ago.
            found = [self.details(game, m["match_id"]) for m in previous]
            return [("recent", command[1], found)]
        return []


class PredggIO:
    """Pred.gg cards, past-season peaks and the hero catalog, one request at a time.

    The catalog and ratings are public. Player statistics need a signed-in account
    (its guest token stopped opening them on 31 October 2026), so cards are
    asked for only when the session holds one.
    """

    def __init__(self, token=None):
        self.catalog = Client(anonymous=True, timeout=TIMEOUT, delay=0.6, retries=0)
        self.client = Client(token=token, timeout=TIMEOUT, delay=0.6, retries=0)

    def handle(self, command):
        kind = command[0]
        if kind == "display":
            return [("display", hero_names(CatalogClient(self.catalog).heroes()))]
        if kind == "peaks":
            ids = list(command[2])
            found = self.catalog.query(*peak_query(ids))
            history = {}
            for i, pid in enumerate(ids):
                raw = found.get(f"p{i}")
                seasons = season_history(raw, found.get("seasons"))
                if seasons is not None:
                    history[pid] = {"peak": best_peak(raw), "seasons": seasons}
            return [("peaks", command[1], history)]
        if kind == "card":
            try:
                raw = self.client.query(CARD_QUERY, {"id": command[1]})["player"]
            except PredError as exc:
                return [("card", command[1], None, str(exc))]
            return [("card_raw", command[1], raw)]
        return []
