"""Sample data for ``--preview`` and the tray's Show preview: a full 5v5 without the game.

PreviewTracker stands in for the live tracker and cycles the draft, the loading
screen, a held TAB and the menu, so every surface can be seen on any PC. The preview
runs over whatever program is in front, so nothing of the screen is ever read for it:
the TAB stage points at a row by itself and its board is the sample teams.
"""

import time
from datetime import datetime, timezone

from .model import Encounter, PlayerView, ViewModel
from .rows import ROLES, rank_step
from .scoreboard import PANELS, ROW_H, ROW_TOPS
from .visibility import LOADING_SCREEN


def _sample_profile(rank, games):
    """A profile shaped like lookup.profiles() for 'Gold II' (or 'UNRANKED')."""
    step = rank_step(rank)
    days = 25 if games < 20 else games * 3 + 30
    first = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc)
    return {
        "current": {
            "rank": rank if step is not None else None,
            "points": step * 100 + (games * 7) % 100 if step is not None else None,
            "season": "Season 1 - Split 6",
        },
        "first_played": first.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stats": _sample_stats(games),
    }


def _sample_stats(games):
    """How a sample player plays (cards.play_stats), for the card's ON HERO line."""
    on = max(1, games // 6)
    form = [(games + i) % 3 != 0 for i in range(10)]
    return {
        "roles": {"OFFLANE": (games // 3, games // 6), "CARRY": (games // 4, games // 8)},
        "heroes": {
            "ltbelica": {
                "played": on,
                "won": on // 2 + 1,
                "kills": on * 6,
                "deaths": on * 4,
                "assists": on * 7,
            }
        },
        "form": form,
        "played": [("Sparrow" if i < (games % 8) else "Grux", None) for i in range(10)],
        "total": games,
    }


def _sample_peak(rank):
    """A past best for the sample players under Diamond (and the unplaced one)."""
    if rank and rank.startswith(("Diamond", "Paragon")):
        return None
    return {"rank": "Diamond II", "season": "Season 1 - Split 2"}


def _sample_seasons():
    """A short history for the sample card."""

    def season(name, final, peak, percentile, date, ranking=None, peak_ranking=None):
        return {
            "name": name,
            "final": final,
            "peak": peak,
            "ranking": ranking,
            "peak_ranking": peak_ranking,
            "percentile": percentile,
            "peak_date": date,
            "unranked": False,
        }

    return (
        season("Season 1 - Split 5", "Platinum III", "Platinum I", 22, "OCT 26", 8120, 4310),
        season("Season 1 - Split 4", "Gold I", "Platinum III", 31, "MAY 26"),
        season("Season 1 - Split 3", "Diamond III", "Diamond II", 6, "FEB 26", 1980, 1120),
        season("Season 1 - Split 2", "Gold II", "Gold I", 40, "OCT 25"),
    )


def _sample_player(
    player_id,
    name,
    team,
    *,
    me=False,
    rank,
    games,
    heroes,
    encounters=(),
):
    """A ready player, named as the game's screen shows them."""
    return PlayerView(
        player_id=player_id,
        name=name,
        team=team,
        is_me=me,
        state="ready",
        profile=_sample_profile(rank, games),
        peak=_sample_peak(rank),
        seasons=_sample_seasons(),
        main={"hero": heroes[0], "role": ROLES[len(name) % len(ROLES)]},
        encounters=tuple(encounters),
        today={"form": ("L", "W", "W", "L", "W"), "vp": 41, "kda": 3.2} if me else None,
    )


def _partied(players, indices, games):
    """``players`` with those at ``indices`` in one party."""
    import dataclasses

    names = {i: (players[i].name or "").upper() for i in indices}
    return tuple(
        dataclasses.replace(
            p,
            party={
                "size": len(indices),
                "group": 0,
                "with": tuple(names[j] for j in indices if j != i),
                "games": games,
            },
        )
        if i in indices
        else p
        for i, p in enumerate(players)
    )


def preview_view(phase="draft", phase_at=0.0, version=1):
    """A complete 5v5 roster for ``--preview``, without the game."""
    allies = (
        _sample_player(
            "preview-a1",
            "Orbitfox",
            "ally",
            rank="Gold III",
            games=212,
            heroes=("Riktor", "Murdock", "Grux"),
        ),
        _sample_player(
            "preview-a2",
            "Lanternjaw90",
            "ally",
            rank="Silver I",
            games=87,
            heroes=("Zinx", "Gideon", "Howitzer"),
        ),
        _sample_player(
            "preview-a3",
            "SamplePlayer",
            "ally",
            me=True,
            rank="Gold II",
            games=340,
            heroes=("Narbash", "Mourn", "Muriel"),
        ),
        _sample_player(
            "preview-a4",
            "Quietstorm05",
            "ally",
            rank="Platinum III",
            games=501,
            heroes=("Khaimera", "Kallari", "Feng Mao"),
        ),
        _sample_player(
            "preview-a5",
            "N-Somebody",
            "ally",
            rank="Bronze II",
            games=36,
            heroes=("Crunch", "Steel", "Grux"),
        ),
    )
    enemies = (
        _sample_player(
            "preview-e1",
            "Rook_Atlas",
            "enemy",
            rank="Gold I",
            games=403,
            heroes=("Akeron", "Riktor", "Rampage"),
            encounters=(Encounter(1, "Riktor", "against", False, kda=(9, 4, 12)),),
        ),
        _sample_player(
            "preview-e2",
            "Vellum_Ash",
            "enemy",
            rank="Diamond II",
            games=820,
            heroes=("Renna", "Scarlett", "Sparrow"),
        ),
        _sample_player(
            "preview-e3",
            "Kpr_0",
            "enemy",
            rank="Silver II",
            games=129,
            heroes=("Steel", "Muriel", "Dekker"),
        ),
        _sample_player(
            "preview-e4",
            "LadyQuillX",
            "enemy",
            rank="Gold III",
            games=264,
            heroes=("Twinblast", "Sparrow", "Murdock"),
            encounters=(
                Encounter(1, "Sparrow", "with", True, kda=(7, 2, 5)),
                Encounter(6, "Sparrow", "against", False, kda=(3, 6, 4)),
                Encounter(9, "Murdock", "with", True, kda=(11, 4, 3)),
            ),
        ),
        _sample_player(
            "preview-e5",
            "Harrowfen",
            "enemy",
            rank="UNRANKED",
            games=14,
            heroes=("Eden",),
        ),
    )
    # Who plays together: an allied duo and an enemy trio, as core.parties marks them.
    allies, enemies = _partied(allies, (0, 3), 4), _partied(enemies, (0, 1, 3), 3)
    return ViewModel(
        version=version,
        phase=phase,
        phase_at=phase_at,
        status="preview · " + phase,
        mode="5v5",
        match_id="preview",
        allies=allies,
        enemies=enemies,
    )


class PreviewTracker:
    """Stand-in for the live tracker: sample roster, cycles draft/loading/TAB/menu."""

    CYCLE = 7.0

    def __init__(self, clock=time.time):
        self.clock = clock
        self.started = clock()
        self.stage = -1
        self.view = None
        self.tab = False

    @property
    def snapshot(self):
        stage = int((self.clock() - self.started) // self.CYCLE)  # counted from the start
        index = stage % 4
        if stage != self.stage:
            self.stage = stage
            # This stage's own beginning, in whichever turn of the cycle: counted from
            # the first turn, a later one's loading screen was "long over" and hidden.
            start = self.started + stage * self.CYCLE
            if index == 0:
                phase, phase_at, self.tab = "draft", start, False
            elif index == 1:
                phase, phase_at, self.tab = "loading", start, False
            elif index == 2:
                phase, phase_at, self.tab = (
                    "loading",
                    start - (LOADING_SCREEN + 10),
                    True,
                )
            else:
                phase, phase_at, self.tab = "menu", start, False
            self.view = preview_view(phase, phase_at, index + 1)
        return self.view

    @property
    def tab_held(self):
        return self.tab

    # Where the TAB stage points, in design pixels: the second enemy row of the board.
    POINT = (PANELS["enemy"][0] + 60, ROW_TOPS[1] + ROW_H / 2)

    @property
    def pointing(self):
        return self.POINT if self.tab else None

    def board_row(self, row):
        """The sample board's row (team, index): who is on it, as a board reading says."""
        team, index = row
        players = (self.view.allies if team == "ally" else self.view.enemies) if self.view else ()
        if not 0 <= index < len(players):
            return None
        player = players[index]
        hero = ((player.main or {}).get("hero") or "").upper() or None
        return {"player_id": player.player_id, "hero": hero, "role": ROLES[index % len(ROLES)]}
