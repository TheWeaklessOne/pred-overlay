"""Game modes as the matchmaking backend names them, normalised for display.

The game rotates featured modes (Overclocked arrived with 1.18, Crucible moved
to a custom 2v2 queue), so nothing here is a closed list: a queue the overlay
has never seen still gets an id, a readable label and a place in the strip.
Known modes only get a nicer label and a fixed position.
"""

import re

# mode id -> (label, position); lower positions sit closer to the strip's head.
KNOWN = {
    "RANKED": ("RANKED", 0),
    "TEAM_VS_TEAM": ("STANDARD", 10),
    "OVERCLOCKED": ("OVERCLOCKED", 20),
    "ARAM": ("ARAM", 30),
    "CRUCIBLE": ("CRUCIBLE", 40),
    "TEAM_VS_AI": ("VS AI", 90),
}
UNKNOWN_POSITION = 50  # a new mode goes after the known PvP ones, before VS AI
OPTIONAL = {"TEAM_VS_AI"}  # shown only while someone waits (the strip lingers)
MAX_SLOTS = 5  # the strip's room; the least important extra modes are left out
LONGEST_LABEL = "OVERCLOCKED"  # labels are cut to this width
PLATFORM = "CROSS_PLAY"  # the queues a Steam player can be matched in


def mode_id(ext):
    """A stable id for one ``extMatchmakingKey``; None for a queue to ignore.

    Custom queues carry their real mode in the last segment of the custom key
    ("2121\\x1fCRUCIBLE" -> CRUCIBLE); a custom queue without one is a private
    lobby and not shown.
    """
    queue = (ext.get("queueName") or "").strip().upper()
    if queue == "CUSTOM":
        key = ext.get("omedaCustomMatchmakingKey") or ""
        custom = key.split("\x1f")[-1] if "\x1f" in key else ""
        if not re.search(r"[A-Za-z]", custom):
            return None  # a lobby code, not a mode
        queue = custom.strip().upper()
    queue = re.sub(r"[^A-Z0-9_]", "", queue.replace(" ", "_"))
    return queue or None


def label(mode):
    """'TEAM_VS_TEAM' -> 'STANDARD'; an unknown 'SOME_NEW_MODE' -> 'SOME NEW MODE'."""
    known = KNOWN.get(mode)
    return known[0] if known else mode.replace("_", " ").strip()


def position(mode):
    known = KNOWN.get(mode)
    return known[1] if known else UNKNOWN_POSITION


# Experience names (from the map URL) that are a queue under another name.
_EXPERIENCES = {"NORMAL": "TEAM_VS_TEAM", "STANDARD": "TEAM_VS_TEAM", "AI": "TEAM_VS_AI"}
_PVE_WORDS = {"AI", "BOT", "BOTS", "PRACTICE", "TUTORIAL"}


def experience_label(experience):
    """The match's mode, as the strip names it, from the map URL's experience:
    'BP_Experience_Game_Overclocked' -> 'OVERCLOCKED', '..._Normal' -> 'STANDARD'.
    None when it only repeats the team size ('..._3v3') or is missing."""
    name = re.sub(r"^(?:BP_)?Experience_(?:Game_)?", "", experience or "", flags=re.IGNORECASE)
    name = re.sub(r"[^A-Za-z0-9_]", "", name).upper().strip("_")
    if not name or re.fullmatch(r"\d+V\d+", name):
        return None
    return label(_EXPERIENCES.get(name, name))


def is_pve(mode_label):
    """True for a mode against bots or alone, whose roster lists only the humans."""
    return bool(set((mode_label or "").split()) & _PVE_WORDS)


def roster_mode(players):
    """'5v5' for ten players, '2v2' for four; None for an odd or out-of-range roster."""
    if players % 2 or not 2 <= players <= 10:
        return None
    return f"{players // 2}v{players // 2}"
