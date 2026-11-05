"""Player cards from Pred.gg: rating, form, mains and per-hero numbers.

One Pred.gg query per player feeds the card. It needs a signed-in Pred.gg
account: since 31 October 2026 Pred.gg serves player statistics to no guest.
"""

import re

from .rows import rank_step

CARD_QUERY = """query($id:ID!){player(by:{id:$id}){
  id name isNameConsole
  ratings{points percentile unranked rank{name} rating{name}}
  generalStatistic{result{matchesPlayed matchesWon totalKills totalDeaths totalAssists}}
  roleStatistics{results{role matchesPlayed matchesWon}}
  heroStatistics{results{hero{slug name} matchesPlayed matchesWon
    totalKills totalDeaths totalAssists}}
  matchesPaginated(limit:10){results{match{id endTime gameMode winningTeam
    matchPlayers{player{id} team role hero{slug name} kills deaths assists}}}}
}}"""


def _rate(won, played):
    return round(won / played, 3) if played else None


def _kda(kills, deaths, assists):
    return round((kills + assists) / max(deaths, 1), 2)


def _season_order(rating):
    """'Season 1 - Split 5' -> (1, 5); Pred.gg does not return seasons in order."""
    return tuple(int(n) for n in re.findall(r"\d+", (rating.get("rating") or {}).get("name") or ""))


def _rating(ratings):
    """The newest season/split the player has a rating in."""
    if not ratings:
        return None
    last = max(ratings, key=_season_order)
    return {
        "season": (last.get("rating") or {}).get("name"),
        "rank": None if last.get("unranked") else (last.get("rank") or {}).get("name"),
        "points": last.get("points"),
        "percentile": last.get("percentile"),
        "unranked": bool(last.get("unranked")),
    }


def _recent(player_id, rows):
    games = []
    for row in rows:
        match = row["match"]
        me = next((p for p in match["matchPlayers"] if p["player"]["id"] == player_id), None)
        if me is None:
            continue
        games.append(
            {
                "match_id": match["id"],
                "ended_at": match["endTime"],
                "mode": match["gameMode"],
                "won": me["team"] == match["winningTeam"],
                "hero": (me.get("hero") or {}).get("name"),
                "role": me.get("role"),
                "kda": [me.get("kills"), me.get("deaths"), me.get("assists")],
            }
        )
    return games


def build(player, *, display=None):
    """Summarize a CARD_QUERY player result into the overlay card."""
    pid = player["id"]
    general = (player.get("generalStatistic") or {}).get("result") or {}
    played, won = general.get("matchesPlayed") or 0, general.get("matchesWon") or 0
    roles = sorted(
        (player.get("roleStatistics") or {}).get("results") or [],
        key=lambda r: r["matchesPlayed"],
        reverse=True,
    )
    heroes = sorted(
        (player.get("heroStatistics") or {}).get("results") or [],
        key=lambda h: h["matchesPlayed"],
        reverse=True,
    )

    display = display or {}

    def label(name):
        return display.get(name, name)

    def hero_line(h):
        return {
            "hero": label(h["hero"]["name"]),
            "games": h["matchesPlayed"],
            "winrate": _rate(h["matchesWon"], h["matchesPlayed"]),
            "kda": _kda(h["totalKills"], h["totalDeaths"], h["totalAssists"]),
        }

    recent = _recent(pid, (player.get("matchesPaginated") or {}).get("results") or [])
    for game in recent:
        game["hero"] = label(game["hero"])
    return {
        "player_id": pid,
        "name": player.get("name"),
        "console": bool(player.get("isNameConsole")),
        "rating": _rating(player.get("ratings")),
        "overall": {
            "games": played,
            "winrate": _rate(won, played),
            "kda": _kda(
                general.get("totalKills") or 0,
                general.get("totalDeaths") or 0,
                general.get("totalAssists") or 0,
            )
            if played
            else None,
        },
        "roles": [
            {
                "role": r["role"],
                "games": r["matchesPlayed"],
                "winrate": _rate(r["matchesWon"], r["matchesPlayed"]),
            }
            for r in roles[:2]
        ],
        "mains": [hero_line(h) for h in heroes[:3]],
        "heroes": [hero_line(h) for h in heroes],  # every hero, for the one on the scoreboard
        "recent": recent,
        "form": "".join("W" if g["won"] else "L" for g in recent),
        "source": "predgg",
    }


def hero_names(heroes):
    """Map internal hero names (CatalogClient.heroes()) to the names shown in game."""
    return {h["name"]: (h.get("data") or {}).get("displayName") or h["name"] for h in heroes}


_PEAK_FIELDS = "ratings{peakPoints peakRank{name} unranked rating{name endTime}}"


def peak_query(player_ids):
    """(query, variables) for every player's seasons in one request. Pred.gg serves
    ratings without an account; a player it does not know comes back as null."""
    ids = list(player_ids)
    declared = ",".join(f"$p{i}:ID!" for i in range(len(ids)))
    players = " ".join(f"p{i}:player(by:{{id:$p{i}}}){{{_PEAK_FIELDS}}}" for i in range(len(ids)))
    return f"query({declared}){{{players}}}", {f"p{i}": pid for i, pid in enumerate(ids)}


def best_peak(player):
    """The highest rank reached in a finished season, or None.

    Returns {"rank": "Diamond II", "season": "Season 1 - Split 2"}. The running
    season is left out: the official profile already shows it. Ranks are compared
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
