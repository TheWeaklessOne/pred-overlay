"""Overlay v2 logic with scripted events and fakes (no network or game)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from predoverlay.errors import AuthenticationError
from predoverlay.overlay import cards, core, lanes, phases
from predoverlay.overlay.rows import row
from predoverlay.overlay.tracker import Tracker
from predoverlay.overlay.visibility import LOADING_SCREEN, Visibility, auto_visible

try:  # PySide6 is an optional extra; the pure tests must run without it.
    from predoverlay.overlay.window import choose_layout, preview_view
except ImportError:  # pragma: no cover - depends on the environment
    choose_layout = preview_view = None

ME = "00000000-0000-0000-0000-0000000000aa"
OTHERS = [f"00000000-0000-0000-0000-00000000000{i}" for i in range(9)]
GAME_A = "10000000-0000-0000-0000-000000000001"
GAME_B = "10000000-0000-0000-0000-000000000002"
FRIEND = OTHERS[0]


def phase(name, **extra):
    return ("log", {"phase": name, "at_ts": 100.0, **extra})


def seen(ids, match_id=GAME_A, in_game=True):
    return ("instance", {"in_game": in_game, "match_id": match_id, "player_ids": list(ids)})


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.state = core.State(
            me=ME, token_account=ME, display={"Wood": "Mourn"}, stats_source="predgg"
        )
        self.roster = OTHERS[:5] + [ME] + OTHERS[5:9]  # my block is the second one

    def feed(self, *events, now=200.0):
        out = []
        for event in events:
            out += core.reduce(self.state, event, now)
        return out

    def accept_roster(self, ids=None):
        ids = ids or self.roster
        self.feed(phase("draft"), ("tick",), seen(ids))
        return self.feed(seen(ids))

    def test_full_roster_is_accepted_after_two_equal_reads(self):
        self.feed(phase("draft"), ("tick",), seen(self.roster[:7]), seen(self.roster))
        self.assertEqual(self.state.roster, ())
        commands = self.feed(seen(self.roster))
        self.assertEqual(self.state.roster, tuple(self.roster))
        names = [c[0] for c in commands]
        self.assertEqual(names.count("card"), 10)
        self.assertIn("identities", names)
        self.assertIn("recent", names)

    def test_teams_split_by_block_with_my_uuid(self):
        self.accept_roster()
        snapshot = core.view(self.state)
        self.assertEqual(snapshot.mode, "5v5")
        self.assertIn(ME, [p.player_id for p in snapshot.allies])
        self.assertEqual([p.player_id for p in snapshot.enemies], self.roster[:5])
        self.assertTrue(next(p for p in snapshot.allies if p.player_id == ME).is_me)

    def test_three_versus_three_is_supported(self):
        small = OTHERS[:3] + [ME] + OTHERS[3:5]
        self.accept_roster(small)
        snapshot = core.view(self.state)
        self.assertEqual(
            (snapshot.mode, len(snapshot.allies), len(snapshot.enemies)), ("3v3", 3, 3)
        )

    def test_unsupported_size_or_missing_me_is_not_accepted(self):
        self.feed(phase("draft"), seen(OTHERS[:7]), seen(OTHERS[:7]))
        self.assertEqual(self.state.roster, ())
        self.assertEqual(core.view(self.state).status, "mode not supported")  # odd roster
        without_me = OTHERS[:9] + ["00000000-0000-0000-0000-0000000000bb"]
        self.feed(seen(without_me), seen(without_me))
        self.assertEqual(self.state.roster, ())
        self.assertEqual(core.view(self.state).status, "not your match")

    def test_any_even_roster_is_a_mode(self):
        duo = [ME] + OTHERS[:3]  # Crucible became 2v2 in 1.18
        self.feed(phase("draft"), seen(duo), seen(duo))
        self.assertEqual(self.state.roster, tuple(duo))
        view = core.view(self.state)
        self.assertEqual((view.mode, len(view.allies), len(view.enemies)), ("2v2", 2, 2))

    def test_dodge_and_new_match_start_a_new_epoch(self):
        self.accept_roster()
        epoch = self.state.epoch
        self.feed(seen([], in_game=False))
        self.assertEqual(self.state.roster, tuple(self.roster))  # one empty read is no proof
        self.feed(*[seen([], in_game=False)] * (core.MISSES_TO_MENU - 1))
        self.assertEqual((self.state.roster, self.state.epoch > epoch), ((), True))
        self.accept_roster()
        epoch = self.state.epoch
        self.feed(seen(self.roster, match_id=GAME_B))
        self.assertEqual((self.state.match_id, self.state.roster), (GAME_B, ()))
        self.assertGreater(self.state.epoch, epoch)

    def test_results_from_an_older_epoch_are_dropped(self):
        self.accept_roster()
        old = self.state.epoch
        self.feed(seen(self.roster, match_id=GAME_B))
        self.feed(("identities", old, {ME: "Me#1"}))
        self.assertEqual(self.state.names, {})

    def test_names_and_cards_reach_the_view(self):
        self.accept_roster()
        self.feed(("identities", self.state.epoch, {ME: "SamplePlayer#1002"}))
        self.feed(("card", ME, {"overall": {"games": 3}}, None), ("card", OTHERS[0], None, None))
        snapshot = core.view(self.state)
        me = next(p for p in snapshot.allies if p.is_me)
        self.assertEqual((me.name, me.stats_state), ("SamplePlayer#1002", "ready"))
        self.assertEqual(
            next(p for p in snapshot.enemies if p.player_id == OTHERS[0]).stats_state, "missing"
        )

    def test_without_pred_gg_no_cards_are_asked_for(self):
        self.state.stats_source = "omeda"
        commands = self.accept_roster()
        self.assertNotIn("card", [c[0] for c in commands])
        self.assertEqual({p.stats_state for p in core.view(self.state).allies}, {"off"})

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

    def test_card_errors_back_off_then_retry(self):
        self.accept_roster()
        self.feed(("card", OTHERS[1], None, "timeout"), now=200.0)
        retry = [c for c in self.feed(("tick",), now=201.0) if c == ("card", OTHERS[1])]
        self.assertEqual(retry, [])
        retry = [c for c in self.feed(("tick",), now=210.0) if c == ("card", OTHERS[1])]
        self.assertEqual(retry, [("card", OTHERS[1])])

    def test_no_session_in_the_menu_asks_for_sign_in_until_one_arrives(self):
        self.state.token_account = None
        self.feed(("me", None, "AuthenticationError: Not signed in"))
        self.assertEqual(core.view(self.state).health, "login")
        self.feed(("me", None, "URLError: offline"))
        self.assertEqual(core.view(self.state).health, "login")  # still not signed in
        self.feed(("relogin", ME))
        self.assertEqual(core.view(self.state).health, "ok")

    def test_sign_in_survives_phase_changes_until_a_session_arrives(self):
        self.state.token_account = None
        self.feed(("me", None, "AuthenticationError: Not signed in"))
        self.feed(phase("menu"), phase("accepted"))
        self.assertEqual(core.view(self.state).health, "login")
        commands = self.feed(("sign_in",))
        self.assertIn(("relogin",), commands)
        self.assertNotIn(("relogin",), self.feed(("sign_in",)))  # one at a time

    def test_offline_clears_on_the_next_good_read(self):
        self.accept_roster()
        self.feed(("instance", None, "URLError: timed out"))
        self.assertEqual(core.view(self.state).health, "offline")
        self.feed(("instance", {"in_game": True, "match_id": GAME_A, "player_ids": self.roster}))
        view = core.view(self.state)
        self.assertEqual((view.health, view.status), ("ok", core.STATUS[self.state.phase]))

    def test_a_finished_match_returns_to_the_menu(self):
        self.feed(phase("finished", reason="REASON_FINISHED", match_id=GAME_A))
        self.assertEqual(self.state.phase, "menu")

    def test_region_switch_changes_the_shown_queues(self):
        self.state.queues = {"EUROPE": {"RANKED": (1, 1)}, "NA": {"RANKED": (9, 5)}}
        before = self.state.version
        self.feed(("set_region", "NA"))
        self.assertGreater(self.state.version, before)
        self.assertEqual(core.view(self.state).queues, {"RANKED": (9, 5)})

    def test_polling_is_fast_until_accepted_then_slow(self):
        self.feed(phase("draft"))
        self.assertEqual(self.feed(("tick",), now=200.0), [("poll_instance",)])
        self.assertEqual(self.feed(("tick",), now=200.5), [])  # one read in flight
        self.feed(seen(self.roster), seen(self.roster), now=201.0)
        self.assertEqual(self.state.next_poll, 201.0 + core.POLL_FAST)
        polls = [c for c in self.feed(("tick",), now=204.0) if c == ("poll_instance",)]
        self.assertEqual(len(polls), 1)
        self.assertEqual(self.state.next_poll, 204.0 + core.POLL_SLOW)

    def test_tokens_are_renewed_only_in_the_menu(self):
        self.assertIn(("refresh_tokens",), self.feed(("tick",), now=0.0))
        self.feed(phase("draft"))
        later = self.feed(("tick",), now=core.REFRESH_EVERY + 1)
        self.assertNotIn(("refresh_tokens",), later)

    def test_a_lobby_gone_without_a_log_line_returns_to_the_menu(self):
        self.accept_roster()
        for _ in range(core.MISSES_TO_MENU - 1):
            self.feed(seen([], in_game=False))
        self.assertEqual(self.state.phase, "draft")
        self.feed(seen([], in_game=False), now=300.0)
        self.assertEqual((self.state.phase, self.state.phase_at), ("menu", 300.0))
        self.assertNotIn(("poll_instance",), self.feed(("tick",), now=400.0))

    def test_after_an_accept_the_instance_gets_a_grace_period(self):
        self.feed(phase("accepted"))  # at_ts 100
        self.feed(*[seen([], in_game=False)] * 5, now=100.0 + core.ACCEPT_GRACE - 1)
        self.assertEqual(self.state.phase, "accepted")
        self.feed(seen([], in_game=False), now=100.0 + core.ACCEPT_GRACE + 1)
        self.assertEqual(self.state.phase, "menu")

    def test_a_live_match_survives_a_short_backend_gap(self):
        self.feed(phase("loading"))
        self.feed(seen(self.roster), seen(self.roster), now=200.0)
        for second in range(0, int(core.MATCH_GRACE) - 2, 2):
            self.feed(seen([], in_game=False), now=210.0 + second)
        self.assertEqual((self.state.phase, len(self.state.roster)), ("loading", 10))
        self.feed(seen(self.roster), now=270.0)
        self.assertEqual(self.state.misses, 0)
        self.feed(*[seen([], in_game=False)] * core.MISSES_TO_MENU, now=280.0)
        self.feed(seen([], in_game=False), now=280.0 + core.MATCH_GRACE)
        self.assertEqual(self.state.phase, "menu")

    def test_an_empty_hero_catalog_backs_off(self):
        state = core.State(me=ME)
        core.reduce(state, ("tick",), 0.0)
        core.reduce(state, ("display", {}), 1.0)
        self.assertNotIn(("display",), core.reduce(state, ("tick",), 2.0))

    def test_menu_log_line_resets_the_match(self):
        self.accept_roster()
        self.assertEqual(self.feed(phase("menu")), [])
        self.assertEqual((self.state.phase, self.state.roster), ("menu", ()))

    def test_roster_waits_for_my_uuid(self):
        self.state.me = self.state.token_account = None
        self.feed(phase("draft"), seen(self.roster), seen(self.roster))
        self.assertEqual(self.state.roster, ())
        self.assertIn(("me",), self.feed(("tick",)))
        self.feed(("me", ME), seen(self.roster))
        self.assertEqual(self.state.roster, tuple(self.roster))

    def test_failed_lookups_are_retried_with_a_backoff(self):
        self.accept_roster()
        epoch = self.state.epoch
        self.feed(("identities", epoch, None), ("recent", epoch, None), now=200.0)
        self.assertEqual(self.feed(("tick",), now=201.0), [])
        again = self.feed(("tick",), now=200.0 + core.CARD_RETRY[0])
        self.assertIn(("identities", epoch, tuple(self.roster)), again)
        self.assertIn(("recent", epoch, GAME_A), again)
        self.feed(("identities", epoch, {ME: "Me#1"}), ("recent", epoch, []))
        self.assertEqual(self.feed(("tick",), now=1000.0), [])

    def test_switching_steam_accounts_reissues_the_tokens(self):
        second = "00000000-0000-0000-0000-0000000000cc"
        self.feed(phase("draft", user_id=second))
        self.assertEqual(self.state.me, second)
        self.assertIn(("relogin",), self.feed(("tick",)))
        self.assertNotIn(("relogin",), self.feed(("tick",)))  # one at a time
        self.feed(("relogin", second))
        self.assertEqual(self.state.token_account, second)
        self.assertNotIn(("relogin",), self.feed(("tick",), now=1000.0))

    def test_steam_on_another_account_backs_off(self):
        second = "00000000-0000-0000-0000-0000000000cc"
        self.feed(phase("draft", user_id=second), ("tick",))
        self.feed(("relogin", ME), now=200.0)  # Steam still signed in as the first account
        self.assertNotIn(("relogin",), self.feed(("tick",), now=201.0))
        self.assertIn(("relogin",), self.feed(("tick",), now=200.0 + core.CARD_RETRY[0]))

    def test_relogin_backoff_grows_and_waits_for_a_match(self):
        second = "00000000-0000-0000-0000-0000000000cc"
        self.feed(phase("menu"))
        self.state.me = second  # e.g. replayed from an old log
        self.assertNotIn(("relogin",), self.feed(("tick",)))  # not from the menu
        self.feed(phase("draft"))
        retries = []
        now = 200.0
        for _ in range(4):
            self.assertIn(("relogin",), self.feed(("tick",), now=now))
            self.feed(("relogin", ME), now=now)  # Steam stays on the other account
            retries.append(self.state.failures[("relogin",)][1] - now)
            now = self.state.failures[("relogin",)][1]
        self.assertEqual(retries, list(core.CARD_RETRY))

    def test_me_and_hero_names_are_retried(self):
        state = core.State()
        first = core.reduce(state, ("tick",), 0.0)
        self.assertIn(("me",), first)
        self.assertIn(("display",), first)
        core.reduce(state, ("me", None), 1.0)
        core.reduce(state, ("display", None), 1.0)
        self.assertNotIn(("me",), core.reduce(state, ("tick",), 2.0))
        retry = core.reduce(state, ("tick",), 1.0 + core.CARD_RETRY[0])
        self.assertIn(("me",), retry)
        self.assertIn(("display",), retry)

    def test_an_unrecorded_previous_match_keeps_numbering_and_is_asked_again(self):
        self.accept_roster()
        epoch, foe = self.state.epoch, self.roster[0]
        older = {"players": [{"player_id": ME, "team": "DAWN"}, {"player_id": foe, "team": "DUSK"}]}
        self.feed(("recent", epoch, [None, older]), now=200.0)
        (encounter,) = self.state.encounters[foe]
        self.assertEqual(encounter.matches_ago, 2)
        again = self.feed(("tick",), now=200.0 + core.CARD_RETRY[0])
        self.assertIn(("recent", epoch, GAME_A), again)


class OfficialProfileTests(unittest.TestCase):
    PROFILE = {"placed": True, "tier": 2, "division": 3, "vp": 45, "level": 329, "created": 0}

    def test_negative_vp_counts_as_the_bottom_of_its_tier(self):
        from predoverlay.overlay.rows import rank_points

        low = dict(self.PROFILE, vp=-25)
        self.assertEqual(rank_points(low), rank_points(dict(self.PROFILE, vp=0)))

    def test_official_rank_shows_while_the_card_still_loads(self):
        state = core.State(me=ME, token_account=ME, display={"x": "y"}, stats_source="predgg")
        roster = OTHERS[:5] + [ME] + OTHERS[5:9]
        for event in (phase("draft"), seen(roster), seen(roster)):
            commands = core.reduce(state, event, 200.0)
        self.assertIn("profiles", [c[0] for c in commands])
        me = next(p for p in core.view(state).allies if p.is_me)
        self.assertEqual(row(me)["rank"], "LOADING")
        core.reduce(state, ("profiles", state.epoch, {ME: self.PROFILE}), 201.0)
        me = next(p for p in core.view(state).allies if p.is_me)
        self.assertEqual(
            (me.state, me.stats_state, row(me)["rank"]), ("ready", "loading", "Gold II")
        )
        stale = {
            "source": "predgg",
            "rating": {"rank": "Bronze II"},
            "overall": {"games": 50, "winrate": 0.5, "kda": 2.0},
        }
        core.reduce(state, ("card", ME, stale, None), 202.0)
        me_row = row(next(p for p in core.view(state).allies if p.is_me))
        self.assertEqual(me_row["rank"], "Gold II")  # current season beats Pred.gg's
        self.assertEqual((me_row["stats"]["games"], me_row["stats"]["wr"]), (50, 50))

    def test_few_pred_gg_games_hide_win_rate_and_kda(self):
        card = {"source": "predgg", "overall": {"games": 7, "winrate": 0.9, "kda": 9.0}}
        view = core.PlayerView(OTHERS[0], "X#1", "ally", False, "ready", card, "ready")
        stats = row(view)["stats"]
        self.assertEqual((stats["games"], stats["wr"], stats["kda"]), (7, None, None))

    def test_official_rows_carry_no_statistics(self):
        view = core.PlayerView(OTHERS[0], "X#1", "ally", False, "ready", None, "off", self.PROFILE)
        data = row(view, now=10 * 86400)
        self.assertIsNone(data["stats"])
        self.assertEqual(
            (data["vp"], data["level"], data["age"], data["new"]), (45, 329, "10 DAYS", True)
        )

    def test_omeda_modes_use_pred_gg_words(self):
        from predoverlay import game

        doc = game.match_document(
            {
                "match_id": GAME_A,
                "ended_at": "1793244684",
                "game_mode": "TEAM_VS_TEAM",
                "players": [],
            }
        )
        self.assertEqual(doc["gameMode"], "STANDARD")

    def test_match_without_an_end_time_is_a_payload_error(self):
        from predoverlay import game
        from predoverlay.errors import PayloadError

        with self.assertRaises(PayloadError):
            game.match_document({"match_id": GAME_A, "ended_at": None, "players": []})

    def test_rows_show_the_name_the_game_shows(self):
        profile = dict(self.PROFILE, shown_name="Echo248")
        view = core.PlayerView(
            OTHERS[0], "Alias248#1003", "enemy", False, "ready", None, "off", profile
        )
        self.assertEqual(row(view)["name"], "Echo248")
        plain = core.PlayerView(
            OTHERS[0], "Alias248#1003", "enemy", False, "ready", None, "off", self.PROFILE
        )
        self.assertEqual(row(plain)["name"], "Alias248")

    def test_rank_names_age_and_team_average(self):
        from predoverlay.overlay.rows import account_age, omeda_rank, team_rank

        self.assertEqual(omeda_rank(self.PROFILE), "Gold II")
        platinum = {"placed": True, "division": 4, "tier": 1}
        self.assertEqual(omeda_rank(platinum), "Platinum III")  # tier 1 is the lowest step
        self.assertEqual(omeda_rank(dict(platinum, tier=3)), "Platinum I")
        self.assertEqual(omeda_rank({"placed": True, "division": 6, "tier": 1}), "Master")
        self.assertEqual(omeda_rank({"placed": True, "division": 9, "tier": 1}), "RANKED")
        self.assertIsNone(omeda_rank(dict(self.PROFILE, placed=False)))
        day = 86400
        self.assertEqual(account_age({"created": 0}, now=3 * 365.25 * day)[1], "3.0 YRS")
        self.assertEqual(account_age({"created": 0}, now=(2 * 365 + 335) * day)[1], "2.9 YRS")
        self.assertEqual(account_age({"created": 0}, now=(365 + 10) * day)[1], "1 YR")
        self.assertEqual(account_age({"created": 0}, now=200 * day)[1], "6 MOS")
        self.assertEqual(account_age({"created": 0}, now=35 * day)[1], "5 WKS")
        gold = [{"points": p} for p in (700, 800, 900)]  # Gold II at 0, 100, 200 VP over III
        self.assertEqual(team_rank(gold), "Gold I")
        unplaced = [{"points": None}] * 3
        self.assertIsNone(team_rank(gold[:2] + unplaced))  # two of five placed: no average
        self.assertEqual(team_rank(gold[:2]), "Gold II")  # a 2v2 team, both placed
        self.assertEqual(omeda_rank({"placed": True, "paragon": True, "division": 0}), "Paragon")

    def test_public_profiles_keep_only_rank_level_and_age(self):
        from predoverlay import game

        client = game.GameClient.__new__(game.GameClient)
        payload = {
            "profiles": {
                ME: {
                    "rank": {
                        "tier": 1.0,
                        "division": 3.0,
                        "vp": 97.0,
                        "isPlaced": True,
                        "isParagon": False,
                    },
                    "level": 123.0,
                    "createdTimestampMillis": "1712413470201",
                    "lastLoggedInProviderAccount": "STEAM",
                    "providerAccounts": [
                        {
                            "idProviderType": "EPIC",
                            "providerDisplayName": {"displayName": "Alias248"},
                        },
                        {
                            "idProviderType": "STEAM",
                            "providerDisplayName": {"displayName": "Echo248"},
                            "email": "x@example.com",
                        },
                    ],
                }
            }
        }
        with mock.patch.object(game.GameClient, "rpc", return_value=payload):
            found = client.public_profiles([ME])
        self.assertEqual(
            found[ME],
            {
                "placed": True,
                "paragon": False,
                "tier": 1,
                "division": 3,
                "vp": 97,
                "level": 123,
                "created": 1712413470,
                "shown_name": "Echo248",
            },
        )


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
        self.assertEqual(
            lanes.failure(("poll_instance",), "boom"),
            [("lane_error", "poll_instance", "boom"), ("instance", None, "boom")],
        )
        self.assertEqual(lanes.failure(("identities", 3, ()), "x")[1], ("identities", 3, None))
        self.assertEqual(lanes.failure(("me",), "x")[1], ("me", None, "x"))

    def test_a_failed_steam_refresh_is_not_repeated_on_every_poll(self):
        calls = []

        def fake(session_path, *, renew=True, margin=0):
            calls.append(renew)
            raise AuthenticationError("Not signed in")

        now = [1000.0]
        io = lanes.OmedaIO(None, Path("unused"), clock=lambda: now[0])
        with mock.patch.object(lanes, "stored_tokens", fake):
            for _ in range(5):
                with self.assertRaises(AuthenticationError):
                    io.clients()
            self.assertEqual(calls.count(True), 1)
            now[0] += lanes.RENEW_BACKOFF + 1
            with self.assertRaises(AuthenticationError):
                io.clients()
        self.assertEqual(calls.count(True), 2)

    def test_new_tokens_report_whose_they_are(self):
        io = lanes.OmedaIO(None, Path("unused"))
        first = {"game_token": "a", "social_token": "a"}
        second = {"game_token": "b", "social_token": "b"}
        official = mock.Mock()
        official.me.return_value = {"gameIdentities": [{"pragmaPlayerId": OTHERS[2]}]}
        game = mock.Mock()
        game.current_game.return_value = {"in_game": False}
        with (
            mock.patch.object(lanes, "stored_tokens", side_effect=[first, second]),
            mock.patch.object(lanes, "GameClient", return_value=game),
            mock.patch.object(lanes, "OfficialClient", return_value=official),
        ):
            self.assertEqual(io.handle(("poll_instance",)), [("instance", {"in_game": False})])
            events = io.handle(("poll_instance",))  # a refresh switched the account
        self.assertEqual(events[0], ("me", OTHERS[2]))

    def test_identities_skip_rows_without_a_name(self):
        io = lanes.OmedaIO(None, Path("unused"))
        official = mock.Mock()
        official.identities.return_value = {
            "results": [
                {
                    "pragmaPlayerId": ME,
                    "pragmaDisplayName": {"displayName": "Me", "discriminator": "1"},
                },
                {"pragmaPlayerId": OTHERS[0], "pragmaDisplayName": None},
            ]
        }
        with mock.patch.object(io, "clients", return_value=(mock.Mock(), official)):
            (event,) = io.handle(("identities", 1, (ME, OTHERS[0])))
        self.assertEqual(event, ("identities", 1, {ME: "Me#1"}))

    def test_damaged_cache_entry_is_fetched_again(self):
        with tempfile.TemporaryDirectory() as folder:
            io = lanes.OmedaIO(None, folder)
            (Path(folder) / f"{GAME_A}.json").write_text("{broken", encoding="utf-8")
            game = mock.Mock()
            game.match_details.return_value = {"players": []}
            self.assertEqual(io.details(game, GAME_A), {"players": []})
            self.assertEqual(io.details(game, GAME_A), {"players": []})
            game.match_details.assert_called_once()


class TrackerTests(unittest.TestCase):
    def test_survives_a_failing_log_follower(self):
        follower = mock.Mock()
        follower.replay.return_value = []
        follower.poll.side_effect = PermissionError("log is being rotated")
        messages = []
        tracker = Tracker({}, follower=follower, log=messages.append, clock=lambda: 0.0)
        tracker.step()
        tracker.step()  # still ticking: the menu asks for its token refresh
        self.assertEqual(len(messages), 1)  # a repeating failure is logged once
        self.assertIn("PermissionError", messages[0])
        self.assertEqual(tracker.state.next_refresh, core.REFRESH_EVERY)


@unittest.skipIf(choose_layout is None, "PySide6 is not installed")
class PaintTests(unittest.TestCase):
    def test_card_and_panel_paint_in_both_modes(self):
        from PySide6.QtGui import QGuiApplication, QImage, QPainter

        from predoverlay.overlay import paint

        _app = QGuiApplication.instance() or QGuiApplication(["test", "-platform", "offscreen"])
        view = preview_view("loading", 0.0, 1)
        rows = [row(p) for p in view.allies]
        image = QImage(700, 700, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        try:
            for source in ("omeda", "predgg"):
                paint.paint_stack(painter, 640, rows, rows, paint.Motion(), "5v5", source)
                seen = {"hero": "STEEL", "role": "OFFLANE"}
                paint.paint_card(painter, rows[0], paint.Motion(), source, seen)
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
            self.assertNotEqual(row(player)["name"], "…")


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


def card_player(ratings=None):
    def match(won, hero="Riktor"):
        return {
            "match": {
                "id": "m-" + str(won),
                "endTime": "2026-10-29T23:41:31Z",
                "gameMode": "STANDARD",
                "winningTeam": "DAWN" if won else "DUSK",
                "matchPlayers": [
                    {
                        "player": {"id": FRIEND},
                        "team": "DAWN",
                        "role": "JUNGLE",
                        "hero": {"slug": "x", "name": hero},
                        "kills": 1,
                        "deaths": 2,
                        "assists": 3,
                    }
                ],
            }
        }

    return {
        "id": FRIEND,
        "name": "orbitfox",
        "isNameConsole": False,
        "ratings": ratings
        if ratings is not None
        else [
            {
                "points": 645.0,
                "unranked": False,
                "rank": {"name": "Gold III"},
                "rating": {"name": "Season 1 - Split 5"},
            },
            {
                "points": 677.0,
                "unranked": False,
                "rank": {"name": "Gold III"},
                "rating": {"name": "Season 0"},
            },
        ],
        "generalStatistic": {
            "result": {
                "matchesPlayed": 10,
                "matchesWon": 6,
                "totalKills": 10,
                "totalDeaths": 5,
                "totalAssists": 10,
            }
        },
        "roleStatistics": {"results": [{"role": "JUNGLE", "matchesPlayed": 6, "matchesWon": 4}]},
        "heroStatistics": {
            "results": [
                {
                    "hero": {"slug": "akeron", "name": "DemonKing"},
                    "matchesPlayed": 4,
                    "matchesWon": 3,
                    "totalKills": 8,
                    "totalDeaths": 2,
                    "totalAssists": 4,
                },
                {
                    "hero": {"slug": "riktor", "name": "Riktor"},
                    "matchesPlayed": 6,
                    "matchesWon": 3,
                    "totalKills": 2,
                    "totalDeaths": 6,
                    "totalAssists": 10,
                },
            ]
        },
        "matchesPaginated": {"results": [match(True), match(False, "DemonKing")]},
    }


class CardTests(unittest.TestCase):
    def test_build_uses_newest_season_and_display_names(self):
        card = cards.build(card_player(), display={"DemonKing": "Akeron"})
        self.assertEqual(
            (card["rating"]["rank"], card["rating"]["season"]), ("Gold III", "Season 1 - Split 5")
        )
        self.assertEqual(card["overall"], {"games": 10, "winrate": 0.6, "kda": 4.0})
        self.assertEqual(card["mains"][0]["hero"], "Riktor")
        self.assertEqual((card["form"], card["recent"][1]["hero"]), ("WL", "Akeron"))
        self.assertEqual((card["source"], len(card["heroes"])), ("predgg", 2))
        self.assertIsNone(cards.build(card_player(ratings=[]))["rating"])


@unittest.skipIf(choose_layout is None, "PySide6 is not installed")
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
        import dataclasses

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
        self.overlay.reader.wanted = lambda match_id: False
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

    def test_crossing_the_gap_to_the_next_row_flies_the_card(self):
        self.tracker.tab_held = True
        self.step(6)
        self.cursor[0] = self.gap("ally", 0)  # between rows for a moment
        between = self.step(3)
        self.assertTrue(all(s[:2] == ("card", True) for s in between))
        self.cursor[0] = self.at("ally", 1)
        self.step(1)
        self.assertIsNotNone(self.overlay.card_move)  # flies from where it was
        self.assertEqual(self.overlay.card[2], ("ally", 1))
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
        import dataclasses

        return dataclasses.replace(view, **extra)

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
        self.assertGreater(self.overlay.design_size[1], 400)  # the window is at full size at once
        self.step(10)
        self.assertIsNone(self.overlay.grow)

    def test_settled_panel_stops_the_frame_timer(self):
        self.tracker.snapshot = self.view("draft")
        self.step(40)
        self.assertFalse(self.overlay.frame.isActive())


class ChangedGroupsTests(unittest.TestCase):
    def test_only_the_fields_that_changed(self):
        from predoverlay.overlay.rows import changed_groups

        view = core.PlayerView(OTHERS[0], None, "ally", False, "loading", None, "off")
        loading = row(view)
        named = row(core.PlayerView(OTHERS[0], "Riktor#1", "ally", False, "loading", None, "off"))
        self.assertEqual(changed_groups(loading, named), {"name"})
        profile = {"placed": True, "division": 3, "tier": 2, "vp": 40, "level": 99, "created": 0}
        ranked = row(
            core.PlayerView(OTHERS[0], "Riktor#1", "ally", False, "ready", None, "off", profile)
        )
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
            ("e", 1): "ORBIT01987",
            ("e", 2): "NATHANIEL-XRQ",
        }
        names = {
            "a": "QUIET LANTERNKEEPER",
            "b": "ORBITO1987",
            "c": "NATHANIEL_XRQ",
            "d": "KORVEN17",
        }
        self.assertEqual(
            scoreboard.assign(readings, names), {("e", 0): "a", ("e", 1): "b", ("e", 2): "c"}
        )

    def test_reader_maps_rows_to_players_roles_and_heroes(self):
        from predoverlay.overlay import scoreboard

        lines = {
            b"0": ["BRAVOX", "STEEL"],
            b"1": ["LIEUTENANT", "FENG MAO"],
            b"2": ["WANDERTRAINER", "VALMONT"],
            b"3": ["SAMPLEPLAYERONE", "MOURN"],
            b"4": [],
        }  # a one-letter name OCR misses
        reader = scoreboard.Reader(read=lambda png: lines[png], clock=lambda: 100.0)
        crops = {("ally", i): str(i).encode() for i in range(5)}
        names = {
            "ally": {
                "m": "BRAVOX",
                "s": "LIEUTENANT",
                "b": "WANDERTRAINER",
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
    def test_a_clear_other_reading_is_not_forced_onto_the_last_player(self):
        from predoverlay.overlay import scoreboard

        lines = {
            b"0": ["BRAVOX"],
            b"1": ["LIEUTENANT"],
            b"2": ["WANDERTRAINER"],
            b"3": ["SAMPLEPLAYERONE"],
            b"4": ["SOMEONE ELSE"],
        }
        reader = scoreboard.Reader(read=lambda png: lines[png], clock=lambda: 0.0)
        names = {
            "ally": {
                "m": "BRAVOX",
                "s": "LIEUTENANT",
                "b": "WANDERTRAINER",
                "t": "SAMPLEPLAYERONE",
                "x": "X",
            }
        }
        reader.match_id, reader.attempts = GAME_A, 1
        reader._run(GAME_A, lambda: {("ally", i): str(i).encode() for i in range(5)}, names)
        self.assertIsNone(reader.player_at(GAME_A, ("ally", 4)))

    def test_alike_names_and_non_scoreboard_screens(self):
        from predoverlay.overlay import scoreboard

        reader = scoreboard.Reader(read=lambda png: ["STORMBOUND369"], clock=lambda: 0.0)
        names = {"enemy": {"a": "Stormbound369", "b": "Stormbound369"}}
        reader.match_id, reader.attempts = GAME_A, 1
        reader._run(GAME_A, {("enemy", 0): b"x", ("enemy", 1): b"y"}, names)
        self.assertEqual(reader.rows, {})  # two alike names: nothing assigned
        self.assertEqual(reader.attempts, 0)  # and the reading is not counted

    def test_heroes_snap_to_known_names_and_lookalikes_fold(self):
        from predoverlay.overlay import scoreboard

        self.assertEqual(scoreboard.snap_hero("vvNG MAO", ["Feng Mao", "Grux"]), "FENG MAO")
        self.assertIsNone(scoreboard.snap_hero("QQQQ", ["Feng Mao", "Grux"]))
        self.assertEqual(scoreboard.similarity("ORBIT01987", "ORBITO1987"), 1.0)


class ReconnectTests(unittest.TestCase):
    def test_joining_the_match_map_from_draft_is_the_match(self):
        state = core.State(me=ME, token_account=ME)
        core.reduce(state, phase("draft"), 100.0)
        core.reduce(state, phase("joined", map="Draft", at_ts=101.0), 101.0)
        self.assertEqual(state.phase, "draft")  # the draft map itself
        core.reduce(state, phase("joined", map="TwilightRC1", at_ts=200.0), 200.0)
        self.assertEqual((state.phase, state.joined), ("loading", True))
        core.reduce(state, phase("presence", at_ts=210.0), 210.0)
        self.assertEqual(state.started_at, 210.0)


class QueueTests(unittest.TestCase):
    def test_counts_per_region_and_queue(self):
        from predoverlay import game

        def queue(region, name, players, parties):
            key = {"extMatchmakingKey": {"regionName": region, "queueName": name}}
            return {
                "matchmakingQueueKey": key,
                "playersInQueue": float(players),
                "partiesInQueue": float(parties),
            }

        payload = {
            "queueInfo": [
                queue("EUROPE", "RANKED", 42, 28),
                queue("EUROPE", "ARAM", 5, 5),
                queue("NA", "RANKED", 40, 34),
                {"matchmakingQueueKey": {}},
            ]
        }
        self.assertEqual(
            game.queue_counts(payload),
            {"EUROPE": {"RANKED": (42, 28), "ARAM": (5, 5)}, "NA": {"RANKED": (40, 34)}},
        )

    def test_queues_are_read_every_30_seconds_in_the_menu_only(self):
        state = core.State(me=ME, token_account=ME, display={"x": "y"}, region="NA")
        self.assertIn(("queues",), core.reduce(state, ("tick",), 0.0))
        self.assertNotIn(("queues",), core.reduce(state, ("tick",), 1.0))  # in flight
        core.reduce(state, ("queues", {"NA": {"RANKED": (40, 34)}, "EUROPE": {}}), 2.0)
        self.assertNotIn(("queues",), core.reduce(state, ("tick",), 10.0))
        self.assertIn(("queues",), core.reduce(state, ("tick",), core.QUEUES_EVERY + 1))
        view = core.view(state)
        self.assertEqual(
            (view.region, view.queues, view.queues_at), ("NA", {"RANKED": (40, 34)}, 2.0)
        )
        core.reduce(state, ("queues", None), 40.0)  # a failure keeps the last counts
        core.reduce(state, phase("draft"), 50.0)
        self.assertNotIn(("queues",), core.reduce(state, ("tick",), 200.0))

    def test_menu_row_keeps_a_fixed_width_so_the_badge_never_moves(self):
        if choose_layout is None:
            self.skipTest("PySide6 is not installed")
        from PySide6.QtGui import QGuiApplication

        from predoverlay.overlay import paint

        _app = QGuiApplication.instance() or QGuiApplication(["test", "-platform", "offscreen"])
        self.assertGreater(paint.menu_width("EUROPE"), paint.BADGE_W)
        strip = paint.new_strip()
        strip.sync({"RANKED": (42, 28)}, 1.0, "EUROPE", 1.0)
        strip.snap()
        widest = paint.menu_width("EUROPE") - paint.QUEUE_GAP - paint.BADGE_W
        self.assertLessEqual(strip.frame(2.0).width, widest)


def _measure(text_, kind):
    return 10.0 * len(text_)


QUEUES = {
    "RANKED": (42, 0),
    "TEAM_VS_TEAM": (26, 0),
    "ARAM": (5, 0),
    "CRUCIBLE": (5, 0),
    "TEAM_VS_AI": (0, 0),
}


class StripMotionTests(unittest.TestCase):
    """The menu strip's motion model (pure, no Qt)."""

    def strip(self, queues=QUEUES, t=100.0):
        from predoverlay.overlay import motion

        s = motion.Strip(_measure)
        s.sync(queues, t, "EUROPE", t)
        s.snap()
        return s

    @staticmethod
    def keys(frame, min_alpha=0.5):
        return [i.key for i in frame.items if i.alpha >= min_alpha]

    def test_settled_strip_is_idle_and_skips_empty_bot_games(self):
        s = self.strip()
        self.assertLessEqual(s.busy_until(), 100.0)
        self.assertEqual(self.keys(s.frame(100.0)), ["RANKED", "TEAM_VS_TEAM", "ARAM", "CRUCIBLE"])

    def test_mode_appears_by_widening_then_fading_in(self):
        from predoverlay.overlay import motion

        s = self.strip()
        w0 = s.frame(100.0).width
        s.sync({**QUEUES, "TEAM_VS_AI": (3, 0)}, 101.0, "EUROPE", 101.0)
        mid = s.frame(101.0 + motion.SLOT_FADE_DELAY / 2)
        self.assertEqual(
            (mid.items[-1].key, mid.items[-1].alpha), ("TEAM_VS_AI", 0.0)
        )  # room first
        self.assertGreater(mid.width, w0)
        widths = [s.frame(101.0 + k / 100).width for k in range(40)]
        self.assertEqual(widths, sorted(widths))
        self.assertEqual(s.frame(s.busy_until()).items[-1].alpha, 1.0)

    def test_bot_games_linger_at_zero_before_leaving(self):
        from predoverlay.overlay import motion

        s = self.strip({**QUEUES, "TEAM_VS_AI": (3, 0)})
        s.sync(QUEUES, 130.0, "EUROPE", 130.0)  # the count drops to 0
        self.assertIn("TEAM_VS_AI", self.keys(s.frame(131.0)))
        self.assertEqual(s.frame(131.0).items[-1].zero, 1.0)
        s.sync(QUEUES, 160.0, "EUROPE", 130.0 + motion.LINGER + 0.01)
        later = 130.0 + motion.LINGER + 0.01 + motion.SLOT_CLOSE_DELAY + motion.SLOT_CLOSE + 0.01
        s.sync(QUEUES, 160.0, "EUROPE", later)
        self.assertNotIn("TEAM_VS_AI", [i.key for i in s.frame(later).items])

    def test_middle_item_leaving_keeps_one_separator(self):
        s = self.strip()
        s.sync({k: v for k, v in QUEUES.items() if k != "TEAM_VS_TEAM"}, 101.0, "EUROPE", 101.0)
        aram = next(i for i in s.frame(101.15).items if i.key == "ARAM")
        self.assertAlmostEqual(aram.sep_alpha, 1.0)

    def test_interrupted_exit_reverses_without_a_jump(self):
        s = self.strip({**QUEUES, "TEAM_VS_AI": (3, 0)})
        s.sync({k: v for k, v in QUEUES.items() if k != "TEAM_VS_AI"}, 101.0, "EUROPE", 101.0)
        before = s.frame(101.15).width
        s.sync({**QUEUES, "TEAM_VS_AI": (4, 0)}, 101.15, "EUROPE", 101.15)
        self.assertAlmostEqual(s.frame(101.15).width, before, places=6)

    def test_counts_roll_only_changed_digits_and_stale_counts_close(self):
        from predoverlay.overlay import motion

        s = self.strip()
        s.sync({**QUEUES, "RANKED": (45, 0)}, 101.0, "EUROPE", 101.0)
        digits = s.frame(101.05).items[0].digits
        self.assertEqual((digits[0][2], digits[1][3]), (1.0, 1))  # the 4 stays; 2 -> 5 rolls up
        self.assertLess(digits[1][2], 1.0)
        t = 101.0 + motion.STALE + 0.05
        s.sync({**QUEUES, "RANKED": (45, 0)}, 101.0, "EUROPE", t)
        self.assertEqual(s.frame(t + motion.DRAWER_CLOSE).open, 0.0)

    def test_hidden_panel_snaps_and_numbers_compact(self):
        from predoverlay.overlay import motion

        s = self.strip()
        s.sync({**QUEUES, "RANKED": (50, 0)}, 101.0, "EUROPE", 101.0, revealed=False)
        self.assertLessEqual(s.busy_until(), 101.0)
        self.assertEqual([motion.fmt(n) for n in (9999, 12345, 250000)], ["9999", "12K", "99K+"])

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
