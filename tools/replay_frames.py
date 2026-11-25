"""Replay a recorded match with its captured TAB frames read again by today's readers.

A match recorded with --capture has full-screen frames of the TAB board next to its
events (recorder.py). This reads each frame as the overlay would (the board's crops,
the name model, Windows OCR for the hero line), puts the rows into the match's events
as "board_read" at the frame's time (in place of what that build recorded about the
board), and replays the core: what the roster would be with the board as a source.

    python tools/replay_frames.py <match folder> [<captures folder>] [--without-loading]

Exits with 1 when a frame whose ten rows were all read left a team short of five seats.

With --without-loading the loading screen's readings are left out, as in the match of
2026-11-21 where it went unread: the board alone must then give the missing players.

Prints counts and checks only: frames and recordings hold other players' names, which
stay on this PC. Needs Windows (the OCR) and the name model.
"""

import datetime
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

from predoverlay.config import home  # noqa: E402
from predoverlay.overlay import core, namereader, recorder, replay, scoreboard  # noqa: E402
from predoverlay.overlay.rows import shown_name  # noqa: E402
from predoverlay.overlay.window import image_bytes  # noqa: E402

DESIGN_W = 2560


def frames(captures, start, end, kind="tab"):
    """[(unix time, path)] of the captures of ``kind`` taken between start and end."""
    found = []
    for path in sorted(Path(captures).glob(f"*-{kind}-*.png")):
        stamp = datetime.datetime.strptime(path.name[:15], "%Y%m%d-%H%M%S").timestamp()
        if start <= stamp <= end:
            found.append((stamp, path))
    return found


def board_rows(path, view, model):
    """Read one TAB frame as the overlay does: {(team, i): {"text", "texts", "hero",
    "role"}} (scoreboard.Reader.read_rows)."""
    image = QImage(str(path))
    scale = image.width() / DESIGN_W
    side = max(len(view.allies), len(view.enemies), 5)
    boxes = {}
    for team in ("ally", "enemy"):
        for index in range(side):
            x, y, w, h = scoreboard.text_box(team, index)
            part = image.copy(
                round(x * scale), round(y * scale), round(w * scale), round(h * scale)
            )

            def ocr(part=part):
                big = part.scaled(
                    part.width() * 2,
                    part.height() * 2,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                return [image_bytes(big)]

            boxes[(team, index)] = scoreboard.Box(namereader.line(part, "tab", h), ocr)
    names = {
        team: {p.player_id: p.read_as or shown_name(p) or "" for p in players}
        for team, players in (("ally", view.allies), ("enemy", view.enemies))
    }
    reader = scoreboard.Reader(names=model)
    reader.match_id = "replay"
    reader._run("replay", boxes, names, view.heroes, size=side)
    return reader.read_rows


def run(folder, captures=None, without_loading=False):
    captures = captures or home() / "captures"
    fields, lines = recorder.read(folder)
    shots = frames(captures, lines[0][0], lines[-1][0]) if lines else []
    model = namereader.READER if namereader.READER.available() else None
    state = replay.start_state(fields)
    out = {"frames": len(shots), "boards": [], "differ": 0}
    pending = list(shots)
    for at, event, _ in lines:
        while pending and pending[0][0] <= at:
            stamp, path = pending.pop(0)
            view = core.view(state)
            if view.phase != "loading" or not view.started_at:
                continue  # a TAB frame outside the match
            rows = board_rows(path, view, model)
            core.reduce(state, ("board_read", f"{state.epoch}", "tab", rows), stamp)
            after = core.view(state)
            named = sum(1 for p in after.allies + after.enemies if p.read_as)
            out["roster"] = {  # the seats after the latest board: team, reading, role
                p.player_id: (p.team, p.read_as, state.seats[p.player_id]["role"])
                for p in after.allies + after.enemies
            }
            out["boards"].append((len(rows), len(after.allies), len(after.enemies), named))
        if event[0] in ("board", "board_read"):
            continue  # what that build recorded about the board: read again above
        if without_loading and event[0] == "screen" and event[1] == "loading":
            continue  # as if the loading screen had gone unread (2026-11-21)
        core.reduce(state, event, at)
    return out


if __name__ == "__main__":
    app = QGuiApplication([])
    args = [a for a in sys.argv[1:] if a != "--without-loading"]
    result = run(args[0], args[1] if len(args) > 1 else None, "--without-loading" in sys.argv)
    print(f"TAB frames in the match: {result['frames']}")
    short = 0
    for rows, allies, enemies, named in result["boards"]:
        print(f"  board read: {rows} rows -> players {allies}+{enemies}, {named} named")
        short += rows >= 2 * core.TEAM and min(allies, enemies) < core.TEAM
    if short:
        print(f"FAIL: {short} full board readings left a team short of its seats")
    sys.exit(1 if short else 0)
