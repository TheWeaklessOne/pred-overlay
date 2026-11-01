"""Game-backend client and live-roster logic, with a fake transport. No network."""

import unittest
from unittest.mock import Mock

from predoverlay.errors import AuthenticationError, PredError
from predoverlay.game import GameClient, login

SOCIAL = "aa.bb.cc"
GAME = "dd.ee.ff"
A = "00000000-0000-0000-0000-00000000000a"
B = "00000000-0000-0000-0000-00000000000b"


def rpc_reply(payload=None, *, service_error=None):
    if service_error is not None:
        return {"serviceError": {"error": service_error}}
    return {"response": {"payload": payload}}


class LoginTests(unittest.TestCase):
    def test_login_waits_for_queue_then_returns_both_tokens(self):
        game = Mock()
        game.request.side_effect = [
            {"isAllowedIn": False, "pollNextDuration": 500, "token": ""},
            {"isAllowedIn": True, "token": "pass"},
        ]
        social = Mock()
        social.request.return_value = {
            "pragmaTokens": {"pragmaSocialToken": SOCIAL, "pragmaGameToken": GAME}
        }
        slept = []
        tokens = login("ticket-hex", social=social, game=game, sleep=slept.append)
        self.assertEqual(tokens, {"social_token": SOCIAL, "game_token": GAME})
        self.assertEqual(slept, [0.5])
        body = social.request.call_args.args[1]
        self.assertEqual(body["providerToken"], "ticket-hex")
        self.assertEqual(body["loginQueuePassToken"], "pass")

    def test_login_without_game_token_is_rejected(self):
        game = Mock()
        game.request.return_value = {"isAllowedIn": True, "token": "p"}
        social = Mock()
        social.request.return_value = {"pragmaTokens": {"pragmaSocialToken": SOCIAL}}
        with self.assertRaises(AuthenticationError):
            login("t", social=social, game=game)


class GameClientTests(unittest.TestCase):
    def make(self, payload=None, **kw):
        client = GameClient.__new__(GameClient)
        client.token = GAME
        client.transport = Mock()
        client.transport.request.return_value = rpc_reply(payload, **kw)
        return client

    def test_only_audited_rpcs_are_allowed(self):
        client = self.make()
        with self.assertRaises(PredError):
            client.rpc("GameInstanceRpc.CreateV1Request", {})
        client.transport.request.assert_not_called()

    def test_current_game_returns_match_and_player_ids_without_connection_details(self):
        client = self.make(
            {
                "isInGameInstance": True,
                "gameInstance": {
                    "gameInstanceId": A,
                    "players": [{"playerId": B}, {"playerId": A}, {"playerId": B}],
                    "hostConnectionDetails": {"connectionToken": "secret"},
                },
            }
        )
        current = client.current_game()
        self.assertEqual(current, {"in_game": True, "match_id": A, "player_ids": [B, A]})

    def test_current_game_when_idle(self):
        current = self.make({"isInGameInstance": False}).current_game()
        self.assertEqual(current, {"in_game": False, "match_id": None, "player_ids": []})

    def test_match_details_maps_teams_and_hides_console_names(self):
        client = self.make(
            {
                "matchDetails": {
                    "matchId": A,
                    "winningTeamId": 1,
                    "gameMode": "TEAM_VS_TEAM",
                    "gameEndedEpochSeconds": "123",
                    "players": [
                        {
                            "teamId": 0,
                            "playerId": A,
                            "playerName": "Me",
                            "playerPlatform": "WINDOWS",
                            "roleName": "Carry",
                            "heroName": "Murdock",
                            "kills": 1,
                            "deaths": 2,
                            "assists": 3,
                        },
                        {
                            "teamId": 1,
                            "playerId": B,
                            "playerName": "HIDDEN",
                            "playerPlatform": "PLAYSTATION5",
                            "roleName": "Support",
                            "heroName": "Riktor",
                            "kills": 0,
                            "deaths": 0,
                            "assists": 0,
                        },
                    ],
                }
            }
        )
        details = client.match_details(A)
        self.assertEqual(details["winning_team"], "DUSK")
        me, other = details["players"]
        self.assertEqual((me["team"], me["role"], me["name"]), ("DAWN", "CARRY", "Me"))
        self.assertEqual(
            (other["team"], other["name"], other["platform"]), ("DUSK", None, "PLAYSTATION5")
        )

    def test_match_details_none_while_match_not_recorded(self):
        self.assertIsNone(self.make({}).match_details(A))


if __name__ == "__main__":
    unittest.main()
