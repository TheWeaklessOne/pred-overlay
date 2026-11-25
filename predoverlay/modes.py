"""Game modes as the game names them, normalised for display.

The game rotates featured modes (Overclocked arrived with 1.18), so nothing
here is a closed list: a mode the overlay has never seen still gets a readable
label. Known modes only get a nicer one.
"""

import re

# mode id -> label
KNOWN = {
    "RANKED": "RANKED",
    "TEAM_VS_TEAM": "STANDARD",
    "OVERCLOCKED": "OVERCLOCKED",
    "ARAM": "ARAM",
    "CRUCIBLE": "CRUCIBLE",
    "TEAM_VS_AI": "VS AI",
}


def label(mode):
    """'TEAM_VS_TEAM' -> 'STANDARD'; an unknown 'SOME_NEW_MODE' -> 'SOME NEW MODE'."""
    return KNOWN.get(mode) or mode.replace("_", " ").strip()


def has_lanes(shown):
    """False for a mode label without lanes, where a scoreboard row says no role."""
    return shown != KNOWN["ARAM"]


# Experience names (from the map URL) that are a mode under another name.
_EXPERIENCES = {"NORMAL": "TEAM_VS_TEAM", "STANDARD": "TEAM_VS_TEAM", "AI": "TEAM_VS_AI"}
_PVE_WORDS = {"AI", "BOT", "BOTS", "PRACTICE", "TUTORIAL"}


def experience_label(experience):
    """The match's mode, as the overlay names it, from the map URL's experience:
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
