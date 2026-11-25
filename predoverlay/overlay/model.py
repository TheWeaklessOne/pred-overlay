"""Immutable view data handed from the tracker thread to the window."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Failure:
    """Why a lane command failed, sorted where it happened instead of parsed from text.

    ``kind`` is "auth" (the session was refused, or there is none), "network"
    (the provider could not be reached or is down), "payload" (it answered
    something unusable) or "other".
    """

    kind: str
    message: str  # "ExceptionName: text", for the log; carries no tokens

    def __str__(self):
        return self.message


@dataclass(frozen=True)
class Encounter:
    """This player was in one of my recent matches."""

    matches_ago: int  # 1 = my previous match
    hero: str | None
    side: str  # "with" or "against" me
    won: bool | None  # from my side
    icon: str | None = None  # the hero's portrait
    kda: tuple | None = None  # their kills, deaths and assists in that match


@dataclass(frozen=True)
class PlayerView:
    player_id: str
    name: str | None  # what the panel calls them: Pred.gg's name, or a made-up label
    team: str  # "ally" or "enemy"
    is_me: bool
    state: str  # Pred.gg profile: "loading", "ready" or "error" (not found)
    profile: dict | None = None  # lookup.profiles() entry: rank, seasons, main, first played
    encounters: tuple[Encounter, ...] = ()
    peak: dict | None = None  # best finished season: {"rank", "season"}
    seasons: tuple | None = None  # finished seasons, newest first; None: not known
    main: dict | None = None  # most played: {"hero": game name, "role": "CARRY", "icon": ...}
    last_played: str | None = None  # Pred.gg's last match before this one, ISO time
    today: dict | None = None  # own row only: see core.today_summary
    read_as: str | None = None  # the name as the screen shows it, to find the TAB row by
    tag: str | None = None  # the overlay's own tag for a player whose name stays hidden
    # Why there is no data (state "error"): "console" (a clean reading Pred.gg does not
    # know: Pred.gg hides console names), "misread" (an unsure reading not found),
    # "offline" (Pred.gg did not answer), "noprofile" (found, but no profile came),
    # "login" (the app is not signed in on Pred.gg: nothing was asked)
    why: str | None = None
    # Plays with others of their team in this match (core.parties): {"size", "group"
    # (0, 1: which party of the team), "with": (their names as shown), "games": the
    # most matches they shared on one side among their last ones}
    party: dict | None = None
    hero: str | None = None  # the hero picked in this draft ("STEEL"), from the pick screen


@dataclass(frozen=True)
class ViewModel:
    version: int = 0
    phase: str = "menu"
    phase_at: float = 0.0  # unix time of the phase change, from the log
    started_at: float = 0.0  # unix time the match started (loading screen over), else 0
    ended_at: float = 0.0  # unix time the match ended (end-of-match screen up), else 0
    status: str = "waiting for a match"
    status_note: bool = False  # the status is a state to read (an error), not a wait
    mode: str | None = None  # "5v5", "3v3", "2v2", ...: from the roster size
    mode_name: str | None = None  # "OVERCLOCKED", "RANKED", ...: from the game log
    match_id: str | None = None
    heroes: tuple = ()  # hero display names, to read the scoreboard's hero lines against
    my_name: str | None = None  # mine, from the game log: a name the screens' lines may hold
    art: int = 0  # grows when new portraits land on disk
    health: str = "ok"  # "ok", "offline" (no answer from Pred.gg) or "login" (sign-in needed)
    allies: tuple[PlayerView, ...] = field(default_factory=tuple)
    enemies: tuple[PlayerView, ...] = field(default_factory=tuple)

    @property
    def roster_ids(self):
        return tuple(p.player_id for p in self.allies + self.enemies)
