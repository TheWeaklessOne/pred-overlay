"""Pred.gg's answers made usable: hero names and portraits, past seasons, the best
peak and the main hero and role. The queries themselves are in lookup.py.
"""

from .rows import rank_step


def hero_names(heroes):
    """Map internal hero names (CatalogClient.heroes()) to the names shown in game."""
    return {h["name"]: (h.get("data") or {}).get("displayName") or h["name"] for h in heroes}


def hero_icons(heroes):
    """Map internal hero names to their portrait's name in Pred.gg's assets."""
    icons = {h["name"]: (h.get("data") or {}).get("icon") for h in heroes}
    return {name: icon for name, icon in icons.items() if icon}


_MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


def _whole(value):
    return int(value) if isinstance(value, (int, float)) else None


def _month(stamp):
    """'2025-11-02T17:13:06Z' -> 'NOV 25'; None for anything else."""
    try:
        year, month = stamp[2:4], _MONTHS[int(stamp[5:7]) - 1]
    except (TypeError, ValueError, IndexError):
        return None
    return f"{month} {year}"


def _known(rank):
    """A rank on our ladder, else None: a new rank name must not reach the drawing."""
    return rank if rank_step(rank) is not None else None


def _top_percent(value):
    """Pred.gg's percentile is the share of players above: 0.018 is the top 2%."""
    return max(1, round(value * 100)) if isinstance(value, (int, float)) else None


def season_history(player, catalog=()):
    """A player's finished seasons, newest first; None if Pred.gg does not know them.

    The running season is left out (lookup.current_rank reads it). ``catalog`` lists
    all seasons: one skipped after the player's first shows as unranked, so a break
    is visible.
    """
    if not player:
        return None
    played = {}
    for entry in player.get("ratings") or []:
        season = entry.get("rating") or {}
        if not season.get("name") or not season.get("endTime"):
            continue
        unranked = bool(entry.get("unranked"))
        played[season["name"]] = {
            "name": season["name"],
            "end": season["endTime"],
            "final": None if unranked else _known((entry.get("rank") or {}).get("name")),
            "peak": None if unranked else _known((entry.get("peakRank") or {}).get("name")),
            "ranking": _whole(entry.get("ranking")),
            "peak_ranking": _whole(entry.get("peakRanking")),
            "percentile": _top_percent(entry.get("percentile")),
            "peak_date": _month(entry.get("peakTimestamp")),
            "unranked": unranked,
        }
    first = min((s["end"] for s in played.values()), default=None)
    for season in catalog or ():
        name, end = (season or {}).get("name"), (season or {}).get("endTime")
        if name and end and name not in played and first and end > first:
            played[name] = {
                "name": name,
                "end": end,
                "final": None,
                "peak": None,
                "ranking": None,
                "peak_ranking": None,
                "percentile": None,
                "peak_date": None,
                "unranked": True,
            }
    return tuple(sorted(played.values(), key=lambda s: s["end"], reverse=True))


def main_of(player):
    """Pred.gg's main hero and role: {"hero": internal name, "role": "CARRY"}, or None."""
    hero = ((player or {}).get("favHero") or {}).get("name")
    role = (player or {}).get("favRole")
    return {"hero": hero, "role": role} if hero or role else None


def hero_key(name):
    """A hero name to compare by: Pred.gg's "LtBelica" and the board's "LT. BELICA" alike."""
    return "".join(c for c in (name or "").casefold() if c.isalnum())


def _count(value):
    return int(value) if isinstance(value, (int, float)) and value >= 0 else 0


def play_stats(player):
    """Pred.gg's statistics of a player, all modes and time: {"roles": {role: (played,
    won)}, "heroes": {hero_key: {"played", "won", "kills", "deaths", "assists"}}, "form":
    [won, ...] of the last matches, newest first, "recent": [(match ID, team), ...] of
    the same matches, "played": [(hero name or None, end time ISO or None), ...] of the
    same matches, "mates": {player ID: (played, won)} the players they played most on
    their side, "total": matches played}, or None when there are none."""
    player = player or {}
    roles = {}
    for row in (player.get("roleStatistics") or {}).get("results") or ():
        if isinstance(row, dict) and row.get("role"):
            roles[row["role"]] = (_count(row.get("matchesPlayed")), _count(row.get("matchesWon")))
    heroes = {}
    for row in (player.get("heroStatistics") or {}).get("results") or ():
        name = ((row or {}).get("hero") or {}).get("name") if isinstance(row, dict) else None
        if name:
            heroes[hero_key(name)] = {
                "name": name,
                "played": _count(row.get("matchesPlayed")),
                "won": _count(row.get("matchesWon")),
                "kills": _count(row.get("totalKills")),
                "deaths": _count(row.get("totalDeaths")),
                "assists": _count(row.get("totalAssists")),
            }
    form, recent, played = [], [], []
    for row in (player.get("form") or {}).get("results") or ():
        team = (row or {}).get("team") if isinstance(row, dict) else None
        match = ((row or {}).get("match") or {}) if team else {}
        if team and match.get("winningTeam"):
            form.append(team == match["winningTeam"])
            played.append((((row.get("hero") or {}).get("name")), match.get("endTime")))
        if team and match.get("id"):
            recent.append((match["id"], team))
    mates = {}
    for row in (player.get("mates") or {}).get("results") or ():
        pid = ((row or {}).get("player") or {}).get("id") if isinstance(row, dict) else None
        if pid:
            mates[pid] = (_count(row.get("matchesPlayed")), _count(row.get("matchesWon")))
    if not (roles or heroes or form):
        return None
    total = sum(played for played, _ in roles.values())
    return {
        "roles": roles,
        "heroes": heroes,
        "form": form,
        "recent": recent,
        "played": played,
        "mates": mates,
        "total": total,
    }


def best_peak(player):
    """The highest rank reached in a finished season, or None.

    Returns {"rank": "Diamond II", "season": "Season 1 - Split 2"}. The running
    season is left out: lookup.current_rank reads it. Ranks are compared
    by name: peak points can be null or lag behind the peak rank (see rank_step).
    """
    best, best_key = None, None
    for entry in (player or {}).get("ratings") or []:
        season = entry.get("rating") or {}
        rank = (entry.get("peakRank") or {}).get("name")
        step = rank_step(rank)
        if entry.get("unranked") or not season.get("endTime") or step is None:
            continue
        key = (step, entry.get("peakPoints") or 0)
        if best_key is None or key > best_key:
            best = {"rank": rank, "season": season.get("name") or ""}
            best_key = key
    return best
