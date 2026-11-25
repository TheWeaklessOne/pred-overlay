"""Pred.gg reads for names read off the screen: who they are, how they rank, whom I met.

Pure query builders and parsers (no I/O): lanes.PredggIO sends the documents.
Every document puts all players of one phase into one request with aliases.
"""

import re
import unicodedata
from datetime import datetime, timezone

from .cards import _whole, best_peak, main_of, play_stats, season_history
from .charset import CHARSET, GAP
from .scoreboard import _TWIN_CAPS, _key

HIDDEN = "HIDDEN"  # what Pred.gg's match lists say for a name it does not show
SEARCH_LIMIT = 6  # candidates per name: same-named accounts are rare
_PLAYER = "id name lastPlayedAt"


def name_key(name):
    """What two spellings of one name share (case, symbols and OCR look-alikes folded)."""
    return _key(name)


_DASHES = str.maketrans({"-": "_", "—": "_", "–": "_"})
# Latin letters the model writes for their Cyrillic twins (TWINS, below), back to Cyrillic
# for Pred.gg's search, which keeps the scripts apart: Russian letters, and in a reading
# that has Cyrillic letters the Ukrainian and Belarusian "і" too.
_TO_CYRILLIC_CHARS = "ABEKMHOPCTXaeopcxy"
_TO_CYRILLIC = str.maketrans(_TO_CYRILLIC_CHARS, "АВЕКМНОРСТХаеорсху")
_TO_CYRILLIC_MIXED = str.maketrans(_TO_CYRILLIC_CHARS + "Ii", "АВЕКМНОРСТХаеорсхуІі")
# A spelling that starts with PIECE is searched by its longest run without twin letters
# (``_twin_free``) and matched as the whole reading: U+2063 draws nothing and every key
# drops it.
PIECE = "⁣"
_TWIN_LETTERS = frozenset(_TO_CYRILLIC_CHARS + "АВЕКМНОРСТХаеорсху")
_LOWER_I = re.compile(r"(?<=[a-z])I")  # "pIayer": an l, read as a capital i
_UPPER_L = re.compile(r"(?<=[A-Z])l(?=[A-Z]|$)")  # "XlX": a capital i, read as an l
_CAPITAL_I = re.compile(r"(?<=[A-Z])I(?=[a-z])")  # "AIpha": an l, read as a capital i


def search_spellings(text):
    """Other spellings of one OCR reading that Pred.gg's search may need, best first.

    The search keeps "_", "-" and letter case apart from look-alikes, and Windows OCR
    reads every "_" as "-" or "—"; in Barlow (the loading screen) "l" and "I" are drawn
    nearly alike. Each comes back with its key unchanged, so ``pick`` still holds.
    """
    text = text or ""
    out = []
    underscored = text.translate(_DASHES)
    dashed = text.replace("—", "-").replace("–", "-")
    swapped = _UPPER_L.sub("I", _CAPITAL_I.sub("l", _LOWER_I.sub("l", text)))
    both = _UPPER_L.sub("I", _CAPITAL_I.sub("l", _LOWER_I.sub("l", underscored)))
    cyrillic = text.translate(_TO_CYRILLIC)
    # The model writes a Cyrillic name's Latin-looking letters in Latin: with Cyrillic
    # letters in it, the Cyrillic spelling first (Russian, then with "і" for an "i" read
    # in it: a Latin "i" in "gribni_Кот" stays); made of look-alikes only, last.
    ukrainian = text.translate(_TO_CYRILLIC_MIXED)
    mixed = any("Ѐ" <= c <= "ӿ" for c in text) and ukrainian != text
    russian = (cyrillic,) if cyrillic != text else ()
    if ukrainian != cyrillic:
        russian += (ukrainian,)
    twins = (
        cyrillic != text
        and sum(c.isalpha() for c in text) >= 3
        and all(c in _TO_CYRILLIC_CHARS or not c.isalpha() for c in text)
    )
    # A name may mix the scripts ("ivan_Кот" with a Latin "o"): only its letters with no
    # twin say which script they are in, so the longest run of them is searched too.
    piece = PIECE + text if mixed and _twin_free(text) else None
    for spelling in (
        (russian if mixed else ())
        + ((piece,) if piece else ())
        + (underscored, swapped, both, dashed)
        + ((cyrillic,) if twins and not mixed else ())
    ):
        if spelling != text and spelling not in out:
            out.append(spelling)
    return out


# Letters drawn alike in Latin, Cyrillic and Greek (the game draws the other two in Noto
# Sans): the model writes the Latin one, as a single shape needs a single label.
TWINS = str.maketrans(
    "АВЕКМНОРСТХЅІЈаеорсхуіјѕΑΒΕΖΗΙΚΜΝΟΡΤΥΧο", "ABEKMHOPCTXSIJaeopcxyijsABEZHIKMNOPTYXo"
)


def ignorable(char):
    """What draws nothing in a name: format characters (ZWJ...), variation selectors,
    tags and combining marks (on an NFC name: those no precomposed letter took in)."""
    o = ord(char)
    return (
        unicodedata.category(char) == "Cf"
        or unicodedata.combining(char) != 0
        or 0xFE00 <= o <= 0xFE0F
        or 0xE0000 <= o <= 0xE0FFF
    )


def _unit(char):
    """One character as the name model writes it: itself; the Latin twin of a Cyrillic or
    Greek letter; the base of an accented letter; else GAP (emoji, ™, CJK, Arabic, and
    superscript, fullwidth or circled letters, which the screen draws in other shapes)."""
    char = char.translate(TWINS)
    if char in CHARSET or char.isspace():
        return char
    plain = "".join(c for c in unicodedata.normalize("NFD", char) if not unicodedata.combining(c))
    plain = plain.translate(TWINS)
    return plain if len(plain) == 1 and plain in CHARSET else GAP


def units(text):
    """A name as the model writes it: NFC, ignorables left out, each character a unit."""
    text = unicodedata.normalize("NFC", text or "")
    return "".join(_unit(c) for c in text if not ignorable(c))


def written(text):
    """A name as the model would write it: ``units`` with each run of GAPs (spaces
    between them too) one GAP, as its labels were made."""
    return _GAP_RUN.sub(GAP, units(text)).strip()


_GAP_RUN = re.compile(GAP + r"(?:\s*" + GAP + r")+")


def gap_key(text, edges=True):
    """``name_key`` with each run of characters the model cannot write kept as one GAP.
    A reading with GAP marks and a Pred.gg name compare equal under it; ``edges=False``
    drops the runs at either end (a small emoji there is easily missed)."""
    joined = GAP.join(_key(piece) for piece in units(text).split(GAP))
    while GAP + GAP in joined:
        joined = joined.replace(GAP + GAP, GAP)
    return joined if edges else joined.strip(GAP)


def unwritable(text):
    """True for a name with something the model writes as GAP (an emoji, a symbol...)."""
    return GAP in units(text)


def search_text(reading):
    """What Pred.gg's search gets for a reading: the reading itself, or for one with GAP
    marks its longest piece (the search matches a part of a name); "" when no piece is
    long enough to search by."""
    reading = reading or ""
    if reading.startswith(PIECE):
        return _twin_free(reading[len(PIECE) :])
    if GAP not in reading:
        return reading
    piece = max((p.strip() for p in reading.split(GAP)), key=lambda p: len(_key(p)))
    return piece if len(_key(piece)) >= 3 else ""


def _twin_free(text):
    """The longest run of a reading with no twin letter and no GAP (at least 3 letters or
    digits), else "": a part of the name whatever script its look-alikes are in."""
    runs, run = [], ""
    for char in text:
        if char in _TWIN_LETTERS or char == GAP:
            runs.append(run)
            run = ""
        else:
            run += char
    runs.append(run)
    best = max((r.strip() for r in runs), key=lambda r: len(_key(r)))
    return best if len(_key(best)) >= 3 else ""


def search_query(names):
    """(document, variables) searching every name at once (``search_text`` of each)."""
    names = [search_text(n) for n in names]
    declared = ",".join(f"$n{i}:String!" for i in range(len(names)))
    parts = " ".join(
        f"s{i}:playersPaginated(filter:{{search:$n{i}}},limit:{SEARCH_LIMIT})"
        f"{{results{{{_PLAYER}}}}}"
        for i in range(len(names))
    )
    return f"query({declared}){{{parts}}}", {f"n{i}": n for i, n in enumerate(names)}


def _stamp(value):
    return value if isinstance(value, str) else ""


def _spelling(text, capitals):
    """A spelling to compare as the screen shows it: spaces, case and script twins aside.
    Twins are folded by shape, so on a screen in mixed case lower-case Cyrillic "т" stays
    apart from Latin "t"; on one in ``capitals`` (draft, TAB) "Т" and "T" are one."""
    text = "".join(text.replace(PIECE, "").split())
    if capitals:
        return text.upper().translate(_TWIN_CAPS)
    return text.translate(TWINS).casefold()


def _marks(name):
    """What a name has that the model writes as GAP, run by run ("Name★" -> ("★",))."""
    runs, run = [], ""
    for char in unicodedata.normalize("NFC", name):
        if ignorable(char):
            continue
        if _unit(char) == GAP:
            run += char
        elif run:
            runs.append(run)
            run = ""
    return tuple(runs + [run] if run else runs)


def pick(reading, candidates):
    """The candidate a screen reading names, or None when none is a safe match.

    Only an exact name (folded with ``name_key``) counts; of several, the one who
    played last. A reading with GAP marks matches a name with something unwritable in
    the same places (``gap_key``). A reading cut off with an ellipsis matches a single
    name it starts.
    """
    reading = reading or ""
    key = name_key(reading.replace("...", "").replace("…", ""))
    if len(key) < 2:
        return None
    rows = [c for c in candidates or () if isinstance(c, dict) and c.get("id") and c.get("name")]
    exact = []
    marked = unwritable(reading)  # a GAP, or a known name's emoji (namereader.choose)
    if not marked:
        exact = [c for c in rows if name_key(c["name"]) == key]
        # "Name" and "Name★" share the key; a reading with no mark cannot tell which of
        # them it saw (the model may miss a small emoji, Windows OCR drops it).
        if any(unwritable(c["name"]) for c in exact) and not all(
            unwritable(c["name"]) for c in exact
        ):
            return None
    gapped = gap_key(reading)
    if not exact and len(gapped.replace(GAP, "")) >= 3:
        # A name with an emoji, a symbol or another script in it: the model wrote GAP for
        # that part; the rest must match, gaps in place. Gaps at the edges too, first; a
        # name that only matches without them (a mark the model added or missed there, or
        # Windows OCR dropped) only when it is the one such name.
        if marked:
            exact = [c for c in rows if gap_key(c["name"]) == gapped]
        if not exact:
            inner = gap_key(reading, edges=False)
            loose = [c for c in rows if gap_key(c["name"], edges=False) == inner]
            exact = loose if len(loose) == 1 else []
    if not exact and reading.rstrip().endswith(("...", "…")) and len(key) >= 4:
        starts = [c for c in rows if name_key(c["name"]).startswith(key)]
        exact = starts if len(starts) == 1 else []
    if not exact:
        return None
    # "Name★" and "Name♥" read alike: which of them the screen showed is unknown.
    if len({_marks(c["name"]) for c in exact}) > 1:
        return None
    # The same spelling beats a name that only folds to the same key.
    capitals = not any(c.islower() for c in reading)
    spelled = _spelling(reading, capitals)
    same = [c for c in exact if _spelling(c["name"], capitals) == spelled]
    best = max(same or exact, key=lambda c: _stamp(c.get("lastPlayedAt")))
    return {"player_id": best["id"], "name": best["name"]}


def same_name(reading, name):
    """A reading is this exact name as the screen shows it: the same gap key, edges and
    all, and the same spelling (case and script twins aside, case kept on a screen in
    mixed case). Stricter than ``pick``'s key match: for names taken with no search."""
    reading, name = reading or "", name or ""
    if not reading or gap_key(reading) != gap_key(name):
        return False
    capitals = not any(c.islower() for c in reading)
    # Both as the model writes them: "Ghost★" read "Ghost�", "João" read "Joao".
    return _spelling(written(reading), capitals) == _spelling(written(name), capitals)


def strict_key(reading):
    """A screen reading's spelling, for names taken with no search (players learned from
    finished matches): as the model writes it, spaces and edge marks aside, upper case
    with script twins folded, "l" and "I" one (the loading screen's Barlow draws them
    alike), "-" as "_" (Windows OCR's slip). Digits, other punctuation and inner marks
    count: "Gh0st", "ghost_" and "Ghost�" are not "Ghost"."""
    text = "".join(written(reading).strip(GAP + " ").split())
    return text.upper().translate(_TWIN_CAPS).replace("L", "I").translate(_DASHES)


def name_keys(name):
    """The keys a reading of ``name`` may have: its own, and that of the name as the model
    writes it ("Bjørn" is read "Bj�rn": the "ø" drops out of the key)."""
    return {k for k in (name_key(name), name_key(written(name))) if k}


def attempt_key(text):
    """What makes two spellings one attempt at Pred.gg: the same search string and the
    same match rule. "ABC★DEF" read as "ABC�DEF" is another attempt than "ABCDEF"."""
    return search_text(text).casefold(), gap_key(text)


def resolve(names, found):
    """{reading: {"player_id", "name"} | None} from a ``search_query`` answer."""
    return {
        name: pick(name, ((found or {}).get(f"s{i}") or {}).get("results"))
        for i, name in enumerate(names)
    }


_RATINGS = (
    "ratings{points peakPoints rank{name} peakRank{name} unranked percentile ranking"
    " peakRanking peakTimestamp rating{name startTime endTime}}"
)
# How they play, for the TAB card: every role and hero they played (all modes and
# time: Pred.gg's own totals, not a sample), their last matches and the players they
# played most on their side (who plays together). About 10 KB a player.
_PLAYS = (
    "roleStatistics{results{role matchesPlayed matchesWon}}"
    " heroStatistics{results{hero{name} matchesPlayed matchesWon"
    " totalKills totalDeaths totalAssists}}"
    " form:matchesPaginated(limit:10){results{team hero{name} match{id winningTeam endTime}}}"
    " mates:commonPlayers(isAlly:true,limit:10){results{matchesPlayed matchesWon player{id}}}"
)
_BASIC = f"name {_RATINGS} favHero{{name}} favRole lastPlayedAt firstPlayedAt isNameConsole"
_PROFILE = f"{_BASIC} {_PLAYS}"


# Parts of the profile request a player's ranks do not need: an error there is let go.
PLAY_FIELDS = ("roleStatistics", "heroStatistics", "form", "mates")


def profile_query(player_ids, plays=True):
    """(document, variables) for every player's ranks, main and dates in one request;
    with ``plays``, how they play too (_PLAYS)."""
    ids = list(player_ids)
    fields = _PROFILE if plays else _BASIC
    declared = ",".join(f"$p{i}:ID!" for i in range(len(ids)))
    players = " ".join(f"p{i}:player(by:{{id:$p{i}}}){{{fields}}}" for i in range(len(ids)))
    return f"query({declared}){{seasons:ratings{{name endTime}} {players}}}", {
        f"p{i}": pid for i, pid in enumerate(ids)
    }


def _running(season, now):
    end = (season or {}).get("endTime")
    return not end or end > now


def current_rank(player, now=None):
    """This season's rank: {"rank": "Gold II", "points": 1234.5, ...}, {"rank": None, ...}
    if unplaced, or None when the player has no entry for the running season."""
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for entry in (player or {}).get("ratings") or []:
        season = entry.get("rating") or {}
        if not season.get("name") or not _running(season, now):
            continue
        if entry.get("unranked"):
            return {"rank": None, "points": None, "season": season["name"]}
        points = entry.get("points")
        # A rank name the ladder does not know stays as it is: placed, but not averaged.
        return {
            "rank": (entry.get("rank") or {}).get("name") or None,
            "points": float(points) if isinstance(points, (int, float)) else None,
            "ranking": _whole(entry.get("ranking")),
            "season": season["name"],
        }
    return None


def profiles(player_ids, found, now=None):
    """{player_id: profile} from a ``profile_query`` answer; unknown players are left out.

    A profile: {"current", "peak", "seasons", "main", "last_played", "first_played",
    "console", "stats"}. ``seasons`` are the finished ones (cards.season_history);
    ``stats`` how they play (cards.play_stats).
    """
    found = found or {}
    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    running = next(
        (
            s["name"]
            for s in found.get("seasons") or ()
            if s and s.get("name") and _running(s, stamp)
        ),
        None,
    )
    out = {}
    for i, pid in enumerate(player_ids):
        raw = found.get(f"p{i}")
        if not isinstance(raw, dict):
            continue
        current = current_rank(raw, now)
        if current is None and running:
            current = {"rank": None, "points": None, "season": running}  # not placed yet
        out[pid] = {
            "current": current,
            "peak": best_peak(raw),
            "seasons": season_history(raw, found.get("seasons")) or (),
            "main": main_of(raw),
            "last_played": raw.get("lastPlayedAt"),
            "first_played": raw.get("firstPlayedAt"),
            "name": raw.get("name"),  # None for a console player
            "console": bool(raw.get("isNameConsole")),  # Pred.gg hides their name
            "stats": play_stats(raw),
        }
    return out


_MATCH_PLAYER = "name team role hero{name} kills deaths assists player{id}"
_MATCH = f"id endTime winningTeam matchPlayers{{{_MATCH_PLAYER}}}"


def running_season(found):
    """The running season's rating ID from a ``{ratings{id endTime}}`` answer, else None."""
    for season in (found or {}).get("ratings") or ():
        if season and season.get("id") and not season.get("endTime"):
            return season["id"]
    return None


SEASONS_QUERY = "{ratings{id endTime}}"


def recent_query(me, limit, rating=None):
    """(document, variables) for my latest matches with everyone who was in them, and,
    given the running season's ``rating`` ID, the points each of them moved me."""
    moved = " rating(ratingId:$r){points newPoints}" if rating else ""
    declared = "$me:ID!,$n:Int" + (",$r:ID!" if rating else "")
    variables = {"me": me, "n": limit, **({"r": rating} if rating else {})}
    return (
        f"query({declared}){{me:player(by:{{id:$me}}){{matchesPaginated(limit:$n)"
        f"{{results{{team kills deaths assists{moved} match{{{_MATCH}}}}}}}}}}}",
        variables,
    )


def match_query(match_id):
    return "query($m:ID!){match(by:{id:$m}){" + _MATCH + "}}", {"m": match_id}


def _epoch(stamp):
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def match_details(match):
    """A Pred.gg match -> the shape core.encounters_for reads, or None."""
    if not isinstance(match, dict) or not match.get("id"):
        return None
    players = []
    for row in match.get("matchPlayers") or []:
        if not isinstance(row, dict):
            continue
        pid = (row.get("player") or {}).get("id")
        if not pid:
            continue
        players.append(
            {
                "player_id": pid,
                "name": row.get("name"),
                "team": row.get("team"),
                "role": row.get("role"),
                "hero": (row.get("hero") or {}).get("name"),
                "kda": (row.get("kills"), row.get("deaths"), row.get("assists")),
            }
        )
    return {
        "match_id": match["id"],
        "ended_at": _epoch(match.get("endTime")),
        "winning_team": match.get("winningTeam"),
        "players": players,
    }


def my_matches(found):
    """(summaries for core.today_summary, details for encounters_for), newest first."""
    rows = ((((found or {}).get("me") or {}).get("matchesPaginated")) or {}).get("results") or []
    summaries, details = [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        match = match_details(row.get("match"))
        if match is None or match["ended_at"] is None:
            continue
        details.append(match)
        summaries.append(
            {
                "match_id": match["match_id"],
                "ended_at": match["ended_at"],
                "won": bool(match["winning_team"]) and match["winning_team"] == row.get("team"),
                "vp": _moved(row.get("rating")),
                "kda": (row.get("kills"), row.get("deaths"), row.get("assists")),
            }
        )
    summaries.sort(key=lambda m: m["ended_at"], reverse=True)
    details.sort(key=lambda m: m["ended_at"], reverse=True)
    return summaries, details


def _moved(rating):
    """The points one match moved me (+23, -21), or None when Pred.gg has none."""
    before, after = (rating or {}).get("points"), (rating or {}).get("newPoints")
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return round(after - before)
    return None


def known_names(details):
    """{name_key: (player_id, name)} from finished matches: exact, so no search is needed."""
    found = {}
    for match in details or ():
        for row in (match or {}).get("players") or ():
            if row.get("name") == HIDDEN:
                continue  # Pred.gg hides this player's name (console players, among others)
            for key in name_keys(row.get("name")) if row.get("player_id") else ():
                found.setdefault(key, (row["player_id"], row["name"]))
    return found
