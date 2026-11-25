"""Who a seat's name is on Pred.gg: one small state per seat, apart from what the screens
read (seats.py holds that).

    unknown --(my name from the log, a name of my recent matches, a learned name)--> known
    unknown --search--> asking --> found | not found (spellings tried) | failed (gave up)
    found/known --(the reading became another name, or a learned name proved wrong)--> unknown

An identity is a dict:
    pid       Pred.gg player ID once known
    name      Pred.gg's spelling of the name
    searched  False, "asked" while a search is out, True once answered (or given up)
    asking    the spellings of the search out
    tried     attempts (lookup.attempt_key) Pred.gg did not know
    failed    Pred.gg did not answer the search: shown without data, asked again at the
              slowest pace until it does
    via       how the pid came: "log", "recent", "alias", or None for a search
    no_alias  a name key whose learned player must not be taken (spelled otherwise)
"""

from . import aliases, lookup

SPELLINGS = 4  # spellings of one name searched at once (the screens', see candidates)


def new():
    return {
        "pid": None,
        "name": None,
        "searched": False,
        "asking": (),
        "tried": set(),
        "failed": False,
        "via": None,
        "no_alias": None,
    }


def forget(ident):
    """The name read became another one: whoever it was is unknown again (the spellings
    tried stay tried)."""
    ident.update(pid=None, name=None, searched=False, via=None)


def mine(state, seat):
    """This seat reads as my own name. Only on my team: the fold that forgives a misread
    of my name ("O" for "0", "l" for "I") would also make an enemy who is spelled almost
    like me into me, with my data on their row."""
    return (
        bool(state.my_name)
        and seat["team"] == "ally"
        and lookup.name_key(seat["reading"]) in lookup.name_keys(state.my_name)
    )


def alias(state, reading):
    """The player an earlier match taught for this reading: by its spelling, or, for a
    name learned before spellings were kept, by its key alone."""
    name = aliases.digest(state.salt, lookup.name_key(reading))
    pid = state.aliases.get(aliases.spelled(state.salt, lookup.strict_key(reading)))
    if pid or state.aliases.get(aliases.checked(state.salt, name)):
        return pid  # the name was learned with its spelling: this one, or nobody
    return state.aliases.get(name)


def name(state, seat, ident):
    """A seat whose reading settled: me (the log names me), a player of my recent matches,
    or one an earlier match taught; else it stays to be searched."""
    reading = seat["reading"]
    if mine(state, seat) and state.me:
        ident.update(pid=state.me, name=state.my_name, via="log")
    elif not ident["pid"]:
        # By key, then by spelling: "Zer0" or "Ghost�" is not the "Zero" or "Ghost" of an
        # earlier match (it folds alike); such a reading is searched instead.
        key = lookup.name_key(reading)
        known = state.known.get(key)
        if known and lookup.same_name(reading, known[1]):
            ident.update(pid=known[0], name=known[1], via="recent")
        elif not known and ident["no_alias"] != key:
            pid = alias(state, reading)
            profile = state.profiles.get(pid) or {}
            shown = profile.get("name")
            if (
                pid
                and shown
                and not profile.get("console")
                and not lookup.same_name(reading, shown)
            ):
                # Its profile came earlier in this match, spelled otherwise ("Zero" for a
                # row read "Zer0"): the profiles check would not run again. Search.
                ident["no_alias"] = key
            elif pid:
                ident.update(pid=pid, via="alias")


def check_alias(state, reading, ident, profile):
    """A learned player whose profile came: the commands to un-learn a wrong one. Pred.gg
    shows this player's name and it is not the one on the screen: forgotten, searched."""
    shown = (profile or {}).get("name")
    if ident["via"] != "alias" or not shown or profile.get("console"):
        return []
    if lookup.name_key(reading) not in lookup.name_keys(shown):
        digest = aliases.digest(state.salt, lookup.name_key(reading))
        wrong = (
            digest,
            aliases.checked(state.salt, digest),
            aliases.spelled(state.salt, lookup.strict_key(reading)),
        )
        for entry in wrong:
            state.aliases.pop(entry, None)
        ident.update(pid=None, via=None, searched=False)
        return [("remember", dict.fromkeys(wrong))]
    if not lookup.same_name(reading, shown):
        # The same key, another spelling ("Zero" learned, "Zer0" on the screen): right for
        # that name, maybe not for this one. Search.
        ident.update(pid=None, via=None, searched=False, no_alias=lookup.name_key(reading))
    return []


def _keeps_marks(text, reading):
    """A spelling of ``reading`` that keeps the marks inside it: "DarkLord" for a reading
    "Dark�Lord" would search, and take, the plain namesake."""
    inner = lookup.gap_key(reading, edges=False)
    return lookup.GAP not in inner or lookup.gap_key(text, edges=False) == inner


def _other_spelling(reading, versions):
    """The most common spelling among a seat's versions with the reading's key and another
    spelling, or None."""
    key = lookup.name_key(reading)
    texts = [
        t
        for t in versions
        if t != reading and lookup.name_key(t) == key and _keeps_marks(t, reading)
    ]
    if not key or not texts:
        return None
    return max(dict.fromkeys(texts), key=texts.count)


def candidates(reading, spelled, versions, ident):
    """Spellings to search a name by, best first, leaving out those Pred.gg did not know.
    ``spelled``: other screens' own spelling of it, best first; ``versions``: every text
    the screens gave."""
    found, seen = [], set()
    for text in (reading, *spelled):
        if (
            lookup.name_key(text)
            and _keeps_marks(text or "", reading or "")
            and lookup.attempt_key(text) not in ident["tried"]
            and text not in seen
        ):
            seen.add(text)
            found.append(text)
    # The versions' other spelling of the agreed key: where the model and OCR spell a name
    # apart ("XIube" against "Xlube"), the one not chosen may be the right one.
    other = _other_spelling(reading, versions)
    extra = 0
    if other and other not in seen and lookup.attempt_key(other) not in ident["tried"] and found:
        found.insert(1, other)
        seen.add(other)
        extra = 1  # on top of the cap: the reading's own spellings keep their places
    # Pred.gg's search keeps spaces, and OCR adds some: the name without them too.
    squeezed = (reading or "").replace(" ", "")
    if " " in (reading or "") and squeezed not in seen and found:
        found.insert(1, squeezed)
        seen.add(squeezed)
    # Then what OCR cannot tell apart ("_" read as "-", "l" as "I"), screens' spellings first.
    for text in list(found):
        for spelling in lookup.search_spellings(text):
            if spelling not in seen:
                seen.add(spelling)
                found.append(spelling)
    # A reading with GAP marks is searched by its longest piece: none, no search.
    return tuple(t for t in found if lookup.search_text(t))[: SPELLINGS + extra]


def another_spelling(ident, text):
    """Another screen spelled the name ``text``: a name not found yet is searched again
    under it (the screens' fonts and backgrounds fail on different letters)."""
    if (
        ident["searched"] is True
        and not ident["pid"]
        and lookup.attempt_key(text) not in ident["tried"]
    ):
        ident["searched"] = False
        return True
    return False
