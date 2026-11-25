"""PlayerView -> plain row data for painting (no Qt, so it is easy to test).

Only what can be stated reliably is shown. Ranks, seasons, mains and the first
match come from Pred.gg (lookup.profiles); encounters from the user's own recent
matches. Win rates and KDAs of other players come only from Pred.gg's own totals
(every match they played, on the TAB card), never from a small local sample.
"""

import time
from datetime import datetime

NEW_DAYS = 60  # an account younger than this is marked new
RESET_DAYS = 21  # after a season ends, ranks say little about where a player belongs
YOUNG_DAYS = 365  # younger than this and already high: its age deserves a look
BACK_DAYS = 90  # a break this long is rare enough to say out loud
BALANCE_EVEN = 50  # team averages closer than this many points read as even
ROLES = ("OFFLANE", "JUNGLE", "MIDLANE", "SUPPORT", "CARRY")  # scoreboard order, top to bottom
TIERS = ("bronze", "silver", "gold", "platinum", "diamond", "master", "paragon")
# Pred.gg's ladder: 100 points per step from Bronze III at 0; Diamond I at 1400
# and Master at 1500 (Season 1 Split 6). Master and Paragon have no steps.
OMEDA_RANKS = ("Bronze", "Silver", "Gold", "Platinum", "Diamond", "Master")
STEPLESS = ("Master", "Paragon")
ROMAN = {1: "III", 2: "II", 3: "I"}
STEPS = len(ROMAN)


def current_rank(profile):
    """'Gold II' this season, or None when not placed or unknown."""
    return ((profile or {}).get("current") or {}).get("rank")


def rank_points(profile):
    """This season's rank as one number (100 per step) for averaging, else None."""
    current = (profile or {}).get("current") or {}
    step = rank_step(current.get("rank"))
    if step is None:
        return None
    points = current.get("points")
    if isinstance(points, (int, float)) and points >= 0:
        return points
    return step * 100


def step_vp(profile):
    """The points within this season's step (what the game calls VP), else None."""
    points, step = rank_points(profile), rank_step(current_rank(profile))
    if points is None or step is None:
        return None
    if current_rank(profile) in STEPLESS:
        return round(max(0, points - (len(OMEDA_RANKS) - 1) * STEPS * 100))
    return round(max(0, min(points - step * 100, 99)))


def rank_step(name):
    """A rank name's place on the ladder, or None for an unknown one.

    Names compare across seasons; Pred.gg's peak points do not: they can be
    null or lag behind the peak rank, and season ladders end differently.
    """
    word, _, step = (name or "").partition(" ")
    if word == "Paragon":
        return len(OMEDA_RANKS) * STEPS
    if word == "Master":
        return (len(OMEDA_RANKS) - 1) * STEPS
    steps = {roman: index for index, roman in ROMAN.items()}
    if word in OMEDA_RANKS and step in steps:
        return OMEDA_RANKS.index(word) * STEPS + steps[step] - 1
    return None


def step_rank(step):
    """A ladder place back to its name: the reverse of rank_step."""
    if step >= len(OMEDA_RANKS) * STEPS:
        return "Paragon"
    if step >= (len(OMEDA_RANKS) - 1) * STEPS:
        return "Master"
    return f"{OMEDA_RANKS[step // STEPS]} {ROMAN[step % STEPS + 1]}"


HIGH_STEP = rank_step("Diamond III")


def rank_from_points(points):
    """An average back to a rank name; VP above Master still averages to Master."""
    return step_rank(min(int(points) // 100, (len(OMEDA_RANKS) - 1) * STEPS))


def account_age(profile, now=None):
    """(days, '2.9 YRS' / '7 MOS' / '5 WKS' / '12 DAYS') since the first match Pred.gg has.

    Years carry one decimal, rounded down, so two years and eleven months reads
    2.9 rather than 2. (None, None) without a first match.
    """
    first = (profile or {}).get("first_played")
    try:
        created = datetime.fromisoformat(first.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
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
    """The name as Pred.gg spells it, else as the screen showed it."""
    return short_name(player.name)


def encounter(player):
    """The latest of the user's recent matches with this player, ready to paint."""
    if not player.encounters:
        return None
    e = player.encounters[0]
    return {
        "hero": (e.hero or "").upper() or None,
        "icon": e.icon,
        "when": "LAST MATCH" if e.matches_ago == 1 else f"{e.matches_ago} MATCHES AGO",
        "ago": e.matches_ago,
        "side": e.side,
        "result": None if e.won is None else ("WE WON" if e.won else "WE LOST"),
        "kda": "/".join(str(n) for n in e.kda) if e.kda else None,
        "met": _met_before(player.encounters),
    }


def _met_before(encounters):
    """'×3 · 2–1' for an old acquaintance: how often, then our wins and losses; else None."""
    if len(encounters) < 2:
        return None
    wins = sum(e.won is True for e in encounters)
    losses = sum(e.won is False for e in encounters)
    return f"×{len(encounters)} · {wins}–{losses}"


def peak(player, rank):
    """The past best worth showing: above the current rank, once that is known."""
    best = player.peak
    if not best or not player.profile:
        return None
    current = rank_step(rank)
    if current is None and rank != "UNRANKED":
        return None  # a rank this table does not know: nothing safe to compare
    if current is not None and rank_step(best["rank"]) <= current:
        return None
    return {"rank": best["rank"], "tier": tier(best["rank"]), "season": best["season"]}


def main(player):
    """What the player usually plays, in caps; None unless both hero and role are known.

    Never read this as their pick: about one ranked player in four is on both.
    """
    known = player.main or {}
    hero, role = known.get("hero"), known.get("role")
    if not hero or role not in ROLES:
        return None
    return {"hero": hero.upper(), "role": role, "icon": known.get("icon")}


def back_after(player, now=None):
    """'BACK AFTER 3 MOS' when this match ends a long break, else None.

    Pred.gg's last match moves on with the first game back, so this shows only once.
    """
    if player.is_me or not player.last_played:
        return None
    try:
        last = datetime.fromisoformat(player.last_played.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    days = ((now or time.time()) - last) / 86400
    if days < BACK_DAYS:
        return None
    if days < 365:
        return f"BACK AFTER {int(days // 30.44)} MOS"
    years = int(days // 365)
    return f"BACK AFTER {years} YR" + ("S" if years > 1 else "")


def off_main(data, seen):
    """'OFF ROLE' when this match differs from the main role, else None.

    A plain fact for the TAB card, not a warning: half of all ranked players are
    off their main role and they win as often as anyone (measured on 39 matches).
    The role comes from the scoreboard row, so there is none outside 5v5.
    """
    known = data.get("main")
    if not known or not seen or data.get("me"):
        return None
    return "OFF ROLE" if seen.get("role") and seen["role"] != known["role"] else None


def _plural(count, word):
    return f"{count} {word}" + ("" if count == 1 else "S")


def _just_reset(seasons, now):
    """True in the first weeks of a season, when the reset has everyone sitting low."""
    ends = [s["end"] for s in seasons if s.get("end")]
    if not ends:
        return False
    try:
        ended = datetime.fromisoformat(max(ends).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return (now or time.time()) - ended < RESET_DAYS * 86400


def verdict(player, rank, days, now=None):
    """One line on the TAB card, read from the player's seasons; None if unknown.

    Facts only: {"text": "BELOW USUAL RANK ...", "alert": True for what deserves a
    look}. The first rule that applies wins.
    """
    if player.seasons is None or not player.profile:
        return None
    ranked = [s for s in player.seasons if rank_step(s["final"]) is not None]
    if not ranked:
        return {"text": "NO RANKED HISTORY", "alert": False}
    finals = [rank_step(s["final"]) for s in ranked]  # newest first
    peaks = [rank_step(s["peak"]) for s in ranked if rank_step(s["peak"]) is not None]
    best = max(finals + peaks)
    word = step_rank(best).split(" ")[0].upper()
    current = rank_step(rank)
    gap = 0
    for season in player.seasons:
        if rank_step(season["final"]) is not None:
            break
        gap += 1
    if gap >= 2 and (current is None or best > current):
        return {"text": f"BACK AFTER {gap} SEASONS · WAS {word}", "alert": False}
    young = days is not None and days < YOUNG_DAYS
    if (young or (days is None and len(ranked) <= STEPS)) and best >= HIGH_STEP:
        seasons = _plural(len(ranked), "SEASON")
        return {"text": f"FAST CLIMB · {word} IN {seasons}", "alert": True}
    usual = sorted(finals)[len(finals) // 2]
    below = current is not None and len(finals) >= 2 and usual - current >= STEPS
    if below and not _just_reset(player.seasons, now):
        return {"text": f"BELOW USUAL RANK · USUAL {step_rank(usual).upper()}", "alert": False}
    if len(finals) >= 3:
        newest, middle, oldest = finals[:3]
        if newest >= middle >= oldest and newest - oldest >= 2:
            return {"text": "CLIMBING", "alert": False}
        if newest <= middle <= oldest and oldest - newest >= 2:
            return {"text": "FALLING", "alert": False}
        if max(finals[:3]) - min(finals[:3]) <= 2:
            return {"text": "STEADY", "alert": False}
    return None


def row(player, now=None):
    profile = player.profile or {}
    current = current_rank(profile)
    if current:
        rank = current
    elif profile:
        rank = "UNRANKED"  # not placed this season
    elif player.state == "loading":
        rank = "LOADING"
    else:
        rank = "NO DATA"  # not found on Pred.gg, or its profile would not come
    days, age = account_age(profile, now)
    high = max(rank_step(rank) or 0, rank_step((player.peak or {}).get("rank")) or 0)
    return {
        "player_id": player.player_id,
        "name": shown_name(player) or "…",
        "tag": player.tag,  # CONSOLE / UNKNOWN PLAYER: the overlay's own tag for them
        "hidden": bool(player.tag),
        # No data, and why (PlayerView.why): the row says so in place of the rank
        "unknown": player.why if player.state == "error" else None,
        "me": player.is_me,
        "team": player.team,
        "state": player.state,
        "rank": rank,
        "tier": tier(rank),
        "points": rank_points(profile),
        "peak": peak(player, rank),
        "main": main(player),
        "seasons": player.seasons,
        "verdict": verdict(player, rank, days, now),
        "vp": step_vp(profile),
        "age": age,
        "new": days is not None and days < NEW_DAYS,
        "young_high": days is not None and days < YOUNG_DAYS and high >= HIGH_STEP,
        "back": back_after(player, now),
        "today": player.today if player.today and player.today["form"] else None,
        "encounter": encounter(player),
        "stats": profile.get("stats"),
        "party": player.party,
        "days": days,
        "picked": picked_line(player.hero, profile.get("stats")),
    }


def picked_line(hero, stats):
    """The draft row's line for the hero picked: 'Steel \u00b7 143 G \u00b7 57%', 'Steel \u00b7 first
    game', or None while nothing is picked or nothing is known."""
    if not hero or not stats or not stats.get("heroes"):
        return None
    from .cards import hero_key

    on = stats["heroes"].get(hero_key(hero))
    name = hero if not hero.isupper() else hero.title()  # the catalog's spelling if known
    if not on or not on["played"]:
        return f"{name} \u00b7 first game"
    if on["played"] < FEW:
        return f"{name} \u00b7 {on['played']} G"
    return f"{name} \u00b7 {on['played']} G \u00b7 {round(100 * on['won'] / on['played'])}%"


# Few matches won at a rate few reach: a fresh account of a strong player, or a
# bought or shared one. Stated as numbers, not as an accusation.
FRESH_GAMES, FRESH_MIN, FRESH_RATE = 150, 20, 0.62
# One hero, lately or for good: 6 of the last 10, or 35% of 50 matches or more.
LATELY_ON, TRICK_SHARE, TRICK_MIN = 6, 0.35, 50
# Games today: this sitting, not the calendar day (midnight is no break, and their
# midnight is not mine): matches back from now, each ending within PLAY_GAP of the next.
PLAY_GAP = 2 * 60 * 60


def _hero_shown(name):
    """A hero in capitals, as the game spells it ('Lt. Belica' as 'LT. BELICA'):
    core.shown_stats has put the game's names in."""
    return (name or "").upper().strip()


def habits(data, now=None):
    """Short facts about how this player plays, for the TAB card, each (text, alert):
    a fresh account winning a lot, one hero lately or for good, the games played today.
    Pred.gg's own counts; none for me."""
    stats = data.get("stats")
    if not stats or data.get("me"):
        return []
    out = []
    total = stats.get("total") or 0
    won = sum(w for _, w in stats.get("roles", {}).values())
    if FRESH_MIN <= total < FRESH_GAMES and won / total >= FRESH_RATE:
        out.append((f"FEW GAMES, WINS A LOT \u00b7 {round(100 * won / total)}% IN {total}", True))
    played = stats.get("played") or []
    heroes = [h for h, _ in played if h]
    lately = max(set(heroes), key=heroes.count) if heroes else None
    if lately and heroes.count(lately) >= LATELY_ON:
        out.append(
            (
                f"MOSTLY {_hero_shown(lately)} \u00b7 {heroes.count(lately)} OF LAST {len(played)}",
                False,
            )
        )
    elif total >= TRICK_MIN and stats.get("heroes"):
        top = max(stats["heroes"].items(), key=lambda kv: kv[1]["played"])
        share = top[1]["played"] / total
        if share >= TRICK_SHARE:
            name = top[1].get("name") or top[0]
            out.append(
                (
                    f"ONE-TRICK {_hero_shown(name)} \u00b7 {round(100 * share)}% OF {total} GAMES",
                    False,
                )
            )
    form = stats.get("form") or []
    games = wins = 0
    later = stats.get("at") or now or time.time()  # when the profile came: steady
    for i, (_, ended) in enumerate(played):  # newest first
        try:
            at = datetime.fromisoformat(ended.replace("Z", "+00:00")).timestamp()
        except (AttributeError, ValueError):
            break
        if later - at > PLAY_GAP:
            break  # a break: the sitting began after it
        games += 1
        wins += bool(i < len(form) and form[i])
        later = at
    if games >= 2:
        more = "+" if games == len(played) else ""
        out.append((f"{games}{more} GAMES TODAY \u00b7 {wins}\u2013{games - wins}", False))
    return out


def party_label(party):
    """DUO, TRIO, 4-STACK, 5-STACK."""
    return {2: "DUO", 3: "TRIO"}.get(party["size"], f"{party['size']}-STACK")


def party_line(party):
    """The card's party line: 'DUO WITH ORBITFOX \u00b7 214 GAMES TOGETHER \u00b7 56% WON',
    or, when only their last matches tell, '... \u00b7 4 OF THEIR LAST 10 TOGETHER'."""
    names = ", ".join(party["with"])
    head = f"{party_label(party)} WITH {names}"
    if party.get("won") is None:
        return f"{head} \u00b7 {party['games']} OF THEIR LAST 10 TOGETHER"
    rate = round(100 * party["won"] / party["games"]) if party["games"] else 0
    return f"{head} \u00b7 {party['games']} GAMES TOGETHER \u00b7 {rate}% WON"


FEW = 5  # games on a hero or role below which a win rate says nothing: games alone


def _rate(played, won):
    if not played:
        return None
    return (
        f"{played} GAMES" if played < FEW else f"{played} GAMES \u00b7 {round(100 * won / played)}%"
    )


def plays(data, seen):
    """What the TAB card says about how this player plays: {"hero": text, "role":
    text, "form": [won, ...] newest first}, each None when unknown; None for nothing.

    Pred.gg's totals over every match they played: on this match's hero (from the
    scoreboard row) with KDA, on this match's role, and their last matches.
    """
    stats = data.get("stats")
    if not stats or data.get("me"):
        return None
    seen = seen or {}
    hero = role = None
    if seen.get("hero") and stats["heroes"]:
        from .cards import hero_key

        on = stats["heroes"].get(hero_key(seen["hero"]))
        if on is None or not on["played"]:
            hero = "FIRST GAME"
        else:
            kda = (on["kills"] + on["assists"]) / max(on["deaths"], 1)
            hero = f"{_rate(on['played'], on['won'])} \u00b7 KDA {kda:.1f}"
    if seen.get("role") and stats["roles"]:
        played, won = stats["roles"].get(seen["role"], (0, 0))
        role = _rate(played, won) or "FIRST GAME"
    form = stats["form"] or None
    if not (hero or role or form):
        return None
    return {"hero": hero, "role": role, "form": form}


def streak(form):
    """'3 WINS IN A ROW' / '2 LOSSES IN A ROW' for a run of 3 or more, else None."""
    if not form:
        return None
    run = next((i for i, won in enumerate(form) if won != form[0]), len(form))
    if run < 3:
        return None
    return f"{run} {'WINS' if form[0] else 'LOSSES'} IN A ROW"


def team_rank(rows):
    """Average current rank of a team ('GOLD II'), when at least three are placed
    (every player in teams of two or three)."""
    points = [r["points"] for r in rows if r["points"] is not None]
    enough = min(3, len(rows))
    return rank_from_points(sum(points) / len(points)) if points and len(points) >= enough else None


def team_balance(allies, enemies):
    """The enemy team against ours on average points: 'EVEN', '+120 VP', '\u2212120 VP'.

    None unless both teams have an average (see team_rank).
    """
    if team_rank(allies) is None or team_rank(enemies) is None:
        return None

    def average(rows):
        points = [r["points"] for r in rows if r["points"] is not None]
        return sum(points) / len(points)

    margin = average(allies) - average(enemies)
    if abs(margin) < BALANCE_EVEN:
        return {"text": "EVEN", "side": None}
    side = "ally" if margin > 0 else "enemy"
    sign = "\u2212" if side == "ally" else "+"
    return {"text": f"{sign}{round(abs(margin))} VP", "side": side}


# Field groups of a row that change together; the window crossfades each on its own.
GROUPS = {
    "name": ("name", "tag", "hidden"),
    "meta": ("age", "new", "young_high", "back", "main", "picked"),
    "rank": ("rank", "tier", "today", "unknown"),
    "peak": ("peak", "today"),
    "main": ("main",),
    "vp": ("vp", "rank", "today"),
    "intel": ("encounter", "me", "today"),
}


def changed_groups(old, new):
    """Names of the field groups that differ between two rows of the same player."""
    return {group for group, keys in GROUPS.items() if any(old[k] != new[k] for k in keys)}
