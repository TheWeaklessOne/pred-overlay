"""Full-screen frames at the moments a screen reader looks, to build and test those readers.

A debugging aid, not a user setting: only the hidden ``--capture`` flag turns it
on, for that run of the app (autostart and updates start it without). Frames are PNG
files in ``%LOCALAPPDATA%\\PredOverlay\\captures``; past KEEP the oldest go.
The frames show the overlay too, as any screenshot would (its panels sit clear
of the name boxes the readers use). Like the
TAB reader this grabs the screen and never touches the game process. The frames
show other players' names: they stay on this PC.
"""

import threading
import time

# moment: (seconds after it began, seconds between frames, frames per occurrence)
MOMENTS = {
    "draft": (2.0, 8.0, 14),
    "loading": (3.0, 7.0, 3),  # the loading screen, both teams
    "tab": (0.6, 40.0, 6),  # TAB held in the match
    "end": (2.0, 4.0, 8),  # the end-of-match board, after the cinematic
}
KEEP = 3000  # about 30 frames (3 MB each at 1440p) a match: some 100 matches, 9 GB


def moment(view, *, tab_since, foreground, on_loading_screen, in_match):
    """(moment, occurrence key, began at) to capture now, or None."""
    if not foreground:
        return None
    if view.phase == "draft":
        return "draft", ("draft", view.phase_at), view.phase_at
    if on_loading_screen:
        return "loading", ("loading", view.phase_at), view.phase_at
    if in_match and view.ended_at:
        return "end", ("end", view.ended_at), view.ended_at
    if in_match and tab_since is not None:
        return "tab", ("tab", view.phase_at), tab_since
    return None


class Planner:
    """When the next frame of each occurrence is due; pure, so it is easy to test."""

    def __init__(self):
        self.taken = {}  # occurrence key -> (frames, last frame at)

    def due(self, kind, key, began, now):
        first, every, most = MOMENTS[kind]
        frames, last = self.taken.get(key, (0, None))
        if frames >= most or now - began < first:
            return False
        if last is not None and now - last < every:
            return False
        self.taken[key] = (frames + 1, now)
        if len(self.taken) > 64:
            self.taken.pop(next(iter(self.taken)))
        return True


def prune(folder, keep=KEEP):
    frames = sorted(folder.glob("*.png"))
    for old in frames[: max(0, len(frames) - keep)]:
        try:
            old.unlink()
        except OSError:
            pass


class Capture:
    """Saves the grabbed frames on a worker thread: PNG encoding takes a while."""

    def __init__(self, folder, clock=time.time, log=None):
        self.folder = folder
        self.clock = clock
        self.log = log or (lambda message: None)
        self.planner = Planner()
        self.saved = 0
        self.error = None

    def due(self, kind, key, began, now):
        return self.planner.due(kind, key, began, now)

    def save(self, image, kind):
        """``image`` is a QImage (reentrant: safe to save on another thread)."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        path = self.folder / f"{stamp}-{kind}-{self.saved:04d}.png"
        self.saved += 1
        threading.Thread(
            target=self._write, args=(image, path), daemon=True, name="pred-capture"
        ).start()

    def _write(self, image, path):
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            if not image.save(str(path), "PNG"):
                raise OSError("the image was not written")
            prune(self.folder)
        except Exception as exc:  # a full disk: frames stop, the overlay goes on
            message = f"{type(exc).__name__}: {exc}"
            if message != self.error:
                self.error = message
                self.log(f"capture failed: {message}")
