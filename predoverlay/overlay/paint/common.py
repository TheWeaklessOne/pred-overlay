"""What every surface shares: design size, palette, the grid, type and the gold rule.

Coordinates are design pixels of a 2560x1440 screen; the window scales the
painter to the real screen. Palette, fonts and ornaments follow the visual
spec sampled from the game's own screens (navy panels, gold rules with diamond
terminals, Dawn blue / Dusk orange team bars, Bahnschrift type). The card
reuses the panel's tab stops, so the panel's grid lives here too.
"""

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
    "party0": "#B99BFF",  # a team's first party (violet), then its second (teal)
    "party1": "#5ED6D2",
    "loss": "#F5442E",
    "self": "#95FAB6",
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
LINE1, LINE2 = 18, 33  # baselines inside a row
RANK1, RANK2, VP_Y = 16, 30, 36  # the rank cell: rank, peak, a thin VP line
PLATE_L, PLATE_R, BAR_W = 8, 632, 2
PORTRAIT = 42  # where the MAIN portrait is still solid: MAIN is centred over this
FADE_W, FADE_FOCUS = 66, 0.30  # the art runs this wide and dissolves into the plate
COL_NAME, COL_NAME_R = 70, 238
TAB_AGE, TAB_ROLE = 58, 114  # offsets from the name column on line 2
TAB_NOTE = 60  # after the role word on line 2
COL_ICON = 254  # centre of the rank emblem, spans both lines
COL_RANK, COL_RANK_R = 272, 408
VP_BAR_W = 50
ENC_SIZE, ENC_R = 42, 633  # the encounter portrait, framed, a pixel proud of the plate
ENC_TEXT_R = 582
COL_R = 620  # the one right edge


def snap(p, dy):
    """``dy`` design pixels rounded to whole screen pixels: a row's text (hinted to the
    pixel grid) and its plate (antialiased) then move together, step for step."""
    device = p.device()
    ratio = device.devicePixelRatioF() if device is not None else 1.0  # 150% Windows scaling
    scale = (p.worldTransform().m22() or 1.0) * (ratio or 1.0)
    return round(dy * scale) / scale


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
    # Unhinted: Windows' hinting puts each letter on a whole pixel, so a condensed
    # (wdth axis) or tracked line gets uneven gaps (seen 2026-11-19 on a long caps name).
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
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


def _advance(s, f):
    return QFontMetricsF(f).horizontalAdvance(s)


def name_font(px, s, max_w):
    """Light and wide like the game's own rows; Bahnschrift's width axis condenses a
    long name before it is elided."""
    for width, track in ((100, 0.09), (94, 0.06), (86, 0.04), (78, 0.03), (70, 0.02)):
        f = font(px, 450, track, caps=True)
        f.setVariableAxis(QFont.Tag("wdth"), float(width))
        if _advance(s, f) <= max_w:
            return f
    return f


def _lerp_color(a, b, t):
    a, b = col(a), col(b)
    return QColor.fromRgbF(*(x + (y - x) * t for x, y in zip(a.getRgbF()[:3], b.getRgbF()[:3])))


def _alpha(color, value):
    color = QColor(color)
    color.setAlphaF(max(0.0, min(1.0, value)))
    return color
