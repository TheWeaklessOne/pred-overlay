"""The player changes Steam accounts between games, often."""

import unittest
from datetime import datetime, timezone

from predoverlay.overlay import core, phases
from predoverlay.overlay.tracker import log_event

OLD = "00000000-0000-0000-0000-00000000000a"
NEW = "00000000-0000-0000-0000-00000000000b"
NO_SESSION = "AuthenticationError: Not signed in, or the session expired."
LINE = "[2026.11.07-00.17.51:560][765]LogPredTask: Pragma_PlayerLoginEx: PlayerId: " + NEW


def login(player, at):
    return ("log", {"phase": "login", "user_id": player, "at_ts": at})


def session_of(account, **fields):
    return core.State(me=account, token_account=account, **fields)


def relogins(state, now):
    return core.reduce(state, ("tick",), now).count(("relogin",))


class Marker(unittest.TestCase):
    def test_the_log_line_names_the_player(self):
        event = phases.parse(LINE)
        self.assertEqual((event["phase"], event["user_id"]), ("login", NEW))
        moment = datetime(2026, 11, 7, 0, 17, 51, tzinfo=timezone.utc)
        self.assertEqual(log_event(event)[1]["at_ts"], moment.timestamp())

    def test_it_is_not_a_phase(self):
        self.assertEqual(phases.current([phases.parse(LINE)])["phase"], "menu")
        state = session_of(OLD, phase="draft")
        core.reduce(state, login(OLD, 100.0), 100.0)
        self.assertEqual(state.phase, "draft")


class Switch(unittest.TestCase):
    def test_the_game_logging_in_as_another_account_signs_in_from_the_menu(self):
        state = session_of(OLD, display={"a": "A"})
        core.reduce(state, login(NEW, 100.0), 101.0)
        self.assertEqual(state.me, NEW)
        self.assertEqual(relogins(state, 101.0), 1)
        self.assertEqual(relogins(state, 102.0), 0)  # in flight
        core.reduce(state, ("relogin", NEW), 103.0)
        self.assertEqual(state.token_account, NEW)
        self.assertEqual(relogins(state, 104.0), 0)
        self.assertEqual(core.view(state).health, "ok")

    def test_an_old_line_replayed_at_start_does_not_sign_in_from_the_menu(self):
        state = session_of(OLD, display={"a": "A"})
        core.reduce(state, login(NEW, 100.0), 100.0 + core.FRESH_LOGIN)
        self.assertEqual(state.me, NEW)
        self.assertEqual(relogins(state, 500.0), 0)

    def test_that_case_signs_in_when_a_match_is_found(self):
        state = session_of(OLD, display={"a": "A"})
        core.reduce(state, login(NEW, 100.0), 100.0 + core.FRESH_LOGIN)
        core.reduce(state, ("log", {"phase": "accepted", "at_ts": 500.0}), 500.0)
        self.assertEqual(relogins(state, 500.0), 1)

    def test_switching_back_works_the_same_way(self):
        state = session_of(OLD, display={"a": "A"})
        for player, at in ((NEW, 100.0), (OLD, 1000.0), (NEW, 2000.0)):
            core.reduce(state, login(player, at), at)
            self.assertEqual(relogins(state, at), 1)
            core.reduce(state, ("relogin", player), at + 2)
            self.assertEqual(state.token_account, player)

    def test_failed_tries_in_the_menu_stop_until_a_match(self):
        state = session_of(OLD, display={"a": "A"})
        core.reduce(state, login(NEW, 0.0), 0.0)
        now = 0.0
        sent = 0
        for _ in range(20):
            now += 200.0
            if relogins(state, now):
                sent += 1
                core.reduce(state, ("relogin", None, NO_SESSION), now)
        self.assertEqual(sent, len(core.CARD_RETRY))
        core.reduce(state, ("log", {"phase": "accepted", "at_ts": now}), now)
        self.assertEqual(relogins(state, now + 200.0), 1)

    def test_a_game_gone_with_its_account_does_not_hold_the_match(self):
        state = core.State(me=NEW, token_account=OLD, phase="loading", display={"a": "A"})
        self.assertEqual(relogins(state, 1.0), 1)
        core.reduce(state, ("relogin", OLD), 2.0)  # Steam is on the other account now
        self.assertEqual(state.me, OLD)
        self.assertEqual(relogins(state, 300.0), 0)
        empty = {"in_game": False, "match_id": None, "player_ids": ()}
        for moment in range(10, 200, 10):
            core.reduce(state, ("instance", empty), float(moment))
        self.assertEqual(state.phase, "menu")

    def test_signing_in_by_hand_after_steam_changed_accounts_is_signed_in(self):
        state = core.State(me=NEW, token_account=NEW, signed_out=True)
        core.reduce(state, ("sign_in",), 1.0)
        core.reduce(state, ("relogin", OLD), 2.0)
        self.assertEqual(core.view(state).health, "ok")

    def test_a_new_game_start_tries_again_at_once(self):
        state = session_of(OLD, display={"a": "A"})
        core.reduce(state, login(NEW, 0.0), 0.0)
        relogins(state, 0.0)
        core.reduce(state, ("relogin", None, NO_SESSION), 1.0)
        core.reduce(state, login(NEW, 3.0), 3.0)
        self.assertEqual(relogins(state, 3.0), 1)


class Honest(unittest.TestCase):
    def test_a_queue_read_without_a_session_is_not_ready(self):
        state = session_of(OLD)
        core.reduce(state, ("queues", None, NO_SESSION), 1.0)
        self.assertEqual(core.view(state).health, "login")

    def test_a_network_failure_of_the_queues_is_not_about_signing_in(self):
        state = session_of(OLD)
        core.reduce(state, ("queues", None, "TransportError: timed out"), 1.0)
        self.assertEqual(core.view(state).health, "ok")

    def test_a_queue_read_with_a_session_clears_it(self):
        state = session_of(OLD, signed_out=True)
        core.reduce(state, ("queues", {"EUROPE": {}}), 1.0)
        self.assertEqual(core.view(state).health, "ok")

    def test_another_accounts_queue_read_proves_nothing(self):
        state = core.State(me=NEW, token_account=OLD, signed_out=True)
        core.reduce(state, ("queues", {"EUROPE": {}}), 1.0)
        self.assertEqual(core.view(state).health, "login")

    def test_an_expired_session_signs_in_as_soon_as_a_match_needs_it(self):
        state = session_of(OLD, display={"a": "A"}, phase="draft")
        core.reduce(state, ("instance", None, NO_SESSION), 1.0)
        self.assertEqual(relogins(state, 1.0), 1)
        core.reduce(state, ("relogin", OLD), 3.0)
        self.assertEqual(core.view(state).health, "ok")

    def test_an_expired_session_does_not_call_steam_from_an_idle_menu(self):
        state = session_of(OLD, display={"a": "A"})
        core.reduce(state, ("queues", None, NO_SESSION), 1.0)
        self.assertEqual(relogins(state, 2.0), 0)

    def test_new_tokens_of_another_account_do_not_read_as_signed_in(self):
        state = core.State(me=NEW, token_account=OLD, signed_out=True, phase="draft")
        core.reduce(state, ("me", OLD), 1.0)
        self.assertEqual(core.view(state).health, "login")


if __name__ == "__main__":
    unittest.main()
