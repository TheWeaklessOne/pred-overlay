"""Pure motion models for the menu badge and the panel's seam (no Qt).

Everything is a function of (state, now): ``sync`` folds new data in,
``frame`` says what to draw at ``now``, ``busy_until`` says when the motion
settles so the 16 ms frame timer can stop.
"""

from dataclasses import dataclass


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
            "old_a": 1 - in_cubic(clip(dt / LABEL_OUT)),
            "old_dy": -LABEL_SHIFT * in_cubic(clip(dt / LABEL_OUT)),
            "new_a": out_cubic(clip((dt - LABEL_IN_DELAY) / LABEL_IN)),
            "new_dy": LABEL_SHIFT * (1 - out_cubic(clip((dt - LABEL_IN_DELAY) / LABEL_IN))),
            "accent": out_cubic(clip(dt / ACCENT_LERP)),  # 0 old colour .. 1 new
            "halo": clip(dt / HALO) if dt < HALO else 1.0,  # one flare 0..1
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
