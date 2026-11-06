"""The game runs on another Steam account than the overlay's session."""

import unittest

from predoverlay import steam_ticket
from predoverlay.errors import PredError
from predoverlay.overlay import core

GAME = "00000000-0000-0000-0000-00000000000a"
OLD = "00000000-0000-0000-0000-00000000000b"
EMPTY = {"in_game": False, "match_id": None, "player_ids": ()}


class Session(unittest.TestCase):
    def state(self, **fields):
        return core.State(me=GAME, token_account=OLD, phase="draft", **fields)

    def test_an_empty_instance_of_another_account_does_not_end_the_draft(self):
        state = self.state()
        for moment in range(1, 40):
            core.reduce(state, ("instance", EMPTY), float(moment))
        self.assertEqual(state.phase, "draft")

    def test_it_does_not_count_as_signed_in(self):
        state = self.state(signed_out=True)
        core.reduce(state, ("instance", EMPTY), 1.0)
        self.assertTrue(state.signed_out)
        self.assertEqual(core.view(state).health, "login")

    def test_it_still_clears_offline(self):
        state = self.state(offline=True)
        core.reduce(state, ("instance", EMPTY), 1.0)
        self.assertFalse(state.offline)

    def test_the_new_session_takes_over(self):
        state = self.state(signed_out=True)
        core.reduce(state, ("relogin", GAME), 1.0)
        self.assertEqual(state.token_account, GAME)
        self.assertFalse(state.signed_out)
        for moment in range(2, 40):
            core.reduce(state, ("instance", EMPTY), float(moment))
        self.assertEqual(state.phase, "menu")  # now an empty instance means a dodge


class Api:
    def __init__(self, running):
        self.running = running

    def SteamAPI_IsSteamRunning(self):
        return self.running


class Start(unittest.TestCase):
    def test_a_working_api_beats_the_registry(self):
        steam_ticket._start(Api(False), lambda: True)

    def test_not_running_and_no_api(self):
        with self.assertRaisesRegex(PredError, "Steam is not running"):
            steam_ticket._start(Api(False), lambda: False)

    def test_a_flat_init_error_without_steam_reads_the_same(self):
        def init():
            raise PredError("Steam API did not start (ESteamAPIInitResult 2: no client).")

        with self.assertRaisesRegex(PredError, "Steam is not running"):
            steam_ticket._start(Api(False), init)

    def test_running_but_refused(self):
        with self.assertRaisesRegex(PredError, "must own Predecessor"):
            steam_ticket._start(Api(True), lambda: False)


if __name__ == "__main__":
    unittest.main()
