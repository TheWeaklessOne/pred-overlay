"""The in-match TAB scoreboard: which row is under the cursor, and who is in it.

The scoreboard lists each team by role (offlane, jungle, midlane, support,
carry). Nothing outside the game process knows the roles, so once per match,
when TAB is first held, the name and hero in each of the ten rows are read
from the screen with Windows OCR and matched against the known roster. After
that, pointing at a row needs no reading.

Reading the screen is like a screenshot tool: it never touches the game
process. Geometry is in design pixels of a 2560x1440 screen, measured from the
5.4.4 client's 5v5 scoreboard; a smaller team (3v3 measured, 2v2 assumed) uses
the first rows in the same places, without the role order.
"""

import asyncio
import re
import threading
import time
from difflib import SequenceMatcher

from .rows import ROLES

ROW_TOPS = (243, 438, 631, 824, 1019)
ROW_H = 166
PANELS = {"ally": (218, 1143), "enemy": (1414, 2340)}  # x range of each team's rows
TEXT = {"ally": (405, 875), "enemy": (1755, 2225)}  # x range of the name and hero lines
TEXT_Y = (10, 76)  # y offsets of the name and hero lines inside a row
MATCH_MIN = 0.55
HERO_MIN = 0.6  # a hero reading must be this close to a known hero name
REFUND_BELOW = 3  # a reading matching fewer rows (not the scoreboard?) is not counted
RETRY_AFTER = 3.0  # seconds between readings while some rows are still unmatched
ATTEMPTS = 3  # readings per match at most


def row_at(x, y, size=len(ROW_TOPS)):
    """(team, index) of the scoreboard row at design point (x, y), or None.

    ``size`` is the players per team: the board lists only that many rows.
    """
    for team, (left, right) in PANELS.items():
        if left <= x <= right:
            for index, top in enumerate(ROW_TOPS[:size]):
                if top <= y <= top + ROW_H:
                    return team, index
    return None


def on_board(x, y, size=len(ROW_TOPS)):
    """True inside the scoreboard's frame (both teams and the gap between them)."""
    left, right = PANELS["ally"][0], PANELS["enemy"][1]
    bottom = ROW_TOPS[max(1, min(size, len(ROW_TOPS))) - 1] + ROW_H
    return left <= x <= right and ROW_TOPS[0] <= y <= bottom


def text_box(team, index):
    """(x, y, w, h) of a row's name and hero lines, in design pixels."""
    left, right = TEXT[team]
    top = ROW_TOPS[index]
    return left, top + TEXT_Y[0], right - left, TEXT_Y[1] - TEXT_Y[0]


_LOOKALIKE = str.maketrans({"O": "0", "I": "1", "L": "1"})


def _key(text):
    """Comparison key: letters and digits only, with OCR look-alikes folded (O/0, I/L/1)."""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper()).translate(_LOOKALIKE)


def similarity(read, name):
    """0..1 likeness of an OCR reading to a roster name; a cut-off name counts as a prefix."""
    a, b = _key(read.replace("...", "").replace("…", "")), _key(name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) >= 4 and b.startswith(a):
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


def assign(readings, names):
    """Match row readings to roster names one to one, best pairs first.

    ``readings``: {row: text}; ``names``: {player_id: short name}.
    Returns {row: player_id} for pairs at least MATCH_MIN alike.
    """
    pairs = sorted(
        (
            (similarity(text, name), row, pid)
            for row, text in readings.items()
            for pid, name in names.items()
        ),
        reverse=True,
    )
    found, used = {}, set()
    for score, row, pid in pairs:
        if score < MATCH_MIN:
            break
        if row in found or pid in used:
            continue
        found[row] = pid
        used.add(pid)
    return found


_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from winrt.windows.media.ocr import OcrEngine

        # Any installed Latin-script recognizer reads nicknames; the profile
        # language may have no OCR pack (seen: en-US UI with only fr-FR OCR).
        _ENGINE = OcrEngine.try_create_from_user_profile_languages() or next(
            (
                OcrEngine.try_create_from_language(language)
                for language in OcrEngine.available_recognizer_languages
            ),
            None,
        )
        if _ENGINE is None:
            raise RuntimeError("No Windows OCR language is installed.")
    return _ENGINE


async def _recognize(png):
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await _engine().recognize_async(bitmap)
    return [line.text for line in result.lines]


def read_lines(png):
    """Text lines in a PNG image, top to bottom (Windows OCR)."""
    return asyncio.run(_recognize(png))


def snap_hero(reading, heroes):
    """The known hero name an OCR reading stands for ('vvNG MAO' -> 'FENG MAO'), or None."""
    if not reading or not heroes:
        return None
    score, best = max((SequenceMatcher(None, _key(reading), _key(h)).ratio(), h) for h in heroes)
    return best.upper() if score >= HERO_MIN else None


class Reader:
    """Reads the ten rows once per match on a worker thread.

    ``start`` takes a function that returns PNG crops of the rows (it runs on
    the worker, so cropping and encoding stay off the UI thread), the roster
    names per team and the known hero names; ``rows`` then maps (team, index)
    to {"player_id", "hero", "role"} for the rows it could match.
    """

    def __init__(self, read=read_lines, clock=time.monotonic):
        self.read = read
        self.clock = clock
        self.match_id = None
        self.rows = {}
        self.busy = False
        self.error = None
        self.attempts = 0
        self.started = 0.0
        self.expected = 2 * len(ROLES)
        self.result = None  # (lines read, rows matched, rows expected) of the last reading

    def wanted(self, match_id):
        """A new match, or rows still unmatched (a few spaced retries per match)."""
        if self.busy:
            return False
        if self.match_id != match_id:
            return True
        complete = len(self.rows) >= self.expected
        spaced = self.clock() - self.started >= RETRY_AFTER
        return not complete and self.attempts < ATTEMPTS and spaced

    def start(self, match_id, crops, names, heroes=()):
        if self.match_id != match_id:
            self.match_id, self.rows, self.attempts = match_id, {}, 0
        self.expected = sum(len(team) for team in names.values())
        self.attempts += 1
        self.started = self.clock()
        self.busy = True
        threading.Thread(
            target=self._run,
            args=(match_id, crops, names, heroes),
            daemon=True,
            name="pred-scoreboard",
        ).start()

    def _run(self, match_id, crops, names, heroes=()):
        try:
            readings, read_heroes = {}, {}
            for row, png in (crops() if callable(crops) else crops).items():
                lines = [line for line in self.read(png) if _key(line)]
                readings[row] = lines[0] if lines else ""
                read_heroes[row] = lines[1] if len(lines) > 1 else None
            found = {}
            for team in ("ally", "enemy"):
                team_rows = {row: text for row, text in readings.items() if row[0] == team and text}
                team_names = names.get(team, {})
                keys = [_key(name) for name in team_names.values()]
                if len(set(keys)) < len(keys):  # two alike names: nothing safe to assign
                    continue
                matched = assign(team_rows, team_names)
                # One row and one player left, and that row read as (almost) nothing:
                # a one-letter name OCR misses. A row with a clear, different
                # reading is never forced onto the leftover player.
                size = len(team_names)  # measured: 5, and 3 in the old Crucible
                rows_left = [(team, i) for i in range(size) if (team, i) not in matched]
                left = [pid for pid in team_names if pid not in matched.values()]
                if (
                    len(rows_left) == 1
                    and len(left) == 1
                    and size in (3, len(ROLES))
                    and len(_key(readings.get(rows_left[0], ""))) <= 2
                ):
                    matched[rows_left[0]] = left[0]
                found.update(matched)
            rows = {
                row: {
                    "player_id": pid,
                    "hero": snap_hero(read_heroes.get(row), heroes)
                    if heroes
                    else (read_heroes.get(row) or "").upper() or None,
                    "role": ROLES[row[1]] if len(names.get(row[0], {})) == len(ROLES) else None,
                }
                for row, pid in found.items()
            }
            self.result = (sum(1 for text in readings.values() if text), len(rows), self.expected)
            if len(rows) < min(REFUND_BELOW, self.expected):
                self.attempts -= 1  # likely not the scoreboard (death screen, shop): retry
            # A reading replaces the last one when it matched at least as many rows,
            # so one player never ends up on two rows from different readings.
            if self.match_id == match_id and len(rows) >= len(self.rows):
                self.rows = rows
            self.error = None
        except Exception as exc:  # no OCR language, a WinRT hiccup: cards just do not show
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.busy = False

    def player_at(self, match_id, row):
        return self.rows.get(row) if self.match_id == match_id else None
