"""The in-match card beside the scoreboard row under the cursor while TAB is held.

Its head repeats the panel row's fields; below come this match, the last
meeting and the season table with a rank ladder per season.
"""

import re

from PySide6.QtCore import QRectF
from PySide6.QtGui import QBrush, QLinearGradient, QPen

from .. import motion
from ..rows import habits, off_main, party_line, plays, rank_step, streak, tier
from .cells import (
    SIDE,
    _flag,
    _flag_of,
    _rank,
    _skeleton,
    _vp_bar,
    name_ink,
    rank_badge,
    tag_chip,
    tag_fits,
    unknown_note,
)
from .common import (
    BAR_W,
    TAB_ROLE,
    TIER_COLOR,
    _advance,
    chamfer,
    col,
    font,
    gold_rule,
    name_font,
    text,
    tnum,
)

CARD_W = 600
C_L, C_R = 20, 580
C_FLAG_R = 352
C_ICON, C_RANK = 374, 396  # head: same x as the table's PEAK column
C_LADDER = 92
C_FINAL, C_FINAL_POS, C_TOP = 216, 340, 384
C_PEAK, C_PEAK_POS = 396, 520
HEAD_H, VERDICT_H, FACTS_H, TABLE_HDR, SEASON_H, CARD_PAD_B = 66, 24, 40, 22, 20, 12
PLAYS_H = 40  # this match's hero and role, and the last matches (rows.plays)
FORM_PIP, FORM_STEP = 9, 14  # one square per match of the last ones, newest on the right
CREDIT_H = 14  # the line under the season table that names its source
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


def card_size(data, seen=None):
    """Width and height of the in-match card for one player."""
    height = HEAD_H
    if data["verdict"]:
        height += VERDICT_H
    if data.get("party"):
        height += VERDICT_H
    height += VERDICT_H * len(habits(data))
    if data["encounter"] or (seen and (seen.get("hero") or seen.get("role"))):
        height += FACTS_H
    if plays(data, seen):
        height += PLAYS_H
    rows = season_rows(data)
    if rows:
        height += TABLE_HDR + len(rows) * SEASON_H + CREDIT_H
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


def _plays(p, y, how):
    """ON HERO / ON ROLE / LAST N: Pred.gg's totals for this match's hero and role,
    and a square per recent match (green won, red lost, newest on the right)."""
    caption = font(10, 600, 0.12)
    value = font(12, 600, 0.06, caps=True)
    for x, label, line, right in (
        (C_L, "ON HERO", how["hero"], C_FINAL - 16),
        (C_FINAL, "ON ROLE", how["role"], C_TOP - 16),
    ):
        if line:
            text(p, x, y + 15, label, caption, col("grey3"))
            ink = col("gold_soft") if line != "FIRST GAME" else col("grey1")
            text(p, x, y + 31, line, value, ink, w=right - x)
    form = how["form"]
    if form:
        text(p, C_TOP, y + 15, f"LAST {len(form)}", caption, col("grey3"))
        run = streak(form)
        if run:
            text(p, C_R, y + 15, run, caption, col("win" if form[0] else "loss"), "r")
        x = C_TOP
        for won in reversed(form):  # oldest first: the newest ends the row
            p.fillRect(QRectF(x, y + 23, FORM_PIP, FORM_PIP), col("win" if won else "loss", 0.85))
            x += FORM_STEP


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


UNKNOWN_HEAD, UNKNOWN_ROW = 66, 24


def unknown_card_size(others):
    """The plate for a row whose player cannot be told: a heading, then ``others``."""
    return CARD_W, UNKNOWN_HEAD + len(others) * UNKNOWN_ROW + CARD_PAD_B


def paint_card_unknown(p, team, others, motion, chrome=True):
    """A scoreboard row the overlay cannot match to a player.

    Console players' names are never read, so a team with two or more of them
    cannot say which row is which: the plate lists that team's console players
    instead. With no ``others`` the row's name could not be read at all.
    """
    width, height = unknown_card_size(others)
    if chrome:
        card_chrome(p, width, height, team, motion.rule)
    title = "CONSOLE PLAYER" if others else "NOT RECOGNISED"
    text(p, C_L, 32, title, name_font(22, title, C_R - C_L), col("name"))
    if others:
        whose = "YOUR TEAM" if team == "ally" else "THEIR TEAM"
        note = f"ONE OF {whose}'S CONSOLE PLAYERS · NAMES ARE NOT READ"
    else:
        note = "THE NAME ON THIS ROW COULD NOT BE READ"
    text(p, C_L, 52, note, font(11, 500, 0.08, caps=True), col("grey3"))
    y = UNKNOWN_HEAD
    label = font(12, 600, 0.06, caps=True)
    for other in others:
        _hairline(p, y)
        base = y + 16
        text(p, C_L, base, other["name"], label, col("grey1"), w=C_RANK - C_L - 12)
        rank = other.get("rank") or ""
        if other.get("unknown"):  # no data, and why: not a bare NO DATA
            rank = {
                "console": "NAME HIDDEN",
                "offline": "NO ANSWER",
                "noprofile": "NO PROFILE",
            }.get(other["unknown"], "NOT FOUND")
        color = TIER_COLOR.get(other.get("tier"), "grey2")
        text(p, C_R, base, rank, label, col(color), "r")
        y += UNKNOWN_ROW


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
    p.setOpacity(base_alpha * entered(len(rows)))
    # Past seasons, ladder places and mains come from Pred.gg: say so where they show.
    text(p, C_R, y + 11, "SEASONS: PRED.GG", font(9, 500, 0.08, caps=True), col("grey3"), "r")
    p.setOpacity(base_alpha)
    return y + CREDIT_H


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
    if r["kind"] == "now":  # the running rank (lookup.current_rank)
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


def _card_meta(p, baseline, data):
    """The account's age and its matches in all (Pred.gg's count over every mode and
    season), from the name's column; returns where the line goes on."""
    f = tnum(font(12, 500, 0.08, caps=True))
    young = data["new"] or data["young_high"]
    total = (data.get("stats") or {}).get("total")
    items = [(data["age"], col("new") if young else col("grey1"))] if data["age"] else []
    if total:
        items.append((f"{total} GAMES", col("grey1")))
    x = C_L
    for i, (value, ink) in enumerate(items):
        if i:
            x += text(p, x, baseline, " · ", f, col("grey3"))
        x += text(p, x, baseline, value, f, ink)
    return x


def _card_main(p, x, right, baseline, data):
    """MAIN and the hero, then the role when it fits; items stay whole. Not on the own card."""
    main = data["main"]
    if not main or data["me"]:
        return
    f = tnum(font(12, 500, 0.08, caps=True))
    x += text(p, x, baseline, "MAIN ", f, col("grey3"))
    for value in (f"{main['hero']} · {main['role']}", main["hero"]):
        if x + _advance(value, f) <= right:
            text(p, x, baseline, value, f, col("grey1"))
            return


def paint_card(p, data, motion, seen=None, chrome=True, reveal=None):
    """The card shown next to the scoreboard row under the cursor while TAB is held.

    ``seen`` is what the scoreboard row says about the player in this match:
    {"hero": "STEEL", "role": "OFFLANE"} (read once from the screen). With
    ``chrome=False`` only the content is drawn (to crossfade between players).
    ``reveal`` is the seconds since the card opened, while its table comes in.
    """
    width, height = card_size(data, seen)
    if chrome:
        card_chrome(p, width, height, data["team"], motion.rule)
    line1, line2 = 32, 52
    room = C_FLAG_R - C_L
    name = name_font(22, data["name"], room)
    used = text(p, C_L, line1, data["name"], name, name_ink(data), w=room)
    tag = data.get("tag")
    if tag and tag_fits(tag, C_FLAG_R - (C_L + used + 10)):
        tag_chip(p, C_L + used + 10, line1 - 7, tag, C_FLAG_R - (C_L + used + 10))
    after = _card_meta(p, line2, data)
    right = C_FLAG_R
    if _flag_of(data):
        right -= _flag(p, C_FLAG_R, line2 - 4, *_flag_of(data))
    _card_main(p, max(C_L + TAB_ROLE, after + 14), right, line2, data)
    if data.get("unknown"):
        unknown_note(p, C_ICON - 13, line1 - 4, line2 - 2, C_R - C_ICON + 13, data["unknown"])
    else:
        rank_badge(p, C_ICON, (line1 + line2) / 2 - 5, data["tier"], 13)
        rank_base = (line1 + line2) / 2 if data["rank"] == "NO DATA" else line1
        _rank(p, C_RANK, rank_base, data, 16, w=C_R - C_RANK)
        _vp_bar(p, C_RANK, line2 - 4, data, w=C_PEAK_POS - C_RANK, right=C_R)
    y = HEAD_H
    if data["verdict"]:
        _hairline(p, y)
        ink = col("new") if data["verdict"]["alert"] else col("gold_soft")
        line = font(12, 600, 0.10, caps=True)
        text(p, C_L, y + 17, data["verdict"]["text"], line, ink, w=C_R - C_L)
        y += VERDICT_H
    if data.get("party"):
        _hairline(p, y)
        ink = col(f"party{data['party']['group'] % 2}")
        line = font(12, 600, 0.10, caps=True)
        text(p, C_L, y + 17, party_line(data["party"]), line, ink, w=C_R - C_L)
        y += VERDICT_H
    for fact, alert in habits(data):
        _hairline(p, y)
        line = font(12, 600, 0.10, caps=True)
        text(p, C_L, y + 17, fact, line, col("new") if alert else col("grey1"), w=C_R - C_L)
        y += VERDICT_H
    played = seen and (seen.get("hero") or seen.get("role"))
    if played or data["encounter"]:
        _hairline(p, y)
        caption = font(10, 600, 0.12)
        value = font(12, 600, 0.06, caps=True)
        separator = "  ·  "
        if played:
            off = off_main(data, seen)
            width_ = text(p, C_L, y + 15, "THIS MATCH", caption, col("grey3"))
            if off:
                text(p, C_L + width_, y + 15, f"  ·  {off}", caption, col("grey1"))
            parts = [part for part in (seen.get("hero"), seen.get("role")) if part]
            text(
                p, C_L, y + 31, separator.join(parts), value, col("gold_soft"), w=C_FINAL - C_L - 16
            )
        if data["encounter"]:
            e = data["encounter"]
            accent, ink = SIDE[e["side"]]
            p.fillRect(QRectF(C_FINAL - 10, y + 8, 2, 25), col(accent, 0.9))
            text(p, C_FINAL, y + 15, "LAST MET", caption, col("grey3"))
            x = C_FINAL
            side = "VS US" if e["side"] == "against" else "WITH US"
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
    how = plays(data, seen)
    if how:
        _hairline(p, y)
        _plays(p, y, how)
        y += PLAYS_H
    rows = season_rows(data)
    if rows:
        _season_table(p, y, rows, data, reveal)
