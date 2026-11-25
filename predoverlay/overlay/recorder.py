"""A local record of each match: what the core took in and gave out, to replay it.

A development aid, off unless the app is started with ``--record``: the files hold
other players' names. One folder per match in ``%LOCALAPPDATA%\\PredOverlay\\matches``,
named by when it began (the accept, or the draft when the app started late):

    state.json    the core's state as the match began
    events.jsonl  every event the core took, with its time, and the commands it gave
    *.jpg         a frame of the loading screen, taken whatever the reading gates say

The core is a pure reducer, so ``state.json`` and ``events.jsonl`` replay the match
exactly (``replay``). These files hold other players' names and Pred.gg answers: they
stay on this PC, like the capture frames; nothing here goes into the log or the
diagnostics zip. The newest KEEP matches are kept.
"""

import dataclasses
import json
import shutil
import threading
import time
from pathlib import Path

KEEP = 20  # matches; about 0.5 MB of events and one frame each
STARTS = ("accepted", "draft", "loading")  # phases a match is recorded from


def encode(value):
    """JSON for the core's values: tuples, sets and non-string keys kept apart."""
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value):
            return {k: encode(v) for k, v in value.items()}
        return {"__d": [[encode(k), encode(v)] for k, v in value.items()]}
    if isinstance(value, tuple):
        return {"__t": [encode(v) for v in value]}
    if isinstance(value, (set, frozenset)):
        return {"__s": [encode(v) for v in sorted(value, key=repr)]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {"__dc": encode(dataclasses.asdict(value))}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"__r": repr(value)}  # nothing the core needs back (a sentinel, say)


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "__d" in value:
        return {_hashable(decode(k)): decode(v) for k, v in value["__d"]}
    if "__t" in value:
        return tuple(decode(v) for v in value["__t"])
    if "__s" in value:
        return {_hashable(decode(v)) for v in value["__s"]}
    if "__dc" in value:
        return decode(value["__dc"])  # as a dict: the core rebuilds those it needs
    if "__r" in value:
        return None
    return {k: decode(v) for k, v in value.items()}


TICK = {"__t": ["tick"]}  # ("tick",) encoded


def _hashable(value):
    return tuple(_hashable(v) for v in value) if isinstance(value, list) else value


class Recorder:
    """Called by the tracker after every event (``after``); the window adds frames."""

    def __init__(self, root, keep=KEEP, log=None):
        self.root = Path(root)
        self.keep = keep
        self.log = log or (lambda message: None)
        self.lock = threading.Lock()
        self.folder = None  # this match's folder, while one is recorded
        self.file = None
        self.epoch = None
        self.broken = False  # a write failed: recording stops for this run

    def after(self, state, event, commands, now, before=None):
        """Note one event the core took: ``event`` as ``encode`` gave it before the core
        saw it (the core changes some payloads in place), ``before`` the state the core
        found (the tracker copies it for the events that can begin a match). Never raises."""
        if self.broken:
            return
        try:
            with self.lock:
                if state.epoch != self.epoch and state.phase in STARTS and state.game_running:
                    self._begin(before or state, now)
                    self.epoch = state.epoch
                    if before is None:
                        return  # the state already holds this event
                if self.file is None:
                    return
                if event == TICK and not commands and state.phase == "menu":
                    return  # the menu's idle ticks replay as nothing
                line = {"t": now, "e": event}
                if commands:
                    line["c"] = encode(commands)
                self.file.write(json.dumps(line, ensure_ascii=False) + "\n")
                self.file.flush()
        except Exception as exc:  # a full disk, a locked folder: the app goes on without it
            self.broken = True
            self.log(f"match recording stopped: {type(exc).__name__}: {exc}")

    def _begin(self, state, now):
        self._close()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        folder, n = self.root / stamp, 1
        while folder.exists():  # two matches begun within a second
            n += 1
            folder = self.root / f"{stamp}-{n}"
        folder.mkdir(parents=True)
        snapshot = encode(dataclasses.asdict(state))
        from .. import __version__

        header = {"at": now, "version": __version__, "state": snapshot}
        (folder / "state.json").write_text(json.dumps(header, ensure_ascii=False), encoding="utf-8")
        self.file = open(folder / "events.jsonl", "a", encoding="utf-8")
        self.folder = folder
        self._prune()

    def _close(self):
        if self.file is not None:
            self.file.close()
        self.file = self.folder = None

    def _prune(self):
        folders = sorted(p for p in self.root.iterdir() if p.is_dir())
        for old in folders[: max(0, len(folders) - self.keep)]:
            shutil.rmtree(old, ignore_errors=True)

    def frame(self, name, image):
        """Save a QImage as ``name``.jpg in this match's folder, on a worker thread."""
        with self.lock:
            folder = self.folder
        if folder is None or self.broken:
            return
        threading.Thread(
            target=lambda: image.save(str(folder / f"{name}.jpg"), "JPG", 85),
            daemon=True,
            name="pred-frame",
        ).start()

    def close(self):
        with self.lock:
            self._close()


def version(folder):
    """The app version that recorded a match, or None (recorded before 0.5.18)."""
    header = json.loads((Path(folder) / "state.json").read_text(encoding="utf-8"))
    return header.get("version")


def read(folder):
    """(state fields, [(time, event, commands)]) of a recorded match."""
    folder = Path(folder)
    header = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    lines = []
    with open(folder / "events.jsonl", encoding="utf-8") as stream:
        for raw in stream:
            if raw.strip():
                item = json.loads(raw)
                lines.append((item["t"], decode(item["e"]), decode(item.get("c", []))))
    return decode(header["state"]), lines
