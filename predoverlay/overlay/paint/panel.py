"""The draft / loading-screen roster panel: team headers, rows and their motion.

A field that changes swaps out then in, a rank landing strikes once, a high
peak glints once. Before a roster is known the panel is a one-line status plate.
"""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QLinearGradient, QPainterPath, QPen

from .. import motion
from ..rows import team_balance, team_rank
from .cells import (
    GLINT,
    STRIKE,
    _flag,
    _flag_of,
    _meta,
    _rank,
    _skeleton,
    _strike,
    _vp_bar,
    name_ink,
    party_chip,
    rank_badge,
    tag_chip,
    tag_fits,
    unknown_note,
)
from .common import (
    BAR_W,
    COL_ICON,
    COL_NAME,
    COL_NAME_R,
    COL_R,
    COL_RANK,
    COL_RANK_R,
    ENC_R,
    ENC_SIZE,
    ENC_TEXT_R,
    LINE1,
    LINE2,
    PAD_BOTTOM,
    PAD_TOP,
    PALETTE,
    PLATE_L,
    PLATE_R,
    PORTRAIT,
    RANK1,
    RANK2,
    STACK_GAP,
    STACK_HDR,
    STACK_ROW_H,
    TAB_NOTE,
    TEAM_GAP,
    TIER_COLOR,
    VP_BAR_W,
    VP_Y,
    _advance,
    _alpha,
    _lerp_color,
    chamfer,
    col,
    font,
    gold_rule,
    name_font,
    ornament_rule,
    snap,
    text,
    tnum,
)
from .portraits import _main, _portrait


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


HEAD_TIME = 0.30  # the team average arrives, the rule draws back to make room


def team_header(p, width, y, team, rows, mode, balance=None, reveal=None):
    """Team word, mode, an ornament, the team average and who is ahead on it.

    Both headers title MAIN and LAST MET AS. ``reveal`` is the seconds since
    the average arrived, None once it has settled.
    """
    label = "ALLIES" if team == "ally" else "ENEMIES"
    title = font(17, 700, 0.12)
    x = COL_NAME + _glow(p, COL_NAME, y, label, title, "dawn" if team == "ally" else "dusk") + 10
    if mode and team == "ally":
        x += text(p, x, y, mode, font(11, 600, 0.10, caps=True), col("grey2")) + 8
    small = font(12, 600, 0.10, caps=True)
    end, average = COL_RANK_R, team_rank(rows)
    caption = font(11, 600, 0.10, caps=True)
    text(p, ENC_R - 1, y, "LAST MET AS", caption, col("grey2"), "r")
    centre = PLATE_L + BAR_W + PORTRAIT / 2
    text(p, centre, y, "MAIN", caption, col("grey2"), "c")
    arrived = 1.0 if reveal is None else motion.out_quart(motion.clip(reveal / HEAD_TIME))
    tint = 1.0 if reveal is None else motion.clip(reveal / HEAD_TIME)
    base = p.opacity()
    p.setOpacity(base * arrived)
    used = 0
    if average:
        tone = TIER_COLOR.get(average.split(" ")[0].lower(), PALETTE["grey1"])
        used = text(p, end, y, average, small, QColor(tone), "r")
        used += _advance(" ", small)
        used += text(p, end - used, y, "AVG", small, col("grey3"), "r") - small.letterSpacing()
    if balance:
        ink = {"ally": "win", "enemy": "loss"}.get(balance["side"], "grey1")
        color = _lerp_color("grey1", ink, tint)
        x += text(p, x, y, balance["text"], font(11, 600, 0.10, caps=True), color) + 8
    p.setOpacity(base)
    middle = y - QFontMetricsF(title).capHeight() / 2
    stop = end - used * arrived - 12
    ornament_rule(p, x, stop, middle, inner=True)


def stack_size(n_allies, n_enemies, width=640):
    """The panel's size; a team without rows takes no room, not even its header."""
    teams = [n for n in (n_allies, n_enemies) if n] or [0, 0]
    rows = sum((STACK_ROW_H + STACK_GAP) * n - STACK_GAP for n in teams)
    gaps = TEAM_GAP * (len(teams) - 1)
    return width, PAD_TOP + len(teams) * STACK_HDR + rows + gaps + PAD_BOTTOM


def _row_background(p, rect, team, me, bar_right=False):
    """The game's draft plate: lit at top and bottom, dark inside, and dissolving away
    from the portrait, so a row with nothing at its right end simply ends as the game's do."""
    edge, line = ("edge_ally", "line_ally") if team == "ally" else ("edge_enemy", "line_enemy")
    base = QLinearGradient(rect.left(), 0, rect.right(), 0)
    base.setColorAt(0.0, col("plate_mid", 0.97))
    base.setColorAt(0.55, col("plate_mid", 0.90))
    base.setColorAt(1.0, col("plate_end", 0.30))
    p.fillRect(rect, base)
    bands = QLinearGradient(0, rect.top(), 0, rect.bottom())
    bands.setColorAt(0.0, col(edge, 0.95))
    bands.setColorAt(0.38, col(edge, 0.0))
    bands.setColorAt(0.62, col(edge, 0.0))
    bands.setColorAt(1.0, col(edge, 0.95))
    p.save()
    p.setOpacity(p.opacity() * 0.9)
    p.fillRect(rect, bands)
    p.restore()
    hair = QLinearGradient(rect.left(), 0, rect.right(), 0)
    hair.setColorAt(0.0, col("self" if me else line, 0.8 if me else 0.9))
    hair.setColorAt(0.7, col("self" if me else line, 0.35))
    hair.setColorAt(1.0, col("self" if me else line, 0.0))
    p.fillRect(QRectF(rect.left(), rect.top(), rect.width(), 1), hair)
    p.fillRect(QRectF(rect.left(), rect.bottom() - 1, rect.width(), 1), hair)
    if me:
        tint = QLinearGradient(rect.left(), 0, rect.right(), 0)
        tint.setColorAt(0.0, col("self", 0.13))
        tint.setColorAt(1.0, col("self", 0.0))
        p.fillRect(rect, tint)
        rise = QLinearGradient(0, rect.bottom() - 12, 0, rect.bottom())  # the game's lit floor
        rise.setColorAt(0.0, col("self", 0.0))
        rise.setColorAt(1.0, col("self", 0.10))
        p.fillRect(QRectF(rect.left(), rect.bottom() - 12, rect.width(), 12), rise)
    bar_x = rect.right() - BAR_W if bar_right else rect.left()
    p.fillRect(
        QRectF(bar_x, rect.top(), BAR_W, rect.height()),
        col("self" if me else "dawn_hdr" if team == "ally" else "dusk_hdr"),
    )


def _peak_font():
    return font(12, 500, 0.08, caps=True)


def _peak(p, top, data):
    """Line two of the rank cell: the best past rank in its own tier colour on a fixed tab.
    Without a peak the line tells what the bar cannot: a negative VP, or no placement."""
    f = _peak_font()
    base = top + RANK2
    if data["peak"]:
        tab = text(p, COL_RANK, base, "PEAK ", f, col("grey3"))
        color = TIER_COLOR.get(data["peak"]["tier"], TIER_COLOR["none"])
        room = COL_RANK_R - COL_RANK - tab
        text(p, COL_RANK + tab, base, data["peak"]["rank"], f, QColor(color), w=room)
    elif data["vp"] is not None and data["vp"] < 0:
        text(p, COL_RANK, base, f"\u2212{-data['vp']} VP", tnum(f), col("loss", 0.9))
    elif data["rank"] == "UNRANKED":
        text(p, COL_RANK, base, "NOT PLACED", f, col("grey3"))


def _glint(p, top, data, dt, loud=True):
    """One band of light across a Master or Paragon peak, along the chamfer's angle."""
    if dt < 0 or dt >= GLINT or not data["peak"]:
        return
    f = _peak_font()
    rank = data["peak"]["rank"]
    left = COL_RANK + _advance("PEAK ", f)
    room = COL_RANK_R - left
    width = min(_advance(rank, f), room)
    band, height = 22, 13
    base = top + RANK2
    x = left - band - height + (width + 2 * band + height) * motion.in_out_quad(dt / GLINT)
    path = QPainterPath()
    path.moveTo(x + height, base - height + 3)
    path.lineTo(x + band + height, base - height + 3)
    path.lineTo(x + band, base + 3)
    path.lineTo(x, base + 3)
    path.closeSubpath()
    p.save()
    p.setClipPath(path, Qt.ClipOperation.IntersectClip)
    color = TIER_COLOR.get(data["peak"]["tier"], TIER_COLOR["none"])
    text(p, left, base, rank, f, _lerp_color(color, "white", 0.8 if loud else 0.5), w=room)
    p.restore()


# A field that changes swaps out then in: different-length text never sits on itself.
SWAP_OUT, SWAP_IN_AT, SWAP_IN = 0.07, 0.05, 0.12
VP_DELAY, VP_TIME = 0.10, 0.36
SWAP_DONE = VP_DELAY + VP_TIME  # the longest field change
# A hero picked in the draft: the line under the name glides out to the left and the
# hero's line slides in from the right, slowing as it lands.
PICK_OUT, PICK_IN_AT, PICK_IN, PICK_SHIFT = 0.14, 0.12, 0.32, 16.0
_MIX_TIME = {"vp": SWAP_DONE, "strike": STRIKE, "glint": GLINT, "meta": PICK_IN_AT + PICK_IN}


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
        p.translate(0, snap(p, (1 - a_new) * rise))
        draw(data)
        p.restore()


def _slide(p, mix, data, draw, band):
    """The line under the name when the hero picked changed: old out to the left, new in
    from the right, within ``band``."""
    old, dt = (mix or {}).get("meta", (None, None))
    if dt >= PICK_IN_AT + PICK_IN:
        draw(data)
        return
    base = p.opacity()
    p.save()
    p.setClipRect(band, Qt.ClipOperation.IntersectClip)
    t_out = motion.clip(dt / PICK_OUT)
    if t_out < 1:
        p.save()
        p.setOpacity(base * (1 - motion.out_cubic(t_out)))  # gone before the new one is
        p.translate(snap(p, -0.5 * PICK_SHIFT * motion.in_cubic(t_out)), 0)
        draw(old)
        p.restore()
    t_in = motion.clip((dt - PICK_IN_AT) / PICK_IN)
    if t_in > 0:
        p.save()
        p.setOpacity(base * motion.out_cubic(t_in))
        p.translate(snap(p, (1 - motion.out_quart(t_in)) * PICK_SHIFT), 0)
        draw(data)
        p.restore()
    p.restore()


def _vp_swap(p, mix, x, y_mid, data, w=VP_BAR_W, right=COL_RANK_R, label=True, thin=False):
    """The VP bar fills from its old value; its label and the track swap like text."""
    old, dt = (mix or {}).get("vp", (None, None))
    if old is None or data["vp"] is None:
        _swap(
            p,
            mix,
            "vp",
            data,
            lambda d: _vp_bar(p, x, y_mid, d, w, thin=thin, label=label, right=right),
        )
        return
    start = old["vp"] if old["vp"] is not None else 0
    t = motion.out_cubic(motion.clip((dt - VP_DELAY) / VP_TIME))
    track = 1.0 if old["vp"] is not None else motion.out_cubic(motion.clip(dt / 0.16))
    fill = start + (data["vp"] - start) * t
    _vp_bar(p, x, y_mid, data, w, thin=thin, fill=fill, track=track, label=False, right=right)
    if 0.0 < t < 1.0:  # the charge's bright head, gone as it arrives
        color = TIER_COLOR.get(data["tier"], TIER_COLOR["none"])
        head = x + w * motion.clip(fill / 100) - 1
        p.fillRect(QRectF(head, y_mid - 3.5, 2, 6), _alpha(_lerp_color(color, "white", 0.6), 1 - t))
    if label:
        _swap(
            p,
            mix,
            "vp",
            data,
            lambda d: _vp_bar(p, x, y_mid, d, w, thin=thin, track=0.0, right=right),
        )


def _games_ago(e):
    return "LAST GAME" if e["ago"] == 1 else f"{e['ago']} GAMES AGO"


ENCOUNTER_SIDE = {"with": "dawn_hdr", "against": "dusk_hdr"}  # the panel's team colours


def _encounter(p, rect, e):
    """The hero they played against or with us, at the row's far edge. Two separate
    signals: the frame says whose side they were on, in the panel's team colours (blue
    with us, gold against), and the result word says how it went (green, red). Beside
    it their KDA, how often we met and when."""
    result = {"WE WON": "WON", "WE LOST": "LOST"}.get(e["result"])
    tone = {"WON": "win", "LOST": "loss"}.get(result)
    side_tone = ENCOUNTER_SIDE.get(e["side"], "grey3")
    box = QRectF(ENC_R - ENC_SIZE, rect.center().y() - ENC_SIZE / 2, ENC_SIZE, ENC_SIZE)
    wash = QLinearGradient(rect.right(), 0, rect.right() - 190, 0)
    wash.setColorAt(0.0, col(side_tone, 0.14))
    wash.setColorAt(1.0, col(side_tone, 0.0))
    p.fillRect(QRectF(rect.right() - 190, rect.top() + 1, 190, rect.height() - 2), wash)
    _portrait(p, box, e["icon"], e["hero"], side_tone)
    top = rect.top()
    baseline = top + LINE1 - 1
    figures = tnum(font(13, 500, 0.04))
    right = ENC_TEXT_R
    if e["kda"]:
        right -= text(p, right, baseline, e["kda"], figures, col("grey1"), "r") + 10
    word = f"WE {result}" if result else "MET"
    head = font(14, 700, 0.08, caps=True)
    right -= text(p, right, baseline, word, head, col(tone or "grey1"), "r") + 10
    if e["met"]:
        text(p, right, baseline, e["met"], figures, col("grey2"), "r")
    tail = tnum(font(12, 500, 0.08, caps=True))
    side = "VS US" if e["side"] == "against" else "WITH US"
    used = text(p, ENC_TEXT_R, top + LINE2, _games_ago(e), tail, col("grey1"), "r")
    used -= tail.letterSpacing()
    used += text(p, ENC_TEXT_R - used, top + LINE2, " \u00b7 ", tail, col("grey3"), "r")
    used -= tail.letterSpacing()
    text(p, ENC_TEXT_R - used, top + LINE2, side, tail, col(side_tone), "r")


PIP, PIP_GAP, PIPS_MAX = 12, 4, 10  # the own row's games of today: the newest fit


def _pips(p, x, middle, form):
    """One small tile per game, oldest first."""
    p.setPen(Qt.PenStyle.NoPen)
    for index, result in enumerate(form[-PIPS_MAX:]):
        p.setBrush(col("win" if result == "W" else "loss"))
        p.drawPath(chamfer(QRectF(x + index * (PIP + PIP_GAP), middle - PIP / 2, PIP, PIP), 3))


def _signed(value):
    return f"+{value}" if value > 0 else f"\u2212{-value}" if value < 0 else "0"


def _today(p, rect, today):
    """The own row after the name: today only, nothing the game itself shows."""
    top = rect.top()
    x = COL_ICON - 12
    form = today["form"]
    used = text(p, x, top + LINE1 - 1, "TODAY", font(12, 600, 0.10, caps=True), col("grey2"))
    _pips(p, x + used + 12, top + LINE1 - 5, form)
    wins = sum(result == "W" for result in form)
    line = f"{wins}W {len(form) - wins}L"
    if today["kda"] is not None:
        line += f"  \u00b7  KDA {today['kda']:.1f}"
    text(p, x, top + LINE2, line, tnum(font(12, 500, 0.08, caps=True)), col("grey1"))
    vp = today["vp"]
    if vp is None:
        return
    ink = "win" if vp > 0 else "loss" if vp < 0 else "grey1"
    used = text(p, COL_R, top + 28, " VP", font(13, 600, 0.08), col("grey2"), "r")
    text(p, COL_R - used, top + 28, _signed(vp), tnum(font(22, 700, 0.02)), col(ink), "r")


def _note(p, line2, data, flag=True):
    """Under the name: the player's main role ("Main Support": not this match's, which
    the game shows itself), then a long break if there was one, else the account's age. The
    flag keeps its chip at the end of the line (``flag=False``: the caller draws it)."""
    chip = _flag_of(data) if data["new"] else None  # the green name says YOU here
    if chip and flag:
        _flag(p, COL_NAME_R, line2 - 4, *chip)
    role, x = (data["main"] or {}).get("role"), COL_NAME
    word = f"Main {role.title()}" if role else None
    if data.get("picked"):  # the hero picked in this draft: their record on it
        line = data["picked"]
        end = COL_NAME_R - (52 if chip else 0)  # clear of a NEW chip
        if data.get("tag"):  # a hidden name: the tag still tells them apart, and the
            line = line.split(" \u00b7 ", 1)[-1]  # record alone: the game shows the hero
            narrow = font(13, 500, 0.04)
            narrow.setVariableAxis(QFont.Tag("wdth"), 80.0)
            room = end - COL_NAME
            left = room - _advance(line, narrow) - 8  # the tag condenses for the record
            x += (
                tag_chip(
                    p,
                    COL_NAME,
                    line2 - 4,
                    data["tag"],
                    left if tag_fits(data["tag"], left) else room,
                )
                + 8
            )
        for width in (100, 90, 80):  # condensed before it is elided
            f = font(13, 500, 0.04)
            f.setVariableAxis(QFont.Tag("wdth"), float(width))
            if _advance(line, f) <= end - x:
                break
        if not data.get("tag") or _advance(line, f) <= end - x:  # beside a tag: whole or not
            text(p, x, line2, line, f, col("gold_soft"), w=end - x)
        return
    if data.get("tag"):
        # A hidden name: the tag tells this player apart; the role follows if it fits.
        x += (
            tag_chip(
                p, COL_NAME, line2 - 4, data["tag"], COL_NAME_R - COL_NAME - (52 if chip else 0)
            )
            + 8
        )
        if word and x + _advance(word, font(13, 400, 0.05)) <= COL_NAME_R - (52 if chip else 0):
            text(p, x, line2, word, font(13, 400, 0.05), col("grey2"))
        return
    after = x  # where free text may start: past the role word
    if word:
        text(p, COL_NAME, line2, word, font(13, 400, 0.05), col("grey2"))
        after = COL_NAME + _advance(word, font(13, 400, 0.05)) + 10
        x += TAB_NOTE  # the age keeps its tab stop, clear of the rank emblem
    if data["back"]:
        x = max(x, after)
        room = COL_NAME_R - x
        text(p, x, line2, data["back"], name_font(12, data["back"], room), col("new"), w=room)
    elif not data["me"]:
        # With a role and a NEW chip on the line, the chip stands in for the age.
        shown = dict(data, age=None) if role and chip else data
        _meta(p, x, line2, shown)


def stack_row(p, rect, data, motion, mix=None):
    """One roster row on the panel's grid. ``mix`` maps a changed field group to
    (old row, seconds since)."""
    _row_background(p, rect, data["team"], data["me"])
    top = rect.top()
    line1, line2 = top + LINE1, top + LINE2
    middle = top + rect.height() / 2

    def name(d):
        room = COL_NAME_R - COL_NAME  # a flag lives on line 2: it never squeezes the name
        if d.get("party"):  # who plays together: a chip at the end of the name line
            room -= party_chip(p, COL_NAME_R, line1 - 6, d["party"]) + 6
        text(p, COL_NAME, line1, d["name"], name_font(18, d["name"], room), name_ink(d), w=room)

    # On the own row today takes the place of the rank, VP and peak cells.
    def rank(d):
        if d["today"]:
            return
        if d.get("unknown"):
            # Nothing to rank: one note across the rank and peak cells, no emblem.
            unknown_note(
                p, COL_ICON - 11, top + RANK1, top + RANK2 + 2, COL_R - COL_ICON + 11, d["unknown"]
            )
            return
        rank_badge(p, COL_ICON, middle, d["tier"])
        if d["state"] == "loading":  # only this cell waits; the row itself stays still
            p.save()
            p.setOpacity(p.opacity() * (0.55 + 0.45 * motion.pulse))
            _skeleton(p, COL_RANK, top + RANK1 - 5, 70, 9)
            p.restore()
            return
        # NO DATA has no peak or bar under it: it sits in the middle, by the emblem.
        base = middle + 5 if d["rank"] == "NO DATA" else top + RANK1
        _rank(p, COL_RANK, base, d, 15, w=COL_RANK_R - COL_RANK)

    fx = mix or {}
    _swap(p, mix, "main", data, lambda d: _main(p, rect, d))
    _swap(p, mix, "name", data, name)
    old_meta = (mix or {}).get("meta", (None, None))[0]
    if old_meta is not None and old_meta.get("picked") != data.get("picked"):
        chip = _flag_of(data) if data["new"] else None
        if chip:  # the chip stays put while the line slides past under it
            _flag(p, COL_NAME_R, line2 - 4, *chip)
        end = COL_NAME_R - (52 if chip else 0)
        band = QRectF(COL_NAME - 2, line2 - 16, end - COL_NAME + 2, 22)
        _slide(p, mix, data, lambda d: _note(p, line2, d, flag=False), band)
    else:
        _swap(p, mix, "meta", data, lambda d: _note(p, line2, d))
    _swap(p, mix, "rank", data, rank)
    if "strike" in fx and not data["today"] and not data.get("unknown"):
        _strike(p, COL_ICON, middle, 11, data["tier"], fx["strike"][1], motion.loud)
    if not data["today"]:
        _vp_swap(
            p, mix, COL_RANK, top + VP_Y, data, w=COL_RANK_R - COL_RANK, label=False, thin=True
        )

    def peak_cell(d):
        if not d["today"]:
            _peak(p, top, d)

    _swap(p, mix, "peak", data, peak_cell)
    if "glint" in fx and not data["today"]:
        _glint(p, top, data, fx["glint"][1], motion.loud)

    def intel(d):
        if d["today"]:
            _today(p, rect, d["today"])
        elif d["encounter"] and not d["me"]:
            _encounter(p, rect, d["encounter"])

    _swap(p, mix, "intel", data, intel, rise=4.0)


def paint_stack(
    p,
    width,
    allies,
    enemies,
    motion,
    mode=None,
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
    since its average arrived (see team_header). A team without rows gets no
    header and takes no room (the draft).
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
    balance = team_balance(allies, enemies)
    teams = [(team, rows) for team, rows in (("ally", allies), ("enemy", enemies)) if rows]
    for team, rows in teams or [("ally", allies), ("enemy", enemies)]:
        first = motion.row(index)
        if first > 0:
            p.save()
            p.setOpacity(p.opacity() * first)
            shared = balance if team == "enemy" else None
            reveal = (heads or {}).get(team)
            team_header(p, width, y + 18, team, rows, mode, shared, reveal)
            p.restore()
        y += STACK_HDR
        for data in rows:
            progress = motion.row(index)
            if progress > 0:
                p.save()
                p.setOpacity(p.opacity() * progress)
                p.translate(0, snap(p, (1 - progress) * motion.rise))
                mix = (mixes or {}).get(data["player_id"])
                rect = QRectF(PLATE_L, y, PLATE_R - PLATE_L, STACK_ROW_H)
                stack_row(p, rect, data, motion, mix)
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
