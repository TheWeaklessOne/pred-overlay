"""Main hero and role: from Pred.gg through the state to the row and the TAB card."""

import dataclasses
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from predoverlay import modes
from predoverlay.errors import AuthenticationError, PredError, TransportError
from predoverlay.overlay import cards, core, lanes, paint, rows, window
from predoverlay.overlay.model import PlayerView

ME = "00000000-0000-0000-0000-000000000001"
PEER = "00000000-0000-0000-0000-000000000002"
RAW = {"ratings": [], "favHero": {"name": "Wood"}, "favRole": "CARRY"}


class Catalog:
    """Answers like Pred.gg; ``refuse`` is raised while the query asks for mains."""

    def __init__(self, refuse=None):
        self.refuse = refuse
        self.queries = []

    def query(self, document, variables=None):
        self.queries.append(document)
        if self.refuse and "favHero" in document:
            raise self.refuse
        return {"seasons": [], "p0": {"ratings": []}}


def player(main=None, **fields):
    defaults = {
        "player_id": PEER,
        "name": "Peer#1",
        "team": "enemy",
        "is_me": False,
        "state": "ready",
        "profile": {"placed": True, "division": 3, "tier": 1, "vp": 10, "level": 90, "created": 0},
        "main": main,
    }
    return PlayerView(**{**defaults, **fields})


class Source(unittest.TestCase):
    def test_query_asks_for_mains_by_default(self):
        document, _ = cards.peak_query([ME])
        self.assertIn("favHero{name} favRole", document)
        self.assertNotIn("favHero", cards.peak_query([ME], mains=False)[0])

    def test_main_of(self):
        self.assertEqual(cards.main_of(RAW), {"hero": "Wood", "role": "CARRY"})
        self.assertIsNone(cards.main_of({"favHero": None, "favRole": None}))
        self.assertIsNone(cards.main_of(None))

    def test_lane_keeps_seasons_when_mains_close(self):
        io = lanes.PredggIO()
        io.catalog = Catalog(PredError("GraphQL: Forbidden"))
        (_, (kind, _, found)) = io.handle(("peaks", 1, (ME,)))
        self.assertEqual(kind, "peaks")
        self.assertIsNone(found[ME]["main"])
        self.assertEqual(found[ME]["seasons"], ())
        self.assertEqual(len(io.catalog.queries), 2)

    def test_lane_keeps_seasons_when_the_server_refuses_mains(self):
        for refusal in (
            AuthenticationError("HTTP 403", status=403),
            TransportError("HTTP 400", status=400),
        ):
            io = lanes.PredggIO()
            io.catalog = Catalog(refusal)
            events = io.handle(("peaks", 1, (ME,)))
            self.assertEqual([event[0] for event in events], ["note", "peaks"])
            self.assertEqual(events[-1][2][ME]["seasons"], ())

    def test_lane_does_not_hide_an_outage(self):
        io = lanes.PredggIO()
        io.catalog = Catalog(TransportError("HTTP 502"))
        with self.assertRaises(TransportError):
            io.handle(("peaks", 1, (ME,)))
        self.assertEqual(len(io.catalog.queries), 1)


class Data(unittest.TestCase):
    def test_hero_shows_under_the_game_name_only(self):
        known = {"Wood": "Mourn"}
        main = {"hero": "Wood", "role": "CARRY"}
        self.assertEqual(
            core.shown_main(main, known, {"Wood": "abc"}),
            {"hero": "Mourn", "role": "CARRY", "icon": "abc"},
        )
        self.assertEqual(core.shown_main(main, {})["hero"], None)
        self.assertIsNone(core.shown_main(None, known))

    def test_row_needs_both_hero_and_a_known_role(self):
        self.assertEqual(
            rows.row(player({"hero": "Mourn", "role": "CARRY"}))["main"],
            {"hero": "MOURN", "role": "CARRY", "icon": None},
        )
        self.assertIsNone(rows.row(player({"hero": None, "role": "CARRY"}))["main"])
        self.assertIsNone(rows.row(player({"hero": "Mourn", "role": "NONE"}))["main"])
        self.assertIsNone(rows.row(player())["main"])

    def test_landing_main_is_its_own_field_change(self):
        old = rows.row(player())
        new = rows.row(player({"hero": "Mourn", "role": "CARRY"}))
        self.assertEqual(rows.changed_groups(old, new), {"main", "meta"})

    def test_this_match_against_the_main(self):
        data = {"main": {"hero": "MOURN", "role": "CARRY"}, "me": False}
        same = {"hero": "MOURN", "role": "CARRY"}
        self.assertIsNone(rows.off_main(data, same))
        self.assertEqual(rows.off_main(data, dict(same, role="SUPPORT")), "OFF ROLE")
        self.assertIsNone(rows.off_main(data, dict(same, hero="WRAITH")))
        self.assertEqual(rows.off_main(data, {"hero": "Mourn", "role": None}), None)

    def test_a_mode_without_lanes_has_no_role_to_compare(self):
        self.assertFalse(modes.has_lanes("ARAM"))
        for shown in ("RANKED", "STANDARD", "OVERCLOCKED", None):
            self.assertTrue(modes.has_lanes(shown))

    def test_no_comparison_without_data_or_on_the_own_card(self):
        seen = {"hero": "WRAITH", "role": "SUPPORT"}
        self.assertIsNone(rows.off_main({"main": None, "me": False}, seen))
        self.assertIsNone(rows.off_main({"main": {"hero": "A", "role": "CARRY"}, "me": True}, seen))
        self.assertIsNone(
            rows.off_main({"main": {"hero": "A", "role": "CARRY"}, "me": False}, None)
        )


class Painted(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])

    def pixels(self, draw):
        image = QImage(700, 800, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        draw(painter)
        painter.end()
        return image

    def test_a_main_without_art_shows_its_initials(self):
        rect = QRectF(paint.PLATE_L, 0, 600, paint.STACK_ROW_H)
        data = rows.row(player({"hero": "Mourn", "role": "CARRY"}))
        plain = self.pixels(lambda p: paint._main(p, rect, dict(data, main=None)))
        self.assertNotEqual(self.pixels(lambda p: paint._main(p, rect, data)), plain)

    def test_main_does_not_change_the_card_height(self):
        plain = rows.row(player())
        full = rows.row(player({"hero": "Iggy & Scorch", "role": "MIDLANE"}))
        self.assertEqual(paint.card_size(plain, "omeda"), paint.card_size(full, "omeda"))

    def test_long_main_drops_the_role_whole(self):
        data = {"main": {"hero": "IGGY & SCORCH", "role": "MIDLANE"}, "me": False}
        role = {"main": {"hero": "IGGY & SCORCH", "role": "SUPPORT"}, "me": False}
        narrow = paint.C_L + paint.TAB_ROLE + 180
        one = self.pixels(lambda p: paint._card_main(p, paint.TAB_ROLE, narrow, 52, data))
        two = self.pixels(lambda p: paint._card_main(p, paint.TAB_ROLE, narrow, 52, role))
        self.assertEqual(one, two)

    def test_preview_shows_mains(self):
        view = window.preview_view("loading", 1.0)
        peer = dataclasses.replace(view.enemies[0])
        self.assertIsNotNone(rows.row(peer)["main"])


if __name__ == "__main__":
    unittest.main()
