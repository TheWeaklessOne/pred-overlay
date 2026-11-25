"""The panel with hero portraits: the art, the own row's day and who we met before."""

import http.client
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from predoverlay import portraits
from predoverlay.overlay import cards, core, lanes, lookup, paint, rows
from predoverlay.overlay.model import Encounter, PlayerView

ME = "00000000-0000-0000-0000-000000000001"
PEER = "00000000-0000-0000-0000-000000000002"
HOUR = 3600
WEBP = b"RIFF" + (12).to_bytes(4, "little") + b"WEBPVP8 data"


def drafted():
    """A draft that shows my name and a peer's, the peer found on Pred.gg."""
    state = core.State(me=ME, my_name="Me", display={"Wood": "Mourn"})
    core.reduce(state, ("log", {"phase": "draft", "at_ts": 1.0}), 1.0)
    texts = {("ally", 0): ["Me", "Me"], ("ally", 1): ["Peer", "Peer"]}
    texts = {**texts, ("anchor", 0): ["ALLY", "ALLY"]}
    core.reduce(state, ("screen", "draft", state.phase_at, texts), 1.0)
    core.reduce(state, ("found", state.epoch, {"a1": {"player_id": PEER, "name": "Peer"}}), 1.0)
    return state


def game_row(ended, won=True, vp=20, kda=(1, 2, 3)):
    return {"match_id": str(ended), "ended_at": ended, "won": won, "vp": vp, "kda": kda}


class Today(unittest.TestCase):
    NOW = 100 * HOUR

    def test_real_night_adds_up(self):
        ago = [0.1, 0.9, 1.5, 2.1]  # the four games of 7 November, newest first
        games = [
            game_row(self.NOW - a * HOUR, won, vp, kda)
            for a, won, vp, kda in zip(
                ago,
                (True, False, True, False),
                (24, -2, 27, -25),
                ((3, 4, 21), (4, 7, 5), (5, 0, 17), (5, 3, 1)),
            )
        ]
        self.assertEqual(
            core.today_summary(games, self.NOW),
            {"form": ("L", "W", "L", "W"), "vp": 24, "kda": 4.4},
        )

    def test_a_session_runs_past_midnight(self):
        games = [game_row(self.NOW - HOUR * a) for a in (1, 3, 5, 7, 9)]
        self.assertEqual(len(core.today_summary(games, self.NOW)["form"]), 5)

    def test_a_long_break_ends_the_day(self):
        games = [game_row(self.NOW - HOUR * a) for a in (1, 2, 9, 10)]
        self.assertEqual(len(core.today_summary(games, self.NOW)["form"]), 2)

    def test_nothing_recent_is_an_empty_day(self):
        empty = core.today_summary([game_row(self.NOW - 8 * HOUR)], self.NOW)
        self.assertEqual(empty, {"form": (), "vp": None, "kda": None})
        self.assertEqual(core.today_summary([], self.NOW)["form"], ())

    def test_every_mode_counts_and_only_ranked_brings_vp(self):
        games = [game_row(self.NOW - HOUR, vp=0), game_row(self.NOW - 2 * HOUR, vp=26)]
        summary = core.today_summary(games, self.NOW)
        self.assertEqual((len(summary["form"]), summary["vp"]), (2, 26))

    def test_a_number_left_out_is_not_a_zero(self):
        games = [
            game_row(self.NOW - HOUR, vp=None, kda=(5, None, 1)),
            game_row(self.NOW - 2 * HOUR, vp=20, kda=(4, 2, 2)),
        ]
        summary = core.today_summary(games, self.NOW)
        self.assertEqual((summary["vp"], summary["kda"]), (20, 3.0))
        alone = core.today_summary(games[:1], self.NOW)
        self.assertEqual((alone["vp"], alone["kda"]), (None, None))

    def test_it_reaches_only_the_own_row(self):
        state = drafted()
        core.reduce(state, ("recent", state.epoch, ([game_row(90.0)], [])), 100.0)
        me, peer = core.view(state).allies
        self.assertEqual((state.ids[me.player_id]["pid"], me.today["form"]), (ME, ("W",)))
        self.assertIsNone(peer.today)

    def test_another_account_forgets_the_day(self):
        state = core.State(me=ME, today=core.today_summary([game_row(90.0)], 100.0))
        core.reduce(state, ("log", {"phase": "login", "user_id": PEER, "at_ts": 0.0}), 900.0)
        self.assertIsNone(state.today)

    def test_a_first_game_keeps_the_usual_rank_cell(self):
        view = PlayerView(
            ME, "A#1", "ally", True, "ready", today={"form": (), "vp": 0, "kda": None}
        )
        self.assertIsNone(rows.row(view)["today"])


def met(*results):
    return tuple(
        Encounter(ago, "Kwang", "against", won, kda=(1, 2, 3)) for ago, won in enumerate(results, 1)
    )


class MetBefore(unittest.TestCase):
    def data(self, encounters):
        view = PlayerView(PEER, "A#1", "enemy", False, "ready", encounters=encounters)
        return rows.row(view)["encounter"]

    def test_once_says_nothing_about_a_count(self):
        self.assertIsNone(self.data(met(True))["met"])

    def test_counts_and_our_score(self):
        self.assertEqual(self.data(met(True, False, True))["met"], "×3 · 2–1")

    def test_a_result_unknown_counts_as_met_only(self):
        self.assertEqual(self.data(met(True, None))["met"], "×2 · 1–0")

    def test_the_latest_decides_the_rest(self):
        e = self.data(met(False, True))
        self.assertEqual((e["ago"], e["result"], e["kda"]), (1, "WE LOST", "1/2/3"))

    def test_encounters_carry_kda_and_art(self):
        details = {
            "winning_team": "DAWN",
            "players": [
                {"player_id": ME, "team": "DAWN"},
                {"player_id": PEER, "team": "DUSK", "hero": "Wood", "kda": [4.0, 7.0, 11.0]},
            ],
        }
        found = core.encounters_for((ME, PEER), ME, [details], {"Wood": "Mourn"}, {"Wood": "abc"})
        e = found[PEER][0]
        self.assertEqual((e.hero, e.icon, e.kda), ("Mourn", "abc", (4, 7, 11)))

    def test_portraits_arriving_late_reach_the_encounters(self):
        state = drafted()
        details = {
            "match_id": "m1",
            "winning_team": "DAWN",
            "players": [
                {"player_id": ME, "team": "DAWN"},
                {"player_id": PEER, "team": "DUSK", "hero": "Wood"},
            ],
        }
        core.reduce(state, ("recent", state.epoch, ([], [details])), 1.0)
        self.assertIsNone(state.encounters[PEER][0].icon)
        core.reduce(state, ("icons", {"Wood": "abc"}), 2.0)
        state.profiles[PEER] = {"name": "Peer"}
        (peer,) = [p for p in core.view(state).allies if state.ids[p.player_id]["pid"] == PEER]
        self.assertEqual(peer.encounters[0].icon, "abc")

    def test_a_row_comes_together_with_its_name(self):
        state = drafted()
        details = {
            "match_id": "m1",
            "winning_team": "DAWN",
            "players": [
                {"player_id": ME, "team": "DAWN"},
                {"player_id": PEER, "team": "DUSK", "hero": "Wood"},
            ],
        }
        core.reduce(state, ("recent", state.epoch, ([], [details])), 1.0)
        view = core.view(state)
        (peer,) = [p for p in view.allies if state.ids[p.player_id]["pid"] == PEER]
        (me,) = [p for p in view.allies if p.is_me]
        self.assertEqual((peer.state, peer.name, peer.encounters), ("loading", None, ()))
        self.assertEqual((me.state, me.name), ("loading", "Me"))  # the log names me
        state.profiles[PEER] = {"name": "Peer"}
        (peer,) = [p for p in core.view(state).allies if state.ids[p.player_id]["pid"] == PEER]
        self.assertEqual((peer.name, len(peer.encounters)), ("Peer", 1))

    def test_a_broken_kda_is_left_out(self):
        self.assertIsNone(core._whole_kda([1, None, 3]))
        self.assertIsNone(core._whole_kda(None))


class Back(unittest.TestCase):
    NOW = 1_800_000_000.0

    def text(self, days, me=False):
        stamp = self.NOW - days * 86400
        from datetime import datetime, timezone

        iso = datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")
        view = PlayerView(PEER, "A#1", "ally", me, "ready", last_played=iso)
        return rows.back_after(view, self.NOW)

    def test_only_a_long_break_is_worth_saying(self):
        self.assertIsNone(self.text(30))
        self.assertIsNone(self.text(89))
        self.assertEqual(self.text(100), "BACK AFTER 3 MOS")

    def test_years(self):
        self.assertEqual(self.text(400), "BACK AFTER 1 YR")
        self.assertEqual(self.text(800), "BACK AFTER 2 YRS")

    def test_never_on_the_own_row(self):
        self.assertIsNone(self.text(100, me=True))

    def test_nothing_known_says_nothing(self):
        for stamp in (None, "", "not a date"):
            view = PlayerView(PEER, "A#1", "ally", False, "ready", last_played=stamp)
            self.assertIsNone(rows.back_after(view, self.NOW))

    def test_the_query_asks_for_it_and_the_lane_keeps_it(self):
        self.assertIn("lastPlayedAt", lookup.profile_query([ME])[0])
        io = lanes.PredggIO.__new__(lanes.PredggIO)
        io.client = mock.Mock()
        io.client.query.return_value = {
            "p0": {"ratings": [], "lastPlayedAt": "2026-01-01T00:00:00Z"}
        }
        *_, (_, _, found) = io.handle(("profiles", 1, (ME,)))
        self.assertEqual(found[ME]["last_played"], "2026-01-01T00:00:00Z")


class Store(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name) / "portraits"
        self.calls = []

    def io(self, reply=WEBP):
        def download(icon):
            self.calls.append(icon)
            if isinstance(reply, Exception):
                raise reply
            return reply

        return portraits.PortraitIO(self.folder, download, sleep=lambda _: None)

    def test_a_portrait_lands_on_disk(self):
        events = self.io().handle(("portraits", ("abc",)))
        self.assertEqual(events, [("portraits", 1, ())])
        self.assertEqual(portraits.path("abc", self.folder).read_bytes(), WEBP)

    def test_a_second_start_downloads_nothing(self):
        self.io().handle(("portraits", ("abc",)))
        self.calls.clear()
        self.assertEqual(self.io().handle(("portraits", ("abc",))), [("portraits", 0, ())])
        self.assertEqual(self.calls, [])

    def test_a_page_instead_of_a_picture_is_not_kept(self):
        events = self.io(b"<html>nope</html>").handle(("portraits", ("abc",)))
        self.assertEqual(events[-1], ("portraits", 0, ("abc",)))
        self.assertFalse(portraits.path("abc", self.folder).exists())

    def test_a_cut_off_file_is_not_kept(self):
        whole = b"RIFF" + (5000).to_bytes(4, "little") + b"WEBP" + b"0" * 5000
        self.assertTrue(portraits.is_webp(whole))
        self.assertFalse(portraits.is_webp(whole[:4500]))
        self.assertEqual(self.io(whole[:4500]).handle(("portraits", ("abc",)))[-1][2], ("abc",))

    def test_a_broken_connection_costs_only_that_portrait(self):
        calls = []

        def download(icon):
            calls.append(icon)
            if icon == "bad":
                raise http.client.IncompleteRead(b"")
            return WEBP

        io = portraits.PortraitIO(self.folder, download, sleep=lambda _: None)
        self.assertEqual(io.handle(("portraits", ("bad", "good")))[-1], ("portraits", 1, ("bad",)))

    def test_a_huge_answer_is_not_kept(self):
        body = WEBP + b"0" * portraits.MAX_BYTES
        self.assertEqual(self.io(body).handle(("portraits", ("abc",)))[-1][2], ("abc",))

    def test_a_failed_one_does_not_stop_the_rest(self):
        replies = {"bad": OSError("offline"), "good": WEBP}

        def download(icon):
            if isinstance(replies[icon], Exception):
                raise replies[icon]
            return replies[icon]

        io = portraits.PortraitIO(self.folder, download, sleep=lambda _: None)
        note, done = io.handle(("portraits", ("bad", "good")))
        self.assertEqual(done, ("portraits", 1, ("bad",)))
        self.assertIn("1 portraits", note[1])

    def test_a_strange_name_never_reaches_the_disk(self):
        self.assertEqual(self.io().handle(("portraits", ("../evil",))), [("portraits", 0, ())])
        self.assertEqual(self.calls, [])

    def test_icons_of_the_catalog(self):
        heroes = [
            {"name": "Wood", "data": {"displayName": "Mourn", "icon": "abc"}},
            {"name": "New", "data": {"displayName": "New"}},
        ]
        self.assertEqual(cards.hero_icons(heroes), {"Wood": "abc"})


class Downloads(unittest.TestCase):
    def test_all_heroes_are_fetched_once_they_are_known(self):
        state = core.State()
        commands = core.reduce(state, ("icons", {"A": "x", "B": "y", "C": "x"}), 0.0)
        self.assertEqual(commands, [("portraits", ("x", "y"))])

    def test_failures_are_tried_again_later_and_then_left_alone(self):
        state = core.State(display={"a": "A"})
        core.reduce(state, ("icons", {"A": "x"}), 0.0)
        now = 0.0
        sent = 1
        for _ in range(6):
            core.reduce(state, ("portraits", 0, ("x",)), now)
            self.assertNotIn(("portraits", ("x",)), core.reduce(state, ("tick",), now + 1))
            now += core.PORTRAIT_RETRY + 2
            sent += core.reduce(state, ("tick",), now).count(("portraits", ("x",)))
        self.assertEqual(sent, core.PORTRAIT_TRIES)

    def test_new_art_reaches_the_panel(self):
        state = core.State()
        core.reduce(state, ("portraits", 2, ()), 0.0)
        self.assertEqual(core.view(state).art, 1)
        core.reduce(state, ("portraits", 0, ()), 1.0)
        self.assertEqual(core.view(state).art, 1)

    def test_the_catalog_gives_names_and_art_together(self):
        io = lanes.PredggIO.__new__(lanes.PredggIO)
        io.public = None
        heroes = [{"name": "Wood", "data": {"displayName": "Mourn", "icon": "abc"}}]
        with mock.patch.object(lanes, "CatalogClient") as client:
            client.return_value.heroes.return_value = heroes
            self.assertEqual(
                io.handle(("display",)),
                [("reach", True), ("display", {"Wood": "Mourn"}), ("icons", {"Wood": "abc"})],
            )


class Painted(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test"])

    def row(self, **fields):
        view = PlayerView(
            PEER,
            "Somebody#1",
            "enemy",
            False,
            "ready",
            profile={
                "current": {"rank": "Gold III", "points": 640.0},
                "first_played": "2024-03-01T00:00:00Z",
            },
            main={"hero": "Mourn", "role": "CARRY", "icon": None},
        )
        data = dict(rows.row(view), **fields)
        image = QImage(640, 44, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        rect = QRectF(paint.PLATE_L, 0, paint.PLATE_R - paint.PLATE_L, paint.STACK_ROW_H)
        paint.stack_row(painter, rect, data, paint.Motion())
        painter.end()
        return image

    def test_the_own_day_replaces_the_rank_cell(self):
        today = {"form": ("L", "W"), "vp": 24, "kda": 4.4}
        self.assertNotEqual(self.row(me=True, today=today), self.row(me=True))

    def test_a_day_of_only_losses_draws(self):
        self.row(me=True, today={"form": ("L",) * 30, "vp": -300, "kda": 0.0})

    def test_a_row_draws_with_every_encounter_shape(self):
        full = {
            "hero": "IGGY & SCORCH",
            "icon": None,
            "when": "12 MATCHES AGO",
            "ago": 12,
            "side": "with",
            "result": "WE LOST",
            "kda": "12/13/14",
            "met": "×12 · 8–4",
        }
        empty = dict(full, hero=None, result=None, kda=None, met=None, side="against")
        self.assertNotEqual(self.row(encounter=full), self.row())
        self.assertNotEqual(self.row(encounter=empty), self.row())

    def test_every_row_shows_its_age(self):
        self.assertNotEqual(self.row(), self.row(age=None))

    def test_the_own_row_has_none(self):
        self.assertEqual(self.row(me=True), self.row(me=True, age=None))

    def test_a_long_break_is_shown(self):
        self.assertNotEqual(self.row(back="BACK AFTER 3 MOS"), self.row())

    def test_new_art_ends_the_wait_for_a_missing_file(self):
        with mock.patch.object(paint.portraits, "_ABSENT", {"abc": float("inf")}):
            paint.look_for_art()
            self.assertEqual(paint.portraits._ABSENT, {})

    def test_the_card_keeps_its_you_chip_and_the_panel_needs_none(self):
        self.assertEqual(paint._flag_of({"me": True, "new": False}), ("YOU", "me"))
        self.assertNotEqual(self.row(me=True, new=True), self.row(me=True))

    def test_the_card_says_how_many_matches_the_player_has_in_all(self):
        view = PlayerView(
            PEER,
            "Somebody#1",
            "enemy",
            False,
            "ready",
            profile={
                "current": {"rank": "Gold III", "points": 640.0},
                "first_played": "2024-03-01T00:00:00Z",
            },
            main={"hero": "Khaimera", "role": "JUNGLE", "icon": None},
        )

        def head(total):
            stats = {"roles": {}, "heroes": {}, "form": [], "recent": [], "played": [], "mates": {}}
            data = dict(rows.row(view), stats=dict(stats, total=total))
            image = QImage(600, 70, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            painter = QPainter(image)
            painter.setClipRect(0, 0, 600, 70)  # the head only
            paint.paint_card(painter, data, paint.Motion(), chrome=False)
            painter.end()
            return image

        self.assertNotEqual(head(1148), head(0))  # Pred.gg's count, when it has one
        self.assertNotEqual(head(1148), head(23))

    def test_the_glint_runs_over_the_peak_in_the_rank_cell(self):
        peak = {"rank": "Paragon", "tier": "paragon", "season": "S"}
        data = {"peak": peak}
        image = QImage(640, 44, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        paint._glint(painter, 0, data, paint.GLINT / 2)
        painter.end()
        lit = [x for x in range(640) if any(image.pixelColor(x, y).alpha() for y in range(44))]
        self.assertGreaterEqual(min(lit), paint.COL_RANK)
        self.assertLessEqual(max(lit), paint.COL_RANK_R)

    def test_a_missing_file_is_looked_for_again(self):
        art = paint.portraits  # the cache lives there: patch it, not the re-export
        with (
            mock.patch.object(art, "_ABSENT", {}),
            mock.patch.object(art, "_SOURCES", {}),
            mock.patch.object(art, "portrait_path", return_value="nothing.webp"),
            mock.patch.object(art.time, "monotonic", return_value=10.0),
        ):
            self.assertIsNone(art._source("abc"))
            self.assertIn("abc", art._ABSENT)
            with mock.patch.object(art, "QImage") as image:
                self.assertIsNone(art._source("abc"))  # not on every frame
                image.assert_not_called()
            with (
                mock.patch.object(art.time, "monotonic", return_value=99.0),
                mock.patch.object(art, "QImage") as image,
            ):
                image.return_value.isNull.return_value = False
                self.assertIsNotNone(art._source("abc"))


if __name__ == "__main__":
    unittest.main()
