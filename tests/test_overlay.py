"""Overlay v2 logic with scripted events and fakes (no network or game)."""

import dataclasses
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from predoverlay.errors import AuthenticationError
from predoverlay.overlay import core, lanes, phases
from predoverlay.overlay.model import Failure
from predoverlay.overlay.rows import row
from predoverlay.overlay.tracker import ROUTES, Tracker
from predoverlay.overlay.visibility import LOADING_SCREEN, Visibility, auto_visible

try:  # PySide6 is an optional extra; the pure tests must run without it.
    from predoverlay.overlay.window import choose_layout, preview_view
except ImportError:  # pragma: no cover - depends on the environment
    choose_layout = preview_view = None

ME = "00000000-0000-0000-0000-0000000000aa"
OTHERS = [f"00000000-0000-0000-0000-00000000000{i}" for i in range(9)]
GAME_A = "10000000-0000-0000-0000-000000000001"
GAME_B = "10000000-0000-0000-0000-000000000002"
NAMES = ("Me", "Glimrox", "Lieutenant", "Pathcrosser", "Tarvel17")  # the draft, top to bottom
NET = Failure("network", "TransportError: Cannot reach API")


def phase(name, **extra):
    return ("log", {"phase": name, "at_ts": 100.0, **extra})


def iso(epoch):
    """A unix time as Pred.gg writes it."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def screen(state, names=NAMES):
    """A draft reading: every name in two versions that agree."""
    texts = {("ally", i): [name, name] for i, name in enumerate(names)}
    texts = {**texts, ("anchor", 0): ["ALLY", "ALLY"]}
    return ("screen", "draft", state.phase_at, texts)


def match(match_id, *players):
    """A finished match as lookup.match_details shapes it."""
    return {
        "match_id": match_id,
        "ended_at": 50.0,
        "winning_team": "DAWN",
        "players": [{"player_id": pid, "team": team} for pid, team in players],
    }


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.state = core.State(me=ME, my_name="Me", display={"Wood": "Mourn"})

    def feed(self, *events, now=200.0):
        out = []
        for event in events:
            out += core.reduce(self.state, event, now)
        return out

    def draft(self, now=200.0):
        """Into the draft with my team read off the screen."""
        self.feed(phase("draft"), now=now)
        return self.feed(screen(self.state), now=now)

    def test_the_match_start_ends_the_loading_screen(self):
        self.feed(phase("loading"))
        self.feed(phase("presence", at_ts=150.0))  # before the map is in: not the start
        self.assertEqual(self.state.started_at, 0.0)
        self.feed(phase("joined", at_ts=160.0), phase("presence", at_ts=170.0))
        self.assertEqual(core.view(self.state).started_at, 170.0)
        self.feed(phase("presence", at_ts=900.0))  # later updates change nothing
        self.assertEqual(self.state.started_at, 170.0)
        self.feed(phase("draft"))
        self.assertEqual(self.state.started_at, 0.0)

    def test_a_refused_read_asks_for_sign_in_until_one_arrives(self):
        self.feed(("auth", False))
        view = core.view(self.state)
        self.assertEqual((view.health, view.status), ("login", core.NEEDS_LOGIN))
        self.feed(("reach", False))
        self.assertEqual(core.view(self.state).health, "login")  # sign-in outranks offline
        self.feed(("sign_in",))
        self.assertEqual(core.view(self.state).health, "ok")

    def test_nothing_is_looked_up_until_the_sign_in(self):
        self.feed(("auth", False))
        self.assertEqual(self.draft(), [])
        sent = self.feed(("sign_in",))
        (search,) = [c for c in sent if c[0] == "search"]
        self.assertEqual(set(search[2]), {"a1", "a2", "a3", "a4"})  # my own row needs none
        self.assertIn(("recent", self.state.epoch, ME), sent)

    def test_names_given_up_on_are_asked_again_after_a_sign_in(self):
        self.draft()
        key, now = ("search", self.state.epoch), 200.0
        for _ in core.CARD_RETRY:
            self.feed(("found", self.state.epoch, None), ("reach", False), now=now)
            now = self.state.failures[key][1]
            self.feed(("reach", True), ("tick",), now=now)
        self.feed(("found", self.state.epoch, None), now=now)  # the retry that just went out
        self.assertNotIn("search", [c[0] for c in self.feed(("tick",), now=now + 1)])
        others = [p for p in core.view(self.state).allies if not p.is_me]
        self.assertEqual({p.state for p in others}, {"error"})  # shown without data
        sent = self.feed(("sign_in",), now=now + 2)  # at once, not after the backoff
        self.assertIn("search", [c[0] for c in sent])

    def test_offline_clears_on_the_next_good_read(self):
        self.draft()
        self.feed(("reach", False))
        view = core.view(self.state)
        self.assertEqual((view.health, view.status), ("offline", core.OFFLINE))
        self.feed(("reach", True))
        view = core.view(self.state)
        self.assertEqual((view.health, view.status), ("ok", core.STATUS["draft"]))

    def test_a_finished_match_returns_to_the_menu(self):
        self.draft()
        self.feed(phase("finished", reason="REASON_FINISHED", match_id=GAME_A))
        self.assertEqual((self.state.phase, self.state.last_finished), ("menu", GAME_A))
        self.assertEqual(core.view(self.state).allies, ())

    def test_menu_log_line_resets_the_match(self):
        self.draft()
        epoch = self.state.epoch
        self.assertEqual(len(core.view(self.state).allies), 5)
        self.assertEqual(self.feed(phase("menu")), [])
        self.assertEqual((self.state.phase, core.view(self.state).allies), ("menu", ()))
        self.assertGreater(self.state.epoch, epoch)

    def test_results_from_an_older_epoch_are_dropped(self):
        self.draft()
        old = self.state.epoch
        self.feed(phase("menu"), phase("draft"))
        self.feed(("profiles", old, {ME: {"current": {"rank": "Gold II", "points": 750}}}))
        self.assertEqual(self.state.profiles, {})

    def test_an_accept_that_never_reaches_a_lobby_returns_to_the_menu(self):
        self.feed(phase("accepted"), now=100.0)  # at_ts 100
        self.feed(("tick",), now=100.0 + core.ACCEPT_TIMEOUT - 1)
        self.assertEqual(self.state.phase, "accepted")
        self.feed(("tick",), now=100.0 + core.ACCEPT_TIMEOUT + 1)
        self.assertEqual(self.state.phase, "menu")

    def test_a_second_accept_is_another_match(self):
        self.feed(phase("accepted"), now=100.0)
        epoch = self.state.epoch
        self.feed(phase("accepted"), now=140.0)
        self.assertEqual((self.state.phase, self.state.epoch), ("accepted", epoch + 1))

    def test_the_lanes_are_pred_gg_and_its_art_only(self):
        self.assertEqual(
            set(ROUTES),
            {
                "display",
                "search",
                "profiles",
                "recent",
                "match",
                "remember",
                "pending",
                "portraits",
            },
        )
        self.assertEqual(set(ROUTES.values()), {"predgg", "assets"})

    def test_ticks_ask_for_nothing_but_lane_reads(self):
        kinds = set()
        for moment in range(0, 4 * 3600, 30):
            kinds |= {c[0] for c in self.feed(("tick",), now=float(moment))}
        self.assertLessEqual(kinds, set(ROUTES))

    def test_an_empty_hero_catalog_backs_off(self):
        state = core.State()
        self.assertEqual(core.reduce(state, ("tick",), 0.0), [("display",)])
        core.reduce(state, ("display", {}), 1.0)
        self.assertNotIn(("display",), core.reduce(state, ("tick",), 2.0))
        self.assertIn(("display",), core.reduce(state, ("tick",), 1.0 + core.CARD_RETRY[0]))

    def test_hero_names_are_retried_with_a_backoff(self):
        state = core.State()
        core.reduce(state, ("tick",), 0.0)
        retries, now = [], 1.0
        for _ in range(5):
            core.reduce(state, ("display", None), now)
            retries.append(state.failures[("display",)][1] - now)
            now = state.failures[("display",)][1]
            self.assertIn(("display",), core.reduce(state, ("tick",), now))
        self.assertEqual(retries, [*core.CARD_RETRY, core.CARD_RETRY[-1]])  # never gives up

    def test_a_dropped_result_backs_off_like_a_failure(self):
        self.draft()
        epoch = self.state.epoch
        for event, key in (
            (("found", epoch, {}), ("search", epoch)),
            (("profiles", epoch, {}), ("profiles", epoch)),
            (("recent", epoch, ([], [])), ("recent", epoch)),
            (("display", {}), ("display",)),
        ):
            with self.subTest(kind=event[0]):
                self.state.inflight.add(key)
                core.drop_result(self.state, event, 300.0)
                self.assertNotIn(key, self.state.inflight)
                self.assertEqual(self.state.failures[key], (1, 300.0 + core.CARD_RETRY[0]))
        before = dict(self.state.failures)
        core.drop_result(self.state, ("icons", {"Wood": "abc"}), 300.0)
        self.assertEqual(self.state.failures, before)  # not a lookup

    def test_a_match_that_just_ended_is_asked_for_until_pred_gg_lists_it(self):
        self.feed(phase("finished", reason="REASON_FINISHED", match_id=GAME_A))
        epoch = self.state.epoch + 1
        self.assertIn(("recent", epoch, ME), self.feed(phase("draft"), now=300.0))
        older = match(GAME_B, (ME, "DAWN"))
        self.feed(("recent", epoch, ([], [older])), now=300.0)
        self.assertEqual(self.feed(("tick",), now=301.0), [])
        again = self.feed(("tick",), now=300.0 + core.CARD_RETRY[0])
        self.assertIn(("recent", epoch, ME), again)
        listed = match(GAME_A, (ME, "DUSK"))
        self.feed(("recent", epoch, ([], [listed, older])), now=320.0)
        self.assertEqual(self.feed(("tick",), now=1000.0), [])

    def test_an_unrecorded_previous_match_keeps_the_numbering(self):
        foe = OTHERS[0]
        older = match(GAME_B, (ME, "DAWN"), (foe, "DUSK"))
        (encounter,) = core.encounters_for((ME, foe), ME, [None, older])[foe]
        self.assertEqual(encounter.matches_ago, 2)

    def test_the_account_the_log_names_is_the_one_looked_up(self):
        second = "00000000-0000-0000-0000-0000000000cc"
        sent = self.feed(phase("draft", user_id=second, user_name="Second"))
        self.assertEqual((self.state.me, self.state.my_name), (second, "Second"))
        self.assertIn(("recent", self.state.epoch, second), sent)


class ProfileRowTests(unittest.TestCase):
    PROFILE = {"current": {"rank": "Gold II", "points": 745.0}, "first_played": iso(0)}

    def test_negative_points_count_as_the_bottom_of_the_step(self):
        from predoverlay.overlay.rows import rank_points

        low = {"current": {"rank": "Gold II", "points": -25.0}}
        self.assertEqual(rank_points(low), rank_points({"current": {"rank": "Gold II"}}))
        self.assertEqual(rank_points(low), 700)

    def test_the_rank_shows_once_the_profile_is_in(self):
        state = core.State(me=ME, my_name="Me", display={"x": "y"})
        core.reduce(state, phase("draft"), 200.0)
        commands = core.reduce(state, screen(state, NAMES[:1]), 200.0)
        self.assertIn(("profiles", state.epoch, (ME,)), commands)
        (me,) = core.view(state).allies
        self.assertEqual(row(me)["rank"], "LOADING")
        core.reduce(state, ("profiles", state.epoch, {ME: self.PROFILE}), 201.0)
        (me,) = core.view(state).allies
        self.assertEqual((me.state, row(me)["rank"]), ("ready", "Gold II"))

    def test_rows_carry_no_statistics_of_their_own(self):
        # Pred.gg's totals ride along for the TAB card ("stats", None here); nothing the
        # overlay would count itself from a few matches.
        view = core.PlayerView(OTHERS[0], "X#1", "ally", False, "ready", self.PROFILE)
        data = row(view, now=10 * 86400)
        self.assertFalse({"wr", "kda", "games"} & data.keys())
        self.assertIsNone(data["stats"])
        self.assertEqual((data["vp"], data["age"], data["new"]), (45, "10 DAYS", True))

    def test_rows_show_the_name_the_game_shows(self):
        view = core.PlayerView(OTHERS[0], "Alias248#1003", "enemy", False, "ready", self.PROFILE)
        self.assertEqual(row(view)["name"], "Alias248")
        console = dict(self.PROFILE, console=True)  # a console name is shown as it is
        view = core.PlayerView(OTHERS[0], "Echo248", "enemy", False, "ready", console)
        self.assertEqual(row(view)["name"], "Echo248")

    def test_rank_names_age_and_team_average(self):
        from predoverlay.overlay.rows import account_age, current_rank, team_rank

        self.assertEqual(current_rank(self.PROFILE), "Gold II")
        self.assertIsNone(current_rank({"current": {"rank": None, "points": None}}))
        self.assertIsNone(current_rank({"current": None}))
        day = 86400
        first = {"first_played": iso(0)}
        self.assertEqual(account_age(first, now=3 * 365.25 * day)[1], "3.0 YRS")
        self.assertEqual(account_age(first, now=(2 * 365 + 335) * day)[1], "2.9 YRS")
        self.assertEqual(account_age(first, now=(365 + 10) * day)[1], "1 YR")
        self.assertEqual(account_age(first, now=200 * day)[1], "6 MOS")
        self.assertEqual(account_age(first, now=35 * day)[1], "5 WKS")
        self.assertEqual(account_age({"first_played": None}), (None, None))
        gold = [{"points": p} for p in (700, 800, 900)]  # Gold II at 0, 100, 200 VP over III
        self.assertEqual(team_rank(gold), "Gold I")
        unplaced = [{"points": None}] * 3
        self.assertIsNone(team_rank(gold[:2] + unplaced))  # two of five placed: no average
        self.assertEqual(team_rank(gold[:2]), "Gold II")  # a 2v2 team, both placed


class EncounterTests(unittest.TestCase):
    def test_previous_matches_mark_side_hero_and_result(self):
        friend, foe = OTHERS[0], OTHERS[1]
        last = {
            "winning_team": "DAWN",
            "players": [
                {"player_id": ME, "team": "DAWN", "hero": "Wood"},
                {"player_id": friend, "team": "DAWN", "hero": "DemonKing"},
                {"player_id": foe, "team": "DUSK", "hero": "Riktor"},
            ],
        }
        older = {
            "winning_team": "DAWN",
            "players": [
                {"player_id": ME, "team": "DUSK", "hero": "Narbash"},
                {"player_id": foe, "team": "DUSK", "hero": "Murdock"},
            ],
        }
        found = core.encounters_for(
            (ME, friend, foe), ME, [last, older], {"DemonKing": "Akeron", "Wood": "Mourn"}
        )
        self.assertNotIn(ME, found)
        (only,) = found[friend]
        self.assertEqual(
            (only.matches_ago, only.hero, only.side, only.won), (1, "Akeron", "with", True)
        )
        first, second = found[foe]
        self.assertEqual((first.side, first.won), ("against", True))
        self.assertEqual(
            (second.matches_ago, second.hero, second.side, second.won),
            (2, "Murdock", "with", False),
        )


class MatchEndTests(unittest.TestCase):
    def state(self):
        return core.State(me=ME, phase="loading", display={"x": "y"})

    def feed(self, state, kind, at):
        core.reduce(state, ("log", {"phase": kind, "at_ts": at}), at)

    def test_the_second_presence_change_ends_the_match(self):
        state = self.state()
        self.feed(state, "joined", 100.0)
        self.feed(state, "presence", 130.0)
        self.assertEqual((state.started_at, state.ended_at), (130.0, 0.0))
        self.feed(state, "presence", 140.0)  # still the start
        self.assertEqual(state.ended_at, 0.0)
        self.feed(state, "presence", 2000.0)
        self.assertEqual(core.view(state).ended_at, 2000.0)
        self.feed(state, "presence", 2001.0)  # the game sends it twice
        self.assertEqual(state.ended_at, 2000.0)
        core.reduce(state, ("log", {"phase": "finished", "at_ts": 2050.0}), 2050.0)
        self.assertEqual(core.view(state).ended_at, 0.0)


class VisibilityTests(unittest.TestCase):
    def test_automatic_rules(self):
        self.assertTrue(auto_visible("draft", 0, False))
        self.assertTrue(auto_visible("loading", 10, False))
        self.assertFalse(auto_visible("loading", LOADING_SCREEN + 1, False))
        self.assertFalse(auto_visible("loading", LOADING_SCREEN + 1, True))  # TAB: card only
        self.assertTrue(auto_visible("loading", 30, False, started_age=0.5))
        self.assertFalse(auto_visible("loading", 30, False, started_age=3))
        self.assertTrue(auto_visible("menu", 0, False))  # the menu badge
        self.assertFalse(auto_visible("finished", 0, True))

    def test_f8_flips_until_the_phase_changes_and_needs_the_game_in_front(self):
        rule = Visibility()
        args = {"phase_age": 500, "tab_held": False}
        self.assertFalse(rule.update(phase="loading", f8_down=False, foreground=True, **args))
        self.assertTrue(rule.update(phase="loading", f8_down=True, foreground=True, **args))
        self.assertTrue(rule.update(phase="loading", f8_down=False, foreground=True, **args))
        self.assertFalse(rule.update(phase="loading", f8_down=False, foreground=False, **args))
        self.assertTrue(rule.update(phase="draft", f8_down=False, foreground=True, **args))
        self.assertFalse(rule.update(phase="draft", f8_down=True, foreground=True, **args))

    def test_a_pin_survives_tab(self):
        rule = Visibility()
        args = {"phase": "loading", "phase_age": 600, "foreground": True}
        rule.update(tab_held=False, f8_down=False, **args)
        self.assertTrue(rule.update(tab_held=False, f8_down=True, **args))
        self.assertTrue(rule.update(tab_held=True, f8_down=False, **args))
        self.assertTrue(rule.update(tab_held=False, f8_down=False, **args))


class PhaseLogTests(unittest.TestCase):
    def test_returns_to_the_menu_without_a_match_id(self):
        at = "[2026.10.29-00.06.33:588][  0]LogNet: Browse: "
        for tail in (
            "/Game/Voyager/Voyager?closed",
            "/Game/Voyager/Voyager",
            "/Game/Maps/OTA?Name=Player",
        ):
            with self.subTest(tail=tail):
                self.assertEqual(phases.parse(at + tail + "\r")["phase"], "menu")
        draft = at + "x/Game/Voyager/Voyager?userId=" + ME + "?userName=Me"
        self.assertEqual(phases.parse(draft)["phase"], "draft")
        finished = at + "/Game/Voyager/Voyager?closed=REASON_FINISHED?gameInstanceId=" + GAME_A
        self.assertEqual(phases.parse(finished)["phase"], "finished")


class LaneTests(unittest.TestCase):
    def test_failures_log_and_signal_a_retry(self):
        problem = Failure("other", "PredError: boom")
        self.assertEqual(
            lanes.failure(("search", 3, {}), problem),
            [("lane_error", "search", problem), ("found", 3, None)],
        )
        self.assertEqual(lanes.failure(("profiles", 3, ()), problem)[1], ("profiles", 3, None))
        self.assertEqual(lanes.failure(("recent", 3, ME), problem)[1], ("recent", 3, None))
        self.assertEqual(lanes.failure(("display",), problem)[1], ("display", None))
        self.assertEqual(
            lanes.failure(("portraits", ("x",)), problem), [("lane_error", "portraits", problem)]
        )

    def test_a_failure_also_reports_the_session_or_the_reach(self):
        refused = Failure("auth", "AuthenticationError: HTTP 401")
        self.assertEqual(lanes.failure(("search", 1, {}), refused)[-1], ("auth", False))
        self.assertEqual(lanes.failure(("search", 1, {}), NET)[-1], ("reach", False))

    def test_failures_are_sorted_by_kind(self):
        from predoverlay.auth import AuthError
        from predoverlay.errors import PayloadError, PredError, TransportError

        cases = [
            (AuthenticationError("HTTP 401"), "auth"),
            (AuthError("Could not safely read the session file."), "auth"),
            (TransportError("Cannot reach API: timed out"), "network"),
            (TransportError("HTTP 503", status=503), "network"),
            (TransportError("HTTP 429", status=429), "network"),
            (TransportError("HTTP 404", status=404), "other"),
            (TimeoutError("timed out"), "network"),
            (PayloadError("payload must be an object"), "payload"),
            (KeyError("players"), "payload"),
            (PredError("GraphQL: service error"), "other"),
        ]
        for exc, kind in cases:
            with self.subTest(exc=exc):
                self.assertEqual(lanes.failure_kind(exc), kind)

    def test_a_lane_reports_a_structured_failure(self):
        out = []

        def handler(command):
            lane.stop.set()
            raise AuthenticationError("HTTP 401: authentication missing")

        lane = lanes.Lane("test", handler, mock.Mock(put=out.append))
        lane.submit(("search", 4, {}))
        lane.run()
        (_, kind, problem), retry, session = out
        self.assertEqual((kind, problem.kind), ("search", "auth"))
        self.assertEqual((retry, session), (("found", 4, None), ("auth", False)))
        self.assertEqual(str(problem), "AuthenticationError: HTTP 401: authentication missing")

    def test_every_spelling_goes_in_one_request_and_the_first_name_wins(self):
        io = lanes.PredggIO.__new__(lanes.PredggIO)
        io.client = mock.Mock()
        io.client.query.return_value = {
            "s0": {"results": []},  # the draft's spelling: nobody
            "s1": {"results": [{"id": "a", "name": "Kv.Dv.", "lastPlayedAt": "2026-11-01"}]},
            "s2": {"results": [{"id": "b", "name": "KVV", "lastPlayedAt": "2026-11-01"}]},
            "s3": {"results": []},
        }
        slots = {"a1": ("KVDV", "KV.DV.", "KVV"), "a2": ("Nobody",)}
        events = io.handle(("search", 5, slots))
        self.assertEqual(events[:2], [("auth", True), ("reach", True)])
        self.assertEqual(
            events[2], ("found", 5, {"a1": {"player_id": "a", "name": "Kv.Dv."}, "a2": None})
        )
        (document, variables), _ = io.client.query.call_args
        self.assertEqual(variables, {"n0": "KVDV", "n1": "KV.DV.", "n2": "KVV", "n3": "Nobody"})


class FakeLane:
    def __init__(self):
        self.sent, self.dropped, self.out = [], 0, None

    def submit(self, command):
        self.sent.append(command)

    def discard_pending(self):
        self.dropped += 1

    def is_alive(self):
        return True


class TrackerTests(unittest.TestCase):
    def tracker(self, clock, lanes_=None):
        messages = []
        tracker = Tracker(lanes_ or {}, log=messages.append, clock=lambda: clock[0])
        return tracker, messages

    def test_survives_a_failing_log_follower(self):
        follower = mock.Mock()
        follower.replay.return_value = []
        follower.poll.side_effect = PermissionError("log is being rotated")
        messages = []
        tracker = Tracker(
            {}, follower=follower, log=messages.append, clock=lambda: 0.0, game_running=lambda: True
        )
        tracker.step()
        tracker.step()  # still ticking
        failures = [m for m in messages if m.startswith("tracker:")]
        self.assertEqual(len(failures), 1)  # a repeating failure is logged once
        self.assertIn("PermissionError", failures[0])

    def test_commands_go_to_their_lanes(self):
        lanes_ = {"predgg": FakeLane(), "assets": FakeLane()}
        tracker, _ = self.tracker([10.0], lanes_)
        tracker.handle(("game", True))
        tracker.handle(("icons", {"Wood": "abc"}))
        self.assertEqual(lanes_["predgg"].sent, [("display",)])
        self.assertEqual(lanes_["assets"].sent, [("portraits", ("abc",))])

    def test_the_session_line_is_logged_once_per_change(self):
        tracker, messages = self.tracker([1.0])
        tracker.handle(("tick",))
        tracker.handle(("auth", False))
        tracker.handle(("auth", False))
        tracker.handle(("sign_in",))
        lines = [m for m in messages if m.startswith("session:")]
        self.assertEqual(lines, ["session: ok", "session: sign-in needed", "session: ok"])

    def test_a_repeating_failure_is_logged_once(self):
        tracker, messages = self.tracker([1.0])
        problem = Failure("payload", "PayloadError: payload must be an object")
        for _ in range(5):
            tracker.handle(("lane_error", "profiles", problem))
        self.assertEqual(sum("profiles failed" in m for m in messages), 1)
        tracker.handle(("profiles", 0, {}))  # it worked once: the next failure is news
        tracker.handle(("lane_error", "profiles", problem))
        self.assertEqual(sum("profiles failed" in m for m in messages), 2)

    def test_a_failed_answer_does_not_end_the_repeat(self):
        tracker, messages = self.tracker([1.0])
        problem = Failure("payload", "PayloadError: payload must be an object")
        tracker.handle(("lane_error", "search", problem))
        tracker.handle(("found", 0, None))  # the retry signal of that same failure
        tracker.handle(("lane_error", "search", problem))
        self.assertEqual(sum("search failed" in m for m in messages), 1)

    def test_an_outage_is_logged_as_a_pair(self):
        clock = [1000.0]
        tracker, messages = self.tracker(clock)
        for _ in range(42):
            tracker.handle(("lane_error", "search", NET))
            clock[0] += 4.3
        tracker.handle(("found", 0, {}))
        outage = [m for m in messages if m.startswith("Pred.gg")]
        self.assertEqual(len(outage), 2)
        self.assertRegex(outage[0], r"^Pred\.gg offline since \d\d:\d\d \(search: Transp")
        self.assertEqual(outage[1], "Pred.gg back after 3m, 42 failures")
        self.assertFalse([m for m in messages if "search failed" in m])

    def test_portrait_downloads_are_no_pred_gg_outage(self):
        tracker, messages = self.tracker([1.0])
        tracker.handle(("lane_error", "portraits", NET))
        self.assertEqual([m for m in messages if m.startswith("Pred.gg")], [])
        self.assertEqual(sum("portraits failed" in m for m in messages), 1)

    def test_a_result_that_breaks_reduce_waits_for_its_backoff(self):
        clock = [100.0]
        tracker, _ = self.tracker(clock)
        state = tracker.state
        state.display = {"x": "y"}
        state.game_running = True  # as the process check would have it
        state.me, state.phase = ME, "draft"
        epoch = state.epoch
        state.inflight.add(("recent", epoch))
        broken = ("recent", epoch, ([], [{"match_id": GAME_A, "players": ["not a player"]}]))
        with self.assertRaises(Exception):
            tracker.handle(broken)
        self.assertNotIn(("recent", epoch), state.inflight)
        sent = core.reduce(state, ("tick",), 100.25)
        self.assertNotIn("recent", [c[0] for c in sent])  # not a hot loop
        later = core.reduce(state, ("tick",), 100.0 + core.CARD_RETRY[0])
        self.assertIn(("recent", epoch, ME), later)


@unittest.skipIf(choose_layout is None, "PySide6 is not installed")
class PaintTests(unittest.TestCase):
    def test_card_and_panel_paint(self):
        from PySide6.QtGui import QGuiApplication, QImage, QPainter

        from predoverlay.overlay import paint

        _app = QGuiApplication.instance() or QGuiApplication(["test", "-platform", "offscreen"])
        view = preview_view("loading", 0.0, 1)
        rows = [row(p) for p in view.allies]
        image = QImage(700, 700, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        try:
            paint.paint_stack(painter, 640, rows, rows, paint.Motion(), "5v5")
            seen = {"hero": "STEEL", "role": "OFFLANE"}
            paint.paint_card(painter, rows[0], paint.Motion(), seen)
            self.assertAlmostEqual(painter.opacity(), 1.0)
        finally:
            painter.end()

    def test_negative_vp_draws_an_empty_bar_inside_its_track(self):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication, QImage, QPainter

        from predoverlay.overlay import paint

        _app = QGuiApplication.instance() or QGuiApplication(["test", "-platform", "offscreen"])
        image = QImage(200, 40, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        try:
            paint._vp_bar(painter, 60, 20, {"vp": -25, "tier": "GOLD", "rank": "GOLD III"})
        finally:
            painter.end()
        self.assertEqual(image.pixelColor(50, 20).alpha(), 0)  # nothing left of the track


@unittest.skipIf(choose_layout is None, "PySide6 is not installed")
class WindowLayoutTests(unittest.TestCase):
    def test_layout_for_each_screen(self):
        self.assertEqual(choose_layout("accepted"), "draft")
        self.assertEqual(choose_layout("draft"), "draft")
        self.assertEqual(choose_layout("loading"), "loading")
        self.assertEqual(choose_layout("menu"), "menu")

    def test_preview_roster_is_complete(self):
        view = preview_view("draft", 0.0, 1)
        self.assertEqual((len(view.allies), len(view.enemies)), (5, 5))
        self.assertEqual(sum(player.is_me for player in view.allies + view.enemies), 1)
        for player in view.allies + view.enemies:
            self.assertEqual(row(player)["name"], player.name)  # as the screen shows it
            self.assertEqual(player.state, "ready")


class LogFollowerTests(unittest.TestCase):
    LINES = [
        "[2026.10.29-23.15.34:812][175]LogPredTask: Task Started: Pragma_MatchmakingAccept",
        "[2026.10.29-23.15.42:528][632]LogNet: Browse: 1.2.3.4:27098/Game/Voyager/Voyager"
        f"?userId={ME}?userName=SamplePlayer",
        "[2026.10.29-23.19.00:492][ 57]LogNet: Browse: 1.2.3.4:27098/Game/Maps/TwilightRC1"
        f"?userId={ME}?userName=SamplePlayer?game=DefaultGameMode?NumberOfPlayers=10",
        "[2026.10.29-23.42.01:014][243]LogNet: Browse: /Game/Voyager/Voyager?closed=REASON_FINISHED"
        "?gameInstanceId=20000000-0000-0000-0000-000000000002",
    ]

    def test_parse_phases_and_current_state(self):
        events = [phases.parse(line) for line in self.LINES]
        self.assertEqual([e["phase"] for e in events], ["accepted", "draft", "loading", "finished"])
        self.assertEqual(events[2]["map"], "TwilightRC1")
        self.assertEqual(phases.current(events[:3])["phase"], "loading")
        final = phases.current(events)
        self.assertEqual((final["phase"], final["last_match_id"]), ("menu", events[3]["match_id"]))
        self.assertEqual(phases.event_time(events[0]).isoformat(), "2026-10-29T23:15:34+00:00")
        self.assertIsNone(phases.parse("[2026.10.29-23.15.34:812][175]LogRHI: something"))

    def test_follower_reads_appends_and_survives_recreation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Predecessor.log"
            path.write_text(self.LINES[0] + "\n" + self.LINES[1][:30], encoding="utf-8")
            follower = phases.LogFollower(path)
            self.assertEqual([e["phase"] for e in follower.replay()], ["accepted"])
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(self.LINES[1][30:] + "\n")
            self.assertEqual([e["phase"] for e in follower.poll()], ["draft"])
            self.assertEqual(follower.poll(), [])
            os.remove(path)
            path.write_text(self.LINES[2] + "\n", encoding="utf-8")  # client restart
            self.assertEqual([e["phase"] for e in follower.poll()], ["loading"])


class ScreenLossTests(unittest.TestCase):
    """A monitor that goes away (display mode switch, streaming display) mid-run."""

    def test_a_deleted_screen_is_replaced_instead_of_failing_every_tick(self):
        from PySide6.QtWidgets import QApplication

        from predoverlay.overlay import window

        _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        with (
            mock.patch.object(window.win32, "game_in_front", lambda: True),
            mock.patch.object(window.win32, "key_down", lambda code: False),
            mock.patch.object(window.win32, "keep_on_top", lambda hwnd: None),
            mock.patch.object(window.win32, "make_inert", lambda hwnd: None),
        ):
            tracker = mock.Mock(snapshot=window.preview_view("draft", 1000.0, 1), tab_held=False)
            overlay = window.Overlay(tracker, clock=lambda: 1000.0)
            gone = mock.Mock()
            gone.geometry.side_effect = RuntimeError("Internal C++ object already deleted.")
            overlay.target_screen = gone
            overlay.tick()  # used to raise on every tick
            self.assertIsNot(overlay.target_screen, gone)
            self.assertGreater(overlay._geometry().width(), 0)
            overlay.hide()


class CardWindowTests(unittest.TestCase):
    """The TAB card's states in the real window, with a fake clock, keys and cursor."""

    def setUp(self):

        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QApplication

        from predoverlay.overlay import scoreboard, window

        self.app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        self.now = [1000.0]
        self.keys = {"F8": False}
        patches = [
            mock.patch.object(window.win32, "game_in_front", lambda: True),
            mock.patch.object(
                window.win32,
                "key_down",
                lambda code: self.keys["F8"] and code == window.win32.VK["F8"],
            ),
            mock.patch.object(window.win32, "keep_on_top", lambda hwnd: None),
            mock.patch.object(window.win32, "make_inert", lambda hwnd: None),
        ]
        view = window.preview_view("loading", phase_at=self.now[0] - 500, version=1)
        self.view = dataclasses.replace(view, started_at=self.now[0] - 400)
        self.tracker = mock.Mock(snapshot=self.view, tab_held=False)
        self.overlay = window.Overlay(self.tracker, clock=lambda: self.now[0])
        frame_left, frame_top, scale = self.overlay._frame()

        def at(team, design_y):
            left, _ = scoreboard.PANELS[team]
            return QPoint(
                round(frame_left + (left + 50) * scale), round(frame_top + design_y * scale)
            )

        self.at = lambda team, index: at(team, scoreboard.ROW_TOPS[index] + 80)
        self.gap = lambda team, index: at(team, scoreboard.ROW_TOPS[index] + scoreboard.ROW_H + 10)
        self.cursor = [self.at("ally", 0)]
        patches.append(
            mock.patch.object(window.QCursor, "pos", staticmethod(lambda: self.cursor[0]))
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        ids = {("ally", i): p.player_id for i, p in enumerate(self.view.allies)}
        ids.update({("enemy", i): p.player_id for i, p in enumerate(self.view.enemies)})
        self.overlay.reader.wanted = lambda match_id, players=(): False
        self.overlay.reader.named = frozenset(ids.values())
        self.overlay.reader.match_id = self.view.match_id  # it has read this match
        self.overlay.reader.seen = True  # and found the board
        self.overlay.reader.player_at = lambda match_id, row: (
            {"player_id": ids[row], "hero": "STEEL", "role": "OFFLANE"} if row in ids else None
        )
        self.addCleanup(self.overlay.close)

    def step(self, ticks=1):
        seen = []
        for _ in range(ticks):
            self.now[0] += 0.05
            self.overlay.tick()
            progress = self.overlay._progress(self.now[0])
            seen.append((self.overlay.layout_name, self.overlay.shown, round(progress, 2)))
        return seen

    def test_card_shows_on_tab_and_fades_without_flashing_the_panel(self):
        self.assertEqual(self.step(2)[-1][1], False)  # in the match nothing shows by itself
        self.tracker.tab_held = True
        self.assertEqual(self.step(4)[-1][:2], ("card", True))
        self.tracker.tab_held = False
        after = self.step(8)
        # While fading out it stays the card; the roster panel never appears.
        self.assertFalse([s for s in after if s[0] != "card" and s[2] > 0])
        self.assertIsNone(self.overlay.card)

    def test_the_card_keeps_off_the_games_mute_and_report_strip(self):
        from predoverlay.overlay import scoreboard

        left, _, scale = self.overlay._frame()
        self.tracker.tab_held = True
        self.step(6)  # pointing at the left of an ally row
        rect = self.overlay._card_rect()
        self.assertGreaterEqual(rect.x(), left + scoreboard.ACTIONS["ally"][1] * scale)
        self.cursor[0] = self.at("enemy", 1)
        self.step(30)
        rect = self.overlay._card_rect()
        self.assertLessEqual(rect.x() + rect.width(), left + scoreboard.ACTIONS["enemy"][0] * scale)

    def test_a_row_that_cannot_be_matched_gets_its_own_plate_not_the_last_card(self):

        console = 1  # Pred.gg marks the name a console one: the board is not read for it
        allies = tuple(
            dataclasses.replace(p, profile=dict(p.profile, console=True)) if i == console else p
            for i, p in enumerate(self.view.allies)
        )
        self.tracker.snapshot = dataclasses.replace(self.view, allies=allies)
        self.tracker.tab_held = True
        self.step(6)
        self.assertIsNotNone(self.overlay.card.data)
        self.overlay.reader.player_at = lambda match_id, row: None  # nobody on this row
        self.cursor[0] = self.at("ally", console)
        self.step(8)
        card = self.overlay.card
        self.assertIsNone(card.data)
        self.assertEqual(card.hit, ("ally", console))
        self.assertEqual([r["name"] for r in card.others], [allies[console].name])
        self.overlay.grab()  # paints the plate

    def test_the_end_of_match_screen_shows_cards_without_tab(self):

        from PySide6.QtCore import QPoint

        from predoverlay.overlay import scoreboard

        self.tracker.snapshot = dataclasses.replace(self.view, ended_at=self.now[0] - 30)
        frame_left, frame_top, scale = self.overlay._frame()
        left, _ = scoreboard.PANELS["enemy"]
        y = scoreboard.ROW_TOPS[2] + scoreboard.END_DY + 80
        self.cursor[0] = QPoint(
            round(frame_left + (left + 50) * scale), round(frame_top + y * scale)
        )
        self.assertFalse(self.tracker.tab_held)
        end = self.overlay.readers["end"]
        end.wanted = lambda match_id, players=(): False
        end.player_at = self.overlay.readers["tab"].player_at
        end.saw = lambda match_id: False  # the cinematic: no board read yet
        self.assertFalse([s for s in self.step(6) if s[1]])
        self.assertIsNone(self.overlay.card)
        end.saw = lambda match_id: True
        self.assertEqual(self.step(6)[-1][:2], ("card", True))
        self.assertEqual(self.overlay.card.hit, ("enemy", 2))
        self.assertEqual(self.overlay.card.dy, scoreboard.END_DY)
        self.assertEqual(self.overlay.board, "end")

    def test_crossing_the_gap_to_the_next_row_flies_the_card(self):
        self.tracker.tab_held = True
        self.step(6)
        self.cursor[0] = self.gap("ally", 0)  # between rows for a moment
        between = self.step(3)
        self.assertTrue(all(s[:2] == ("card", True) for s in between))
        self.cursor[0] = self.at("ally", 1)
        self.step(1)
        self.assertIsNotNone(self.overlay.card_move)  # flies from where it was
        self.assertEqual(self.overlay.card.hit, ("ally", 1))
        self.assertTrue(all(s[:2] == ("card", True) for s in self.step(6)))

    def test_the_other_team_is_a_longer_flight_not_a_blink(self):
        from predoverlay.overlay import window

        self.tracker.tab_held = True
        self.step(6)
        self.cursor[0] = self.at("enemy", 2)
        seen = self.step(1)
        self.assertEqual(seen[-1][:2], ("card", True))
        self.assertEqual(self.overlay.card_move[2], window.CARD_CROSS)

    def test_slow_crossing_between_the_teams_never_blinks(self):
        from PySide6.QtCore import QPoint

        self.tracker.tab_held = True
        self.step(6)
        frame_left, frame_top, scale = self.overlay._frame()
        y = round(frame_top + 330 * self.overlay._frame()[2])
        for x in range(1150, 1420, 15):  # about 0.9 s across the middle
            self.cursor[0] = QPoint(round(frame_left + x * scale), y)
            self.assertEqual(self.step(1)[-1][:2], ("card", True))

    def test_the_flight_moves_every_frame_and_fades_start_at_once(self):
        self.tracker.tab_held = True
        self.step(6)
        self.cursor[0] = self.at("ally", 3)
        self.step(1)
        ys = []
        for _ in range(8):  # 16 ms frames between ticks
            self.now[0] += 0.016
            self.overlay._frame_step()
            ys.append(self.overlay.geometry().y())
        self.assertGreater(len(set(ys)), 4)
        self.tracker.tab_held = False
        self.step(1)
        self.assertTrue(self.overlay._animating(self.now[0]))  # the exit fade runs from frame one

    def test_leaving_the_board_fades_the_card_after_the_grace(self):
        from PySide6.QtCore import QPoint

        self.tracker.tab_held = True
        self.step(6)
        frame_left, frame_top, scale = self.overlay._frame()
        self.cursor[0] = QPoint(round(frame_left + 1280 * scale), round(frame_top + 120 * scale))
        self.assertTrue(self.step(4)[-1][1])  # still held
        self.assertFalse(self.step(6)[-1][1])  # then fades

    def test_f8_during_the_card_pins_the_panel_after_tab(self):
        self.tracker.tab_held = True
        self.step(3)
        self.keys["F8"] = True
        self.step(1)
        self.keys["F8"] = False
        self.tracker.tab_held = False
        last = self.step(10)[-1]
        self.assertEqual(last[:2], ("loading", True))
        self.assertIsNone(self.overlay.card)

    def test_phase_change_with_tab_held_drops_the_card(self):
        from predoverlay.overlay import model

        self.tracker.tab_held = True
        self.step(3)
        self.tracker.snapshot = model.ViewModel(version=99, phase="menu", phase_at=self.now[0])
        last = self.step(10)[-1]
        self.assertEqual(last[:2], ("menu", True))
        self.assertIsNone(self.overlay.card)


@unittest.skipIf(choose_layout is None, "PySide6 is not installed")
class PanelMotionTests(unittest.TestCase):
    """Panel transitions in the real window with a fake clock and focus."""

    def setUp(self):
        from PySide6.QtWidgets import QApplication

        from predoverlay.overlay import model, window

        self.window_module, self.model = window, model
        self.app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        self.now = [1000.0]
        self.focus = [True]
        for name, value in (
            ("game_in_front", lambda: self.focus[0]),
            ("key_down", lambda code: False),
            ("keep_on_top", lambda hwnd: None),
            ("make_inert", lambda hwnd: None),
        ):
            patcher = mock.patch.object(window.win32, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tracker = mock.Mock(
            snapshot=model.ViewModel(phase="menu", phase_at=self.now[0]), tab_held=False
        )
        self.overlay = window.Overlay(self.tracker, clock=lambda: self.now[0])
        self.addCleanup(self.overlay.close)

    def step(self, ticks=1):
        out = []
        for _ in range(ticks):
            self.now[0] += 0.05
            self.overlay.tick()
            out.append(
                (self.overlay.layout_name, self.overlay.shown, self.overlay._progress(self.now[0]))
            )
        return out

    def view(self, phase, **extra):
        view = self.window_module.preview_view(
            phase, phase_at=self.now[0], version=int(self.now[0] * 100)
        )

        return dataclasses.replace(view, **extra)

    def test_the_menu_row_is_the_badge_at_the_right_end_of_the_sub_menu_row(self):
        from predoverlay.overlay import paint

        self.step(10)
        self.assertEqual(self.overlay.layout_name, "menu")
        frame, scale = self.overlay._geometry(), self.overlay.scale
        box = self.overlay.geometry()
        self.assertEqual(self.overlay.design_size, (paint.BADGE_W, paint.BADGE_H))
        self.assertEqual(box.width(), round(paint.BADGE_W * scale))
        right = frame.x() + frame.width() - round(self.window_module.BADGE_RIGHT * scale)
        self.assertEqual(box.x() + box.width(), right)
        self.assertEqual(box.y(), frame.y() + round(self.window_module.BADGE_TOP * scale))

    def test_leaving_the_menu_fades_the_badge_out_where_it_is(self):
        self.step(10)
        self.tracker.snapshot = self.view("accepted")  # hidden until the draft
        seen = self.step(8)
        # The badge fades out in the menu's place; nothing else shows meanwhile.
        self.assertTrue(all(s[0] == "menu" or s[2] == 0 for s in seen))
        self.assertEqual(round(seen[-1][2], 3), 0.0)
        self.tracker.snapshot = self.view("draft")
        self.assertEqual(self.step(10)[-1][:2], ("draft", True))

    def test_draft_to_loading_and_focus_back_do_not_replay_the_rows(self):
        self.tracker.snapshot = self.view("draft")
        self.step(12)
        started = self.overlay.rows_started
        self.tracker.snapshot = self.view("loading")
        seen = self.step(14)
        self.assertEqual({s[2] for s in seen}, {1.0})  # the seam carries it, no fade
        self.assertEqual(seen[-1][:2], ("loading", True))
        self.assertEqual(self.overlay.rows_started, started)
        self.focus[0] = False
        self.step(6)
        self.focus[0] = True
        self.step(6)
        self.assertEqual(self.overlay.rows_started, started)

    def test_reversed_fade_is_short_and_hiding_freezes_the_rows(self):
        self.tracker.snapshot = self.view("draft")
        self.step(12)
        self.focus[0] = False
        self.step(1)
        frozen = self.overlay._motion(self.now[0]).row(9)
        self.now[0] += 5
        self.assertEqual(self.overlay._motion(self.now[0]).row(9), frozen)
        self.focus[0] = True
        self.now[0] -= 5
        self.step(1)
        self.assertLessEqual(self.overlay.fade_duration, self.window_module.ENTER * 0.5)

    def test_roster_grows_out_of_the_status_plate(self):
        empty = self.model.ViewModel(phase="draft", phase_at=self.now[0], status="draft")
        self.tracker.snapshot = empty
        self.step(10)
        self.assertEqual(self.overlay.design_size[1], 44)
        self.tracker.snapshot = self.view("draft")
        self.step(1)
        self.assertIsNotNone(self.overlay.grow)
        self.assertGreater(self.overlay.design_size[1], 200)  # full size at once: five rows
        self.step(10)
        self.assertIsNone(self.overlay.grow)

    def test_settled_panel_stops_the_frame_timer(self):
        self.tracker.snapshot = self.view("draft")
        self.step(40)
        self.assertFalse(self.overlay.frame.isActive())


class ChangedGroupsTests(unittest.TestCase):
    def test_only_the_fields_that_changed(self):
        from predoverlay.overlay.rows import changed_groups

        view = core.PlayerView(OTHERS[0], None, "ally", False, "loading")
        loading = row(view)
        named = row(core.PlayerView(OTHERS[0], "Riktor#1", "ally", False, "loading"))
        self.assertEqual(changed_groups(loading, named), {"name"})
        profile = {"current": {"rank": "Gold II", "points": 740.0}, "first_played": iso(0)}
        ranked = row(core.PlayerView(OTHERS[0], "Riktor#1", "ally", False, "ready", profile))
        self.assertEqual(changed_groups(named, ranked), {"meta", "rank", "vp"})


class ScoreboardTests(unittest.TestCase):
    def test_rows_under_the_cursor(self):
        from predoverlay.overlay import scoreboard

        self.assertEqual(scoreboard.row_at(500, 300), ("ally", 0))
        self.assertEqual(scoreboard.row_at(1800, 1100), ("enemy", 4))
        self.assertIsNone(scoreboard.row_at(1280, 300))  # the role column in the middle
        self.assertIsNone(scoreboard.row_at(500, 100))

    def test_readings_match_names_including_cut_and_misread_ones(self):
        from predoverlay.overlay import scoreboard

        readings = {
            ("e", 0): "QUIET LANTERNKEEP...",
            ("e", 1): "COMET01234",
            ("e", 2): "NATHANIEL-XRQ",
        }
        names = {
            "a": "QUIET LANTERNKEEPER",
            "b": "COMETO1234",
            "c": "NATHANIEL_XRQ",
            "d": "TARVEL17",
        }
        self.assertEqual(
            scoreboard.assign(readings, names), {("e", 0): "a", ("e", 1): "b", ("e", 2): "c"}
        )

    def test_reader_maps_rows_to_players_roles_and_heroes(self):
        from predoverlay.overlay import scoreboard

        lines = {
            b"0": ["GLIMROX", "STEEL"],
            b"1": ["LIEUTENANT", "FENG MAO"],
            b"2": ["PATHCROSSER", "BELCOURT"],
            b"3": ["SAMPLEPLAYERONE", "MOURN"],
            b"4": [],
        }  # a one-letter name OCR misses
        reader = scoreboard.Reader(read=lambda png: lines[png], clock=lambda: 100.0)
        crops = {("ally", i): str(i).encode() for i in range(5)}
        names = {
            "ally": {
                "m": "GLIMROX",
                "s": "LIEUTENANT",
                "b": "PATHCROSSER",
                "t": "SAMPLEPLAYERONE",
                "x": "X",
            }
        }
        reader.match_id, reader.attempts, reader.started = GAME_A, 1, 100.0
        reader._run(GAME_A, crops, names)
        self.assertEqual(
            reader.player_at(GAME_A, ("ally", 0)),
            {"player_id": "m", "hero": "STEEL", "role": "OFFLANE"},
        )
        self.assertEqual(reader.player_at(GAME_A, ("ally", 4))["player_id"], "x")  # by elimination
        self.assertIsNone(reader.player_at(GAME_B, ("ally", 0)))
        self.assertFalse(reader.wanted(GAME_A))  # just read: retries are spaced
        self.assertTrue(reader.wanted(GAME_B))  # a new match is read again


class CrucibleScoreboardTests(unittest.TestCase):
    def test_three_rows_per_team_without_roles(self):
        from predoverlay.overlay import scoreboard

        lines = {
            b"0": ["ECHO", "IGGY & SCORCH"],
            b"1": ["ECHO248", "SHINBI"],
            b"2": ["ANDROID", "MURDOCK"],
        }
        reader = scoreboard.Reader(read=lambda png: lines[png], clock=lambda: 0.0)
        names = {"enemy": {"d": "Echo", "e": "Echo248", "c": "Android"}}
        reader.start(GAME_A, lambda: {("enemy", i): str(i).encode() for i in range(3)}, names)
        while reader.busy:
            pass
        self.assertEqual(reader.player_at(GAME_A, ("enemy", 1))["player_id"], "e")
        self.assertIsNone(reader.player_at(GAME_A, ("enemy", 1))["role"])
        self.assertFalse(reader.wanted(GAME_A))  # every player matched: done


class ScoreboardSafetyTests(unittest.TestCase):
    def test_the_last_row_of_a_team_read_whole_is_the_last_player(self):
        # The board lists the team row by row: with every row read and four placed, the
        # fifth is the fifth player, however unlike the name another screen read. The
        # core joins them by the same rule; a row left without a card while its panel
        # row was ready was the result of two rules.
        from predoverlay.overlay import scoreboard

        names = {
            "ally": dict(m="GLIMROX", s="LIEUTENANT", b="PATHCROSSER", t="SAMPLEPLAYERONE", x="X")
        }
        lines = {
            b"0": ["GLIMROX"],
            b"1": ["LIEUTENANT"],
            b"2": ["PATHCROSSER"],
            b"3": ["SAMPLEPLAYERONE"],
            b"4": ["SOMEONE ELSE"],
        }
        reader = scoreboard.Reader(read=lambda png: lines[png], clock=lambda: 0.0)
        reader.match_id, reader.attempts = GAME_A, 1
        reader._run(GAME_A, lambda: {("ally", i): str(i).encode() for i in range(5)}, names)
        self.assertEqual(reader.player_at(GAME_A, ("ally", 4))["player_id"], "x")
        # Two rows unlike their players: neither is guessed.
        lines[b"3"] = ["ANOTHER ONE"]
        reader = scoreboard.Reader(read=lambda png: lines[png], clock=lambda: 0.0)
        reader.match_id, reader.attempts = GAME_A, 1
        reader._run(GAME_A, lambda: {("ally", i): str(i).encode() for i in range(5)}, names)
        self.assertIsNone(reader.player_at(GAME_A, ("ally", 3)))
        self.assertIsNone(reader.player_at(GAME_A, ("ally", 4)))

    def test_a_clear_other_reading_is_not_forced_while_a_row_is_unread(self):
        from predoverlay.overlay import scoreboard

        lines = {
            b"0": ["GLIMROX"],
            b"1": ["LIEUTENANT"],
            b"2": ["PATHCROSSER"],
            b"3": [],
            b"4": ["SOMEONE ELSE"],
        }
        reader = scoreboard.Reader(read=lambda png: lines[png], clock=lambda: 0.0)
        names = {
            "ally": {
                "m": "GLIMROX",
                "s": "LIEUTENANT",
                "b": "PATHCROSSER",
                "t": "SAMPLEPLAYERONE",
                "x": "X",
            }
        }
        reader.match_id, reader.attempts = GAME_A, 1
        reader._run(GAME_A, lambda: {("ally", i): str(i).encode() for i in range(5)}, names)
        self.assertIsNone(reader.player_at(GAME_A, ("ally", 4)))

    def test_alike_names_and_non_scoreboard_screens(self):
        from predoverlay.overlay import scoreboard

        reader = scoreboard.Reader(read=lambda png: ["WINDBREAK482"], clock=lambda: 0.0)
        names = {"enemy": {"a": "Windbreak482", "b": "Windbreak482"}}
        reader.match_id, reader.attempts = GAME_A, 1
        reader._run(GAME_A, {("enemy", 0): b"x", ("enemy", 1): b"y"}, names)
        self.assertEqual(reader.rows, {})  # two alike names: nothing assigned
        self.assertEqual(reader.attempts, 0)  # and the reading is not counted

    def test_heroes_snap_to_known_names_and_lookalikes_fold(self):
        from predoverlay.overlay import scoreboard

        self.assertEqual(scoreboard.snap_hero("vvNG MAO", ["Feng Mao", "Grux"]), "FENG MAO")
        self.assertIsNone(scoreboard.snap_hero("QQQQ", ["Feng Mao", "Grux"]))
        self.assertEqual(scoreboard.similarity("COMET01234", "COMETO1234"), 1.0)


class ScoreboardRetryCapTests(unittest.TestCase):
    def test_refunded_readings_still_end_after_a_hard_cap_per_match(self):
        from predoverlay.overlay import scoreboard

        now = [0.0]
        reads = []
        reader = scoreboard.Reader(read=lambda png: reads.append(png) or [], clock=lambda: now[0])
        names = {"ally": {"a": "GLIMROX", "b": "LIEUTENANT"}, "enemy": {"c": "ANDROID"}}
        crops = {("ally", 0): b"0", ("ally", 1): b"1", ("enemy", 0): b"2"}
        readings = 0
        for _ in range(100):  # a whole match of TAB on a board the reader cannot read
            if reader.wanted(GAME_A):
                reader.start(GAME_A, crops, names)
                while reader.busy:
                    pass
                readings += 1
            now[0] += scoreboard.RETRY_AFTER
        self.assertEqual(reader.attempts, 0)  # every reading was refunded...
        self.assertEqual(readings, scoreboard.MAX_READINGS)  # ...and still they stopped
        self.assertEqual(len(reads), 3 * scoreboard.MAX_READINGS)
        self.assertTrue(reader.spent(GAME_A))
        self.assertFalse(reader.spent(GAME_B))
        self.assertTrue(reader.wanted(GAME_B))  # the next match is read again
        reader.start(GAME_B, crops, names)
        while reader.busy:
            pass
        self.assertEqual(reader.readings, 1)


class EndScreenReaderTests(unittest.TestCase):
    """The end screen comes after a cinematic the log does not mark."""

    def run_reader(self, reader, crops, names, now):
        for _ in range(200):
            if reader.wanted(GAME_A):
                reader.start(GAME_A, crops, names)
                while reader.busy:
                    pass
            if reader.saw(GAME_A):
                return
            now[0] += 0.5

    def test_blank_readings_during_the_cinematic_cost_nothing(self):
        from predoverlay.overlay import scoreboard

        now = [0.0]
        board = [False]
        lines = {
            b"0": ["GLIMROX", "STEEL"],
            b"1": ["LIEUTENANT", "GRUX"],
            b"2": ["ANDROID", "KWANG"],
        }
        reader = scoreboard.Reader(
            read=lambda png: lines[png] if board[0] else [], clock=lambda: now[0], patient=True
        )
        names = {"ally": {"a": "GLIMROX", "b": "LIEUTENANT"}, "enemy": {"c": "ANDROID"}}
        crops = {("ally", 0): b"0", ("ally", 1): b"1", ("enemy", 0): b"2"}
        for _ in range(int(30 / 0.5)):  # thirty seconds of cinematic
            if reader.wanted(GAME_A):
                reader.start(GAME_A, crops, names)
                while reader.busy:
                    pass
            now[0] += 0.5
        self.assertFalse(reader.saw(GAME_A))
        self.assertEqual((reader.readings, reader.attempts), (0, 0))
        self.assertEqual(reader.blank, 10)  # one look every RETRY_AFTER seconds
        board[0] = True
        self.run_reader(reader, crops, names, now)
        self.assertTrue(reader.saw(GAME_A))
        self.assertEqual(len(reader.rows), 3)
        self.assertEqual(reader.readings, 1)

    def test_the_blank_looks_end_after_a_cap(self):
        from predoverlay.overlay import scoreboard

        now = [0.0]
        reader = scoreboard.Reader(read=lambda png: [], clock=lambda: now[0], patient=True)
        names = {"ally": {"a": "GLIMROX"}}
        looks = 0
        for _ in range(1000):
            if reader.wanted(GAME_A):
                reader.start(GAME_A, {("ally", 0): b"0"}, names)
                while reader.busy:
                    pass
                looks += 1
            now[0] += scoreboard.RETRY_AFTER
        self.assertEqual(looks, scoreboard.BLANK_MAX + scoreboard.MAX_READINGS)

    def test_the_tab_reader_counts_every_reading(self):
        from predoverlay.overlay import scoreboard

        reader = scoreboard.Reader(read=lambda png: [], clock=lambda: 0.0)
        reader.start(GAME_A, {("ally", 0): b"0"}, {"ally": {"a": "GLIMROX"}})
        while reader.busy:
            pass
        self.assertEqual((reader.readings, reader.blank), (1, 0))
        self.assertFalse(reader.saw(GAME_A))


class ConsoleScoreboardTests(unittest.TestCase):
    """Console players have no readable name: placed by elimination or not at all."""

    LINES = {
        b"0": ["GLIMROX", "STEEL"],
        b"1": ["XB0X GAMERTAG", "GRUX"],
        b"2": ["PATHCROSSER", "BELCOURT"],
        b"3": ["PSN-SOMEONE", "MOURN"],
        b"4": ["SAMPLEPLAYERONE", "KALLARI"],
    }

    def read(self, names, consoles):
        from predoverlay.overlay import scoreboard

        reader = scoreboard.Reader(read=lambda png: self.LINES[png], clock=lambda: 0.0)
        crops = {("ally", i): str(i).encode() for i in range(5)}
        reader.start(GAME_A, lambda: crops, names, consoles=consoles)
        while reader.busy:
            pass
        return reader

    def test_two_console_players_leave_the_rest_of_the_team_matched(self):
        names = {"ally": {"m": "GLIMROX", "b": "PATHCROSSER", "t": "SAMPLEPLAYERONE"}}
        reader = self.read(names, {"ally": ["c1", "c2"]})
        found = {row: entry["player_id"] for row, entry in reader.rows.items()}
        self.assertEqual(found, {("ally", 0): "m", ("ally", 2): "b", ("ally", 4): "t"})
        self.assertEqual(reader.player_at(GAME_A, ("ally", 4))["role"], "CARRY")  # still 5 rows
        self.assertFalse(reader.wanted(GAME_A))  # all it can place is placed: no retries

    def test_a_single_console_player_takes_the_last_row(self):
        names = {
            "ally": {
                "m": "GLIMROX",
                "x": "XB0X GAMERTAG",
                "b": "PATHCROSSER",
                "t": "SAMPLEPLAYERONE",
            }
        }
        reader = self.read(names, {"ally": ["c1"]})
        self.assertEqual(reader.player_at(GAME_A, ("ally", 3))["player_id"], "c1")
        self.assertFalse(reader.wanted(GAME_A))

    def test_a_console_player_is_not_placed_while_a_named_one_is_missing(self):
        names = {"ally": {"m": "GLIMROX", "b": "PATHCROSSER", "t": "SAMPLEPLAYERONE", "q": "Q"}}
        reader = self.read(names, {"ally": ["c1"]})
        self.assertNotIn("c1", {entry["player_id"] for entry in reader.rows.values()})

    def test_the_matcher_looks_for_the_names_the_screen_showed(self):
        from PySide6.QtWidgets import QApplication

        from predoverlay.overlay import window

        _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
        view = window.preview_view("loading", 0.0, 1)
        # A console player: the panel shows a made-up label, the board their real name.
        hidden = dataclasses.replace(view.enemies[1], name='CONSOLE "SILENT HAWK"', read_as="PSN-1")
        view = dataclasses.replace(view, enemies=(view.enemies[0], hidden, *view.enemies[2:]))
        tracker = mock.Mock(snapshot=view, tab_held=False)
        overlay = window.Overlay(tracker)
        self.addCleanup(overlay.close)
        with mock.patch.object(overlay.reader, "start") as start:
            overlay._read_scoreboard(view)
        _, _, names, _ = start.call_args.args
        self.assertEqual(names["enemy"][hidden.player_id], "PSN-1")
        self.assertEqual((len(names["ally"]), len(names["enemy"])), (5, 5))


class ReconnectTests(unittest.TestCase):
    def test_joining_the_match_map_from_draft_is_the_match(self):
        state = core.State(me=ME)
        core.reduce(state, phase("draft"), 100.0)
        core.reduce(state, phase("joined", map="Draft", at_ts=101.0), 101.0)
        self.assertEqual(state.phase, "draft")  # the draft map itself
        core.reduce(state, phase("joined", map="TwilightRC1", at_ts=200.0), 200.0)
        self.assertEqual((state.phase, state.joined), ("loading", True))
        core.reduce(state, phase("presence", at_ts=210.0), 210.0)
        self.assertEqual(state.started_at, 210.0)


class BadgeMotionTests(unittest.TestCase):
    """The menu badge's motion model (pure, no Qt)."""

    def test_badge_state_change_animates_then_settles(self):
        from predoverlay.overlay import motion

        badge = motion.Badge()
        self.assertTrue(badge.sync("offline", 10.0))
        frame = badge.frame(10.05)
        self.assertEqual((frame["old"], frame["new"]), ("ok", "offline"))
        self.assertLess(frame["new_a"], 1.0)
        self.assertGreater(badge.busy_until(), 10.0)
        self.assertEqual(badge.frame(badge.busy_until())["new_a"], 1.0)


if __name__ == "__main__":
    unittest.main()
