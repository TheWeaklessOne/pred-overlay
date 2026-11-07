"""The live overlay: I/O lanes, the tracker thread and the panel window.

Signing in needs no step of its own: the first backend call finds no session
and asks the running Steam client for a ticket (see ``game.stored_tokens``).
Nothing here reads or touches the game process; the roster comes from the
game backend, the phase from the game's log file.
"""

from .config import cache_dir, default_session, token_for
from .errors import PredError
from .overlay import lanes
from .overlay import tracker as tracker_module
from .overlay import window as overlay_window
from .portraits import PortraitIO


class Runtime:
    def __init__(self, settings, log, session_path=None):
        self.settings = settings
        self.log = log
        self.session_path = session_path or default_session()
        self.tracker = self.window = None

    def start(self):
        omeda = lanes.OmedaIO(self.session_path, cache_dir())
        try:
            predgg_token = token_for("predgg", self.session_path)  # optional Pred.gg stats
        except PredError as exc:  # an unreadable session must not stop the app
            self.log(f"session not readable: {exc}")
            predgg_token = None
        predgg = lanes.PredggIO(token=predgg_token)
        self.tracker = tracker_module.Tracker(
            {
                "omeda": lanes.Lane("pred-omeda", omeda.handle, None),
                "predgg": lanes.Lane("pred-predgg", predgg.handle, None),
                "assets": lanes.Lane("pred-assets", PortraitIO().handle, None),
            },
            cards="predgg" if predgg_token else "omeda",
            region=self.settings["region"],
            log=self.log,
        )
        self.window = overlay_window.Overlay(
            self.tracker,
            hotkey=self.settings["hotkey"],
            screen_index=self.settings["screen"],
            log=self.log,
        )
        self.window.set_paused(not self.settings["enabled"])
        self.tracker.start()

    @property
    def snapshot(self):
        return self.tracker.snapshot

    def set_region(self, region):
        self.tracker.events.put(("set_region", region))

    def set_enabled(self, on):
        self.window.set_paused(not on)

    def sign_in(self):
        """Ask Steam for new tokens now (also switches to the account Steam runs)."""
        self.tracker.events.put(("sign_in",))

    def stop(self):
        if self.tracker is not None:
            self.tracker.shutdown()
        if self.window is not None:
            self.window.hide()
