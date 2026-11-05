"""Immutable view data handed from the tracker thread to the window."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Encounter:
    """This player was in one of my recent matches."""

    matches_ago: int  # 1 = my previous match
    hero: str | None
    side: str  # "with" or "against" me
    won: bool | None  # from my side


@dataclass(frozen=True)
class PlayerView:
    player_id: str
    name: str | None  # Name#code once the official lookup returns
    team: str  # "ally" or "enemy"
    is_me: bool
    state: str  # official profile: "loading", "ready" or "error"
    card: dict | None = None  # Pred.gg cards.build() output; read-only
    stats_state: str = "off"  # Pred.gg card: "off", "loading", "ready", "missing" or "error"
    profile: dict | None = None  # GameClient.public_profiles() entry: rank, level, age
    encounters: tuple[Encounter, ...] = ()


@dataclass(frozen=True)
class ViewModel:
    version: int = 0
    phase: str = "menu"
    phase_at: float = 0.0  # unix time of the phase change, from the log
    started_at: float = 0.0  # unix time the match started (loading screen over), else 0
    status: str = "waiting for a match"
    mode: str | None = None  # "5v5", "3v3", "2v2", ...: from the roster size
    mode_name: str | None = None  # "OVERCLOCKED", "RANKED", ...: from the game log
    match_id: str | None = None
    stats_source: str = "omeda"  # "predgg" when Pred.gg cards are on
    heroes: tuple = ()  # hero display names, to read the scoreboard's hero lines against
    region: str = "EUROPE"
    queues: dict = field(default_factory=dict)  # {queue: (players, parties)} in ``region``
    queues_at: float = 0.0  # when they were read; 0 = never
    health: str = "ok"  # "ok", "offline" (no answer from Omeda) or "login" (sign-in needed)
    allies: tuple[PlayerView, ...] = field(default_factory=tuple)
    enemies: tuple[PlayerView, ...] = field(default_factory=tuple)

    @property
    def roster_ids(self):
        return tuple(p.player_id for p in self.allies + self.enemies)
