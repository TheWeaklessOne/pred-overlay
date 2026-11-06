"""Game-styled painting of the roster panel with QPainter.

Coordinates are design pixels of a 2560x1440 screen; the window scales the
painter to the real screen. Palette, fonts and ornaments follow the visual
spec sampled from the game's own screens (navy panels, gold rules with diamond
terminals, Dawn blue / Dusk orange team bars, Bahnschrift type).
"""

import math
import re
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainterPath,
    QPen,
    QPolygonF,
)

from .. import modes
from . import motion
from .rows import HERO_MIN, rank_step, team_balance, team_rank, team_winrate, tier

DESIGN_W, DESIGN_H = 2560, 1440
FONT_FILE = r"C:\Windows\Fonts\bahnschrift.ttf"

PALETTE = {
    "bg": "#010914",
    "panel_top": "#06101C",
    "row_a": "#242732",
    "row_b": "#17191A",
    "row_c": "#000B17",
    "row_tab": "#1F252B",
    "border": "#404864",
    "hilite": "#222531",
    "row_line": "#1F252E",
    "sep": "#343A41",
    "gold": "#EDD01E",
    "gold_rule": "#C7925B",
    "gold_soft": "#F5E09E",
    "dawn": "#259CFB",
    "dawn_hdr": "#418DF1",
    "dusk": "#FFAE06",
    "dusk_hdr": "#D4B354",
    "white": "#FFFFFF",
    "grey1": "#BDC2C7",
    "grey2": "#8A8B8C",
    "grey3": "#787D87",
    "grey4": "#5B6472",
    "win": "#78F596",
    "loss": "#F5442E",
    "self": "#95FAB6",
    "low_wr": "#E07A6A",
    "against_text": "#D2CDC8",  # neutral: the red bar says it, Paragon owns the salmon
    "with_text": "#BFE9FF",
    "info_text": "#C9CED6",
    "new": "#FFCF70",
    "name": "#E9ECF0",
    "plate_mid": "#141518",
    "plate_end": "#0E1118",
    "edge_ally": "#232832",
    "edge_enemy": "#23221F",
    "line_ally": "#3A4A66",
    "line_enemy": "#5A5140",
}
TIER_COLOR = {
    "bronze": "#B07A4F",
    "silver": "#AEB6C2",
    "gold": "#EDD01E",
    "platinum": "#5FD3C6",
    "diamond": "#89B9F3",
    "master": "#C58BF2",
    "paragon": "#F2867A",
    "none": "#5B6472",
}

# The panel's grid, in design pixels from its top-left, one for both teams.
STACK_ROW_H, STACK_GAP, STACK_HDR = 40, 4, 26
PAD_TOP, TEAM_GAP, PAD_BOTTOM = 10, 8, 14
LINE1, LINE2 = 18, 34  # baselines inside a row
PLATE_L, PLATE_R, BAR_W = 8, 632, 3
COL_NAME, COL_NAME_R = 22, 210
TAB_AGE, TAB_ROLE = 58, 114  # offsets from the name column on line 2
COL_ICON = 224  # centre of the rank emblem, spans both lines
COL_RANK, COL_RANK_R = 242, 336
VP_BAR_W = 50
COL_PEAK, COL_PEAK_R = 348, 430
COL_ENC = 438  # 2px accent
COL_TEXT = 447
COL_R = 620  # the one right edge: AVG, result, when, form


@dataclass
class Motion:
    """Animation progress, all 0..1. ``rows(i)`` gives row i's entrance progress."""

    panel: float = 1.0
    rule: float = 1.0
    pulse: float = 1.0
    rows: object = None
    rise: float = 8.0  # px a row rises as it enters
    loud: bool = True  # False in the draft: the player is reading the game's own screen

    def row(self, index):
        return 1.0 if self.rows is None else self.rows(index)


def col(key, alpha=1.0):
    color = QColor(PALETTE.get(key, key))
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color


def font(px, weight=400, track_em=0.0, caps=False):
    f = QFont("Bahnschrift")
    f.setPixelSize(px)
    f.setVariableAxis(QFont.Tag("wght"), float(weight))
    if track_em:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, px * track_em)
    if caps:
        f.setCapitalization(QFont.Capitalization.AllUppercase)
    return f


def tnum(f):
    f.setFeature(QFont.Tag("tnum"), 1)
    return f


def text(p, x, y, s, f, color, align="l", w=None):
    """As before, but right-aligned ink ends exactly at x: tracking trails the last glyph."""
    p.setFont(f)
    p.setPen(color)
    metrics = QFontMetricsF(f)
    width = metrics.horizontalAdvance(s)
    if w is not None and width > w:
        s = metrics.elidedText(s, Qt.TextElideMode.ElideRight, max(w, 0))
        width = metrics.horizontalAdvance(s)
    if align == "r":
        x -= width - f.letterSpacing()
    elif align == "c":
        x -= (width - f.letterSpacing()) / 2
    p.drawText(QPointF(x, y), s)
    return width


def diamond(p, cx, cy, r, pen=None, brush=None):
    polygon = QPolygonF(
        [QPointF(cx, cy - r), QPointF(cx + r, cy), QPointF(cx, cy + r), QPointF(cx - r, cy)]
    )
    p.setPen(pen if pen else Qt.PenStyle.NoPen)
    p.setBrush(brush if brush else Qt.BrushStyle.NoBrush)
    p.drawPolygon(polygon)


def ornament_rule(p, x0, x1, y, color="sep", alpha=1.0, inner=False):
    """As in draft: a small single diamond by the word, a double one at the far end."""
    if x1 - x0 < 40:
        return
    pen = QPen(col(color, alpha), 1.0)
    p.setPen(pen)
    start = x0 + (6 if inner else 14)
    p.drawLine(QPointF(start, y + 0.5), QPointF(x1 - 14, y + 0.5))
    if inner:
        diamond(p, x0 + 3, y + 0.5, 3, pen)
    else:
        diamond(p, x0 + 4, y + 0.5, 4, pen)
        diamond(p, x0 + 10, y + 0.5, 4, pen)
    diamond(p, x1 - 4, y + 0.5, 4, pen)
    diamond(p, x1 - 10, y + 0.5, 4, pen)


def _glow(p, x, y, s, f, team_color):
    """White word with a team-coloured halo, as ALLY/ENEMY in the draft screen."""
    for dx, dy, a in (
        (-1.5, 0, 0.16),
        (1.5, 0, 0.16),
        (0, -1.5, 0.16),
        (0, 1.5, 0.16),
        (-1, -1, 0.14),
        (1, -1, 0.14),
        (-1, 1, 0.14),
        (1, 1, 0.14),
        (-2.5, 0, 0.06),
        (2.5, 0, 0.06),
        (0, -2.5, 0.06),
        (0, 2.5, 0.06),
    ):
        text(p, x + dx, y + dy, s, f, col(team_color, a))
    return text(p, x, y, s, f, col("white"))


RULE_HEAD = 28  # px of the bright tip a travelling rule carries


def gold_rule(p, x0, x1, y, progress=1.0, center_diamond=True):
    """Gold rule fading at both ends, drawn outward from the centre as it animates;
    while it travels each end carries a bright tip."""
    cx = (x0 + x1) / 2
    half = (x1 - x0) / 2 * progress
    if half <= 0:
        return
    gradient = QLinearGradient(x0, 0, x1, 0)
    gradient.setColorAt(0.0, col("gold_rule", 0.0))
    gradient.setColorAt(0.18, col("gold_rule", 0.85))
    gradient.setColorAt(0.82, col("gold_rule", 0.85))
    gradient.setColorAt(1.0, col("gold_rule", 0.0))
    p.setPen(QPen(QBrush(gradient), 1.0))
    p.drawLine(QPointF(cx - half, y + 0.5), QPointF(cx + half, y + 0.5))
    if progress < 1.0:
        tip = 0.9 * min(1.0, (1.0 - progress) / 0.15)
        length = min(RULE_HEAD, half)
        for end, inner in ((cx - half, cx - half + length), (cx + half, cx + half - length)):
            head = QLinearGradient(end, 0, inner, 0)
            head.setColorAt(0.0, col("gold_soft", tip))
            head.setColorAt(1.0, col("gold_soft", 0.0))
            p.fillRect(QRectF(min(end, inner), y, length, 1.5), head)
    if center_diamond:
        diamond(p, cx, y + 0.5, 5, QPen(col("gold_rule"), 1.0), QBrush(col("bg")))


def chamfer(rect, cut):
    """Rect with the top-left and bottom-right corners cut at 45 degrees."""
    path = QPainterPath()
    path.moveTo(rect.left() + cut, rect.top())
    path.lineTo(rect.right(), rect.top())
    path.lineTo(rect.right(), rect.bottom() - cut)
    path.lineTo(rect.right() - cut, rect.bottom())
    path.lineTo(rect.left(), rect.bottom())
    path.lineTo(rect.left(), rect.top() + cut)
    path.closeSubpath()
    return path


def cap_middle(f, baseline):
    """The visual middle of capital letters drawn with ``f`` at ``baseline``."""
    return baseline - QFontMetricsF(f).capHeight() / 2


def rank_badge(p, cx, cy, tier, r=11):
    """Double diamond as on the game's role icons; both text lines sit beside it."""
    color = TIER_COLOR.get(tier, TIER_COLOR["none"])
    diamond(p, cx, cy, r, QPen(col(color, 0.5), 1.0), None)
    diamond(p, cx, cy, r - 4, QPen(col(color), 1.2), QBrush(col(color, 0.2)))
    diamond(p, cx, cy, 2, None, QBrush(col(color)))


STRIKE, GLINT = 0.24, 0.32  # the one-shot accents on a rank and on a high peak


def _strike(p, cx, cy, r, tier_key, dt, loud=True):
    """A rank landing: a diamond ring leaves the emblem and the core flashes once."""
    if dt < 0 or dt >= STRIKE:
        return
    e = motion.out_cubic(dt / STRIKE)
    color = TIER_COLOR.get(tier_key, TIER_COLOR["none"])
    diamond(p, cx, cy, r + (r * 0.8) * e, QPen(col(color, (0.8 if loud else 0.5) * (1 - e)), 1.2))
    diamond(p, cx, cy, 2 + (1 - e), None, QBrush(col("white", (0.9 if loud else 0.5) * (1 - e))))


def _flag(p, right, y_mid, label, kind):
    """A small chip with a fixed right edge: NEW and YOU share one slot."""
    f = font(10, 700, 0.10)
    w = _advance(label, f) - f.letterSpacing() + 10
    rect = QRectF(right - w, y_mid - 7, w, 14)
    if kind == "new":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col("new", 0.9))
        ink = col("bg")
    else:
        p.setPen(QPen(col("self", 0.6), 1.0))
        p.setBrush(col("self", 0.10))
        rect = rect.adjusted(0.5, 0.5, -0.5, -0.5)
        ink = col("self")
    p.drawPath(chamfer(rect, 3))
    text(p, right - 5, y_mid + 3.5, label, f, ink, "r")
    return w + 6


def _flag_of(d):
    return ("YOU", "me") if d["me"] else (("NEW", "new") if d["new"] else None)


def wr_color(wr):
    if wr is None:
        return col("grey4")
    if wr >= 55:
        return col("gold")
    if wr < 47:
        return col("low_wr")
    return col("white")


SIDE = {"against": ("loss", "against_text"), "with": ("dawn", "with_text")}


def _advance(s, f):
    return QFontMetricsF(f).horizontalAdvance(s)


def name_font(px, s, max_w):
    """Bahnschrift's width axis: condense a long name before eliding it."""
    for width in (100, 92, 84, 76):
        f = font(px, 500, 0.05 if width == 100 else 0.03, caps=True)
        f.setVariableAxis(QFont.Tag("wdth"), float(width))
        if _advance(s, f) <= max_w:
            return f
    return f


HEAD_TIME = 0.30  # the team average arrives, the rule draws back to make room


def team_header(p, width, y, team, rows, mode, source, balance=None, reveal=None):
    """Team word, mode, an ornament, the team average and who is ahead on it.

    ``reveal`` is the seconds since the average arrived, None once it has settled.
    """
    label = "ALLIES" if team == "ally" else "ENEMIES"
    title = font(17, 700, 0.12)
    x = COL_NAME + _glow(p, COL_NAME, y, label, title, "dawn" if team == "ally" else "dusk") + 10
    if mode and team == "ally":
        x += text(p, x, y, mode, font(11, 600, 0.10, caps=True), col("grey2")) + 8
    small = font(12, 600, 0.10, caps=True)
    if source == "predgg":
        end, average = COL_RANK_R, team_winrate(rows)
        value, tier_key = (f"{average}%" if average is not None else ""), None
        caption = font(10, 600, 0.12)
        for key, title_ in (("wr", "WIN %"), ("kda", "KDA"), ("games", "GAMES")):
            text(p, STACK_B[key], y, title_, caption, col("grey3"), "r")
        text(p, COL_R, y, "LAST 10", caption, col("grey3"), "r")
    else:
        end, average = COL_R, team_rank(rows)
        value, tier_key = (average or ""), average
    arrived = 1.0 if reveal is None else motion.out_quart(motion.clip(reveal / HEAD_TIME))
    tint = 1.0 if reveal is None else motion.clip(reveal / HEAD_TIME)
    base = p.opacity()
    p.setOpacity(base * arrived)
    used = 0
    if value:
        tone = TIER_COLOR.get((tier_key or "").split(" ")[0].lower(), PALETTE["grey1"])
        used = text(p, end, y, value, small, QColor(tone), "r")
        used += _advance(" ", small)
        used += text(p, end - used, y, "AVG", small, col("grey3"), "r") - small.letterSpacing()
    if balance:
        ink = {"ally": "win", "enemy": "loss"}.get(balance["side"], "grey1")
        color = _lerp_color("grey1", ink, tint)
        used += 14 + text(p, end - used - 14, y, balance["text"], small, color, "r")
    p.setOpacity(base)
    middle = y - QFontMetricsF(title).capHeight() / 2
    ornament_rule(p, x, end - used * arrived - 12, middle, inner=True)


def stack_size(n_allies, n_enemies, width=640):
    rows = (STACK_ROW_H + STACK_GAP) * (n_allies + n_enemies) - 2 * STACK_GAP
    return width, PAD_TOP + 2 * STACK_HDR + rows + TEAM_GAP + PAD_BOTTOM


def _row_background(p, rect, team, me, bar_right=False):
    """The game's plate: lit at top and bottom, dark inside, fading away from the bar."""
    edge, line = ("edge_ally", "line_ally") if team == "ally" else ("edge_enemy", "line_enemy")
    base = QLinearGradient(rect.left(), 0, rect.right(), 0)
    base.setColorAt(0.0, col("plate_mid", 0.96))
    base.setColorAt(0.6, col("plate_mid", 0.88))
    base.setColorAt(1.0, col("plate_end", 0.62))
    p.fillRect(rect, base)
    bands = QLinearGradient(0, rect.top(), 0, rect.bottom())
    bands.setColorAt(0.0, col(edge, 0.95))
    bands.setColorAt(0.38, col(edge, 0.0))
    bands.setColorAt(0.62, col(edge, 0.0))
    bands.setColorAt(1.0, col(edge, 0.95))
    p.fillRect(rect, bands)
    hair = QLinearGradient(rect.left(), 0, rect.right(), 0)
    hair.setColorAt(0.0, col("self" if me else line, 0.7 if me else 0.9))
    hair.setColorAt(1.0, col("self" if me else line, 0.1))
    p.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 1), hair)
    p.fillRect(QRectF(rect.left(), rect.bottom() - 1, rect.width(), 1), hair)
    if me:
        tint = QLinearGradient(rect.left(), 0, rect.right(), 0)
        tint.setColorAt(0.0, col("self", 0.12))
        tint.setColorAt(1.0, col("self", 0.0))
        p.fillRect(rect, tint)
    bar_x = rect.right() - BAR_W if bar_right else rect.left()
    p.fillRect(
        QRectF(bar_x, rect.top(), BAR_W, rect.height()),
        col("dawn_hdr" if team == "ally" else "dusk_hdr"),
    )


def _peak(p, top, data):
    """The best rank of a past season, in its own column of the row."""
    if not data["peak"]:
        return
    color = TIER_COLOR.get(data["peak"]["tier"], TIER_COLOR["none"])
    f = font(12, 600, 0.08, caps=True)
    text(
        p, COL_PEAK, top + LINE1, data["peak"]["rank"], f, col(color, 0.9), w=COL_PEAK_R - COL_PEAK
    )
    text(p, COL_PEAK, top + LINE2, "PEAK", font(11, 500, 0.08, caps=True), col("grey3"))


def _glint(p, top, data, dt, loud=True):
    """One band of light across a Master or Paragon peak, along the chamfer's angle."""
    if dt < 0 or dt >= GLINT or not data["peak"]:
        return
    f = font(12, 600, 0.08, caps=True)
    rank = data["peak"]["rank"]
    room = COL_PEAK_R - COL_PEAK
    width = min(_advance(rank, f), room)
    band, height = 22, 13
    base = top + LINE1
    x = COL_PEAK - band - height + (width + 2 * band + height) * motion.in_out_quad(dt / GLINT)
    path = QPainterPath()
    path.moveTo(x + height, base - height + 3)
    path.lineTo(x + band + height, base - height + 3)
    path.lineTo(x + band, base + 3)
    path.lineTo(x, base + 3)
    path.closeSubpath()
    p.save()
    p.setClipPath(path, Qt.ClipOperation.IntersectClip)
    color = TIER_COLOR.get(data["peak"]["tier"], TIER_COLOR["none"])
    text(p, COL_PEAK, base, rank, f, _lerp_color(color, "white", 0.8 if loud else 0.5), w=room)
    p.restore()


def _meta(p, x, baseline, data, with_role=False):
    """LV, age and role on fixed tab stops: nothing depends on another field's width."""
    f = tnum(font(12, 500, 0.08, caps=True))
    value = col("new") if data["new"] or data["young_high"] else col("grey1")
    if data["level"] is not None:
        lv = text(p, x, baseline, "LV ", f, col("grey3"))
        text(p, x + lv, baseline, str(data["level"]), f, value)
    if data["age"]:
        text(p, x + TAB_AGE, baseline, data["age"], f, value)
    stats = data.get("stats") or {}
    if with_role and stats.get("role") and not _flag_of(data):  # the flag has this slot
        text(p, x + TAB_ROLE, baseline, stats["role"], f, col("grey3"))


def _rank(p, x, baseline, data, px=14, w=None, icon=True):
    f = font(px, 600, 0.06, caps=True)
    color = TIER_COLOR.get(data["tier"]) if data["tier"] != "none" else PALETTE["grey2"]
    text(p, x, baseline, data["rank"], f, QColor(color), w=w)


def _vp_bar(p, x, y_mid, data, w=VP_BAR_W, fill=None, track=1.0, label=True, right=COL_RANK_R):
    """The bar and its number, right-aligned at ``right``; a negative VP reads in red."""
    if data["vp"] is None:
        if data["rank"] == "UNRANKED" and label:
            text(p, x, y_mid + 4, "NOT PLACED", font(11, 500, 0.08, caps=True), col("grey3"))
        return
    color = TIER_COLOR.get(data["tier"], TIER_COLOR["none"])
    value = data["vp"] if fill is None else fill
    p.fillRect(QRectF(x, y_mid - 2, w, 3), col("grey4", 0.35 * track))
    filled = max(0.0, min(value, 100)) / 100
    p.fillRect(QRectF(x, y_mid - 2, w * filled, 3), col(color, 0.9 * track))
    if label:
        f = tnum(font(11, 500, 0.06, caps=True))
        vp = data["vp"]
        if vp < 0:
            text(p, right, y_mid + 4, f"\u2212{-vp} VP", f, col("loss", 0.9), "r")
        else:
            text(p, right, y_mid + 4, f"{vp} VP", f, col("grey1"), "r")


# A field that changes swaps out then in: different-length text never sits on itself.
SWAP_OUT, SWAP_IN_AT, SWAP_IN = 0.07, 0.05, 0.12
VP_DELAY, VP_TIME = 0.10, 0.36
SWAP_DONE = VP_DELAY + VP_TIME  # the longest field change
_MIX_TIME = {"vp": SWAP_DONE, "strike": STRIKE, "glint": GLINT}


def mix_time(group):
    """How long a field group's change keeps animating, in seconds."""
    return _MIX_TIME.get(group, SWAP_IN_AT + SWAP_IN)


def _swap(p, mix, group, data, draw, rise=3.0):
    """Draw ``data`` with ``draw``; if ``group`` just changed, old out then new in."""
    old, dt = (mix or {}).get(group, (None, None))
    if old is None or dt >= SWAP_IN_AT + SWAP_IN:
        draw(data)
        return
    base = p.opacity()
    a_old = 1 - motion.in_cubic(min(1.0, max(0.0, dt / SWAP_OUT)))
    a_new = motion.out_cubic(min(1.0, max(0.0, (dt - SWAP_IN_AT) / SWAP_IN)))
    if a_old > 0:
        p.save()
        p.setOpacity(base * a_old)
        draw(old)
        p.restore()
    if a_new > 0:
        p.save()
        p.setOpacity(base * a_new)
        p.translate(0, (1 - a_new) * rise)
        draw(data)
        p.restore()


def _vp_swap(p, mix, x, y_mid, data, w=VP_BAR_W, right=COL_RANK_R):
    """The VP bar fills from its old value; its label and the track swap like text."""
    old, dt = (mix or {}).get("vp", (None, None))
    if old is None or data["vp"] is None:
        _swap(p, mix, "vp", data, lambda d: _vp_bar(p, x, y_mid, d, w, right=right))
        return
    start = old["vp"] if old["vp"] is not None else 0
    t = motion.out_cubic(motion.clip((dt - VP_DELAY) / VP_TIME))
    track = 1.0 if old["vp"] is not None else motion.out_cubic(motion.clip(dt / 0.16))
    fill = start + (data["vp"] - start) * t
    _vp_bar(p, x, y_mid, data, w, fill=fill, track=track, label=False, right=right)
    if 0.0 < t < 1.0:  # the charge's bright head, gone as it arrives
        color = TIER_COLOR.get(data["tier"], TIER_COLOR["none"])
        head = x + w * motion.clip(fill / 100) - 1
        p.fillRect(QRectF(head, y_mid - 3.5, 2, 6), _alpha(_lerp_color(color, "white", 0.6), 1 - t))
    _swap(p, mix, "vp", data, lambda d: _vp_bar(p, x, y_mid, d, w, track=0.0, right=right))


def _encounter_block(p, top, data):
    """Four fields, four fixed slots: hero | result over side | when."""
    e = data["encounter"]
    accent, ink = SIDE[e["side"]]
    p.fillRect(QRectF(COL_ENC, top + 6, 2, 28), col(accent, 0.9))
    result_w = 0
    if e["result"]:
        f = font(11, 500, 0.08, caps=True)
        result_w = text(p, COL_R, top + LINE1, e["result"], f, col("grey1"), "r") + 8
    room = COL_R - COL_TEXT - result_w
    if e["hero"]:
        f = name_font(14, e["hero"], room)
        text(p, COL_TEXT, top + LINE1, e["hero"], f, col("name"), w=room)
    else:
        text(p, COL_TEXT, top + LINE1, "–", font(14, 500), col("grey4"))
    side = "AGAINST US" if e["side"] == "against" else "WITH US"
    text(p, COL_TEXT, top + LINE2, side, font(11, 600, 0.08, caps=True), col(ink))
    text(p, COL_R, top + LINE2, e["when"], font(11, 500, 0.08, caps=True), col("grey3"), "r")


def _encounter_pill(p, right, y_mid, data, max_w):
    """Compact encounter for Pred.gg rows: 'VS · RIKTOR · LAST', right-aligned."""
    e = data["encounter"]
    accent, ink = SIDE[e["side"]]
    when = "LAST" if e["when"] == "LAST MATCH" else e["when"].split()[0] + " AGO"
    parts = ["VS" if e["side"] == "against" else "WITH", e["hero"], when]
    label = " · ".join(part for part in parts if part)
    f = font(11, 600, 0.07, caps=True)
    inner = min(_advance(label, f), max_w - 14)
    w, h = inner + 14, 17
    rect = QRectF(right - w, y_mid - h / 2, w, h)
    p.setPen(QPen(col(accent, 0.6), 1.0))
    p.setBrush(col(accent, 0.14))
    p.drawPath(chamfer(rect.adjusted(0.5, 0.5, -0.5, -0.5), 5))
    text(p, right - w + 7, y_mid + 4, label, f, col(ink), w=inner)
    return w


def _heroes(p, x, baseline, heroes, max_w, px=12):
    """Top heroes 'NAME 58%': whole items only, never cut mid-name."""
    name_f, value_f = font(px, 500, 0.05, caps=True), font(px, 600, 0.02)
    gap = _advance("   ", name_f)
    used = 0
    for i, (hero, winrate) in enumerate(heroes):
        value = f"{winrate}%" if winrate is not None else ""
        need = _advance(hero, name_f) + (_advance(" " + value, value_f) if value else 0)
        need += gap if i else 0
        if used + need > max_w:
            break
        cx = x + used + (gap if i else 0)
        w = text(p, cx, baseline, hero, name_f, col("grey1"))
        if value:
            text(p, cx + w + _advance(" ", value_f), baseline, value, value_f, wr_color(winrate))
        used += need
    return used


def _skeleton(p, x, y_mid, w, h=8, right=False):
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(col("grey4", 0.28))
    p.drawRoundedRect(QRectF(x - w if right else x, y_mid - h / 2, w, h), 2, 2)


def _form(p, right, y_mid, form, step=8.6):
    form = form[-10:]
    x = right - len(form) * step + 1
    for i, result in enumerate(form):
        recent = i >= len(form) - 3
        color = col("win" if result == "W" else "loss", 0.95 if recent else 0.5)
        diamond(p, x + i * step + 3.5, y_mid, 3.5, None, QBrush(color))


STACK_B = {"wr": 392, "kda": 440, "games": 496}  # Pred.gg number columns: right edges


def stack_row(p, rect, data, motion, source="omeda", mix=None):
    """One roster row on the panel's grid. ``mix`` maps a changed field group to
    (old row, seconds since)."""
    _row_background(p, rect, data["team"], data["me"])
    top = rect.top()
    line1, line2 = top + LINE1, top + LINE2
    middle = top + rect.height() / 2

    def name(d):
        room = COL_NAME_R - COL_NAME  # a flag lives on line 2: it never squeezes the name
        color = col("self") if d["me"] else col("name")
        text(p, COL_NAME, line1, d["name"], name_font(18, d["name"], room), color, w=room)

    def meta(d):
        _meta(p, COL_NAME, line2, d, with_role=source == "predgg")
        if _flag_of(d):
            _flag(p, COL_NAME_R, line2 - 4, *_flag_of(d))

    def rank(d):
        rank_badge(p, COL_ICON, middle, d["tier"])
        if d["state"] == "loading":  # only this cell waits; the row itself stays still
            p.save()
            p.setOpacity(p.opacity() * (0.55 + 0.45 * motion.pulse))
            _skeleton(p, COL_RANK, line1 - 5, 70, 9)
            p.restore()
            return
        _rank(p, COL_RANK, line1, d, 14, w=COL_RANK_R - COL_RANK)

    fx = mix or {}
    _swap(p, mix, "name", data, name)
    _swap(p, mix, "meta", data, meta)
    _swap(p, mix, "rank", data, rank)
    if "strike" in fx:
        _strike(p, COL_ICON, middle, 11, data["tier"], fx["strike"][1], motion.loud)
    _vp_swap(p, mix, COL_RANK, line2 - 4, data)
    if source != "predgg":
        _swap(p, mix, "peak", data, lambda d: _peak(p, top, d))
        if "glint" in fx:
            _glint(p, top, data, fx["glint"][1], motion.loud)

        def intel(d):
            if d["encounter"]:
                _encounter_block(p, top, d)

        _swap(p, mix, "intel", data, intel, rise=4.0)
        return
    numbers = tnum(font(17, 600, 0.02))

    def numbers_block(d):
        stats = d["stats"]
        if stats is None:
            if d["stats_state"] == "loading":
                for key in STACK_B:
                    _skeleton(p, STACK_B[key], cap_middle(numbers, line1), 32, 9, right=True)
                _skeleton(p, COL_PEAK, line2 - 4, 150, 7)
            return
        for key, value, color in (
            ("wr", None if stats["wr"] is None else f"{stats['wr']}%", wr_color(stats["wr"])),
            ("kda", None if stats["kda"] is None else f"{stats['kda']:.1f}", col("white")),
            ("games", str(stats["games"]), col("grey1")),
        ):
            shown = value or "–"
            text(p, STACK_B[key], line1, shown, numbers, color if value else col("grey4"), "r")
        _form(p, COL_R, cap_middle(numbers, line1), stats["form"])
        pill = 0
        if d["encounter"]:
            pill = _encounter_pill(p, COL_R, line2 - 4, d, COL_R - COL_PEAK) + 10
        _heroes(p, COL_PEAK, line2, stats["heroes"], COL_R - COL_PEAK - pill)

    _swap(p, mix, "stats", data, numbers_block)


def paint_stack(
    p,
    width,
    allies,
    enemies,
    motion,
    mode=None,
    source="omeda",
    mixes=None,
    height=None,
    from_bottom=False,
    status=None,
    status_alpha=0.0,
    heads=None,
):
    """The draft / loading-screen panel; origin at the panel's top-left.

    ``height`` draws the panel shorter than its full size, grown from the top
    (or from the bottom with ``from_bottom``), while it grows out of the status
    plate; ``status`` fades out over it. ``mixes`` maps a player to the field
    groups that just changed (see stack_row), ``heads`` a team to the seconds
    since its average arrived (see team_header).
    """
    _, full = stack_size(len(allies), len(enemies), width)
    shown = full if height is None else min(full, height)
    top = full - shown if from_bottom else 0.0
    panel = QRectF(0, top, width, shown)
    gradient = QLinearGradient(0, top, 0, top + shown)
    gradient.setColorAt(0.0, col("panel_top", 0.86))
    gradient.setColorAt(1.0, col("bg", 0.90))
    p.fillRect(panel, gradient)
    p.setPen(QPen(col("border", 0.28), 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(panel.adjusted(0.5, 0.5, -0.5, -0.5))
    p.save()
    p.setClipRect(QRectF(0, top + 1, width, shown - 1))
    y, index = PAD_TOP, 0
    balance = team_balance(allies, enemies) if source != "predgg" else None
    for team, rows in (("ally", allies), ("enemy", enemies)):
        first = motion.row(index)
        if first > 0:
            p.save()
            p.setOpacity(p.opacity() * first)
            shared = balance if team == "enemy" else None
            reveal = (heads or {}).get(team)
            team_header(p, width, y + 18, team, rows, mode, source, shared, reveal)
            p.restore()
        y += STACK_HDR
        for data in rows:
            progress = motion.row(index)
            if progress > 0:
                p.save()
                p.setOpacity(p.opacity() * progress)
                p.translate(0, (1 - progress) * motion.rise)
                mix = (mixes or {}).get(data["player_id"])
                rect = QRectF(PLATE_L, y, PLATE_R - PLATE_L, STACK_ROW_H)
                stack_row(p, rect, data, motion, source, mix)
                p.restore()
            y += STACK_ROW_H + STACK_GAP
            index += 1
        y += TEAM_GAP - STACK_GAP
    p.restore()
    gold_rule(p, 0, width, top, motion.rule)
    # The closing rule finishes with the last row, whichever end that is.
    tail = min(motion.row(0), motion.row(max(index - 1, 0))) if index else 1.0
    gold_rule(p, 0, width, top + shown - 1, min(motion.rule, tail), center_diamond=False)
    if status and status_alpha > 0:
        p.save()
        p.setOpacity(p.opacity() * status_alpha)
        plate_top = full - 44 if from_bottom else 0.0
        text(
            p,
            width / 2,
            plate_top + 28,
            status,
            font(15, 600, 0.14, caps=True),
            col("gold_soft"),
            "c",
        )
        p.restore()


CARD_W = 600
C_L, C_R = 20, 580
C_FLAG_R = 352
C_ICON, C_RANK = 374, 396  # head: same x as the table's PEAK column
C_LADDER = 92
C_FINAL, C_FINAL_POS, C_TOP = 216, 340, 384
C_PEAK, C_PEAK_POS = 396, 520
HEAD_H, VERDICT_H, FACTS_H, STATS_H, TABLE_HDR, SEASON_H, CARD_PAD_B = 66, 24, 40, 50, 22, 20, 12
MAX_SEASONS = 9  # beyond this the oldest fold into one line: the card has no scrolling
PIP_W, PIP_STEP, TIER_GAP = 5, 6, 2
SEASON_STAGGER, SEASON_FADE = 0.02, 0.13  # at most ten rows: done within 350 ms
TABLE_TIME = 10 * SEASON_STAGGER + SEASON_FADE
LADDER_TIERS = (
    ("B", "bronze", 3),
    ("S", "silver", 3),
    ("G", "gold", 3),
    ("P", "platinum", 3),
    ("D", "diamond", 3),
    ("M", "master", 1),
    ("P", "paragon", 1),
)
LADDER_CELLS = sum(n for _, _, n in LADDER_TIERS)


def _pip(name):
    """A rank's cell on the ladder: three steps per rank, Master and Paragon one each."""
    step = rank_step(name)
    if step is None:
        return None
    return LADDER_CELLS - 1 if name == "Paragon" else min(step, LADDER_CELLS - 2)


def _pip_x(cell):
    group = sum(1 for boundary in (3, 6, 9, 12, 15, 16) if cell >= boundary)
    return cell * PIP_STEP + group * TIER_GAP


def season_short(name):
    m = re.fullmatch(r"Season (\d+)(?: - Split (\d+))?", name or "")
    if not m:
        return (name or "").replace(" - ", " ").upper()
    return f"S{m[1]} SPLIT {m[2]}" if m[2] else f"SEASON {m[1]}"


def season_rows(data):
    """Newest first: the running rank, then finished seasons, the oldest folded if too many."""
    seasons = data.get("seasons")
    if not seasons:
        return []
    rows = [{"kind": "now"}] + [{"kind": "season", **s} for s in seasons]
    if len(seasons) > MAX_SEASONS:
        keep, old = seasons[: MAX_SEASONS - 1], seasons[MAX_SEASONS - 1 :]
        best = max((s for s in old if s["peak"]), key=lambda s: rank_step(s["peak"]), default=None)
        fold = {"kind": "fold", "count": len(old), "best": best}
        rows = [{"kind": "now"}] + [{"kind": "season", **s} for s in keep] + [fold]
    return rows


def card_size(data, source, seen=None):
    """Width and height of the in-match card for one player."""
    height = HEAD_H
    if data["verdict"]:
        height += VERDICT_H
    if data["encounter"] or (seen and (seen.get("hero") or seen.get("role"))):
        height += FACTS_H
    if source == "predgg":
        height += STATS_H
    rows = season_rows(data)
    if rows:
        height += TABLE_HDR + len(rows) * SEASON_H
    return CARD_W, height + CARD_PAD_B


def card_chrome(p, width, height, team, rule=1.0, was=None, bar=1.0):
    """The card's plate, gold rule and team bar (drawn once while its content swaps).

    Flying to the other team, ``team``'s colour runs down over ``was`` as ``bar`` grows.
    """
    gradient = QLinearGradient(0, 0, 0, height)
    gradient.setColorAt(0.0, col("panel_top", 0.96))
    gradient.setColorAt(1.0, col("bg", 0.97))
    p.setPen(QPen(col("border", 0.6), 1.0))
    p.setBrush(QBrush(gradient))
    p.drawPath(chamfer(QRectF(0.5, 0.5, width - 1, height - 1), 9))
    gold_rule(p, 12, width - 12, 0.5, rule)
    length = height - 24
    if was and bar < 1.0:
        p.fillRect(QRectF(0, 12, BAR_W, length), col("dawn_hdr" if was == "ally" else "dusk_hdr"))
        length *= bar
    p.fillRect(QRectF(0, 12, BAR_W, length), col("dawn_hdr" if team == "ally" else "dusk_hdr"))


def paint_card_reading(p, team, motion, chrome=True):
    """A card skeleton while the scoreboard is being read (the first TAB of a match)."""
    width, height = CARD_W, 92
    if chrome:
        card_chrome(p, width, height, team, motion.rule)
    p.save()
    p.setOpacity(p.opacity() * (0.55 + 0.45 * motion.pulse))
    _skeleton(p, C_L, 28, 190, 12)
    _skeleton(p, C_RANK, 28, 120, 10)
    _skeleton(p, C_L, 52, 120, 8)
    p.restore()
    text(p, C_R, 56, "READING SCOREBOARD", font(11, 600, 0.12, caps=True), col("grey3"), "r")


def _hairline(p, y, alpha=1.0):
    gradient = QLinearGradient(C_L, 0, C_R, 0)
    gradient.setColorAt(0.0, col("sep", 0.9 * alpha))
    gradient.setColorAt(1.0, col("sep", 0.35 * alpha))
    p.fillRect(QRectF(C_L, y, C_R - C_L, 1), gradient)


def _ladder(p, x, top, final, peak, fill=1.0):
    """17 pips, Bronze III to Paragon, in the same places on every row; ``fill`` runs
    the pips in from the left as the row reveals."""
    y = top + (SEASON_H - 8) / 2
    a, b = _pip(final), _pip(peak)
    reached = fill * LADDER_CELLS
    for cell in range(LADDER_CELLS):
        cx = x + _pip_x(cell)
        if cell >= reached:
            color = col("grey4", 0.16)
        elif a is not None and cell <= a:
            color = col(TIER_COLOR[tier(final)], 0.75)
        elif b is not None and cell <= b:
            color = col(TIER_COLOR[tier(peak)], 0.28)
        else:
            color = col("grey4", 0.16)
        p.fillRect(QRectF(cx, y, PIP_W, 8), color)
    if b is not None and b < reached:
        p.fillRect(QRectF(x + _pip_x(b), y - 2, PIP_W, 12), col(TIER_COLOR[tier(peak)]))


def _ladder_axis(p, x, baseline):
    f = font(10, 600, 0.0)
    cell = 0
    for letter, key, n in LADDER_TIERS:
        cx = x + _pip_x(cell) + (_pip_x(cell + n - 1) + PIP_W - _pip_x(cell)) / 2
        text(p, cx, baseline, letter, f, col(TIER_COLOR[key], 0.75), "c")
        cell += n


def _season_table(p, y, rows, data, reveal=None):
    """The history; ``reveal`` (seconds since the card opened) brings the rows in one
    after another, each ladder filling from the left. None draws it settled."""
    caption = font(10, 600, 0.12)
    grey = col("grey3")
    base_alpha = p.opacity()

    def entered(index):
        if reveal is None:
            return 1.0
        return motion.out_cubic(motion.clip((reveal - index * SEASON_STAGGER) / SEASON_FADE))

    _hairline(p, y)
    p.setOpacity(base_alpha * entered(0))
    head = y + 15
    text(p, C_L, head, "SEASON", caption, grey)
    _ladder_axis(p, C_LADDER, head)
    text(p, C_FINAL, head, "FINAL", caption, grey)
    text(p, C_FINAL_POS, head, "#", caption, grey, "r")
    text(p, C_TOP, head, "TOP", caption, grey, "r")
    text(p, C_PEAK, head, "PEAK", caption, grey)
    text(p, C_PEAK_POS, head, "#", caption, grey, "r")
    text(p, C_R, head, "DATE", caption, grey, "r")
    y += TABLE_HDR
    ranks = font(11, 600, 0.06, caps=True)
    label = font(11, 500, 0.06, caps=True)
    numeric = tnum(font(11, 500, 0.04, caps=True))
    peaks = [_row_peak(r) for r in rows if _row_peak(r)]
    best = max((rank_step(peak) for peak in peaks), default=None) if len(peaks) > 1 else None
    for index, row_ in enumerate(rows):
        amount = entered(index)
        p.setOpacity(base_alpha * amount)
        base = y + 14
        if _row_peak(row_) and rank_step(_row_peak(row_)) == best:
            p.fillRect(QRectF(C_L - 6, y + 1, C_R - C_L + 12, SEASON_H - 2), col("gold_rule", 0.10))
        _season_row(p, row_, y, base, ranks, label, numeric, data, amount)
        y += SEASON_H
        if row_ is not rows[-1]:
            p.fillRect(QRectF(C_L, y - 1, C_R - C_L, 1), col("row_line", 0.8))
    p.setOpacity(base_alpha)
    return y


def _row_peak(r):
    if r["kind"] == "fold":
        return r["best"]["peak"] if r["best"] else None
    return r.get("peak")


def _rank_text(p, x, base, name, f, alpha=1.0):
    if name:
        text(p, x, base, name, f, col(TIER_COLOR[tier(name)], alpha), w=C_FINAL_POS - C_FINAL - 40)


def _num(p, right, base, value, f, prefix="", suffix=""):
    known = value is not None
    label = f"{prefix}{value}{suffix}" if known else "–"
    text(p, right, base, label, f, col("grey1") if known else col("grey4"), "r")


def _season_row(p, r, y, base, ranks, label, numeric, data, fill=1.0):
    if r["kind"] == "now":  # the running rank, from the official profile
        text(p, C_L, base, "NOW", label, col("gold_soft"))
        placed = data["tier"] != "none"
        _ladder(p, C_LADDER, y, data["rank"] if placed else None, None, fill)
        if placed:
            _rank_text(p, C_FINAL, base, data["rank"], ranks)
        else:
            text(p, C_FINAL, base, data["rank"], ranks, col("grey4"))
        if data["vp"] is not None:
            text(p, C_FINAL_POS, base, f"{data['vp']} VP", numeric, col("grey1"), "r")
        return
    if r["kind"] == "fold":
        text(p, C_L, base, f"+{r['count']} OLDER", label, col("grey3"))
        peak = r["best"]["peak"] if r["best"] else None
        _ladder(p, C_LADDER, y, None, peak, fill)
        _rank_text(p, C_PEAK, base, peak, ranks, 0.9)
        return
    text(p, C_L, base, season_short(r["name"]), label, col("grey1"), w=C_LADDER - C_L - 4)
    if r.get("unranked") or not (r["final"] or r["peak"]):
        _ladder(p, C_LADDER, y, None, None, fill)
        text(p, C_FINAL, base, "UNRANKED", ranks, col("grey4"))
        return
    _ladder(p, C_LADDER, y, r["final"], r["peak"], fill)
    _rank_text(p, C_FINAL, base, r["final"], ranks)
    _num(p, C_FINAL_POS, base, r["ranking"], numeric, "#")
    _num(p, C_TOP, base, r["percentile"], numeric, suffix="%")
    _rank_text(p, C_PEAK, base, r["peak"], ranks, 0.9)
    _num(p, C_PEAK_POS, base, r["peak_ranking"], numeric, "#")
    text(p, C_R, base, r["peak_date"] or "–", numeric, col("grey3"), "r")


def paint_card(p, data, motion, source="omeda", seen=None, chrome=True, reveal=None):
    """The card shown next to the scoreboard row under the cursor while TAB is held.

    ``seen`` is what the scoreboard row says about the player in this match:
    {"hero": "STEEL", "role": "OFFLANE"} (read once from the screen). With
    ``chrome=False`` only the content is drawn (to crossfade between players).
    ``reveal`` is the seconds since the card opened, while its table comes in.
    """
    width, height = card_size(data, source, seen)
    if chrome:
        card_chrome(p, width, height, data["team"], motion.rule)
    line1, line2 = 32, 52
    room = C_FLAG_R - C_L
    color = col("self") if data["me"] else col("name")
    text(p, C_L, line1, data["name"], name_font(22, data["name"], room), color, w=room)
    _meta(p, C_L, line2, data, with_role=source == "predgg")
    if _flag_of(data):
        _flag(p, C_FLAG_R, line2 - 4, *_flag_of(data))
    rank_badge(p, C_ICON, (line1 + line2) / 2 - 5, data["tier"], 13)
    _rank(p, C_RANK, line1, data, 16, w=C_R - C_RANK)
    _vp_bar(p, C_RANK, line2 - 4, data, w=C_PEAK_POS - C_RANK, right=C_R)
    y = HEAD_H
    if data["verdict"]:
        _hairline(p, y)
        ink = col("new") if data["verdict"]["alert"] else col("gold_soft")
        line = font(12, 600, 0.10, caps=True)
        text(p, C_L, y + 17, data["verdict"]["text"], line, ink, w=C_R - C_L)
        y += VERDICT_H
    played = seen and (seen.get("hero") or seen.get("role"))
    if played or data["encounter"]:
        _hairline(p, y)
        caption = font(10, 600, 0.12)
        value = font(12, 600, 0.06, caps=True)
        separator = "  ·  "
        if played:
            text(p, C_L, y + 15, "THIS MATCH", caption, col("grey3"))
            parts = [part for part in (seen.get("hero"), seen.get("role")) if part]
            stats = data.get("stats") or {}
            hero = (stats.get("hero_stats") or {}).get((seen.get("hero") or "").upper())
            if hero and hero.get("games"):
                enough = hero["games"] >= HERO_MIN and hero.get("winrate") is not None
                share = f" {round(hero['winrate'] * 100)}%" if enough else ""
                parts.append(f"{hero['games']} GAMES{share}")
            text(
                p, C_L, y + 31, separator.join(parts), value, col("gold_soft"), w=C_FINAL - C_L - 16
            )
        if data["encounter"]:
            e = data["encounter"]
            accent, ink = SIDE[e["side"]]
            p.fillRect(QRectF(C_FINAL - 10, y + 8, 2, 25), col(accent, 0.9))
            text(p, C_FINAL, y + 15, "LAST MET", caption, col("grey3"))
            x = C_FINAL
            side = "AGAINST US" if e["side"] == "against" else "WITH US"
            for s, c in (
                (e["hero"], col("name")),
                (e["when"], col("grey1")),
                (side, col(ink)),
                (e["result"], col("grey1")),
            ):
                if s:
                    if x > C_FINAL:
                        x += text(p, x, y + 31, separator, value, col("grey4"))
                    x += text(p, x, y + 31, s, value, c)
        y += FACTS_H
    if source == "predgg":
        _hairline(p, y)
        stats = data["stats"]
        caption = font(10, 600, 0.12)
        if stats is None:
            message = "LOADING PRED.GG" if data["stats_state"] == "loading" else "NO PRED.GG DATA"
            text(p, C_L, y + 30, message, font(12, 600, 0.10, caps=True), col("grey3"))
        else:
            numbers = tnum(font(20, 600, 0.02))
            for x, title_, value, color in (
                (
                    C_L,
                    "WIN %",
                    None if stats["wr"] is None else f"{stats['wr']}%",
                    wr_color(stats["wr"]),
                ),
                (
                    C_LADDER,
                    "KDA",
                    None if stats["kda"] is None else f"{stats['kda']:.1f}",
                    col("white"),
                ),
                (152, "GAMES", str(stats["games"]), col("grey1")),
            ):
                text(p, x, y + 15, title_, caption, col("grey3"))
                text(p, x, y + 38, value or "–", numbers, color if value else col("grey4"))
            text(p, C_FINAL, y + 15, "MAINS", caption, col("grey3"))
            _heroes(p, C_FINAL, y + 36, stats["heroes"], C_PEAK_POS - C_FINAL)
            text(p, C_R, y + 15, "LAST 10", caption, col("grey3"), "r")
            _form(p, C_R, y + 32, stats["form"], step=10)
        y += STATS_H
    rows = season_rows(data)
    if rows:
        _season_table(p, y, rows, data, reveal)


BADGE_W, BADGE_H = 300, 34
BADGE_STATE = {
    "ok": ("READY", "win"),
    "offline": ("OFFLINE", "loss"),
    "login": ("SIGN IN", "gold"),
}


REGION_SHORT = {"EUROPE": "EU"}
QUEUE_GAP = 14  # between the queue strip and the badge
QUEUE_PAD = 56  # the strip's fading left end
SEPARATOR = 16  # space on each side of a "|" between modes


def _queue_fonts():
    count = font(16, 700, 0.02)
    count.setFeature(QFont.Tag("tnum"), 1)  # tabular digits: rolling counts keep their width
    return font(12, 600, 0.12, caps=True), font(14, 600, 0.08, caps=True), count


_MENU_FONTS = {}


def menu_measure(text_, kind):
    """Design-px advance of menu text; ``kind`` is "head", "name" or "count"."""
    if not _MENU_FONTS:
        _MENU_FONTS.update(zip(("head", "name", "count"), _queue_fonts()))
    return QFontMetricsF(_MENU_FONTS[kind]).horizontalAdvance(text_)


def new_strip():
    return motion.Strip(menu_measure, pad=QUEUE_PAD, sep=SEPARATOR)


def menu_width(region):
    """The menu row's fixed width: the widest strip (MAX_SLOTS modes with the longest
    label, 4-digit counts) and the badge. The window keeps this width so the badge
    never moves while modes come and go.
    """

    def widest(text, kind):
        return menu_measure(modes.LONGEST_LABEL if kind == "name" and text != "|" else text, kind)

    strip = motion.Strip(widest, pad=QUEUE_PAD, sep=SEPARATOR)
    strip.sync({f"MODE{i}": (8888, 0) for i in range(modes.MAX_SLOTS)}, 1.0, region, 1.0)
    return math.ceil(strip.frame(10.0).width) + QUEUE_GAP + BADGE_W


def _lerp_color(a, b, t):
    a, b = col(a), col(b)
    return QColor.fromRgbF(*(x + (y - x) * t for x, y in zip(a.getRgbF()[:3], b.getRgbF()[:3])))


def _alpha(color, value):
    color = QColor(color)
    color.setAlphaF(max(0.0, min(1.0, value)))
    return color


def _snap(value, scale):
    """A moving x or y on whole device pixels: no shimmer of sub-pixel text."""
    return round(value * scale) / scale


def paint_badge(p, frame, pulse=1.0, rule=1.0, scale=1.0, strike=None):
    """The "overlay running" chip; ``frame`` is motion.Badge.frame(), ``strike`` the
    seconds since the menu appeared, for the one flash of its diamond."""
    width, height = BADGE_W, BADGE_H
    gradient = QLinearGradient(0, 0, 0, height)
    gradient.setColorAt(0.0, col("panel_top", 0.88))
    gradient.setColorAt(1.0, col("bg", 0.92))
    p.setPen(QPen(col("border", 0.75), 1.0))
    p.setBrush(QBrush(gradient))
    p.drawPath(chamfer(QRectF(0.5, 0.5, width - 1, height - 1), 7))
    gold_rule(p, 10, width - 10, 0.5, rule, center_diamond=False)
    middle = height / 2
    diamond(p, 20, middle, 7, QPen(col("gold"), 1.4), None)
    diamond(p, 20, middle, 2.6, None, QBrush(col("gold")))
    if strike is not None:
        _strike(p, 20, middle, 7, "gold", strike)
    title = font(15, 700, 0.12, caps=True)
    baseline = middle + QFontMetricsF(title).capHeight() / 2
    title_w = text(p, 36, baseline, "PRED OVERLAY", title, col("white"))
    state_font = font(13, 600, 0.12, caps=True)
    old_label, old_color = BADGE_STATE[frame["old"]]
    new_label, new_color = BADGE_STATE[frame["new"]]
    right = width - 14
    old_w, new_w = (QFontMetricsF(state_font).horizontalAdvance(s) for s in (old_label, new_label))
    label_w = old_w + (new_w - old_w) * frame["accent"]  # drives the dot and the ornament
    accent = _lerp_color(old_color, new_color, frame["accent"])
    changing = frame["old"] != frame["new"]
    p.save()
    p.setClipRect(
        QRectF(right - max(old_w, new_w) - 2, 4, max(old_w, new_w) + 4, height - 8),
        Qt.ClipOperation.IntersectClip,
    )
    if changing and frame["old_a"] > 0:
        old_y = _snap(baseline + frame["old_dy"], scale)
        text(p, right, old_y, old_label, state_font, _alpha(col(old_color), frame["old_a"]), "r")
    new_y = _snap(baseline + frame["new_dy"], scale)
    new_a = frame["new_a"] if changing else 1.0
    text(p, right, new_y, new_label, state_font, _alpha(col(new_color), new_a), "r")
    p.restore()
    dot = right - label_w - 12
    p.setPen(Qt.PenStyle.NoPen)
    if frame["halo"] < 1.0:  # one flare when the state changes
        h = motion.out_cubic(frame["halo"])
        p.setBrush(_alpha(accent, 0.45 * (1 - h)))
        p.drawEllipse(QPointF(dot, middle), 6 + 6 * h, 6 + 6 * h)
    p.setBrush(_alpha(accent, 0.25 + 0.2 * pulse))
    p.drawEllipse(QPointF(dot, middle), 6, 6)
    p.setBrush(accent)
    p.drawEllipse(QPointF(dot, middle), 3, 3)
    ornament_rule(p, 36 + title_w + 12, dot - 14, middle)


def paint_strip(p, right, frame, rule=1.0, scale=1.0):
    """The queue strip right-anchored at ``right``; ``frame`` is motion.Strip.frame()."""
    opening = frame.open
    if opening <= 0:
        return
    head_f, name_f, count_f = _queue_fonts()
    shown = frame.width * opening
    dx = (1 - opening) * motion.DRAWER_SLIDE
    left = right - shown
    plate = QLinearGradient(left, 0, right, 0)
    plate.setColorAt(0.0, col("bg", 0.0))
    plate.setColorAt(min(0.99, QUEUE_PAD / max(shown, 1)), col("bg", 0.62))
    plate.setColorAt(1.0, col("bg", 0.78))
    p.fillRect(QRectF(left, 0, shown, BADGE_H), plate)
    line = QLinearGradient(left, 0, right, 0)
    line.setColorAt(0.0, col("gold_rule", 0.0))
    line.setColorAt(1.0, col("gold_rule", 0.55 * rule))
    p.fillRect(QRectF(left, BADGE_H - 1, shown, 1), line)
    p.save()
    p.setClipRect(QRectF(left, 0, shown, BADGE_H))
    p.setOpacity(p.opacity() * frame.content)
    baseline = BADGE_H / 2 + QFontMetricsF(name_f).capHeight() / 2
    head_color = _alpha(col("gold_soft"), 0.85 * frame.head_alpha)
    text(p, _snap(right + frame.head_x + dx, scale), baseline, frame.head, head_f, head_color)
    digit_w = QFontMetricsF(count_f).horizontalAdvance("0")
    cap = QFontMetricsF(count_f).capHeight()
    for item in frame.items:
        x = right + item.x + dx
        if item.sep_x is not None and item.sep_alpha > 0:
            sep_x = _snap(right + item.sep_x + dx, scale)
            text(p, sep_x, baseline, "|", name_f, _alpha(col("grey4"), item.sep_alpha), "c")
        if item.alpha <= 0.002:
            continue
        y = _snap(baseline + item.rise, scale)
        label_color = _lerp_color("grey1", "grey3", item.dim)
        label_alpha = _alpha(label_color, item.alpha)
        text(p, _snap(x, scale), y, item.label, name_f, label_alpha, w=item.label_w + 0.5)
        number_color = _lerp_color("white", "grey4", item.zero)
        number_color = _lerp_color(number_color.name(), "grey3", item.dim * 0.8)
        number_right = x + item.label_w + 7 + item.num_w
        p.save()
        p.setClipRect(
            QRectF(
                number_right - item.num_w - digit_w, y - cap - 2, item.num_w + digit_w + 2, cap + 5
            ),
            Qt.ClipOperation.IntersectClip,
        )
        count = len(item.digits)
        for j, (old, new, progress, direction) in enumerate(item.digits):
            cx = number_right - (count - j) * digit_w
            for char, offset, a in (
                (old, -direction * motion.ROLL_DIST * progress, max(0.0, 1 - 1.6 * progress)),
                (new, direction * motion.ROLL_DIST * (1 - progress), min(1.0, 1.6 * progress)),
            ):
                if char.strip() and a > 0.001:
                    color = _alpha(number_color, a * item.alpha)
                    text(p, _snap(cx, scale), _snap(y + 0.5 + offset, scale), char, count_f, color)
        p.restore()
    p.restore()


def paint_menu(p, width, strip_frame, badge_frame, pulse=1.0, rule=1.0, scale=1.0, strike=None):
    """The main-menu row: the queue strip, then the badge flush with the right edge."""
    badge_x = width - BADGE_W
    paint_strip(p, badge_x - QUEUE_GAP, strip_frame, rule, scale)
    p.save()
    p.translate(badge_x, 0)
    paint_badge(p, badge_frame, pulse, rule, scale, strike)
    p.restore()


def paint_status(p, width, message, motion):
    """A one-line plate while no roster is known yet (e.g. 'waiting for the lobby')."""
    height = 44
    gradient = QLinearGradient(0, 0, 0, height)
    gradient.setColorAt(0.0, col("panel_top", 0.86))
    gradient.setColorAt(1.0, col("bg", 0.90))
    p.fillRect(QRectF(0, 0, width, height), gradient)
    p.setPen(QPen(col("border", 0.28), 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(QRectF(0.5, 0.5, width - 1, height - 1))
    gold_rule(p, 0, width, 0, motion.rule)
    gold_rule(p, 0, width, height - 1, motion.rule, center_diamond=False)
    p.setOpacity(p.opacity() * status_alpha(motion.pulse))
    text(p, width / 2, 28, message, font(15, 600, 0.14, caps=True), col("gold_soft"), "c")
    return height


def status_alpha(pulse):
    return 0.6 + 0.4 * pulse
