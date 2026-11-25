"""Who a name on the screen was, learned from finished matches and kept on this PC only.

Pred.gg hides console players' names, so a console player cannot be found by
the name the game shows. Their profile is still there under their player ID:
once a match is over, Pred.gg lists who played which role on which team, and
the loading screen showed the same teams in the same role columns. Each name
read there so gets its player ID, and the next match with that player shows
their rank and "met before" from the first second.

The file never holds a name: only a keyed hash of it (HMAC-SHA256 with a random
key made on this PC), so nobody can read names out of it, while the same name
still finds the same player. Each name is kept by two hashes: of its folded key
(as before) and of its spelling (lookup.strict_key), with a third entry saying the
spelling is known. A name learned so is taken only for its own spelling ("Gh0st"
is not the "Ghost" of an earlier match); one learned before by its key alone. Names the panel cannot show (console players,
players Pred.gg does not know) get a made-up two-word label from the same hash:
stable on this PC, so the same player looks the same match after match, and
meaningless anywhere else.
"""

import hashlib
import hmac
import json
import math
import os
import secrets

LIMIT = 15000  # entries kept (up to three a player); the oldest learned go first

ADJECTIVES = (
    "AMBER", "ASHEN", "BOLD", "BRAVE", "BRIGHT", "CALM", "CRIMSON", "DARK",
    "DUSTY", "EAGER", "FAST", "FIERCE", "FROSTY", "GOLDEN", "GRIM", "HIDDEN",
    "IRON", "JADE", "KEEN", "LONE", "LUCKY", "MIGHTY", "NIGHT", "PALE",
    "QUIET", "RAPID", "ROGUE", "RUSTY", "SILENT", "SLY", "STONE", "WILD",
)  # fmt: skip
NOUNS = (
    "BADGER", "BEAR", "BOAR", "COBRA", "CRANE", "CROW", "DRAKE", "EAGLE",
    "FALCON", "FOX", "GECKO", "HAWK", "HERON", "JACKAL", "LYNX", "MANTIS",
    "MOOSE", "OTTER", "OWL", "PANDA", "PANTHER", "RAVEN", "RHINO", "SHARK",
    "SPIDER", "STAG", "TIGER", "TOAD", "VIPER", "WOLF", "WOLVERINE", "YAK",
)  # fmt: skip


def digest(salt, key):
    """The stored stand-in for a name key: not reversible without this PC's salt."""
    return hmac.new((salt or "").encode(), key.encode(), hashlib.sha256).hexdigest()[:24]


def spelled(salt, strict):
    """The stored stand-in for a name's spelling (lookup.strict_key)."""
    return digest(salt, "spelled:" + strict)


def checked(salt, name_digest):
    """The entry saying that the name behind ``name_digest`` was learned with its
    spelling: then only that spelling finds the player."""
    return digest(salt, "checked:" + name_digest)


def label(salt, key):
    """'SILENT HAWK': a made-up name for a name key, the same every time on this PC."""
    raw = hmac.new((salt or "").encode(), b"label:" + key.encode(), hashlib.sha256).digest()
    return f"{ADJECTIVES[raw[0] % len(ADJECTIVES)]} {NOUNS[raw[1] % len(NOUNS)]}"


ROLE_SOURCES = ("loading", "board", None)  # where a pending row's role was read


def _pending_row(row):
    """team, role, name digest; since 0.5.3 the spelling's digest; since 0.5.16 the screen
    that gave the role (core._learning writes all five)."""
    return (
        3 <= len(row) <= 5
        and isinstance(row[0], str)
        and isinstance(row[1], str)
        and all(isinstance(d, str) and len(d) == 24 for d in row[2:4])
        and (len(row) < 5 or row[4] in ROLE_SOURCES)
    )


def _pending(entry):
    """A pending match as the core keeps it, and nothing else (no names)."""
    try:
        return (
            isinstance(entry["match_id"], str)
            and isinstance(entry["due"], (int, float))
            and math.isfinite(entry["due"])
            and isinstance(entry["tries"], int)
            and entry["tries"] >= 0
            and isinstance(entry.get("ended", 0), (int, float))
            and all(_pending_row(row) for row in entry["rows"])
        )
    except (KeyError, TypeError, ValueError):
        return False


class Store:
    """``known-players.json``: {"version": 1, "salt": hex, "players": {digest: player ID},
    "pending": [finished matches still to learn from: their ID, rows of team, role and
    name digest, when to ask]}."""

    def __init__(self, path):
        self.path = path

    def load(self):
        """(salt, {digest: pid}, pending, state): ``state`` is "ok", "fresh" (no file or a
        damaged one: start afresh with a new salt, and save it, or the labels change on
        the next start) or "unreadable" (it could not be read: never save over it)."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeError):  # absent, or not text at all
            return secrets.token_hex(16), {}, [], "fresh"
        except OSError:
            return secrets.token_hex(16), {}, [], "unreadable"
        try:
            found = json.loads(raw)
            salt, players = found["salt"], found["players"]
            if not isinstance(salt, str) or len(salt) < 32 or not isinstance(players, dict):
                raise ValueError("odd file")
            clean = {
                k: v
                for k, v in players.items()
                if isinstance(k, str) and isinstance(v, str) and len(k) == 24 and len(v) < 80
            }
            pending = [p for p in found.get("pending") or () if _pending(p)]
            return salt, clean, pending, "ok"
        except (ValueError, KeyError, TypeError, RecursionError):
            return secrets.token_hex(16), {}, [], "fresh"

    def save(self, salt, players, pending=()):
        items = list(players.items())[-LIMIT:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"version": 1, "salt": salt, "players": dict(items), "pending": list(pending)}
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
