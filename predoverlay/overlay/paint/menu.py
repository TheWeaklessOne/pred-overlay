"""The main-menu row: the "overlay running" badge.

It is drawn from the frames motion.Badge computes; this module only paints them.
"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QFontMetricsF, QLinearGradient, QPen

from .. import motion
from .cells import _strike
from .common import _alpha, _lerp_color, chamfer, col, diamond, font, gold_rule, ornament_rule, text

BADGE_W, BADGE_H = 300, 34
BADGE_STATE = {
    "ok": ("READY", "win"),
    "offline": ("OFFLINE", "loss"),
    "login": ("SIGN IN", "gold"),
}


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


def paint_menu(p, width, badge_frame, pulse=1.0, rule=1.0, scale=1.0, strike=None):
    """The main-menu row: the badge, flush with the right edge."""
    p.save()
    p.translate(width - BADGE_W, 0)
    paint_badge(p, badge_frame, pulse, rule, scale, strike)
    p.restore()
