"""Nothing is asked of Pred.gg or its art while the game is closed."""

import unittest
from unittest import mock

from predoverlay.overlay import core, lanes, tracker
from predoverlay.overlay.tracker import Tracker

ME = "00000000-0000-0000-0000-0000000000aa"
OTHER = "00000000-0000-0000-0000-0000000000bb"
PEER = "10000000-0000-0000-0000-000000000001"
NAMES = ["Orbitfox", "Lanternjaw", "SamplePlayer", "Quietstorm", "Somebody"]
HOUR = 60 * 60.0


def log(phase, at=0.0, **extra):
    return ("log", {"phase": phase, "at_ts": at, **extra})


def draft(state, names=NAMES):
    """A reading of the draft screen: every name in two versions that agree."""
    texts = {("ally", i): [name, name] for i, name in enumerate(names)}
    texts = {**texts, ("anchor", 0): ["ALLY", "ALLY"]}
    return ("screen", "draft", state.phase_at, texts)


def remote(commands):
    """Commands that leave the PC: everything a lane would send to Pred.gg or its CDN."""
    return [c for c in commands if tracker.ROUTES.get(c[0]) in ("predgg", "assets")]


def run(state, steps):
    """Feed (time, event) pairs; return the commands each one emitted."""
    return [core.reduce(state, event, now) for now, event in steps]


def ticks(start, end, every=1.0):
    now, out = start, []
    while now < end:
        out.append((now, ("tick",)))
        now += every
    return out


# (name, state fields at the start, events while the game is closed)
CLOSED = [
    ("a fresh start: no session yet", {}, ticks(0.0, 2 * HOUR, 7.0)),
    ("signed in, a day of idling", {"me": ME}, ticks(0.0, 24 * HOUR, 60.0)),
    ("a refused session waits for the game", {"auth": "login"}, ticks(0.0, HOUR, 5.0)),
    (
        "an old log replayed: login and a match",
        {},
        [
            (10.0, log("login", at=1.0, user_id=ME)),
            (10.0, log("accepted", at=2.0)),
            (10.0, log("draft", at=3.0, user_id=ME, user_name="SamplePlayer")),
            (10.0, log("loading", at=4.0, user_id=ME, experience="BP_Experience_Game_Ranked")),
            *ticks(11.0, 600.0, 2.0),
        ],
    ),
    (
        "a fresh login line before the process check sees the game",
        {},
        [(100.0, log("login", at=99.0, user_id=ME)), *ticks(100.0, 300.0, 1.0)],
    ),
    (
        "the user asks to sign in again",
        {"me": ME},
        [(5.0, ("sign_in",)), *ticks(5.0, 900.0, 3.0), (900.0, ("sign_in",))],
    ),
    (
        "screen readings and lane results that were on their way",
        {"me": ME},
        [
            (
                1.0,
                (
                    "screen",
                    "draft",
                    0.0,
                    {("ally", 0): ["Orbitfox", "Orbitfox"], ("anchor", 0): ["ALLY", "ALLY"]},
                ),
            ),
            (1.0, ("found", 0, {"a0": {"player_id": PEER, "name": "Orbitfox"}})),
            (1.0, ("profiles", 0, {PEER: {"current": {"rank": "Gold II", "points": 750}}})),
            (1.0, ("recent", 0, ([], []))),
            (1.0, ("display", {"Wood": "Mourn"})),
            (1.0, ("icons", {"Wood": "mourn"})),
            (1.0, ("auth", True)),
            (1.0, ("reach", True)),
            *ticks(2.0, 3 * HOUR, 30.0),
        ],
    ),
]


class ClosedGame(unittest.TestCase):
    def test_no_request_of_any_kind_while_the_game_is_closed(self):
        for name, fields, steps in CLOSED:
            with self.subTest(name):
                state = core.State(game_running=False, **fields)
                sent = [c for commands in run(state, steps) for c in commands]
                self.assertEqual(remote(sent), [])
                shown = core.view(state)
                self.assertEqual(shown.phase, "closed")
                self.assertEqual((shown.allies, shown.enemies), ((), ()))

    def test_the_view_waits_for_predecessor(self):
        state = core.State(game_running=False)
        self.assertEqual(core.view(state).status, core.WAITING)
        self.assertEqual(core.view(state).health, "ok")
        state = core.State(auth="login", game_running=False)
        self.assertEqual(core.view(state).status, core.NEEDS_LOGIN)  # still true, still shown


class GameStarts(unittest.TestCase):
    def test_the_hero_catalog_follows_the_start(self):
        state = core.State(game_running=False)
        self.assertEqual(remote(core.reduce(state, ("tick",), 10.0)), [])
        self.assertEqual(core.reduce(state, ("game", True), 20.0), [("display",)])
        self.assertEqual(core.reduce(state, ("tick",), 20.25), [])  # in flight; nothing else
        self.assertEqual(core.view(state).phase, "menu")

    def test_a_sign_in_while_closed_counts_once_the_game_runs(self):
        state = core.State(auth="login", display={"x": "y"}, game_running=False)
        self.assertEqual(core.reduce(state, ("sign_in",), 1.0), [])  # no read while closed
        core.reduce(state, ("game", True), 50.0)
        self.assertEqual(core.view(state).health, "ok")
        sent = core.reduce(state, log("draft", 51.0, user_id=ME), 51.0)
        self.assertIn(("recent", state.epoch, ME), sent)

    def test_the_account_the_log_names_while_closed_is_the_one_read(self):
        state = core.State(me=ME, display={"x": "y"}, game_running=False)
        self.assertEqual(remote(core.reduce(state, log("login", 99.0, user_id=OTHER), 100.0)), [])
        self.assertEqual(remote(core.reduce(state, ("tick",), 101.0)), [])
        core.reduce(state, ("game", True), 102.0)
        sent = core.reduce(state, log("draft", 103.0), 103.0)
        self.assertIn(("recent", state.epoch, OTHER), sent)

    def test_portraits_from_a_late_catalog_download_once_the_game_runs(self):
        state = core.State(game_running=False)
        self.assertEqual(core.reduce(state, ("icons", {"Wood": "mourn"}), 1.0), [])
        core.reduce(state, ("game", True), 2.0)
        self.assertIn(("portraits", ("mourn",)), core.reduce(state, ("tick",), 2.0))


class GameCloses(unittest.TestCase):
    def setUp(self):
        self.state = core.State(me=ME, my_name="SamplePlayer", display={"Wood": "Mourn"})

    def feed(self, *events, now=200.0):
        return [c for event in events for c in core.reduce(self.state, event, now)]

    def test_lookups_stop_and_the_match_goes_when_the_game_quits(self):
        self.feed(log("draft", at=200.0))
        self.feed(draft(self.state))
        self.assertEqual(len(core.view(self.state).allies), 5)
        self.assertEqual(self.feed(("game", False), now=210.0), [])
        self.assertEqual(self.state.me, ME)  # who plays here stays known
        shown = core.view(self.state)
        self.assertEqual((shown.phase, shown.status), ("closed", core.WAITING))
        self.assertEqual((shown.allies, shown.enemies), ((), ()))
        later = run(self.state, ticks(210.0, 3 * HOUR, 2.0))
        self.assertEqual(remote([c for commands in later for c in commands]), [])

    def test_nothing_waits_for_commands_the_lanes_dropped(self):
        self.assertIn(("recent", 1, ME), self.feed(log("draft", at=200.0)))
        self.feed(("game", False), ("game", True), now=220.0)
        sent = self.feed(log("draft", at=230.0), now=230.0)
        self.assertIn(("recent", self.state.epoch, ME), sent)  # not held back as in flight

    def test_a_second_report_of_the_same_state_changes_nothing(self):
        version = self.state.version
        self.assertEqual(self.feed(("game", True)), [])
        self.assertEqual(self.state.version, version)


class FakeLane:
    def __init__(self):
        self.sent, self.dropped, self.out = [], 0, None

    def submit(self, command):
        self.sent.append(command)

    def discard_pending(self):
        self.dropped += 1

    def is_alive(self):
        return True


class TrackedGame(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.running = [False]
        self.messages = []
        self.lanes = {"predgg": FakeLane(), "assets": FakeLane()}
        follower = mock.Mock()
        follower.replay.return_value = []
        follower.poll.return_value = []
        self.tracker = Tracker(
            self.lanes,
            follower=follower,
            log=self.messages.append,
            clock=lambda: self.clock[0],
            game_running=lambda: self.running[0],
        )
        self.follower = follower

    def turn(self, seconds=1.0):
        """What the tracker loop does once, without waiting for a lane result."""
        self.clock[0] += seconds
        self.tracker.check_game()
        self.tracker.handle(("tick",))

    def sent(self):
        return [c for lane in self.lanes.values() for c in lane.sent]

    def test_requests_follow_the_game_process(self):
        for _ in range(120):
            self.turn()
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.tracker.snapshot.phase, "closed")
        self.running[0] = True
        for _ in range(int(tracker.GAME_CHECK) + 1):
            self.turn()
        self.assertIn(("display",), self.sent())
        self.running[0] = False
        for _ in range(int(tracker.GAME_CHECK) + 1):
            self.turn()
        self.assertEqual([lane.dropped for lane in self.lanes.values()], [1, 1])
        before = len(self.sent())
        for _ in range(600):
            self.turn()
        self.assertEqual(len(self.sent()), before)
        transitions = [m for m in self.messages if m.startswith("game ")]
        self.assertEqual(transitions, ["game started", "game closed"])

    def test_an_unreadable_process_list_keeps_the_last_answer(self):
        self.running[0] = True
        self.turn()
        self.running[0] = None
        for _ in range(30):
            self.turn()
        self.assertTrue(self.tracker.state.game_running)
        self.assertEqual([m for m in self.messages if m.startswith("game ")], ["game started"])

    def test_at_start_a_replayed_match_counts_only_with_the_game_running(self):
        replay = [
            {"phase": "login", "at": "2026.11.09-10.00.00", "user_id": ME},
            {"phase": "draft", "at": "2026.11.09-10.05.00", "user_id": ME, "user_name": "x"},
        ]
        for running, phase in ((False, "closed"), (True, "draft")):
            with self.subTest(running=running):
                self.setUp()
                self.follower.replay.return_value = replay
                self.running[0] = running
                self.tracker.stop.set()  # run() does the start-up part only
                self.tracker.run()
                self.tracker.handle(("tick",))
                self.assertEqual(self.tracker.snapshot.phase, phase)
                recent = [c for c in self.sent() if c[0] == "recent"]
                self.assertEqual(bool(recent), running)


class LaneDrop(unittest.TestCase):
    def test_pending_commands_are_dropped(self):
        lane = lanes.Lane("test", lambda command: [], None)
        for n in range(3):
            lane.submit(("search", n, {}))
        lane.discard_pending()
        self.assertTrue(lane.inbox.empty())


if __name__ == "__main__":
    unittest.main()
