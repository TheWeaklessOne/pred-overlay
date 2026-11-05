"""Game-styled painting of the roster panel with QPainter.

Coordinates are design pixels of a 2560x1440 screen; the window scales the
painter to the real screen. Palette, fonts and ornaments follow the visual
spec sampled from the game's own screens (navy panels, gold rules with diamond
terminals, Dawn blue / Dusk orange team bars, Bahnschrift type).
"""

import math
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
from .rows import HERO_MIN, team_rank, team_winrate

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
    "sep": "#35383E",
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
    "against_text": "#FFB4A8",
    "with_text": "#BFE9FF",
    "info_text": "#C9CED6",
    "new": "#FFCF70",
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

STACK_ROW_H, STACK_GAP, STACK_HDR = 40, 2, 28
SPLIT_ROW_H, SPLIT_GAP, SPLIT_HDR = 30, 3, 28


@dataclass
class Motion:
    """Animation progress, all 0..1. ``rows(i)`` gives row i's entrance progress."""

    panel: float = 1.0
    rule: float = 1.0
    pulse: float = 0.0
    rows: object = None

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


def text(p, x, y, s, f, color, align="l", w=None):
    """Draw ``s`` with its baseline at y; returns the drawn width."""
    p.setFont(f)
    p.setPen(color)
    metrics = QFontMetricsF(f)
    width = metrics.horizontalAdvance(s)
    if w is not None and width > w:
        s = metrics.elidedText(s, Qt.TextElideMode.ElideRight, max(w, 0))
        width = metrics.horizontalAdvance(s)
    if align == "r":
        x -= width
    elif align == "c":
        x -= width / 2
    p.drawText(QPointF(x, y), s)
    return width


def diamond(p, cx, cy, r, pen=None, brush=None):
    polygon = QPolygonF(
        [QPointF(cx, cy - r), QPointF(cx + r, cy), QPointF(cx, cy + r), QPointF(cx - r, cy)]
    )
    p.setPen(pen if pen else Qt.PenStyle.NoPen)
    p.setBrush(brush if brush else Qt.BrushStyle.NoBrush)
    p.drawPolygon(polygon)


def ornament_rule(p, x0, x1, y, color="sep", alpha=1.0):
    """1px rule with double-diamond terminals, as beside ALLY/ENEMY in draft."""
    if x1 - x0 < 40:
        return
    pen = QPen(col(color, alpha), 1.0)
    p.setPen(pen)
    p.drawLine(QPointF(x0 + 14, y + 0.5), QPointF(x1 - 14, y + 0.5))
    for x, d in ((x0, 1), (x1, -1)):
        diamond(p, x + d * 4, y + 0.5, 4, pen)
        diamond(p, x + d * 10, y + 0.5, 4, pen)


def gold_rule(p, x0, x1, y, progress=1.0, center_diamond=True):
    """Gold rule fading at both ends, drawn outward from the centre as it animates."""
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


def rank_badge(p, x, y_mid, tier):
    color = TIER_COLOR.get(tier, TIER_COLOR["none"])
    diamond(p, x + 6, y_mid, 6, QPen(col(color), 1.2), QBrush(col(color, 0.25)))
    diamond(p, x + 6, y_mid, 2.2, None, QBrush(col(color)))


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
        f = font(px, 600, 0.05 if width == 100 else 0.03, caps=True)
        f.setVariableAxis(QFont.Tag("wdth"), float(width))
        if _advance(s, f) <= max_w:
            return f
    return f


def team_header(p, width, y, team, rows, mode, source):
    """Team label, an ornament, the team average and (with Pred.gg) column captions."""
    color = "dawn_hdr" if team == "ally" else "dusk_hdr"
    label = "ALLIES" if team == "ally" else "ENEMIES"
    if mode and team == "ally":
        label += f"  ·  {mode.upper()}"
    if source == "predgg":
        end = 8 + 0.555 * (width - 16) - 4
        average = team_winrate(rows)
        summary = f"AVG {average}%" if average is not None else ""
    else:
        end = width - 14
        average = team_rank(rows)
        summary = f"AVG {average.upper()}" if average else ""
    summary_font = font(12, 600, 0.10)
    room = end - QFontMetricsF(summary_font).horizontalAdvance(summary) - 14 - 30
    label_w = text(p, 14, y, label, font(17, 700, 0.12), col(color), w=room)
    if source == "predgg":
        inner = width - 16
        caption = font(10, 600, 0.12)
        for key, title in (("wr", "WIN %"), ("kda", "KDA"), ("games", "GAMES")):
            text(p, 8 + STACK_B[key] * inner, y, title, caption, col("grey4"), "r")
        text(p, 8 + inner - 10, y, "LAST 10", caption, col("grey4"), "r")
    summary_w = text(p, end, y, summary, summary_font, col("grey2"), "r")
    ornament_rule(p, 14 + label_w + 12, end - summary_w - 12, y - 6)


def stack_size(n_allies, n_enemies, width=640):
    rows = n_allies + n_enemies
    return width, 14 + 2 * STACK_HDR + rows * (STACK_ROW_H + STACK_GAP) + 18


def _row_background(p, rect, team, me, bar_right=False):
    gradient = QLinearGradient(rect.left(), 0, rect.right(), 0)
    gradient.setColorAt(0.0, col("row_a", 0.94))
    gradient.setColorAt(0.55, col("row_b", 0.86))
    gradient.setColorAt(1.0, col("row_c", 0.60))
    p.fillRect(rect, gradient)
    p.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 1), col("hilite", 0.9))
    p.fillRect(QRectF(rect.left(), rect.bottom() - 1, rect.width(), 1), col("row_line", 0.9))
    bar_x = rect.right() - 4 if bar_right else rect.left()
    p.fillRect(
        QRectF(bar_x, rect.top(), 4, rect.height()), col("dawn" if team == "ally" else "dusk")
    )
    if me:  # a green tint and an inner strip rather than a heavy outline
        tint = QLinearGradient(rect.left(), 0, rect.right(), 0)
        tint.setColorAt(0.0, col("self", 0.13))
        tint.setColorAt(1.0, col("self", 0.0))
        p.fillRect(rect, tint)
        strip_x = rect.right() - 7 if bar_right else rect.left() + 5
        p.fillRect(QRectF(strip_x, rect.top() + 3, 2, rect.height() - 6), col("self", 0.9))


def _peak_width(data):
    return _advance(f"PEAK {data['peak']['rank'].upper()}", _peak_font()) if data["peak"] else 0.0


def _peak_font():
    return font(11, 600, 0.08, caps=True)


def _peak(p, right, baseline, data):
    """'PEAK DIAMOND II' right-aligned at ``right``: the best rank of a past season."""
    if not data["peak"]:
        return
    f = _peak_font()
    rank = data["peak"]["rank"].upper()
    color = TIER_COLOR.get(data["peak"]["tier"], TIER_COLOR["none"])
    rank_w = text(p, right, baseline, rank, f, col(color, 0.9), "r")
    text(p, right - rank_w - _advance(" ", f), baseline, "PEAK", f, col("grey4"), "r")


def _meta(p, x, baseline, data, max_w, with_role=False):
    """'LV 409 · 3 YRS', amber with a NEW chip for an account under two months old."""
    f = font(12, 500, 0.08, caps=True)
    if data["new"]:
        chip = font(10, 700, 0.10, caps=True)
        w = _advance("NEW", chip) + 10
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col("new", 0.85))
        p.drawPath(chamfer(QRectF(x, baseline - 10.5, w, 13), 3))
        text(p, x + 5, baseline - 0.5, "NEW", chip, col("bg"))
        x, max_w = x + w + 7, max_w - w - 7
    parts = []
    stats = data.get("stats") or {}
    if with_role and stats.get("role"):
        parts.append(stats["role"])
    if data["level"] is not None:
        parts.append(f"LV {data['level']}")
    if data["age"]:
        parts.append(data["age"])
    separator = "  ·  "
    while len(parts) > 1 and _advance(separator.join(parts), f) > max_w:
        parts.pop()
    text(p, x, baseline, separator.join(parts), f, col("new" if data["new"] else "grey3"), w=max_w)


def _rank(p, x, baseline, data, px=14, w=None):
    f = font(px, 600, 0.06, caps=True)
    rank_badge(p, x, cap_middle(f, baseline), data["tier"])
    color = TIER_COLOR.get(data["tier"]) if data["tier"] != "none" else PALETTE["grey2"]
    text(p, x + 18, baseline, data["rank"], f, QColor(color), w=w)


def _vp_bar(p, x, y_mid, data, w=58, fill=None, track=1.0, label=True):
    if data["vp"] is None:
        if data["rank"] == "UNRANKED" and label:
            text(p, x, y_mid + 4, "NOT PLACED", font(11, 500, 0.08, caps=True), col("grey4"))
        return
    color = TIER_COLOR.get(data["tier"], TIER_COLOR["none"])
    value = data["vp"] if fill is None else fill
    p.fillRect(QRectF(x, y_mid - 1.5, w, 3), col("grey4", 0.35 * track))
    filled = max(0.0, min(value, 100)) / 100  # below the tier floor: an empty bar
    p.fillRect(QRectF(x, y_mid - 1.5, w * filled, 3), col(color, 0.9 * track))
    if label:
        vp_font = font(11, 500, 0.06, caps=True)
        vp = data["vp"]
        if vp < 0:  # lost VP at the bottom of a tier: one more loss may demote
            text(p, x + w + 7, y_mid + 4, f"\u2212{-vp} VP", vp_font, col("loss", 0.9))
        else:
            text(p, x + w + 7, y_mid + 4, f"{vp} VP", vp_font, col("grey3"))


# A field that changes swaps out then in: different-length text never sits on itself.
SWAP_OUT, SWAP_IN_AT, SWAP_IN = 0.07, 0.05, 0.12
VP_DELAY, VP_TIME = 0.06, 0.36
SWAP_DONE = VP_DELAY + VP_TIME  # the longest field change


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


def _vp_swap(p, mix, x, y_mid, data, w=58):
    """The VP bar fills from its old value; its label and the track swap like text."""
    old, dt = (mix or {}).get("vp", (None, None))
    if old is None or data["vp"] is None:
        _swap(p, mix, "vp", data, lambda d: _vp_bar(p, x, y_mid, d, w))
        return
    start = old["vp"] if old["vp"] is not None else 0
    t = motion.out_cubic(min(1.0, max(0.0, (dt - VP_DELAY) / VP_TIME)))
    track = 1.0 if old["vp"] is not None else motion.out_cubic(min(1.0, dt / 0.16))
    _vp_bar(p, x, y_mid, data, w, fill=start + (data["vp"] - start) * t, track=track, label=False)
    _swap(p, mix, "vp", data, lambda d: _vp_bar(p, x, y_mid, d, w, track=0.0))


def _encounter_block(p, x, top, data, max_w):
    """A two-line note: hero and when, then side and result, with a side-coloured accent."""
    e = data["encounter"]
    accent, ink = SIDE[e["side"]]
    p.fillRect(QRectF(x, top + 6, 2, 28), col(accent, 0.9))
    w = 0
    if e["hero"]:
        w = text(p, x + 9, top + 18, e["hero"], font(14, 600, 0.06, caps=True), col("white")) + 8
    when_font = font(11, 600, 0.08, caps=True)
    text(p, x + 9 + w, top + 18, e["when"], when_font, col("grey3"), w=max_w - w - 20)
    side = "AGAINST US" if e["side"] == "against" else "WITH US"
    w2 = text(p, x + 9, top + 34, side, font(12, 600, 0.08, caps=True), col(ink))
    if e["result"]:
        result_font = font(11, 500, 0.08, caps=True)
        text(p, x + 9 + w2 + 8, top + 34, e["result"], result_font, col("grey2"))


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


# Pred.gg number columns (right edges as fractions of the row width).
STACK_B = {"wr": 0.640, "kda": 0.735, "games": 0.835}


def stack_row(p, rect, data, motion, source="omeda", mix=None):
    """One roster row. ``mix`` maps a changed field group to (old row, seconds since)."""
    if data["state"] == "loading":
        p.setOpacity(p.opacity() * (0.65 + 0.35 * motion.pulse))
    _row_background(p, rect, data["team"], data["me"])
    left, width, top = rect.left(), rect.width(), rect.top()
    x0, right = left + 14, rect.right() - 10
    line1, line2 = top + 18, top + 34
    name_color = col("self") if data["me"] else col("white")
    if source != "predgg":
        rank_x, intel_x = left + 0.42 * width, left + 0.635 * width
        name_w = rank_x - x0 - 12

        def name(d):
            text(p, x0, line1, d["name"], name_font(18, d["name"], name_w), name_color, w=name_w)

        def intel(d):
            if d["encounter"]:
                _encounter_block(p, intel_x, top, d, right - intel_x)
            elif d["me"]:
                text(p, right, line1 + 8, "YOU", font(11, 600, 0.14), col("self", 0.7), "r")

        _swap(p, mix, "name", data, name)

        def meta_w(d):
            return name_w - (_peak_width(d) + 12 if d["peak"] else 0)

        _swap(p, mix, "meta", data, lambda d: _meta(p, x0, line2, d, meta_w(d)))
        _swap(p, mix, "peak", data, lambda d: _peak(p, x0 + name_w, line2, d))
        _swap(
            p, mix, "rank", data, lambda d: _rank(p, rank_x, line1, d, 14, w=intel_x - rank_x - 30)
        )
        _vp_swap(p, mix, rank_x + 2, line2 - 4, data)
        _swap(p, mix, "intel", data, intel, rise=4.0)
        return
    rank_x, heroes_x = left + 0.345 * width, left + 0.555 * width
    name_w = rank_x - x0 - 10

    def name(d):
        text(p, x0, line1, d["name"], name_font(17, d["name"], name_w), name_color, w=name_w)

    _swap(p, mix, "name", data, name)
    _swap(p, mix, "meta", data, lambda d: _meta(p, x0, line2, d, name_w, with_role=True))
    _swap(p, mix, "rank", data, lambda d: _rank(p, rank_x, line1, d, 13, w=heroes_x - rank_x - 24))
    _vp_swap(p, mix, rank_x + 2, line2 - 4, data, w=44)
    numbers = font(17, 600, 0.02)
    middle = cap_middle(numbers, line1)
    stats = data["stats"]
    if stats is None:
        if data["stats_state"] == "loading":
            for key in STACK_B:
                _skeleton(p, left + STACK_B[key] * width, middle, 32, 9, right=True)
            _skeleton(p, heroes_x, line2 - 4, 150, 7)
        return
    for key, value, color in (
        ("wr", None if stats["wr"] is None else f"{stats['wr']}%", wr_color(stats["wr"])),
        ("kda", None if stats["kda"] is None else f"{stats['kda']:.1f}", col("white")),
        ("games", str(stats["games"]), col("grey1")),
    ):
        text(
            p,
            left + STACK_B[key] * width,
            line1,
            value or "–",
            numbers,
            color if value else col("grey4"),
            "r",
        )
    _form(p, right, middle, stats["form"])
    pill_w = 0
    if data["encounter"]:
        pill_w = _encounter_pill(p, right, line2 - 4, data, right - heroes_x) + 10
    _heroes(p, heroes_x, line2, stats["heroes"], right - heroes_x - pill_w)


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
):
    """The draft / loading-screen panel; origin at the panel's top-left.

    ``height`` draws the panel shorter than its full size, grown from the top
    (or from the bottom with ``from_bottom``), while it grows out of the status
    plate; ``status`` fades out over it. ``mixes`` maps a player to the field
    groups that just changed (see stack_row).
    """
    _, full = stack_size(len(allies), len(enemies), width)
    shown = full if height is None else min(full, height)
    top = full - shown if from_bottom else 0.0
    panel = QRectF(0, top, width, shown)
    gradient = QLinearGradient(0, top, 0, top + shown)
    gradient.setColorAt(0.0, col("panel_top", 0.86))
    gradient.setColorAt(1.0, col("bg", 0.90))
    p.fillRect(panel, gradient)
    p.setPen(QPen(col("border", 0.55), 1.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(panel.adjusted(0.5, 0.5, -0.5, -0.5))
    p.save()
    p.setClipRect(QRectF(0, top + 1, width, shown - 1))
    y, index = 14, 0
    for team, rows in (("ally", allies), ("enemy", enemies)):
        first = motion.row(index)  # a team's header arrives with its first row
        if first > 0:
            p.save()
            p.setOpacity(p.opacity() * first)
            team_header(p, width, y + 19, team, rows, mode, source)
            p.restore()
        y += STACK_HDR
        for data in rows:
            progress = motion.row(index)
            if progress > 0:
                p.save()
                p.setOpacity(p.opacity() * progress)
                p.translate(0, (1 - progress) * 8)
                mix = (mixes or {}).get(data["player_id"])
                stack_row(p, QRectF(8, y, width - 16, STACK_ROW_H), data, motion, source, mix)
                p.restore()
            y += STACK_ROW_H + STACK_GAP
            index += 1
    p.restore()
    gold_rule(p, 0, width, top, motion.rule)
    gold_rule(p, width * 0.2, width * 0.8, top + shown - 8, motion.rule, center_diamond=False)
    if status and status_alpha > 0:
        p.save()
        p.setOpacity(p.opacity() * status_alpha)
        plate_top = full - 44 if from_bottom else 0.0
        status_font = font(15, 600, 0.14, caps=True)
        text(p, width / 2, plate_top + 28, status, status_font, col("gold_soft"), "c")
        p.restore()


CARD_W = 520


def card_size(data, source, seen=None):
    """Height of the in-match card for one player."""
    height = 92
    if data["encounter"]:
        height += 26
    if seen and (seen.get("hero") or seen.get("role")):
        height += 24
    if data["peak"]:
        height += 24
    if source == "predgg":
        height += 70
    return CARD_W, height


def card_chrome(p, width, height, team, rule=1.0):
    """The card's plate, gold rule and team bar (drawn once while its content swaps)."""
    gradient = QLinearGradient(0, 0, 0, height)
    gradient.setColorAt(0.0, col("panel_top", 0.94))
    gradient.setColorAt(1.0, col("bg", 0.96))
    p.setPen(QPen(col("border", 0.75), 1.0))
    p.setBrush(QBrush(gradient))
    p.drawPath(chamfer(QRectF(0.5, 0.5, width - 1, height - 1), 9))
    gold_rule(p, 12, width - 12, 0.5, rule)
    team_color = col("dawn" if team == "ally" else "dusk")
    p.fillRect(QRectF(0, 14, 4, height - 28), team_color)


def paint_card_reading(p, team, motion, chrome=True):
    """A card skeleton while the scoreboard is being read (the first TAB of a match)."""
    width, height = CARD_W, 92
    if chrome:
        card_chrome(p, width, height, team, motion.rule)
    p.save()
    p.setOpacity(p.opacity() * (0.55 + 0.45 * motion.pulse))
    _skeleton(p, 20, 30, 190, 12)
    _skeleton(p, width * 0.56, 30, 120, 10)
    _skeleton(p, 20, 58, 120, 8)
    p.restore()
    text(p, width - 18, 64, "READING SCOREBOARD", font(11, 600, 0.12, caps=True), col("grey4"), "r")


def paint_card(p, data, motion, source="omeda", seen=None, chrome=True):
    """The card shown next to the scoreboard row under the cursor while TAB is held.

    ``seen`` is what the scoreboard row says about the player in this match:
    {"hero": "STEEL", "role": "OFFLANE"} (read once from the screen). With
    ``chrome=False`` only the content is drawn (to crossfade between players).
    """
    width, height = card_size(data, source, seen)
    if chrome:
        card_chrome(p, width, height, data["team"], motion.rule)
    x0, right = 20, width - 18
    name_color = col("self") if data["me"] else col("white")
    rank_x = width * 0.56
    text(
        p,
        x0,
        36,
        data["name"],
        name_font(22, data["name"], rank_x - x0 - 12),
        name_color,
        w=rank_x - x0 - 12,
    )
    _rank(p, rank_x, 34, data, 15, w=right - rank_x - 18)
    _meta(p, x0, 62, data, rank_x - x0 - 12, with_role=source == "predgg")
    _vp_bar(p, rank_x + 2, 58, data, w=96)
    y = 92
    if seen and (seen.get("hero") or seen.get("role")):
        parts = [part for part in (seen.get("hero"), seen.get("role")) if part]
        line = "THIS MATCH  ·  " + "  ·  ".join(parts)
        stats = data.get("stats") or {}
        hero = (stats.get("hero_stats") or {}).get((seen.get("hero") or "").upper())
        if hero and hero.get("games"):
            enough = hero["games"] >= HERO_MIN and hero.get("winrate") is not None
            share = f"  ·  {round(hero['winrate'] * 100)}%" if enough else ""
            line += f"  ·  {hero['games']} GAMES{share}"
        text(p, x0, y - 8, line, font(12, 600, 0.10, caps=True), col("gold_soft"), w=right - x0)
        y += 24
    if data["peak"]:
        best = data["peak"]
        f = font(12, 600, 0.08, caps=True)
        x = x0 + text(p, x0, y - 8, "PEAK  ", f, col("grey4"))
        color = TIER_COLOR.get(best["tier"], TIER_COLOR["none"])
        x += text(p, x, y - 8, best["rank"].upper(), f, col(color, 0.95))
        season = best["season"].replace(" - ", " ").upper()
        text(p, x, y - 8, f"  ·  {season}", f, col("grey3"), w=right - x)
        y += 24
    if data["encounter"]:
        e = data["encounter"]
        accent, ink = SIDE[e["side"]]
        p.fillRect(QRectF(x0, y - 22, 2, 18), col(accent, 0.9))
        side = "AGAINST US" if e["side"] == "against" else "WITH US"
        parts = [e["when"], e["hero"], side, e["result"]]
        label = "  ·  ".join(part for part in parts if part)
        text(p, x0 + 10, y - 8, label, font(12, 600, 0.08, caps=True), col(ink), w=right - x0)
        y += 26
    if source != "predgg":
        return
    stats = data["stats"]
    numbers = font(22, 600, 0.02)
    caption = font(10, 600, 0.12)
    if stats is None:
        message = "LOADING PRED.GG" if data["stats_state"] == "loading" else "NO PRED.GG DATA"
        text(p, x0, y + 16, message, font(12, 600, 0.10, caps=True), col("grey4"))
        return
    columns = (
        ("WIN %", None if stats["wr"] is None else f"{stats['wr']}%", wr_color(stats["wr"])),
        ("KDA", None if stats["kda"] is None else f"{stats['kda']:.1f}", col("white")),
        ("GAMES", str(stats["games"]), col("grey1")),
    )
    for i, (title, value, color) in enumerate(columns):
        cx = x0 + i * 92
        text(p, cx, y + 4, title, caption, col("grey4"))
        text(p, cx, y + 30, value or "–", numbers, color if value else col("grey4"))
    _form(p, right, y + 22, stats["form"], step=10)
    _heroes(p, x0 + 3 * 92, y + 52, stats["heroes"], right - x0 - 3 * 92)


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


def paint_badge(p, frame, pulse=1.0, rule=1.0, scale=1.0):
    """The "overlay running" chip; ``frame`` is motion.Badge.frame()."""
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


def paint_menu(p, width, strip_frame, badge_frame, pulse=1.0, rule=1.0, scale=1.0):
    """The main-menu row: the queue strip, then the badge flush with the right edge."""
    badge_x = width - BADGE_W
    paint_strip(p, badge_x - QUEUE_GAP, strip_frame, rule, scale)
    p.save()
    p.translate(badge_x, 0)
    paint_badge(p, badge_frame, pulse, rule, scale)
    p.restore()


def paint_status(p, width, message, motion):
    """A one-line plate while no roster is known yet (e.g. 'waiting for the lobby')."""
    height = 44
    gradient = QLinearGradient(0, 0, 0, height)
    gradient.setColorAt(0.0, col("panel_top", 0.86))
    gradient.setColorAt(1.0, col("bg", 0.90))
    p.fillRect(QRectF(0, 0, width, height), gradient)
    gold_rule(p, 0, width, 0, motion.rule)
    p.setOpacity(p.opacity() * (0.6 + 0.4 * motion.pulse))
    text(p, width / 2, 28, message, font(15, 600, 0.14, caps=True), col("gold_soft"), "c")
    return height
