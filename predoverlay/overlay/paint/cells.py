"""Fields a panel row and the TAB card both draw, so the two read alike.

The rank emblem and its landing strike, the rank, the VP bar, the NEW / YOU
flag and account age.
"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPen

from .. import motion
from .common import (
    COL_RANK_R,
    PALETTE,
    TAB_AGE,
    TIER_COLOR,
    VP_BAR_W,
    _advance,
    chamfer,
    col,
    diamond,
    font,
    text,
    tnum,
)


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


def party_chip(p, right, y_mid, party):
    """DUO / TRIO / 4-STACK in the party's colour, right edge at ``right``; its width."""
    from ..rows import party_label

    label = party_label(party)
    f = font(11, 700, 0.08)
    w = _advance(label, f) - f.letterSpacing() + 10
    ink = col(f"party{party['group'] % 2}")
    rect = QRectF(right - w, y_mid - 7, w, 14).adjusted(0.5, 0.5, -0.5, -0.5)
    p.setPen(QPen(ink, 1.0))
    p.setBrush(QColor(ink.red(), ink.green(), ink.blue(), 32))
    p.drawPath(chamfer(rect, 3))
    text(p, right - w / 2, y_mid + 4, label, f, ink, "c")
    return w


def _flag(p, right, y_mid, label, kind):
    """A small chip with a fixed right edge: NEW and YOU share one slot."""
    f = font(11, 700, 0.08)
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


def _tag_fit(label, room):
    for width in (100, 88, 76):
        f = font(11, 600, 0.08)
        f.setVariableAxis(QFont.Tag("wdth"), float(width))
        w = _advance(label, f) - f.letterSpacing() + 20
        if room is None or w <= room:
            return f, w, True
    return f, w, False


def tag_fits(label, room):
    """Whether the tag's chip fits ``room`` px whole (condensed at most)."""
    return _tag_fit(label, room)[2]


def tag_chip(p, x, y_mid, label, room=None):
    """The overlay's own tag for a player whose name stays hidden: an outlined chip with
    a small diamond, so it never reads as a name. Within ``room`` px it condenses, then
    elides. Returns its width."""
    f, w, _ = _tag_fit(label, room)
    w = min(w, room) if room is not None else w
    rect = QRectF(x + 0.5, y_mid - 7 + 0.5, w - 1, 13)
    p.setPen(QPen(col("grey3", 0.8), 1.0))
    p.setBrush(col("grey4", 0.12))
    p.drawPath(chamfer(rect, 3))
    diamond(p, x + 7, y_mid, 2.5, None, QBrush(col("grey2")))
    text(p, x + 13, y_mid + 4, label, f, col("grey1"), w=w - 17)
    return w


def name_ink(d):
    """A name in white, mine in green, a stand-in (CONSOLE PLAYER) in grey."""
    if d["me"]:
        return col("self")
    return col("grey2") if d.get("hidden") else col("name")


# Why a player has no data (PlayerView.why) -> what the row says and what comes next.
UNKNOWN_NOTES = {
    "console": ("PRED.GG HIDES CONSOLE NAMES", "RANK NEXT TIME YOU MEET"),
    "misread": ("NOT FOUND ON PRED.GG", "NAME MAY BE MISREAD"),
    "offline": ("PRED.GG DID NOT ANSWER", "ASKING AGAIN"),
    "noprofile": ("NO PROFILE ON PRED.GG", "FOUND BY NAME, NOTHING MORE"),
    "login": ("NOT SIGNED IN ON PRED.GG", "SIGN IN FROM THE TRAY ICON"),
}


def unknown_note(p, x, line1, line2, w, why="misread"):
    """In place of rank, peak and bar for a player with no data: why, and what comes."""
    title, note = UNKNOWN_NOTES.get(why, UNKNOWN_NOTES["misread"])
    text(p, x, line1, title, font(12, 600, 0.08, caps=True), col("grey2"), w=w)
    text(p, x, line2, note, font(11, 500, 0.08, caps=True), col("grey3"), w=w)


def _flag_of(d):
    return ("YOU", "me") if d["me"] else (("NEW", "new") if d["new"] else None)


SIDE = {"against": ("loss", "against_text"), "with": ("dawn", "with_text")}


def _meta(p, x, baseline, data):
    """The age on a fixed tab stop: nothing depends on another field's width."""
    f = tnum(font(12, 500, 0.08, caps=True))
    value = col("new") if data["new"] or data["young_high"] else col("grey1")
    if data["age"]:
        text(p, x + TAB_AGE, baseline, data["age"], f, value)


def _rank(p, x, baseline, data, px=14, w=None, icon=True):
    f = font(px, 600, 0.06, caps=True)
    color = TIER_COLOR.get(data["tier"]) if data["tier"] != "none" else PALETTE["grey2"]
    text(p, x, baseline, data["rank"], f, QColor(color), w=w)


def _vp_bar(
    p, x, y_mid, data, w=VP_BAR_W, fill=None, track=1.0, label=True, right=COL_RANK_R, thin=False
):
    """The bar and its number, right-aligned at ``right``; a negative VP reads in red."""
    if data["vp"] is None:
        if data["rank"] == "UNRANKED" and label:
            text(p, x, y_mid + 4, "NOT PLACED", font(11, 500, 0.08, caps=True), col("grey3"))
        return
    color = TIER_COLOR.get(data["tier"], TIER_COLOR["none"])
    value = data["vp"] if fill is None else fill
    h = 2 if thin else 3
    p.fillRect(QRectF(x, y_mid - 2, w, h), col("grey4", 0.35 * track))
    filled = max(0.0, min(value, 100)) / 100
    p.fillRect(QRectF(x, y_mid - 2, w * filled, h), col(color, 0.9 * track))
    if label:
        f = tnum(font(11, 500, 0.06, caps=True))
        vp = data["vp"]
        if vp < 0:
            text(p, right, y_mid + 4, f"\u2212{-vp} VP", f, col("loss", 0.9), "r")
        else:
            text(p, right, y_mid + 4, f"{vp} VP", f, col("grey1"), "r")


def _skeleton(p, x, y_mid, w, h=8):
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(col("grey4", 0.28))
    p.drawRoundedRect(QRectF(x, y_mid - h / 2, w, h), 2, 2)
