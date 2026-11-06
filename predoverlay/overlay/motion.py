"""Pure motion model for the menu strip, the badge and the panel's seam (no Qt).

Everything is a function of (state, now): ``sync`` folds new data into tracks,
``frame`` turns the tracks into what to draw at ``now``, ``busy_until`` says
when the last track settles so the 16 ms frame timer can stop.

The strip opens like a drawer from under the badge; a mode first makes room
and then fades in, and leaves the other way round; counts roll digit by digit;
"VS AI" keeps a grey 0 for LINGER before it leaves, so a short-lived queue
never just blinks. Text measuring is injected (``measure(text, kind)``) so
this stays testable without Qt.
"""

from dataclasses import dataclass, field

from .. import modes


def out_cubic(t):
    return 1 - (1 - t) ** 3


def in_cubic(t):
    return t**3


def in_out_cubic(t):
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def in_out_quad(t):
    return 2 * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 2 / 2


def out_quart(t):
    return 1 - (1 - t) ** 4


def clip(v):
    return max(0.0, min(1.0, v))


EASE = {
    "out_cubic": out_cubic,
    "in_cubic": in_cubic,
    "in_out_cubic": in_out_cubic,
    "in_out_quad": in_out_quad,
    "out_quart": out_quart,
}


@dataclass(frozen=True)
class Tween:
    a: float = 1.0
    b: float = 1.0
    t0: float = 0.0
    dur: float = 0.0
    ease: str = "out_cubic"

    def at(self, now):
        if self.dur <= 0 or now >= self.t0 + self.dur:
            return self.b
        if now <= self.t0:
            return self.a
        return self.a + (self.b - self.a) * EASE[self.ease]((now - self.t0) / self.dur)

    @property
    def end(self):
        return self.t0 + max(self.dur, 0.0)

    def to(self, b, now, dur, ease, delay=0.0):
        """Retarget from wherever the value is now: no jumps on interruption."""
        if b == self.b:
            return self
        return Tween(self.at(now), b, now + delay, dur, ease)


# --- timing (seconds); same vocabulary as window.py: ENTER .22 OutCubic, EXIT .14 InCubic
SLOT_OPEN, SLOT_FADE_IN, SLOT_FADE_DELAY = 0.26, 0.18, 0.10  # make room, then appear
SLOT_FADE_OUT, SLOT_CLOSE, SLOT_CLOSE_DELAY = 0.14, 0.22, 0.08  # disappear, then close gap
RISE = 6.0  # px an entering item rises (rows use 8)
ROLL, ROLL_STAGGER = 0.26, 0.02  # digit roll, right-most digit first
ROLL_DIST = 16.0  # px a digit travels (> cap height: old and new never overlap much)
ZERO_TINT = 0.24  # white <-> grey4 when a count crosses 0
DRAWER_OPEN, DRAWER_CLOSE = 0.30, 0.18
ITEM_STAGGER, ITEM_FADE = 0.03, 0.18  # drawer open: items fade in from the badge outwards
DRAWER_SLIDE = 16.0
DIM_AFTER, DIM = 65.0, 0.40  # two missed polls: counts go grey
STALE = 90.0  # = core.QUEUES_STALE: drawer closes
LINGER = 55.0  # VS AI keeps a grey 0 this long before it leaves
COLLAPSE_MAX = 9999  # above this, format compactly


def fmt(n):
    return str(n) if n <= COLLAPSE_MAX else f"{n / 1000:.0f}K" if n < 100_000 else "99K+"


@dataclass
class Slot:
    key: str
    label: str
    value: int
    prev: str = ""  # string rolled away from
    roll_t0: float = -1e9
    roll_dir: int = 1  # +1 up (new digit from below), -1 down
    width: Tween = field(default_factory=Tween)  # layout presence 0..1
    alpha: Tween = field(default_factory=Tween)  # opacity 0..1
    zero_tint: Tween = field(default_factory=lambda: Tween(0, 0))  # 1 = grey4
    num_w: Tween = field(default_factory=Tween)  # number width (design px)
    zero_since: float | None = None
    leaving: bool = False


@dataclass
class ItemFrame:
    key: str
    label: str
    x: float  # left edge, relative to the strip's right edge (negative)
    label_w: float
    num_w: float
    alpha: float
    rise: float
    sep_x: float | None  # the "|" centre, or None
    sep_alpha: float
    digits: list  # [(old_char, new_char, progress, dir)] right-aligned
    zero: float  # 0 white .. 1 grey4
    dim: float


@dataclass
class StripFrame:
    open: float  # drawer 0..1 (eased)
    width: float  # full strip width at this instant (design px)
    head_x: float
    items: list
    head: str = ""
    head_alpha: float = 1.0
    content: float = 1.0  # extra content opacity while the drawer closes


class Strip:
    """Per-item tracks for the strip. ``measure(text, kind)`` gives design-px advances."""

    def __init__(self, measure, *, pad=56, head_gap=22, sep=16, label_gap=7, end_pad=14):
        self.measure = measure
        self.pad, self.head_gap, self.sep, self.label_gap, self.end_pad = (
            pad,
            head_gap,
            sep,
            label_gap,
            end_pad,
        )
        self.slots = {}  # mode id -> Slot, kept in display order
        self.seen = {}  # mode id -> order of first sight (ties among unknown modes)
        self.label_max = measure(modes.LONGEST_LABEL, "name")
        self.drawer = Tween(0, 0)
        self.drawer_t0 = -1e9  # when the current opening started (for the stagger)
        self.dim = Tween(0, 0)
        self.head = ""

    # --- data in -----------------------------------------------------------
    def sync(self, queues, queues_at, region, now, *, revealed=True):
        """Fold the latest snapshot in; cheap, call on every 50 ms tick.

        ``revealed`` False (panel invisible): snap everything, nothing animates.
        """
        before = self.busy_until()
        age = now - queues_at if queues_at else 1e9
        fresh = age <= STALE and bool(queues)
        head = f"IN QUEUE · {'EU' if region == 'EUROPE' else region}"
        if head != self.head:
            self.head = head
        if fresh and self.drawer.b == 0:
            # Opening from closed: start from current numbers, no rolls.
            self.slots = {}
            self._fold(queues, now, animate=False)
            self.drawer = self.drawer.to(1, now, DRAWER_OPEN, "out_cubic")
            self.drawer_t0 = now
            self.dim = Tween(0, 0)
        elif not fresh and self.drawer.b == 1:
            self.drawer = self.drawer.to(0, now, DRAWER_CLOSE, "in_cubic")
        elif fresh:
            self._fold(queues, now, animate=True)
        self.dim = self.dim.to(1.0 if age > DIM_AFTER else 0.0, now, DIM, "in_out_quad")
        # Drop slots whose exit finished.
        for key in [k for k, s in self.slots.items() if s.leaving and now >= s.width.end]:
            del self.slots[key]
        if not revealed:
            self.snap()
        return self.busy_until() != before

    def _order(self, key):
        return (modes.position(key), self.seen.get(key, 0), key)

    def _fold(self, queues, now, animate):
        keys = list(dict.fromkeys([*queues, *self.slots]))
        for key in keys:
            self.seen.setdefault(key, len(self.seen))
        wanted = {}
        for key in keys:
            slot = self.slots.get(key)
            present = key in queues
            value = queues[key][0] if present else 0
            if key in modes.OPTIONAL and present:
                if value:
                    if slot:
                        slot.zero_since = None
                else:
                    if slot is None or slot.leaving:
                        present = False
                    else:
                        slot.zero_since = slot.zero_since or now
                        present = now - slot.zero_since < LINGER
            wanted[key] = (present, value)
        # More modes than room: empty ones go first, then the least important.
        shown = sorted(
            (k for k, (p, _) in wanted.items() if p),
            key=lambda k: (0 if wanted[k][1] else 1, self._order(k)),
        )
        shown = set(shown[: modes.MAX_SLOTS])
        order = []
        for key in keys:
            slot = self.slots.get(key)
            present, value = wanted[key]
            present = present and key in shown
            if present and (slot is None or slot.leaving):
                if slot is None:
                    slot = Slot(key, modes.label(key), value, prev=fmt(value))
                    slot.num_w = Tween(self._num_w(value), self._num_w(value))
                    slot.zero_tint = Tween(*(2 * [1.0 if value == 0 else 0.0]))
                if animate:
                    slot.width = (
                        slot.width.to(1, now, SLOT_OPEN, "out_cubic")
                        if slot.leaving
                        else Tween(0, 1, now, SLOT_OPEN, "out_cubic")
                    )
                    slot.alpha = Tween(
                        slot.alpha.at(now) if slot.leaving else 0,
                        1,
                        now + SLOT_FADE_DELAY,
                        SLOT_FADE_IN,
                        "out_cubic",
                    )
                else:
                    slot.width, slot.alpha = Tween(1, 1), Tween(1, 1)
                slot.leaving = False
                self.slots[key] = slot
            elif not present and slot and not slot.leaving:
                slot.leaving = True
                slot.alpha = slot.alpha.to(0, now, SLOT_FADE_OUT, "in_cubic")
                slot.width = Tween(
                    slot.width.at(now), 0, now + SLOT_CLOSE_DELAY, SLOT_CLOSE, "in_out_cubic"
                )
            if slot and not slot.leaving and value != slot.value:
                if animate:
                    slot.prev = fmt(slot.value)
                    slot.roll_t0 = now
                    slot.roll_dir = 1 if value > slot.value else -1
                    slot.num_w = slot.num_w.to(self._num_w(value), now, ROLL, "out_cubic")
                    slot.zero_tint = slot.zero_tint.to(
                        1.0 if value == 0 else 0.0, now, ZERO_TINT, "out_cubic"
                    )
                else:
                    slot.prev = fmt(value)
                    slot.num_w = Tween(self._num_w(value), self._num_w(value))
                    slot.zero_tint = Tween(*(2 * [1.0 if value == 0 else 0.0]))
                slot.value = value
            if slot:
                order.append(slot)
        # Slots stay in menu order (a leaving one keeps its place while it closes).
        self.slots = {s.key: s for s in sorted(order, key=lambda s: self._order(s.key))}

    def snap(self):
        """Hidden panel: jump every track to its target."""

        def done(t):
            return Tween(t.b, t.b)

        self.drawer, self.dim = done(self.drawer), done(self.dim)
        self.drawer_t0 = -1e9
        for key in [k for k, s in self.slots.items() if s.leaving]:
            del self.slots[key]
        for s in self.slots.values():
            s.width, s.alpha, s.num_w, s.zero_tint = map(
                done, (s.width, s.alpha, s.num_w, s.zero_tint)
            )
            s.prev, s.roll_t0 = fmt(s.value), -1e9

    def replay(self, now):
        """A fresh reveal (menu entered, F8 show, focus back): open the drawer again."""
        self.snap()
        if self.drawer.b == 1:
            self.drawer = Tween(0, 1, now, DRAWER_OPEN, "out_cubic")
            self.drawer_t0 = now

    # --- frames out --------------------------------------------------------
    def _num_w(self, value):
        return self.measure(fmt(value), "count")

    def busy_until(self):
        ends = [self.drawer.end, self.dim.end]
        if self.drawer_t0 > -1e8:
            ends.append(self.drawer_t0 + ITEM_STAGGER * len(self.slots) + ITEM_FADE)
        for s in self.slots.values():
            ends += [
                s.width.end,
                s.alpha.end,
                s.num_w.end,
                s.zero_tint.end,
                s.roll_t0 + ROLL + ROLL_STAGGER * 4,
            ]
        return max(ends)

    def frame(self, now):
        sep_w = self.sep * 2 + self.measure("|", "name")
        slots = list(self.slots.values())
        widths = [s.width.at(now) for s in slots]
        x = -self.end_pad
        items = []
        n = len(slots)
        for i in range(n - 1, -1, -1):
            s = slots[i]
            wp = widths[i]
            label_w = min(self.measure(s.label, "name"), self.label_max)
            num_w = s.num_w.at(now)
            x -= (label_w + self.label_gap + num_w) * wp
            left_of = max(widths[:i], default=0.0)
            sep_p = wp * left_of
            sep_x = None
            if i and sep_p > 0:
                sep_x = x - sep_w * sep_p / 2
            a = s.alpha.at(now)
            from_badge = n - 1 - i  # the drawer unrolls away from the badge
            if self.drawer_t0 > -1e8:
                a *= out_cubic(
                    _clip((now - self.drawer_t0 - from_badge * ITEM_STAGGER) / ITEM_FADE)
                )
            items.append(
                ItemFrame(
                    s.key,
                    s.label,
                    x,
                    label_w,
                    num_w,
                    a,
                    RISE * (1 - a) if not s.leaving else 0.0,
                    sep_x,
                    sep_p * (a if not s.leaving else s.alpha.at(now)),
                    self._digits(s, now),
                    s.zero_tint.at(now),
                    self.dim.at(now),
                )
            )
            x -= sep_w * sep_p
        x -= self.head_gap
        head_x = x - self.measure(self.head, "head")
        width = -(head_x - self.pad)
        op = self.drawer.at(now)
        head_a = 1.0
        if self.drawer_t0 > -1e8:
            head_a = out_cubic(_clip((now - self.drawer_t0 - n * ITEM_STAGGER) / ITEM_FADE))
        content = 1.0 if self.drawer.b == 1 else op * op
        return StripFrame(op, width, head_x, list(reversed(items)), self.head, head_a, content)

    def _digits(self, s, now):
        new, old = fmt(s.value), s.prev
        k = max(len(new), len(old))
        new, old = new.rjust(k), old.rjust(k)
        out = []
        for j in range(k):  # j = position from the left
            from_right = k - 1 - j
            p = _clip((now - s.roll_t0 - from_right * ROLL_STAGGER) / ROLL)
            p = out_cubic(p) if old[j] != new[j] else 1.0
            out.append((old[j], new[j], p, s.roll_dir))
        return out


_clip = clip


# --- badge -----------------------------------------------------------------
LABEL_OUT, LABEL_IN, LABEL_IN_DELAY, LABEL_SHIFT = 0.09, 0.18, 0.06, 4.0
ACCENT_LERP, HALO = 0.24, 0.60


@dataclass
class Badge:
    health: str = "ok"
    prev: str = "ok"
    t0: float = -1e9

    def sync(self, health, now, revealed=True):
        if health != self.health:
            self.prev, self.health = (self.health, health) if revealed else (health, health)
            self.t0 = now if revealed else -1e9
            return True
        return False

    def busy_until(self):
        return self.t0 + max(LABEL_IN_DELAY + LABEL_IN, ACCENT_LERP, HALO)

    def frame(self, now):
        dt = now - self.t0
        return {
            "old": self.prev,
            "new": self.health,
            "old_a": 1 - in_cubic(_clip(dt / LABEL_OUT)),
            "old_dy": -LABEL_SHIFT * in_cubic(_clip(dt / LABEL_OUT)),
            "new_a": out_cubic(_clip((dt - LABEL_IN_DELAY) / LABEL_IN)),
            "new_dy": LABEL_SHIFT * (1 - out_cubic(_clip((dt - LABEL_IN_DELAY) / LABEL_IN))),
            "accent": out_cubic(_clip(dt / ACCENT_LERP)),  # 0 old colour .. 1 new
            "halo": _clip(dt / HALO) if dt < HALO else 1.0,  # one flare 0..1
        }


# --- the seam ---------------------------------------------------------------
# The panel enters and leaves through its gold rule: the body folds into the
# rule, the rule draws in to its diamond, and on a handoff the same thing runs
# backwards at the new place. Times are (body, rule starts at, rule).
SEAM_CLOSE = (0.20, 0.16, 0.14)
SEAM_OPEN = (0.12, 0.08, 0.22)  # (rule, body starts at, body)
SEAM_END = (0.12, 0.08, 0.12)  # the match starts: a quicker close, nothing opens


@dataclass(frozen=True)
class SeamFrame:
    body: float  # share of the panel's height that shows, 0..1
    rule: float  # the gold rule's length, 0..1
    moved: bool  # the close is over: the panel may change place
    done: bool


@dataclass
class Seam:
    """One close (and, with ``opens``, one open at the new place), by the clock:
    a slow frame skips ahead instead of stretching the motion. ``stop`` freezes
    it where it is, for a fade to take over."""

    t0: float
    opens: bool = True
    stopped: float | None = None

    @property
    def close_time(self):
        body, rule_at, rule = SEAM_CLOSE if self.opens else SEAM_END
        return max(body, rule_at + rule)

    @property
    def total(self):
        rule, body_at, body = SEAM_OPEN
        return self.close_time + (max(rule, body_at + body) if self.opens else 0.0)

    def stop(self, now):
        if self.stopped is None:
            self.stopped = now

    def frame(self, now):
        at = now if self.stopped is None else min(now, self.stopped)
        t = at - self.t0
        if t < self.close_time:
            body, rule_at, rule = SEAM_CLOSE if self.opens else SEAM_END
            return SeamFrame(
                1 - in_out_cubic(clip(t / body)),
                1 - in_cubic(clip((t - rule_at) / rule)),
                False,
                False,
            )
        if not self.opens:
            return SeamFrame(0.0, 0.0, False, True)
        t -= self.close_time
        rule, body_at, body = SEAM_OPEN
        return SeamFrame(
            out_cubic(clip((t - body_at) / body)),
            out_quart(clip(t / rule)),
            True,
            t >= max(rule, body_at + body),
        )
