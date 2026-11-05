"""PlayerView -> plain row data for painting (no Qt, so it is easy to test).

Only what can be stated reliably is shown. Rank, level and account age come
from the official profile; encounters from the user's own recent matches.
Win rate, KDA, games, mains and form exist only with Pred.gg cards (``stats``);
without them those fields are absent, never filled from a small local sample.
"""

import time

NEW_DAYS = 60  # an account younger than this is marked new
STATS_MIN = 20  # Pred.gg games before a win rate or KDA is shown
HERO_MIN = 5  # games on a hero before its win rate is shown
TIERS = ("bronze", "silver", "gold", "platinum", "diamond", "paragon")
# The official profile gives the rank as numbers: ``division`` is the rank
# (1 Bronze, 2 Silver, 3 Gold, 4 Platinum) and ``tier`` the step within it,
# 1 = III (lowest) to 3 = I, each step 100 VP. Checked on 31 Oct 2026 against
# loading-screen border colours and Pred.gg totals: "697, Gold III" = division
# 3, tier 1, vp 97 and "709, Gold II" = division 3, tier 2, vp 9.
# Diamond and Paragon are assumed to follow; Paragon has no steps.
OMEDA_RANKS = ("Bronze", "Silver", "Gold", "Platinum", "Diamond", "Paragon")
ROMAN = {1: "III", 2: "II", 3: "I"}
STEPS = len(ROMAN)


def omeda_rank(profile):
    """Official public profile -> 'Gold II', or None when not placed or unknown."""
    if not profile or not profile.get("placed"):
        return None
    if profile.get("paragon"):
        return "Paragon"
    index = (profile.get("division") or 0) - 1
    if not 0 <= index < len(OMEDA_RANKS):
        return "RANKED"  # placed, in a rank this table does not know
    name = OMEDA_RANKS[index]
    step = ROMAN.get(profile.get("tier"))
    return name if name == "Paragon" or not step else f"{name} {step}"


def rank_points(profile):
    """A placed rank as one number (100 per step) for averaging, else None."""
    if omeda_rank(profile) == "Paragon" and profile.get("paragon"):
        return len(OMEDA_RANKS[:-1]) * STEPS * 100 + max(0, min(profile.get("vp") or 0, 99))
    if omeda_rank(profile) in (None, "RANKED") or profile.get("tier") not in ROMAN:
        return None
    division, step = profile["division"], profile["tier"]
    vp = max(0, min(profile.get("vp") or 0, 99))  # negative VP is still this tier
    return ((division - 1) * STEPS + step - 1) * 100 + vp


def rank_from_points(points):
    step_index = int(points) // 100
    division, step = step_index // STEPS + 1, step_index % STEPS + 1
    return omeda_rank({"placed": True, "division": division, "tier": step})


def account_age(profile, now=None):
    """(days, '2.9 YRS' / '7 MOS' / '5 WKS' / '12 DAYS') from the creation time.

    Years carry one decimal, rounded down, so two years and eleven months reads
    2.9 rather than 2. (None, None) without a creation time.
    """
    created = (profile or {}).get("created")
    if created is None:
        return None, None
    seconds = max(0.0, (now or time.time()) - created)
    days = int(seconds // 86400)
    if days >= 365:
        tenths = int(seconds * 10 // (365.25 * 86400))
        return days, "1 YR" if tenths == 10 else f"{tenths // 10}.{tenths % 10} YRS"
    if days >= 60:
        return days, f"{int(days // 30.44)} MOS"
    if days >= 14:
        return days, f"{days // 7} WKS"
    return days, f"{days} DAY{'S' if days != 1 else ''}"


def tier(rank):
    """'Gold III' -> 'gold'; unknown or missing -> 'none'."""
    word = (rank or "").split(" ")[0].lower()
    return word if word in TIERS else "none"


def short_name(name):
    """Name#code -> NAME."""
    return (name or "").rsplit("#", 1)[0] if name else None


def shown_name(player):
    """The name the game shows: the one on the player's current platform, else the official one."""
    return (player.profile or {}).get("shown_name") or short_name(player.name)


def encounter(player):
    """The latest of the user's recent matches with this player, ready to paint."""
    if not player.encounters:
        return None
    e = player.encounters[0]
    return {
        "hero": (e.hero or "").upper() or None,
        "when": "LAST MATCH" if e.matches_ago == 1 else f"{e.matches_ago} MATCHES AGO",
        "side": e.side,
        "result": None if e.won is None else ("WE WON" if e.won else "WE LOST"),
    }


def stats(card):
    """Pred.gg numbers, or None without a Pred.gg card."""
    if not card or card.get("source") != "predgg":
        return None
    overall = card.get("overall") or {}
    games = overall.get("games") or 0
    enough = games >= STATS_MIN
    winrate = overall.get("winrate")
    roles = card.get("roles") or []
    return {
        "games": games,
        "wr": round(winrate * 100) if enough and winrate is not None else None,
        "kda": overall.get("kda") if enough else None,
        "heroes": [
            (
                m["hero"].upper(),
                round(m["winrate"] * 100)
                if m.get("winrate") is not None and (m.get("games") or 0) >= HERO_MIN
                else None,
            )
            for m in (card.get("mains") or [])[:3]
            if m.get("hero")
        ],
        "hero_stats": {
            m["hero"].upper(): m
            for m in (card.get("heroes") or card.get("mains") or [])
            if m.get("hero")
        },
        "form": (card.get("form") or "")[::-1],  # oldest left, newest right
        "role": roles[0]["role"] if roles else None,
    }


def row(player, now=None):
    profile = player.profile or {}
    official = omeda_rank(profile)
    if official:
        rank = official
    elif profile:
        rank = "UNRANKED"  # the official profile says not placed this season
    elif player.state == "loading":
        rank = "LOADING"
    else:
        rank = "NO DATA"  # the lookup failed or the server sent no profile for them
    days, age = account_age(profile, now)
    return {
        "player_id": player.player_id,
        "name": shown_name(player) or "…",
        "me": player.is_me,
        "team": player.team,
        "state": player.state,
        "rank": rank,
        "tier": tier(rank),
        "points": rank_points(profile),
        "vp": profile.get("vp") if rank_points(profile) is not None else None,
        "level": profile.get("level"),
        "age": age,
        "new": days is not None and days < NEW_DAYS,
        "encounter": encounter(player),
        "stats": stats(player.card),
        "stats_state": player.stats_state,
    }


def team_rank(rows):
    """Average official rank of a team ('GOLD II'), when at least three are placed
    (every player in teams of two or three)."""
    points = [r["points"] for r in rows if r["points"] is not None]
    enough = min(3, len(rows))
    return rank_from_points(sum(points) / len(points)) if points and len(points) >= enough else None


def team_winrate(rows):
    known = [r["stats"]["wr"] for r in rows if r["stats"] and r["stats"]["wr"] is not None]
    return round(sum(known) / len(known)) if len(known) >= 3 else None


# Field groups of a row that change together; the window crossfades each on its own.
GROUPS = {
    "name": ("name",),
    "meta": ("level", "age", "new"),
    "rank": ("rank", "tier"),
    "vp": ("vp", "rank"),
    "intel": ("encounter", "me"),
    "stats": ("stats", "stats_state"),
}


def changed_groups(old, new):
    """Names of the field groups that differ between two rows of the same player."""
    return {group for group, keys in GROUPS.items() if any(old[k] != new[k] for k in keys)}
