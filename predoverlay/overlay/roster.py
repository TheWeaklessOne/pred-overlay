"""The names on the draft and loading screens, read by the overlay's model and Windows OCR.

The draft lists your own team on the left, one name per row (the game shows
the other team as "OPPONENT 1..5"). The loading screen shows all ten cards,
the other team on top or below. The model (namereader) reads each name line
first; a name it is not sure of is also read by Windows OCR in a few versions
(as it is, and dark text on white at two brightness cuts): the loading
screen's art behind the names fools one version or another, rarely all. The
core compares the versions; nothing here decides who a name is.

Geometry is in design pixels of a 2560x1440 screen, measured on the 5.4.4
client (Ranked 5v5, November 2026). Like the TAB reader this grabs the screen
and never touches the game process.
"""

import threading
import time

from ..errors import describe
from .scoreboard import Box, _key, model_readings, read_lines, sure, usable, versions_of

DRAFT_ROWS = (230, 358, 486, 614, 742)  # centre of each name line
DRAFT_X = (192, 563)  # the rows being picked sit further right: the box covers both
DRAFT_H = 32  # half the box height: a short name needs room around it to be read
# The pick screen's "ALLY" header: the role screen before it has other text where
# the names will be, so a draft reading counts only with this in it.
ANCHOR, ANCHOR_TEXT = (240, 138, 120, 40), "ALLY"
# The line under each pick-screen name: the hero picked ("Bayle") or "Waiting...",
# dim grey in mixed case (measured 2026-11-19: rows 28..40 below the name's centre).
HERO_DY, HERO_X, HERO_W, HERO_H = 34, 192, 320, 17  # centre offset, left, width, half height
# A short name in the wide draft box often reads as nothing; the box's left part
# alone reads it (2026-11-18: 315 of 315 draft rows, against 307 for the wide box).
NARROW = 200
# The role screen before the pick screen: a name plate under each role a player
# picked, stacked down when several pick the same (measured on 2026-11-19 frames:
# plates 240 x 33, one under the other every 41.5 px; FILL's column by the spacing).
# Plates slide in a little from the right: the boxes are wider than a plate.
ROLE_X = (778, 1023, 1308, 1594, 1839, 2085)  # plate centres: OFFLANE .. CARRY, FILL
ROLE_Y, ROLE_STEP, ROLE_STACK = 546, 41.5, 5  # first plate's centre; plates per column
ROLE_W, ROLE_H = 128, 17  # half the box width and height
# The column labels over the plates ("MIDLANE"): a reading counts only with it in
# it (or my own name on a plate), as the pick screen's does with "ALLY".
ROLE_ANCHOR, ROLE_ANCHOR_TEXT = (1209, 272, 200, 40), "MIDLANE"
LOADING_ROWS = {"top": 511, "bottom": 1222}
LOADING_X = tuple((round(892 + 345.6 * i), round(1110 + 345.6 * i)) for i in range(5))
LOADING_H = 17
# The loading screen's columns, left to right, for both teams (5v5 with lanes).
LOADING_ROLES = ("CARRY", "SUPPORT", "MIDLANE", "JUNGLE", "OFFLANE")
# Brightness cuts of the dark-on-white versions: the draft's names are dim, the
# loading screen's bright over busy art (measured on 2026-11-18 frames).
CUTS = {"draft": (100, 130), "loading": (170, 200)}
EVERY = {"draft": 3.0, "loading": 2.5}  # seconds between readings
START = {"draft": 2.0, "loading": 2.5}  # seconds into the phase before the first
MOST = {"draft": 120, "loading": 10}  # readings per phase at most: the draft is read to
# its end (the role screen, then picks with bans: six minutes)
TEAM = 5  # names per team on these screens
SETTLED = 2  # readings after every name is in, in case one of them was misread


def boxes(kind):
    """{slot: (x, y, w, h)} of every name box of a screen, in design pixels.

    Draft slots are ("ally", 0..4), and the role screen's plates ("role", column *
    ROLE_STACK + place in its stack); loading slots ("top" | "bottom", 0..4). The
    draft phase shows the role screen, then the pick screen: one reading has both.
    """
    if kind == "draft":
        left, right = DRAFT_X
        found = {
            ("ally", i): (left, y - DRAFT_H, right - left, 2 * DRAFT_H)
            for i, y in enumerate(DRAFT_ROWS)
        }
        found[("anchor", 0)] = ANCHOR
        for i, y in enumerate(DRAFT_ROWS):
            found[("hero", i)] = (HERO_X, y + HERO_DY - HERO_H, HERO_W, 2 * HERO_H)
        for column, x in enumerate(ROLE_X):
            for place in range(ROLE_STACK):
                y = round(ROLE_Y + place * ROLE_STEP)
                box = (x - ROLE_W, y - ROLE_H, 2 * ROLE_W, 2 * ROLE_H)
                found[("role", column * ROLE_STACK + place)] = box
        found[("roles", 0)] = ROLE_ANCHOR
        return found
    return {
        (row, i): (left, y - LOADING_H, right - left, 2 * LOADING_H)
        for row, y in LOADING_ROWS.items()
        for i, (left, right) in enumerate(LOADING_X)
    }


def plate(image):
    """Where a role box holds a name plate: (left, right) of the plate's inside in image
    pixels, or None. A plate is a dark band at least 200 design px long on its top rows,
    above the name, under a lighter border line (the map and the pick screen's dark
    spots have no such line)."""
    from PySide6.QtGui import QImage

    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    width, height = gray.width(), gray.height()
    if width < 8 or height < 8:
        return None
    stride = gray.bytesPerLine()
    data = bytes(gray.constBits())[: stride * height]

    def row(y_design):
        y = min(height - 1, round(y_design / (2 * ROLE_H) * height))
        return data[y * stride : y * stride + width]

    inside = row(5)  # a 34 px box: border on rows 1-2, the inside from 3, the name from 9
    best_run, start = (0, 0), None
    for x, value in enumerate(list(inside) + [255]):
        if value < 60 and start is None:
            start = x
        elif value >= 60 and start is not None:
            if x - start > best_run[1] - best_run[0]:
                best_run = (start, x)
            start = None
    left, right = best_run
    if right - left < 200 * width / (2 * ROLE_W):
        return None
    # The border: an even line lighter than the inside, on row 0..2 of the box (a plate
    # lower in a stack starts higher); the map above it is lighter too, but uneven.
    n = right - left
    dark = sum(row(6)[left:right]) / n
    for y in (0.5, 1.5, 2.5):
        values = row(y)[left:right]
        mean = sum(values) / n
        spread = (sum((v - mean) ** 2 for v in values) / n) ** 0.5
        if mean >= dark + 15 and spread <= 12:
            return left, right
    return None  # no border line over the band: not a plate


def plate_line(part, inside):
    """The name line of a plate for the model: its inside, the border lines above and
    below painted over in the plate's own dark (they read as marks), at the box's
    height (the name keeps the size the model knows from the loading screen)."""
    from PySide6.QtGui import QColor, QPainter

    left, right = inside
    line = part.copy(left + 2, 0, max(8, right - left - 4), part.height())
    rows = line.height()
    top, bottom = round(3 / (2 * ROLE_H) * rows), round(31 / (2 * ROLE_H) * rows)
    fill = QColor(line.pixel(min(3, line.width() - 1), min(top + 1, rows - 1)))
    painter = QPainter(line)
    painter.fillRect(0, 0, line.width(), top, fill)
    painter.fillRect(0, bottom, line.width(), rows - bottom, fill)
    painter.end()
    return line


def best(texts):
    """The reading most versions agree on (by key), ties to the longest; "" if none."""
    keys = {}
    for text in texts or ():
        key = _key(text)
        if key:
            keys.setdefault(key, []).append(text)
    if not keys:
        return ""
    top = max(keys, key=lambda k: (len(keys[k]), len(k)))
    return keys[top][0]


def due(kind, began, last, readings, now, settled=0):
    """Time for another reading of this phase's screen; ``settled`` counts the readings
    since the panel had every name of it."""
    if readings >= MOST[kind] or now - began < START[kind] or settled >= SETTLED:
        return False
    return last is None or now - last >= EVERY[kind]


class Reader:
    """Reads one screen's boxes on a worker thread and hands back every version's text.

    ``crops()`` runs on the worker and returns {slot: box}: a scoreboard.Box (the name
    line for the overlay's model, and Windows OCR's versions) or a list of PNGs (one
    per version, OCR alone). ``done(texts)`` gets {slot: [text, ...]} or None after a
    failure. With a model, a name it is sure of comes as two agreeing versions and
    Windows OCR is not asked; one it is not sure of is version 0 next to OCR's.
    """

    def __init__(self, read=read_lines, names=None):
        self.read = read
        self.names = names  # the overlay's model (namereader.NameReader), or None
        self.busy = False
        self.error = None
        self.note = None  # why Windows OCR did not read along (the last reading)
        self.ms = None  # how long the last reading took

    def start(self, crops, done, known=()):
        """``known``: names the boxes may hold, spelled as this screen shows them (mine,
        the draft's on the loading screen): the model tries them on lines it is unsure of.
        A list for every box, or {box: [name, ...]}."""
        self.busy = True
        threading.Thread(
            target=self._run, args=(crops, done, known), daemon=True, name="pred-roster"
        ).start()

    def _run(self, crops, done, known=()):
        began = time.perf_counter()
        try:
            texts, ocr_error = {}, None
            boxes = crops()
            if known and not isinstance(known, dict):
                known = {k: list(known) for k in boxes}
            model = model_readings(self.names, boxes, known or None)
            for slot, box in boxes.items():
                reading = model.get(slot)
                if sure(reading):
                    texts[slot] = [reading[0], reading[0]]
                    continue
                ocr = []
                if ocr_error is None:  # OCR failed on an earlier box: it fails on this one
                    try:
                        # The first line: a draft box also holds the hero line under the name.
                        ocr = [
                            next((line.strip() for line in self.read(img) if _key(line)), "")
                            for img in versions_of(box)
                        ]
                    except Exception as exc:  # no OCR language: the model reads alone
                        if not model:
                            raise
                        ocr_error = describe(exc)
                # Version 0 is the model's ("" when it is too unsure: see FLOOR), so OCR's
                # versions keep their places from one reading to the next.
                has_line = isinstance(box, Box) and box.line is not None and self.names is not None
                own = [reading[0] if usable(reading) else ""] if has_line else []
                texts[slot] = own + ocr
            self.error, self.note = None, ocr_error
        except Exception as exc:  # no OCR language, a WinRT hiccup: no roster this phase
            self.error = describe(exc)
            texts = None
        finally:
            self.ms = (time.perf_counter() - began) * 1000
            self.busy = False
        done(texts)
