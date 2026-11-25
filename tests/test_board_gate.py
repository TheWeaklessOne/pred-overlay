"""When the window reads the TAB board: whatever the other screens gave, and afresh
every match."""

import dataclasses
import os
import queue
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None

from predoverlay.overlay import core, scoreboard

T = 10_000.0


@unittest.skipIf(QApplication is None, "PySide6 is not installed")
class BoardGate(unittest.TestCase):
    def setUp(self):
        from predoverlay.overlay import window

        self.window = window
        self.app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        self.now = [T]
        self.logged = []
        for patcher in (
            mock.patch.object(window.win32, "game_in_front", lambda: True),
            mock.patch.object(window.win32, "key_down", lambda code: False),
            mock.patch.object(window.win32, "keep_on_top", lambda hwnd: None),
            mock.patch.object(window.win32, "make_inert", lambda hwnd: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def overlay(self, view):
        self.tracker = mock.Mock(snapshot=view, tab_held=True)
        self.tracker.events = queue.Queue()
        overlay = self.window.Overlay(
            self.tracker, clock=lambda: self.now[0], log=self.logged.append
        )
        self.addCleanup(overlay.close)
        left, top, scale = overlay._frame()
        x, y = scoreboard.PANELS["enemy"][0] + 50, scoreboard.ROW_TOPS[1] + 80
        cursor = QPoint(round(left + x * scale), round(top + y * scale))
        patcher = mock.patch.object(self.window.QCursor, "pos", staticmethod(lambda: cursor))
        patcher.start()
        self.addCleanup(patcher.stop)
        return overlay

    def hold_tab(self, overlay, seconds):
        for _ in range(int(seconds / 0.05)):
            self.now[0] += 0.05
            overlay.tick()
        self.assertEqual(overlay.failures, {})

    def test_a_match_with_no_seats_still_has_its_board_read(self):
        # The app started in the middle of a match: no draft, no loading screen.
        state = core.State(game_running=True)
        for line in (
            {"phase": "loading", "at_ts": T - 600, "map": "x", "user_id": "me", "user_name": "Me"},
            {"phase": "joined", "at_ts": T - 580, "map": "x"},
            {"phase": "presence", "at_ts": T - 560},
        ):
            core.reduce(state, ("log", line), T)
        view = core.view(state)
        self.assertEqual((len(view.allies), len(view.enemies), view.mode), (0, 0, None))
        overlay = self.overlay(view)
        started = []
        overlay.reader.start = lambda match_id, crops, names, *rest, **more: started.append(
            (match_id, more.get("size"), len(crops()))
        )
        with mock.patch.object(self.window.namereader, "line", lambda part, board, h: None):
            self.hold_tab(overlay, 2)
        self.assertTrue(started)
        self.assertEqual(started[0], (view.match_id, 5, 10))  # five rows a side, both teams

    def test_a_first_look_that_missed_the_board_is_followed_by_another(self):
        # No players yet, and the first reading saw something else than the board (the
        # shop, a board still fading in): the reader must look again.
        reader = scoreboard.Reader(read=lambda png: [], clock=lambda: self.now[0])
        self.assertTrue(reader.wanted("1"))
        boxes = {(team, i): [b"x"] for team in ("ally", "enemy") for i in range(5)}
        reader.start("1", boxes, {"ally": {}, "enemy": {}}, size=5)
        while reader.busy:
            pass
        self.assertFalse(reader.saw("1"))
        self.assertFalse(reader.wanted("1"))  # not at once
        self.now[0] += scoreboard.RETRY_AFTER + 0.1
        self.assertTrue(reader.wanted("1"))

    def test_a_reading_is_pending_until_the_board_was_found_or_the_readings_are_spent(self):
        reader = scoreboard.Reader(read=lambda png: [], clock=lambda: self.now[0])
        self.assertTrue(reader.pending("1"))  # its first reading
        boxes = {(team, i): [b"x"] for team in ("ally", "enemy") for i in range(5)}
        reader.start("1", boxes, {"ally": {}, "enemy": {}}, size=5)
        while reader.busy:
            pass
        # Nobody named and the board not found: the next look is due in three seconds,
        # and the row says "reading" until then, not "could not be read".
        self.assertFalse(reader.wanted("1"))
        self.assertTrue(reader.pending("1"))
        reader.readings = scoreboard.MAX_READINGS
        self.assertFalse(reader.pending("1"))
        self.assertTrue(reader.pending("2"))  # another match starts afresh

    def test_a_board_not_found_yet_is_still_looked_for_with_everybody_named(self):
        # A normal match: every player named, and the first TAB look sees no board (the
        # death screen, the shop). The row is "reading" until the retry, not "not
        # recognised" for three seconds.
        reader = scoreboard.Reader(read=lambda png: [], clock=lambda: self.now[0])
        names = {"ally": {"a0": "SOMEBODY"}, "enemy": {"e0": "ANOTHER"}}
        boxes = {(team, i): [b"x"] for team in ("ally", "enemy") for i in range(5)}
        reader.start("1", boxes, names, size=5)
        while reader.busy:
            pass
        self.assertFalse(reader.saw("1"))
        self.assertTrue(reader.pending("1", ["a0", "e0"]))
        reader.attempts = scoreboard.ATTEMPTS  # no further look will come
        self.assertFalse(reader.pending("1", ["a0", "e0"]))

    def test_with_nobody_named_the_look_for_the_board_lasts_until_the_readings_are_spent(self):
        # The app started in the middle of a match and the board is not up for three
        # looks. Nothing is refunded with nobody to match, so the counted attempts run
        # out; the reader still looks on (wanted), and the row says so.
        reader = scoreboard.Reader(read=lambda png: [], clock=lambda: self.now[0])
        boxes = {(team, i): [b"x"] for team in ("ally", "enemy") for i in range(5)}
        for _ in range(scoreboard.ATTEMPTS + 1):
            self.now[0] += scoreboard.RETRY_AFTER + 0.1
            self.assertTrue(reader.wanted("1"))
            reader.start("1", boxes, {"ally": {}, "enemy": {}}, size=5)
            while reader.busy:
                pass
            self.assertTrue(reader.pending("1"))
        reader.readings = scoreboard.MAX_READINGS
        self.assertFalse(reader.pending("1"))
        self.assertFalse(reader.wanted("1"))

    def test_players_seated_by_a_board_just_seen_are_read_at_once(self):
        reader = scoreboard.Reader(
            read=lambda png: ["SOMENAME", "STEEL"], clock=lambda: self.now[0]
        )
        boxes = {(team, i): [b"x"] for team in ("ally", "enemy") for i in range(5)}
        reader.start("1", boxes, {"ally": {}, "enemy": {}}, size=5)
        while reader.busy:
            pass
        self.assertTrue(reader.saw("1"))
        self.assertFalse(reader.wanted("1"))  # nobody to place
        # Its rows are on their way through the core: "reading" for a moment, and not
        # for ever if the core takes none of them (a mode it does not support).
        self.assertTrue(reader.pending("1"))
        self.now[0] += scoreboard.RETRY_AFTER + 0.1
        self.assertFalse(reader.pending("1"))
        self.assertTrue(reader.wanted("1", ["a0", "e0"]))  # the board's rows got seats

    def test_a_row_without_a_hero_in_this_reading_shows_the_one_the_core_holds(self):
        # A reading taken with the overlay's own skeleton over the row loses its hero
        # line (measured on captured frames); the core keeps the first clean one.
        view = self.window.preview_view("loading", phase_at=T - 500, version=1)
        enemy = dataclasses.replace(view.enemies[1], hero="Grux")
        enemies = view.enemies[:1] + (enemy,) + view.enemies[2:]
        view = dataclasses.replace(view, started_at=T - 400, match_id="1", enemies=enemies)
        overlay = self.overlay(view)
        reader = overlay.reader
        reader.match_id = "1"
        reader.named = frozenset(p.player_id for p in view.allies + view.enemies)
        reader.wanted = lambda match_id, players=(): False
        for read, shown in ((None, "GRUX"), ("STEEL", "STEEL")):
            reader.player_at = lambda match_id, row, read=read: (
                {"player_id": enemy.player_id, "hero": read, "role": "OFFLANE"}
                if row == ("enemy", 1)
                else None
            )
            self.hold_tab(overlay, 0.5)
            self.assertEqual(overlay.card.seen["hero"], shown)

    def test_players_the_reader_had_no_names_for_wait_as_a_skeleton(self):
        # The board's own rows gave the players their seats; the reading that will place
        # them is three seconds away. Until then a row is "reading", not "could not be
        # read": that card would cover the rows and hold the reading back.
        view = self.window.preview_view("loading", phase_at=T - 500, version=1)
        view = dataclasses.replace(view, started_at=T - 400, match_id="1")
        overlay = self.overlay(view)
        reader = overlay.reader
        reader.match_id, reader.named, reader.started = "1", frozenset(), self.now[0]
        reader.clock = lambda: self.now[0]
        started = []
        reader.start = lambda *args, **more: started.append(self.now[0])
        with mock.patch.object(self.window.namereader, "line", lambda part, board, h: None):
            self.hold_tab(overlay, 1)
            self.assertIsNotNone(overlay.card)
            self.assertIsNone(overlay.card.others)  # a skeleton
            self.hold_tab(overlay, scoreboard.RETRY_AFTER)
        self.assertTrue(started)  # and the reading came

    def test_a_sixth_seat_does_not_stop_the_board_reading(self):
        view = self.window.preview_view("loading", phase_at=T - 500, version=1)
        extra = dataclasses.replace(view.allies[0], player_id="a9", read_as="MISREAD")
        view = dataclasses.replace(view, started_at=T - 400, allies=view.allies + (extra,))
        overlay = self.overlay(view)
        started = []
        overlay.reader.start = lambda *args, **more: started.append(more.get("size"))
        with mock.patch.object(self.window.namereader, "line", lambda part, board, h: None):
            self.hold_tab(overlay, 2)
        self.assertEqual(started[:1], [5])

    def test_a_reading_that_raised_does_not_stop_the_next_match(self):
        view = self.window.preview_view("loading", phase_at=T - 500, version=1)
        first = dataclasses.replace(view, started_at=T - 400, match_id="1")
        overlay = self.overlay(first)
        reader = overlay.reader
        reader.clock = lambda: self.now[0]
        fail, started = [True], []

        def read(image):
            if fail[0]:
                raise OSError("the OCR engine did not answer")
            return ["SOMENAME", "STEEL"]

        reader.read = read
        real_start = reader.start

        def start(match_id, crops, names, heroes=(), consoles=None, size=None):
            started.append(match_id)
            boxes = {(team, i): [b"x"] for team in ("ally", "enemy") for i in range(5)}
            real_start(match_id, boxes, names, heroes, consoles=consoles, size=size)
            while reader.busy:
                pass

        reader.start = start
        self.hold_tab(overlay, 5)
        self.assertEqual(started, ["1"])  # it raised: no more readings in this match
        self.assertIsNotNone(reader.failed("1"))
        fail[0] = False
        self.tracker.snapshot = dataclasses.replace(
            first, match_id="2", version=2, phase_at=self.now[0] - 300, started_at=self.now[0] - 200
        )
        self.hold_tab(overlay, 5)
        self.assertIn("2", started)
        self.assertIsNone(reader.failed("2"))
        failures = [line for line in self.logged if "scoreboard reading failed" in line]
        self.assertEqual(len(failures), 1)  # said once, for the match it happened in

    def test_the_same_rows_in_another_match_are_posted_again(self):
        view = self.window.preview_view("loading", phase_at=T - 500, version=1)
        first = dataclasses.replace(view, started_at=T - 400, match_id="1")
        overlay = self.overlay(first)
        rows = {("enemy", 0): {"text": "SOMENAME", "texts": ["SOMENAME"] * 2, "hero": None}}
        reader = overlay.reader
        reader.wanted = lambda match_id, players=(): False
        for match_id in ("1", "2"):
            reader.match_id, reader.read_rows = match_id, dict(rows)
            self.tracker.snapshot = dataclasses.replace(first, match_id=match_id, version=3)
            self.hold_tab(overlay, 0.5)
        posted = [e[1] for e in list(self.tracker.events.queue) if e[0] == "board_read"]
        self.assertEqual(posted, ["1", "2"])


if __name__ == "__main__":
    unittest.main()
