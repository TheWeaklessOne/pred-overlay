"""The screen-to-Pred.gg flow of the core: names read off the screen, looked up, shown."""

import unittest

from predoverlay.overlay import core, lookup

ME = "00000000-0000-0000-0000-00000000000a"
T0 = 1_000_000.0
DRAFT_NAMES = ["SILVERFALCON", "HULGARVENTAS4", "BRENK141", "WAN ZOGODUKE-", "URVAK"]
TOP = ["GTame52", "iVaaK--Dimbrandt", "tiikvarn", "La-Venuta-4", "Lam-ka72"]
BOTTOM = ["wanzogoduke_", "hulgarventas4", "BrenK141", "SilverFalcon", "UrVak"]


def draft_texts(names, versions=2):
    texts = {("ally", i): [n] * versions for i, n in enumerate(names)}
    texts[("anchor", 0)] = ["ALLY", "ALLY"]
    return texts


def loading_texts(top, bottom, versions=2):
    texts = {("top", i): [n] * versions for i, n in enumerate(top)}
    texts.update({("bottom", i): [n] * versions for i, n in enumerate(bottom)})
    return texts


def profile(rank="Gold II", points=None):
    return {
        "current": {"rank": rank, "points": points, "season": "S6"},
        "peak": None,
        "seasons": (),
        "main": None,
        "last_played": None,
        "first_played": None,
    }


class Flow(unittest.TestCase):
    def setUp(self):
        self.state = core.State()
        self.now = T0
        self.sent = []

    def step(self, *event, dt=0.0):
        self.now += dt
        commands = core.reduce(self.state, event, self.now)
        self.sent += commands
        return commands

    def of(self, kind):
        return [c for c in self.sent if c[0] == kind]

    def enter_draft(self):
        self.step("display", {"Steel": "Steel"})
        self.step("log", {"phase": "accepted", "at_ts": self.now})
        self.step(
            "log",
            {"phase": "draft", "at_ts": self.now, "user_id": ME, "user_name": "SilverFalcon"},
        )
        return self.state.phase_at

    def read_draft(self, names=DRAFT_NAMES):
        return self.step("screen", "draft", self.state.phase_at, draft_texts(names))

    def answer_search(self, found):
        ((_, epoch, _),) = self.of("search")[-1:]
        return self.step("found", epoch, found)


class DraftTests(Flow):
    def test_my_row_needs_no_search_and_the_others_are_looked_up_together(self):
        self.enter_draft()
        self.read_draft()
        searches = self.of("search")
        self.assertEqual(len(searches), 1)
        self.assertEqual(sorted(searches[0][2]), ["a1", "a2", "a3", "a4"])  # a0 is me
        self.assertEqual(self.state.ids["a0"]["pid"], ME)
        self.assertEqual(self.of("recent"), [("recent", self.state.epoch, ME)])
        self.assertEqual(self.of("profiles"), [])  # they wait for the search

    def test_a_name_needs_two_agreeing_versions(self):
        self.enter_draft()
        self.step("screen", "draft", self.state.phase_at, draft_texts(DRAFT_NAMES, versions=1))
        self.assertFalse(self.of("search"))
        self.read_draft()
        self.assertTrue(self.of("search"))

    def test_found_players_get_their_profiles_and_the_panel_shows_them(self):
        self.enter_draft()
        self.read_draft()
        self.answer_search(
            {
                "a1": {"player_id": "p1", "name": "hulgarventas4"},
                "a2": None,
                "a3": {"player_id": "p3", "name": "wanzogoduke_"},
                "a4": {"player_id": "p4", "name": "UrVak"},
            }
        )
        self.assertEqual(len(self.of("profiles")), 1)  # me with the others, in one request
        self.assertEqual(set(self.of("profiles")[0][2]), {ME, "p1", "p3", "p4"})
        self.step("profiles", self.state.epoch, {ME: profile(), "p1": profile("Gold I")})
        view = core.view(self.state)
        by_name = {p.read_as: p for p in view.allies}
        self.assertEqual(by_name["HULGARVENTAS4"].state, "ready")
        self.assertEqual(by_name["HULGARVENTAS4"].name, "hulgarventas4")  # as Pred.gg spells it
        self.assertEqual(by_name["BRENK141"].state, "error")  # not on Pred.gg: no data
        self.assertEqual(by_name["URVAK"].state, "error")  # asked for, and Pred.gg had none
        self.assertTrue(by_name["SILVERFALCON"].is_me)
        # never a name: a clean reading Pred.gg does not know is most likely a console player
        self.assertEqual(by_name["BRENK141"].name, core.LIKELY_CONSOLE)
        self.assertTrue(by_name["BRENK141"].tag)  # but a tag to tell them apart
        self.assertEqual(view.mode, "5v5")
        self.assertEqual(view.enemies, ())

    def test_names_from_my_recent_matches_need_no_search(self):
        self.enter_draft()
        details = [
            {
                "match_id": "m1",
                "ended_at": T0 - 600,
                "winning_team": "DAWN",
                "players": [
                    {
                        "player_id": ME,
                        "name": "SilverFalcon",
                        "team": "DAWN",
                        "hero": "x",
                        "kda": (1, 2, 3),
                    },
                    {
                        "player_id": "p1",
                        "name": "hulgarventas4",
                        "team": "DAWN",
                        "hero": "x",
                        "kda": (1, 2, 3),
                    },
                ],
            }
        ]
        summaries = [
            {"match_id": "m1", "ended_at": T0 - 600, "won": True, "vp": None, "kda": (1, 2, 3)}
        ]
        self.step("recent", self.state.epoch, (summaries, details))
        self.read_draft()
        self.assertEqual(self.state.ids["a1"]["pid"], "p1")
        self.assertNotIn("a1", self.of("search")[0][2])
        view = core.view(self.state)
        me = next(p for p in view.allies if p.is_me)
        self.assertEqual(me.today["form"], ("W",))
        self.step("profiles", self.state.epoch, {ME: profile(), "p1": profile()})
        view = core.view(self.state)
        mate = next(p for p in view.allies if self.state.ids[p.player_id]["pid"] == "p1")
        self.assertEqual(mate.name, "hulgarventas4")  # Pred.gg's spelling, no search needed
        self.assertEqual([(e.side, e.won) for e in mate.encounters], [("with", True)])

    def test_a_reading_of_another_phase_or_a_closed_game_is_ignored(self):
        key = self.enter_draft()
        self.step("screen", "draft", key - 1, draft_texts(DRAFT_NAMES))
        self.step("screen", "loading", key - 1, loading_texts(TOP, BOTTOM))
        self.assertEqual(self.state.seats, {})
        self.step("game", False)
        self.step("screen", "draft", key, draft_texts(DRAFT_NAMES))
        self.assertEqual(self.state.seats, {})

    def test_the_loading_screen_is_taken_before_the_log_says_so(self):
        key = self.enter_draft()  # the pixels show the loading screen; the log says draft
        self.step("screen", "loading", key, loading_texts(TOP, BOTTOM))
        teams = sorted(s["team"] for s in self.state.seats.values())
        self.assertEqual(teams, ["ally"] * 5 + ["enemy"] * 5)

    def test_a_changed_name_on_a_row_is_looked_up_again(self):
        self.enter_draft()
        self.read_draft()
        self.answer_search({s: None for s in ("a1", "a2", "a3", "a4")})
        names = list(DRAFT_NAMES)
        names[2] = "SOMEONEELSE"
        for _ in range(core.MISSED):
            self.read_draft(names)
        new = [k for k, s in self.state.seats.items() if s["reading"] == "SOMEONEELSE"]
        self.assertEqual(list(self.of("search")[-1][2]), new)
        self.assertTrue(self.state.seats["a2"]["gone"])  # the old reading: not seen again
        shown = [p.read_as for p in core.view(self.state).allies]
        self.assertEqual(len(shown), 5)
        self.assertNotIn("BRENK141", shown)

    def test_rows_that_swap_keep_their_players(self):
        self.enter_draft()
        self.read_draft()
        self.answer_search({s: None for s in ("a1", "a2", "a3", "a4")})
        swapped = [DRAFT_NAMES[i] for i in (0, 3, 2, 1, 4)]
        for _ in range(4):
            self.read_draft(swapped)
        self.assertEqual(len(self.of("search")), 1)  # nobody new: no new search
        self.assertEqual(self.state.seats["a1"]["order"], 3)  # it moved down
        self.assertEqual(len(core.view(self.state).allies), 5)

    def test_the_role_screen_before_the_pick_screen_is_not_read(self):
        self.enter_draft()
        texts = draft_texts(["", "", "", "Cyan XP Buff", ""])
        del texts[("anchor", 0)]
        self.step("screen", "draft", self.state.phase_at, texts)
        self.assertEqual(self.state.seats, {})

    def test_one_version_read_again_is_weak_evidence_only(self):
        self.enter_draft()

        def read(text, times):
            for _ in range(times):  # the same text, in the same version, on a static screen
                texts = {("ally", 0): [text, "", ""], ("anchor", 0): ["ALLY"]}
                self.step("screen", "draft", self.state.phase_at, texts)

        read("SPLOTCH", core.REPEATS - 1)
        self.assertEqual(core.view(self.state).allies, ())
        read("SPLOTCH", 1)  # now often enough, and long enough to search for
        self.assertEqual([p.read_as for p in core.view(self.state).allies], ["SPLOTCH"])
        self.enter_draft()
        read("CY", 10)  # a fragment never settles from one version
        self.assertEqual(core.view(self.state).allies, ())


class LoadingTests(Flow):
    def to_loading(self, named=True):
        line = {"phase": "loading", "at_ts": self.now + 60, "map": "Sanctuary"}
        if named:
            line.update(user_id=ME, user_name="SilverFalcon")
        self.step("log", line, dt=60)

    def test_the_other_team_comes_from_the_row_without_my_name(self):
        self.enter_draft()
        self.read_draft()
        self.answer_search({s: None for s in ("a1", "a2", "a3", "a4")})
        self.to_loading()
        self.step("screen", "loading", self.state.phase_at, loading_texts(TOP, BOTTOM))
        enemies = sorted(k for k, s in self.state.seats.items() if s["team"] == "enemy")
        self.assertEqual(enemies, ["e0", "e1", "e2", "e3", "e4"])
        self.assertEqual(self.state.seats["e1"]["reading"], "iVaaK--Dimbrandt")
        self.assertEqual(sorted(self.of("search")[-1][2]), enemies)
        allies = [s for s in self.state.seats.values() if s["team"] == "ally"]
        self.assertEqual(len(allies), 5)  # the draft's, not read again

    def test_my_row_may_be_the_top_one(self):
        self.enter_draft()
        self.read_draft()
        self.to_loading()
        self.step("screen", "loading", self.state.phase_at, loading_texts(BOTTOM, TOP))
        self.assertEqual(self.state.seats["e0"]["reading"], "GTame52")

    def test_started_late_my_team_comes_from_the_loading_screen(self):
        self.step("log", {"phase": "login", "user_id": ME})
        self.state.my_name = "SilverFalcon"
        self.to_loading()
        self.step("screen", "loading", self.state.phase_at, loading_texts(TOP, BOTTOM))
        allies = {s["reading"] for s in self.state.seats.values() if s["team"] == "ally"}
        self.assertEqual(allies, set(BOTTOM))
        mine = [k for k, s in self.state.seats.items() if s["reading"] == "SilverFalcon"]
        self.assertEqual([self.state.ids[k]["pid"] for k in mine], [ME])

    def test_without_my_name_no_row_is_taken(self):
        self.to_loading(named=False)
        self.step("screen", "loading", self.state.phase_at, loading_texts(TOP, BOTTOM))
        self.assertEqual(self.state.seats, {})

    def test_names_seen_before_the_log_says_loading_wait_for_the_mode(self):
        key = self.enter_draft()
        self.sent.clear()
        self.step("screen", "loading", key, loading_texts(TOP, BOTTOM))
        asked = [sid for c in self.of("search") for sid in c[2]]
        self.assertFalse([sid for sid in asked if sid.startswith("e")])  # bots, maybe
        self.step(
            "log",
            {"phase": "loading", "at_ts": self.now, "experience": "BP_Experience_Game_Normal"},
        )
        asked = [sid for c in self.of("search") for sid in c[2]]
        self.assertTrue([sid for sid in asked if sid.startswith("e")])

    def test_the_match_itself_takes_no_readings(self):
        self.enter_draft()
        self.to_loading()
        self.step("log", {"phase": "joined", "map": "Sanctuary", "at_ts": self.now})
        self.step("log", {"phase": "presence", "at_ts": self.now + 30})
        self.step("screen", "loading", self.state.phase_at, loading_texts(TOP, BOTTOM))
        self.assertEqual(self.state.seats, {})


class FailureTests(Flow):
    def test_a_failed_search_backs_off_then_gives_up_into_no_data(self):
        self.enter_draft()
        self.read_draft()
        for attempt in range(len(core.CARD_RETRY)):
            ((_, epoch, _),) = self.of("search")[-1:]
            self.step("found", epoch, None)
            self.step("tick", dt=1)
            if attempt < len(core.CARD_RETRY) - 1:
                self.step("tick", dt=core.CARD_RETRY[attempt])
        self.assertEqual(len(self.of("search")), len(core.CARD_RETRY))
        states = {p.read_as: p.state for p in core.view(self.state).allies}
        self.assertEqual(states["HULGARVENTAS4"], "error")

    def test_a_refused_session_stops_the_reads_until_a_sign_in(self):
        self.enter_draft()
        self.step("auth", False)
        self.sent.clear()
        self.read_draft()
        self.step("tick", dt=5)
        self.assertEqual(self.sent, [])
        view = core.view(self.state)
        self.assertEqual(view.status, core.NEEDS_LOGIN)
        # The rows say why they are empty: "..." would promise data that cannot come.
        self.assertEqual({(p.state, p.why) for p in view.allies}, {("error", "login")})
        me = next(p for p in view.allies if p.is_me)
        self.assertEqual(me.name, "SilverFalcon")  # the log names me, signed in or not
        self.step("sign_in")
        self.assertTrue(self.of("search"))
        view = core.view(self.state)
        self.assertEqual(view.health, "ok")
        self.assertEqual({p.why for p in view.allies}, {None})  # asked for now: loading

    def test_an_unreachable_pred_gg_shows_offline(self):
        self.enter_draft()
        self.step("reach", False)
        self.assertEqual(core.view(self.state).status, core.OFFLINE)
        self.step("reach", True)
        self.assertEqual(core.view(self.state).status, "draft")

    def test_a_just_finished_match_not_listed_yet_is_asked_for_again(self):
        self.step(
            "log",
            {
                "phase": "finished",
                "match_id": "m-last",
                "reason": "REASON_FINISHED",
                "at_ts": self.now,
            },
        )
        self.enter_draft()
        self.step("recent", self.state.epoch, ([], []))
        self.step("tick", dt=core.CARD_RETRY[0] + 1)
        self.assertEqual(len(self.of("recent")), 2)

    def test_back_to_the_menu_drops_the_roster(self):
        self.enter_draft()
        self.read_draft()
        self.step("log", {"phase": "menu", "at_ts": self.now})
        self.assertEqual(self.state.seats, {})
        self.assertEqual(core.view(self.state).allies, ())


class ReviewTests(Flow):
    def to_loading(self):
        line = {
            "phase": "loading",
            "at_ts": self.now + 60,
            "map": "x",
            "user_id": ME,
            "user_name": "SilverFalcon",
        }
        self.step("log", line, dt=60)

    def test_a_partial_draft_is_completed_by_name_on_the_loading_screen(self):
        self.enter_draft()
        self.read_draft(DRAFT_NAMES[:4] + [""])  # the last one never read in the draft
        self.to_loading()
        self.step("screen", "loading", self.state.phase_at, loading_texts(TOP, BOTTOM))
        allies = [
            s["reading"] for s in self.state.seats.values() if s["team"] == "ally" and s["reading"]
        ]
        self.assertEqual(len(allies), 5)
        self.assertEqual(sum(lookup_key(a) == lookup_key("UrVak") for a in allies), 1)
        self.assertEqual(sum(lookup_key(a) == lookup_key("SilverFalcon") for a in allies), 1)
        roles = {s["reading"]: s["role"] for s in self.state.seats.values() if s["team"] == "ally"}
        self.assertEqual(roles["SILVERFALCON"], "JUNGLE")  # its loading column

    def test_offline_clears_in_the_menu(self):
        self.enter_draft()
        self.step("reach", False)
        self.step("log", {"phase": "menu", "at_ts": self.now})
        self.assertEqual(core.view(self.state).health, "ok")

    def test_a_search_waiting_out_a_backoff_does_not_hold_my_profile(self):
        self.enter_draft()
        self.read_draft()
        ((_, epoch, _),) = self.of("search")
        self.step("found", epoch, None)
        self.step("tick", dt=1)
        self.assertIn(ME, self.of("profiles")[-1][2])

    def test_no_name_is_shown_while_it_is_unknown_whether_to_show_it(self):
        self.enter_draft()
        self.read_draft()
        names = {p.read_as: p.name for p in core.view(self.state).allies}
        self.assertIsNone(names["URVAK"])


def lookup_key(text):
    from predoverlay.overlay import lookup

    return lookup.name_key(text)


class RoundTwoTests(Flow):
    def test_a_game_in_another_language_is_read_by_my_own_name(self):
        self.enter_draft()
        texts = draft_texts(DRAFT_NAMES)
        texts[("anchor", 0)] = ["ALLIÉ", "ALLIÉ"]  # no "ALLY" header
        self.step("screen", "draft", self.state.phase_at, texts)
        self.assertEqual(len(core.view(self.state).allies), 5)

    def test_a_draft_reading_without_header_or_my_name_is_logged_once(self):
        self.enter_draft()
        texts = draft_texts(["", "", "", "Cyan XP Buff", ""])
        del texts[("anchor", 0)]
        for _ in range(3):
            self.step("screen", "draft", self.state.phase_at, texts)
        logged = [n for n in self.state.notes if n.startswith("draft: names read")]
        self.assertEqual(len(logged), 1)


class RoundFourTests(Flow):
    def test_profiles_that_never_came_are_asked_for_again_at_once_after_a_sign_in(self):
        self.enter_draft()
        self.read_draft()
        self.answer_search({s: None for s in ("a1", "a2", "a3", "a4")})
        for _ in core.CARD_RETRY:
            ((_, epoch, _),) = self.of("profiles")[-1:]
            self.step("profiles", epoch, None)
            self.step("tick", dt=1)
        before = len(self.of("profiles"))
        self.step("sign_in")  # names not found are searched again first; profiles follow it
        self.answer_search({s: None for s in ("a1", "a2", "a3", "a4")})
        self.assertEqual(len(self.of("profiles")), before + 1)
        self.assertIn(ME, self.of("profiles")[-1][2])

    def test_an_outage_that_ends_gives_the_rows_their_data(self):
        # Four failures in a row: the rows say that Pred.gg did not answer. The lookups go
        # on at the slowest pace, and the first answer fills the rows.
        self.enter_draft()
        self.read_draft()
        for _ in core.CARD_RETRY:
            ((_, epoch, _),) = self.of("search")[-1:]
            self.step("found", epoch, None)
            self.step("tick", dt=core.CARD_RETRY[-1] + 1)
        others = [p for p in core.view(self.state).allies if not p.is_me]
        self.assertEqual({(p.state, p.why) for p in others}, {("error", "offline")})
        ((_, epoch, names),) = self.of("search")[-1:]  # still asking
        self.assertEqual(set(names), {"a1", "a2", "a3", "a4"})
        self.answer_search({"a1": {"player_id": "p1", "name": DRAFT_NAMES[1]}})
        ((_, epoch, ids),) = self.of("profiles")[-1:]
        for _ in core.CARD_RETRY:  # and the profiles do not come either
            self.step("profiles", epoch, None)
            self.step("tick", dt=core.CARD_RETRY[-1] + 1)
        row = next(p for p in core.view(self.state).allies if p.player_id == "a1")
        self.assertEqual((row.state, row.why), ("error", "offline"))
        self.step("profiles", epoch, {"p1": {"name": DRAFT_NAMES[1], "console": False}})
        row = next(p for p in core.view(self.state).allies if p.player_id == "a1")
        self.assertEqual(row.state, "ready")

    def test_a_failed_request_does_not_make_its_players_profileless(self):
        # The allies' request fails for good; a later one, with them in it again, is
        # answered for all but one: only that one has no profile on Pred.gg.
        self.enter_draft()
        self.read_draft()
        self.answer_search(
            {f"a{i}": {"player_id": f"p{i}", "name": DRAFT_NAMES[i]} for i in range(1, 5)}
        )
        for _ in core.CARD_RETRY:
            ((_, epoch, ids),) = self.of("profiles")[-1:]
            self.step("profiles", epoch, None)
            self.step("tick", dt=core.CARD_RETRY[-1] + 1)
        self.assertEqual(self.state.missing, set())
        ((_, epoch, ids),) = self.of("profiles")[-1:]
        self.assertTrue({"p1", "p2", "p3", "p4"} <= set(ids))
        found = {pid: {"name": pid, "console": False} for pid in ids if pid != "p4"}
        self.step("profiles", epoch, found)
        self.assertEqual(self.state.missing, {"p4"})
        why = {p.player_id: p.why for p in core.view(self.state).allies}
        self.assertEqual((why["a1"], why["a4"]), (None, "noprofile"))

    def test_a_draft_after_a_match_whose_end_was_missed_is_a_new_match(self):
        self.enter_draft()
        self.read_draft()
        self.step("log", {"phase": "loading", "at_ts": self.now + 60, "map": "x"}, dt=60)
        self.assertTrue(self.state.seats)
        epoch = self.state.epoch
        self.step("log", {"phase": "draft", "at_ts": self.now + 900})
        self.assertNotEqual(self.state.epoch, epoch)
        self.assertEqual(self.state.seats, {})

    def test_one_unread_column_does_not_drop_the_drafts_names(self):
        self.enter_draft()
        self.read_draft(DRAFT_NAMES + ["EXTRA"])
        line = {
            "phase": "loading",
            "at_ts": self.now + 60,
            "map": "x",
            "user_id": ME,
            "user_name": "SilverFalcon",
        }
        self.step("log", line, dt=60)
        bottom = list(BOTTOM)
        bottom[1] = ""  # one column unread this time
        self.step("screen", "loading", self.state.phase_at, loading_texts(TOP, bottom))
        gone = [s["reading"] for s in self.state.seats.values() if s["gone"]]
        self.assertNotIn("HULGARVENTAS4", gone)


class SpellingTests(Flow):
    def test_a_name_not_found_is_searched_again_as_the_tab_board_spells_it(self):
        self.enter_draft()
        self.read_draft(DRAFT_NAMES[:3] + ["KVV", "URVAK"])  # the draft dropped a letter
        self.answer_search({s: None for s in ("a1", "a2", "a3", "a4")})
        rows = {("ally", 3): {"text": "KV.DV.", "texts": ["KV.DV.", "KV.DV."], "role": None}}
        self.step("board_read", f"{self.state.epoch}", "tab", rows)
        ((_, _, asked),) = self.of("search")[-1:]
        self.assertEqual(asked, {"a3": ("KV.DV.",)})  # only the new spelling
        self.answer_search({"a3": {"player_id": "pj", "name": "Kv.Dv."}})
        self.assertEqual(self.state.ids["a3"]["pid"], "pj")

    def test_spellings_pred_gg_did_not_know_are_not_asked_twice(self):
        self.enter_draft()
        self.read_draft()
        self.answer_search({s: None for s in ("a1", "a2", "a3", "a4")})
        searches = len(self.of("search"))
        rows = {("ally", 2): {"text": "BRENK141", "texts": ["BRENK141"] * 2, "role": None}}
        self.step("board_read", f"{self.state.epoch}", "tab", rows)  # the same name again
        self.assertEqual(len(self.of("search")), searches)


class NoiseTests(Flow):
    def test_a_loading_placeholder_is_no_name(self):
        self.enter_draft()
        names = ["LOADING IN PLAYER..", *DRAFT_NAMES[1:]]
        self.read_draft(names)
        readings = [p.read_as for p in core.view(self.state).allies]
        self.assertNotIn("LOADING IN PLAYER..", readings)

    def test_a_name_with_spaces_ocr_put_in_is_also_searched_without(self):
        self.enter_draft()
        texts = draft_texts(DRAFT_NAMES)
        texts[("ally", 1)] = ["Mar Mine-blezi", "Ma r Mine-blezi", ""]
        self.step("screen", "draft", self.state.phase_at, texts)
        ((_, _, asked),) = self.of("search")[-1:]
        self.assertEqual(asked["a1"][:2], ("Mar Mine-blezi", "MarMine-blezi"))

    def test_a_dash_ocr_read_is_also_searched_as_an_underscore(self):
        self.enter_draft()
        texts = draft_texts(DRAFT_NAMES)
        texts[("ally", 1)] = ["ABC-Defghijk", "ABC-Defghijk", ""]
        self.step("screen", "draft", self.state.phase_at, texts)
        ((_, _, asked),) = self.of("search")[-1:]
        self.assertEqual(asked["a1"][:2], ("ABC-Defghijk", "ABC_Defghijk"))
        self.assertLessEqual(len(asked["a1"]), core.SPELLINGS)

    def test_a_reading_of_gaps_alone_is_not_searched(self):
        self.enter_draft()
        texts = draft_texts(DRAFT_NAMES)
        texts[("ally", 1)] = ["�", "�", ""]
        self.step("screen", "draft", self.state.phase_at, texts)
        for _, _, asked in self.of("search"):
            self.assertNotIn("�", "".join(asked.get("a1", ())))


if __name__ == "__main__":
    unittest.main()


class AgreedSpellingTests(unittest.TestCase):
    """core._agreed: of the spellings that agree by key, the one the search gets."""

    G = "�"

    def test_the_models_gap_beats_ocrs_spelling_without_it(self):
        texts = [(0, "ABC" + self.G + "DEF"), (1, "ABCDEF"), (2, "ABCDEF")]
        self.assertEqual(core._agreed(texts), "ABC" + self.G + "DEF")

    def test_a_new_gap_spelling_is_another_attempt(self):
        from predoverlay.overlay import identity

        ident = dict(identity.new(), searched=True, tried={lookup.attempt_key("ABCDEF")})
        self.assertTrue(identity.another_spelling(ident, "ABC" + self.G + "DEF"))
        ident["searched"] = True
        self.assertFalse(identity.another_spelling(ident, "AbcDef"))

    def test_the_models_spelling_beats_ocrs_of_the_same_key(self):
        texts = [(0, "xl2xx2"), (1, "x12xx2"), (2, "x12xx2"), (3, "x12xx2")]
        self.assertEqual(core._agreed(texts), "xl2xx2")

    def test_a_found_player_stays_when_only_the_spelling_changes(self):
        from predoverlay.overlay import identity, seats

        state = core.State()
        state.seats["a1"] = dict(seats.new("ally", 1), reading="NAME_1", primary="draft")
        state.ids["a1"] = dict(identity.new(), pid="p", name="Name_1", searched=True)
        changed = seats.take(state, "a1", "draft", ["", "NAME-1", "NAME-1", "NAME-1"])
        self.assertFalse(changed)
        self.assertEqual(state.ids["a1"]["pid"], "p")
        state.seats["a1"]["evidence"] = {}
        changed = seats.take(state, "a1", "draft", ["NAME_1" + self.G, "NAME-1", "NAME-1"])
        self.assertTrue(changed)
        self.assertIsNone(state.ids["a1"]["pid"])  # a mark appeared: another name, searched

    def test_the_other_spelling_of_the_agreed_key_is_searched_too(self):
        from predoverlay.overlay import identity

        versions = ["XIube77", "Xlube77", "Xlube77"]
        found = identity.candidates("XIube77", (), versions, identity.new())
        self.assertEqual(found[:2], ("XIube77", "Xlube77"))

    def test_a_known_name_is_taken_only_for_its_own_spelling(self):
        state = core.State()
        state.known = {lookup.name_key("Zero"): ("pid-zero", "Zero")}
        from predoverlay.overlay import identity

        for reading, pid in (("Zer0", None), ("zero_", None), ("ZERO", "pid-zero")):
            ident = identity.new()
            identity.name(state, dict(core.seating.new("ally", 0), reading=reading), ident)
            self.assertEqual(ident["pid"], pid, reading)

    def test_a_marked_readings_other_spelling_keeps_the_mark(self):
        g = self.G
        from predoverlay.overlay import identity

        found = identity.candidates(
            "Dark" + g + "Lord", ("DARKLORD",), ["Dark" + g + "Lord", "DarkLord"], identity.new()
        )
        self.assertEqual(found, ("Dark" + g + "Lord",))

    def test_an_alias_whose_profile_is_in_already_is_checked_at_once(self):
        state = core.State()
        state.aliases = {core.aliases.digest(state.salt, lookup.name_key("Zero")): "pid-zero"}
        state.profiles = {"pid-zero": {"name": "Zero", "console": False}}
        from predoverlay.overlay import identity

        for reading, pid in (("Zer0", None), ("ZERO", "pid-zero")):
            ident = identity.new()
            identity.name(state, dict(core.seating.new("ally", 0), reading=reading), ident)
            self.assertEqual(ident["pid"], pid, reading)

    def test_only_a_seat_of_my_team_can_be_me(self):
        from predoverlay.overlay import identity

        state = core.State()
        state.me, state.my_name = "pid-me", "Solo"
        for team, reading, mine in (
            ("ally", "Solo", True),
            ("ally", "S0lo", True),  # my own name misread: still me
            ("enemy", "S0lo", False),  # an enemy spelled almost like me is not
            ("enemy", "Solo", False),
        ):
            seat, ident = dict(core.seating.new(team, 0), reading=reading), identity.new()
            identity.name(state, seat, ident)
            self.assertEqual(identity.mine(state, seat), mine, (team, reading))
            self.assertEqual(ident["pid"] == "pid-me", mine, (team, reading))
