"""The live overlay: I/O lanes, the tracker thread and the panel window.

Pred.gg is read with the session the user stored by signing in from the tray
(``predgg.sign_in``); nothing here signs in on its own. Nothing here reads or
touches the game process: the roster comes from the screen, the phase from
the game's log file.
"""

from . import predgg as session
from .config import capture_dir, default_session, home
from .overlay import aliases, capture, lanes, namereader, recorder
from .overlay import tracker as tracker_module
from .overlay import window as overlay_window
from .portraits import PortraitIO


def _drop_retired(path, log):
    """An older version's Omeda tokens in the session file go now, not at the next sign-in."""
    from .auth import AuthError, SessionStore

    try:
        if b"omeda" not in path.read_bytes():
            return
        store = SessionStore(path)
        kept = store.load()
        if not kept["providers"]:
            return  # not signed in with Pred.gg yet: the file is left to whoever wrote it
        store.save(kept)
        log("removed the Omeda session an older version kept")
    except (OSError, AuthError):
        pass


class Runtime:
    def __init__(self, settings, log, session_path=None, capture=False, record=False):
        self.settings = settings
        self.capture = capture  # the --capture flag (debugging)
        self.record = record  # the --record flag (debugging): matches kept for replay
        self.log = log
        self.session_path = session_path or default_session()
        self.tracker = self.window = None
        self.recorder = None

    def start(self):
        store = aliases.Store(home() / "known-players.json")
        salt, learned, pending, found = store.load()
        if found == "fresh":
            try:
                store.save(salt, {}, [])  # the labels stay the same from the first start on
            except OSError as exc:
                self.log(f"known players not saved: {exc}")
        elif found == "unreadable":
            self.log("known players could not be read: kept as they are, nothing learned is saved")
            store = None
        predgg = lanes.PredggIO(self.session_path, store=store, salt=salt)
        predgg.learned, predgg.pending = dict(learned), list(pending)
        _drop_retired(self.session_path, self.log)
        if self.record:
            self.recorder = recorder.Recorder(home() / "matches", log=self.log)
        self.tracker = tracker_module.Tracker(
            {
                "predgg": lanes.Lane("pred-predgg", predgg.handle, None),
                "assets": lanes.Lane("pred-assets", PortraitIO().handle, None),
            },
            log=self.log,
            session_path=self.session_path,
            on_game=lambda: namereader.READER.warm(self.log),
            recorder=self.recorder,
        )
        self.window = overlay_window.Overlay(
            self.tracker,
            hotkey=self.settings["hotkey"],
            screen_index=self.settings["screen"],
            log=self.log,
            capture=capture.Capture(capture_dir(), log=self.log) if self.capture else None,
            names=namereader.READER,
            recorder=self.recorder,
        )
        self.window.set_paused(not self.settings["enabled"])
        self.tracker.events.put(("aliases", salt, learned, pending))  # before anything is read
        self.tracker.events.put(("auth", session.Session(self.session_path).signed_in()))
        self.tracker.start()

    @property
    def snapshot(self):
        return self.tracker.snapshot

    def set_enabled(self, on):
        self.window.set_paused(not on)

    def signed_in(self):
        """New tokens are stored: use them from now on."""
        self.tracker.events.put(("sign_in",))

    def signed_out(self):
        """The tokens are gone: the view asks for a sign-in at once."""
        self.tracker.events.put(("auth", False))

    def stop(self):
        if self.tracker is not None:
            self.tracker.shutdown()
        if self.recorder is not None:
            self.recorder.close()
        if self.window is not None:
            self.window.hide()
