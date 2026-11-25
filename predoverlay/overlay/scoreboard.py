"""The in-match TAB scoreboard: which row is under the cursor, and who is in it.

The scoreboard lists each team by role (offlane, jungle, midlane, support,
carry). Nothing outside the game process knows the roles, so once per match,
when TAB is first held, the name and hero in each of the ten rows are read
from the screen (names by the overlay's model first, namereader; heroes and
unsure names by Windows OCR) and matched against the known roster; every row
also goes to the core (``read_rows``), which places it on the seats like any
other screen's reading. After that, pointing at a row needs no reading.

Reading the screen is like a screenshot tool: it never touches the game
process. Geometry is in design pixels of a 2560x1440 screen, measured from the
5.4.4 client's 5v5 scoreboard; a smaller team (3v3 measured, 2v2 assumed) uses
the first rows in the same places, without the role order.
"""

import asyncio
import math
import re
import threading
import time
import unicodedata
from collections import namedtuple
from difflib import SequenceMatcher

from ..errors import describe
from .rows import ROLES

ROW_TOPS = (243, 438, 631, 824, 1019)
ROW_H = 166
END_DY = 70  # the end-of-match screen lists the same rows this much lower
PANELS = {"ally": (218, 1143), "enemy": (1414, 2340)}  # x range of each team's rows
# The game's strip beside the row under the cursor (voice, chat, ping, mute, report):
# right of an ally row, left of an enemy row (measured 2026-11-19). The card keeps off it.
ACTIONS = {"ally": (1143, 1225), "enemy": (1330, 1414)}
TEXT = {"ally": (405, 875), "enemy": (1755, 2225)}  # x range of the name and hero lines
TEXT_Y = (10, 76)  # y offsets of the name and hero lines inside a row
MATCH_MIN = 0.55
HERO_MIN = 0.6  # a hero reading must be this close to a known hero name
REFUND_BELOW = 3  # a reading matching fewer rows (not the scoreboard?) is not counted
RETRY_AFTER = 3.0  # seconds between readings while some rows are still unmatched
ATTEMPTS = 3  # readings per match at most
# Refunded readings included: on a board layout the reader does not handle, every
# reading is refunded, and without this a 10-crop OCR pass ran every RETRY_AFTER
# seconds of TAB for the whole match.
MAX_READINGS = 8
# The end-of-match screen comes after the victory or defeat cinematic, which the
# log does not mark. A patient reader (see Reader) keeps looking every RETRY_AFTER
# seconds without spending a reading while too few rows have text, up to this many
# blank readings.
BLANK_MAX = 20


def row_at(x, y, size=len(ROW_TOPS), dy=0):
    """(team, index) of the scoreboard row at design point (x, y), or None.

    ``size`` is the players per team: the board lists only that many rows.
    ``dy`` moves the rows down (END_DY for the end-of-match screen).
    """
    for team, (left, right) in PANELS.items():
        if left <= x <= right:
            for index, top in enumerate(ROW_TOPS[:size]):
                if top + dy <= y <= top + dy + ROW_H:
                    return team, index
    return None


def on_board(x, y, size=len(ROW_TOPS), dy=0):
    """True inside the scoreboard's frame (both teams and the gap between them)."""
    left, right = PANELS["ally"][0], PANELS["enemy"][1]
    bottom = ROW_TOPS[max(1, min(size, len(ROW_TOPS))) - 1] + ROW_H + dy
    return left <= x <= right and ROW_TOPS[0] + dy <= y <= bottom


def text_box(team, index, dy=0):
    """(x, y, w, h) of a row's name and hero lines, in design pixels."""
    left, right = TEXT[team]
    top = ROW_TOPS[index] + dy
    return left, top + TEXT_Y[0], right - left, TEXT_Y[1] - TEXT_Y[0]


_LOOKALIKE = str.maketrans({"O": "0", "I": "1", "L": "1"})
# Capitals drawn alike in Cyrillic, Greek and Latin: the same letter to a key (the name
# model writes them as the Latin one, and a name may mix the scripts).
_TWIN_CAPS = str.maketrans("АВЕКМНОРСТХЅІЈУΑΒΕΖΗΙΚΜΝΟΡΤΥΧ", "ABEKMHOPCTXSIJYABEZHIKMNOPTYX")


def _key(text):
    """Comparison key: letters and digits only (any script, accents dropped), upper case,
    with look-alikes folded (Cyrillic and Greek twins of Latin capitals, O/0, I/L/1)."""
    plain = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in plain if not unicodedata.combining(c)).upper()
    return re.sub(r"[\W_]", "", plain).translate(_TWIN_CAPS).translate(_LOOKALIKE)


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
# One recognition at a time: the TAB reader and the roster reader may both be at it,
# and nothing says WinRT's OcrEngine is safe to share between threads.
_OCR_LOCK = threading.Lock()


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


async def _recognize(image):
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    engine = _engine()  # no OCR language: fail before decoding anything
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(image)
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    return [line.text for line in result.lines]


def read_lines(image):
    """Text lines in an encoded image (BMP, PNG...), top to bottom (Windows OCR)."""
    with _OCR_LOCK:
        return asyncio.run(_recognize(image))


# One box for a reader: its name line for the overlay's model (namereader; a QImage,
# or None for a box that holds no name), a function giving Windows OCR's versions
# (image bytes), and whether a reading of the line may be taken as sure (False for a
# line drawn too small to trust: see SMALL). A reader also takes plain image bytes (or
# a list of them): OCR alone.
Box = namedtuple("Box", "line versions trusted", defaults=(True,))
# A model reading whose text (the CTC sum over its paths) is at least this likely is
# taken without Windows OCR's word. 2026-11-19, model v8: of the 751 real lines of other
# players, the 83 % at 0.99 or more held no misread but 12 lines of one name written in
# Greek capitals and a hooked Latin letter, read as the plain Latin letters (no player
# has that spelling); the best path at 0.9 had taken 7 of 46 % drawn Cyrillic lines wrong.
SURE = math.log(0.99)
# Below this a model reading is left out: a static screen read again repeats it, so
# repeats would count one doubtful reading as agreement (core._agreed).
FLOOR = math.log(0.5)
# A loading-screen line under this many pixels high (screens below 900p) is upscaled
# so much that v7 misread one name in four at 1280x720: its readings are never sure,
# and only those at UNTRUSTED or more count at all (a static screen read again would
# otherwise agree with itself, with no OCR language most of all).
SMALL = 20
UNTRUSTED = math.log(0.9)


def sure(reading):
    """A model reading ((text, log-probability, picked) or None) to take as it is: one
    the model is sure of, or a known name it picked for the line (namereader.choose)."""
    if not reading or not _key(reading[0]):
        return False
    return reading[1] >= SURE or (len(reading) > 2 and bool(reading[2]))


def usable(reading):
    """A model reading worth one version next to Windows OCR's (see FLOOR)."""
    if not reading or not _key(reading[0]):
        return False
    return reading[1] >= FLOOR or (len(reading) > 2 and bool(reading[2]))


def versions_of(box):
    """Windows OCR's versions of a box: image bytes each."""
    if isinstance(box, Box):
        return box.versions()
    return list(box) if isinstance(box, (list, tuple)) else [box]


def model_readings(names, boxes, known=None):
    """{key: (text, log-probability, picked)} for the boxes with a name line; {} without
    a model (``names`` None or unavailable) or when it fails: Windows OCR then reads
    alone. ``known``: {key: [name, ...]} the line may hold, as the screen spells them."""
    lines = {k: b.line for k, b in boxes.items() if isinstance(b, Box) and b.line is not None}
    if names is None or not lines:
        return {}
    try:
        found = names.read(lines, known) if known else names.read(lines)
    except Exception:  # a WinRT hiccup (namereader keeps the first): Windows OCR reads alone
        return {}
    for key, reading in found.items():
        if not boxes[key].trusted and reading and not (len(reading) > 2 and reading[2]):
            value = min(reading[1], SURE - 1e-9) if reading[1] >= UNTRUSTED else FLOOR - 1.0
            found[key] = (reading[0], value, *reading[2:])
    return found


def matchable(names, consoles=None):
    """Players a reading can place: every named one, and a team's console player only
    when it is the team's only one (two of them could be on either remaining row)."""
    consoles = consoles or {}
    return sum(len(team) for team in names.values()) + sum(
        1 for team in consoles.values() if len(team) == 1
    )


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

    def __init__(self, read=read_lines, clock=time.monotonic, patient=False, names=None):
        self.read = read
        self.names = names  # the overlay's model (namereader.NameReader), or None
        self.note = None  # why Windows OCR did not read along (the last reading)
        self.clock = clock
        self.patient = patient  # blank readings are free (up to BLANK_MAX), see saw
        self.seen = False  # a reading of this match found the board (half its rows have text)
        self.blank = 0  # this match's readings that found no board
        self.match_id = None
        self.rows = {}
        self.busy = False
        self.error = None
        self.attempts = 0
        self.readings = 0  # this match's readings, refunded or not
        self.started = 0.0
        self.expected = 2 * len(ROLES)
        self.result = None  # (lines read, rows matched, rows expected) of the last reading
        self.named = frozenset()  # the players the latest reading had names for
        # (team, index) -> {"text", "texts", "hero", "role"}: every row of the latest
        # reading of the board, matched or not: the core takes them as a source
        self.read_rows = {}

    def wanted(self, match_id, players=()):
        """A new match, rows still unmatched (a few spaced retries per match), or players
        the last reading had no names for (the board's own rows, looked up since)."""
        if self.busy:
            return False
        if self.match_id != match_id:
            return True
        spaced = self.clock() - self.started >= RETRY_AFTER
        if set(players) - self.named and not self.spent():
            # Players the last reading had no names for. When that reading found the
            # board (it is what gave them their seats), the next one need not wait.
            return spaced or self.seen
        if not self.named and not self.seen and not self.spent():
            # Nobody to place yet (the app started in the middle of a match) and no
            # reading has found the board (the shop, the death screen, a board still
            # fading in): look again, or the match has no players at all.
            return spaced
        complete = len(self.rows) >= self.expected
        blind = self.patient and not self.seen and self.blank < BLANK_MAX
        if blind:
            return spaced
        return not complete and self.attempts < ATTEMPTS and not self.spent() and spaced

    def pending(self, match_id, players=()):
        """A reading of this match is still to come whatever the spacing says: its first
        one, one for players the last had no names for, or, while nobody is named, one
        more look for the board. The window shows a skeleton meanwhile, not "could not
        be read"."""
        if self.busy or self.match_id != match_id:
            return True
        if self.spent():
            return False
        if set(players) - self.named:
            return True
        if not self.seen:
            # The board was not found (TAB during the death screen or the shop, a board
            # not drawn yet): another look comes while the counted readings last, and
            # with nobody named until the readings are spent (see wanted).
            return self.attempts < ATTEMPTS or not self.named
        # Found with nobody named: its rows are on their way through the core, which
        # gives them seats a moment later (those players are then the unnamed, above).
        return not self.named and self.clock() - self.started < RETRY_AFTER

    def failed(self, match_id):
        """Why a reading of ``match_id`` raised (neither reader works: no more readings
        this match), or None. Another match starts afresh."""
        return self.error if self.match_id == match_id else None

    def saw(self, match_id):
        """A reading of ``match_id`` found the board, not a screen in front of it."""
        return self.match_id == match_id and self.seen

    def spent(self, match_id=None):
        """This match (or ``match_id``, if it is this one) has had every reading it gets."""
        same = match_id is None or match_id == self.match_id
        return same and self.readings >= MAX_READINGS

    def start(self, match_id, crops, names, heroes=(), consoles=None, size=None):
        """``consoles`` maps a team to its console players: they have no name to read,
        so they stay out of ``names`` and are placed by elimination (see _run)."""
        if self.match_id != match_id:
            self.match_id, self.rows, self.attempts, self.readings = match_id, {}, 0, 0
            self.seen, self.blank, self.read_rows, self.error = False, 0, {}, None
        self.expected = matchable(names, consoles)
        self.named = frozenset(pid for team in names.values() for pid in team)
        self.size = size  # rows a side; None: as many as there are players
        self.attempts += 1
        self.readings += 1
        self.started = self.clock()
        self.busy = True
        threading.Thread(
            target=self._run,
            args=(match_id, crops, names, heroes, consoles, size),
            daemon=True,
            name="pred-scoreboard",
        ).start()

    def _run(self, match_id, crops, names, heroes=(), consoles=None, size=None):
        try:
            readings, read_heroes, ocr_error = {}, {}, None
            versions = {}  # row -> its texts as roster.Reader gives them (core's evidence)
            boxes = crops() if callable(crops) else crops
            # The board shows names in capitals: those of the row's team are the ones a
            # line the model is unsure of may hold.
            known = {row: [n.upper() for n in names.get(row[0], {}).values() if n] for row in boxes}
            model = model_readings(self.names, boxes, known)
            for row, box in boxes.items():
                # Windows OCR still reads the hero line under the name; the name is the
                # model's when it is sure, else the reading closer to a team name.
                lines = []
                if ocr_error is None:  # OCR failed on an earlier row: it fails on this one
                    try:
                        lines = [line for img in versions_of(box)[:1] for line in self.read(img)]
                        lines = [line for line in lines if _key(line)]
                    except Exception as exc:  # no OCR language: the model reads alone
                        if not model:
                            raise
                        ocr_error = describe(exc)
                ocr = lines[0] if lines else ""
                read_heroes[row] = lines[1] if len(lines) > 1 else None
                reading = model.get(row) if usable(model.get(row)) else None
                # Two equal versions mean the model was sure by itself; a name it picked
                # from the roster's own names (sure() takes those too) is no evidence
                # for the seat that gave it.
                alone = reading is not None and reading[1] >= SURE
                versions[row] = [reading[0]] * 2 if alone else [reading[0] if reading else "", ocr]
                if sure(reading) or (reading and not _key(ocr)):
                    readings[row] = reading[0]
                elif reading:
                    team = names.get(row[0], {}).values()
                    near = lambda text: max((similarity(text, n) for n in team), default=0.0)  # noqa: E731
                    readings[row] = reading[0] if near(reading[0]) >= near(ocr) else ocr
                else:
                    readings[row] = ocr
            found = {}
            consoles = consoles or {}
            for team in ("ally", "enemy"):
                team_rows = {row: text for row, text in readings.items() if row[0] == team and text}
                team_names = names.get(team, {})
                team_consoles = list(consoles.get(team, ()))
                keys = [_key(name) for name in team_names.values()]
                if len(set(keys)) < len(keys):  # two alike names: nothing safe to assign
                    continue
                matched = assign(team_rows, team_names)
                # measured: 5 rows, and 3 in the old Crucible
                rows = size or len(team_names) + len(team_consoles)
                rows_left = [(team, i) for i in range(rows) if (team, i) not in matched]
                left = [pid for pid in team_names if pid not in matched.values()]
                if team_consoles:
                    # Every named player found and one console player: the last row is
                    # theirs. Two or more could be on either row, so none is placed.
                    if not left and len(team_consoles) == 1 and len(rows_left) == 1:
                        matched[rows_left[0]] = team_consoles[0]
                elif (
                    # One row and one player left of a full team. The row read as (almost)
                    # nothing: a one-letter name OCR misses. Or every row of the team was
                    # read: the board lists the team row by row, so the row left is the
                    # player left, however unlike the name the other screens read (the
                    # core joins them by the same rule, seats.place, and searches the
                    # board's spelling).
                    len(rows_left) == 1
                    and len(left) == 1
                    and rows in (3, len(ROLES))
                    and len(team_names) + len(team_consoles) == rows
                    and (len(_key(readings.get(rows_left[0], ""))) <= 2 or len(team_rows) == rows)
                ):
                    matched[rows_left[0]] = left[0]
                found.update(matched)
            sizes = {
                team: size or len(names.get(team, {})) + len(consoles.get(team, ()))
                for team in ("ally", "enemy")
            }
            rows = {
                row: {
                    "player_id": pid,
                    "hero": snap_hero(read_heroes.get(row), heroes)
                    if heroes
                    else (read_heroes.get(row) or "").upper() or None,
                    "role": ROLES[row[1]] if sizes.get(row[0]) == len(ROLES) else None,
                }
                for row, pid in found.items()
            }
            # Rows with letters or digits: a cinematic's specks read as "-" or "." are none.
            lines = sum(1 for text in readings.values() if _key(text))
            self.result = (lines, len(rows), self.expected)
            board = bool(rows) or lines >= max(2, len(readings) // 2)
            if self.match_id == match_id and board:
                self.seen = True
                self.read_rows = {
                    row: {
                        "text": text,
                        "texts": versions.get(row, [text]),
                        "hero": snap_hero(read_heroes.get(row), heroes) if heroes else None,
                        "role": ROLES[row[1]] if sizes.get(row[0]) == len(ROLES) else None,
                    }
                    for row, text in readings.items()
                    if _key(text)
                }

            if (
                self.patient
                and self.match_id == match_id
                and not self.seen
                and self.blank < BLANK_MAX
            ):
                # Not the board yet (the end cinematic): this look costs nothing.
                self.blank += 1
                self.attempts -= 1
                self.readings -= 1
            elif len(rows) < min(REFUND_BELOW, self.expected):
                self.attempts -= 1  # likely not the scoreboard (death screen, shop): retry
            # A reading replaces the last one when it matched at least as many rows,
            # so one player never ends up on two rows from different readings.
            if self.match_id == match_id and len(rows) >= len(self.rows):
                self.rows = rows
            self.error = None
            self.note = ocr_error  # Windows OCR failed and the model read alone
        except Exception as exc:  # no OCR language, a WinRT hiccup: cards just do not show
            self.error = describe(exc)
        finally:
            self.busy = False

    def player_at(self, match_id, row):
        return self.rows.get(row) if self.match_id == match_id else None
