import unittest

from predoverlay.overlay import core, lookup

NOW = "2026-11-18T12:00:00Z"


def candidate(name, pid, last="2026-11-01T00:00:00Z"):
    return {"id": pid, "name": name, "lastPlayedAt": last}


class PickTests(unittest.TestCase):
    def test_only_an_exact_name_counts_with_ocr_lookalikes_folded(self):
        rows = [candidate("V0na", "a"), candidate("V0naMurr", "b")]
        self.assertEqual(lookup.pick("VONA", rows), {"player_id": "a", "name": "V0na"})
        self.assertIsNone(lookup.pick("V0n", rows))
        self.assertIsNone(lookup.pick("Von4", rows))

    def test_the_most_recent_of_several_exact_names(self):
        rows = [candidate("Ace", "old", "2025-01-01T00:00:00Z"), candidate("ace", "new")]
        self.assertEqual(lookup.pick("Ace", rows)["player_id"], "new")

    def test_a_cut_off_name_matches_one_name_it_starts(self):
        rows = [candidate("LongPlayerName", "a"), candidate("Other", "b")]
        self.assertEqual(lookup.pick("LongPlay...", rows)["player_id"], "a")
        two = rows + [candidate("LongPlayerTwo", "c")]
        self.assertIsNone(lookup.pick("LongPlay...", two))

    def test_nothing_from_empty_or_odd_answers(self):
        self.assertIsNone(lookup.pick("", [candidate("x", "a")]))
        self.assertIsNone(lookup.pick("Ace", None))
        self.assertIsNone(lookup.pick("Ace", [None, {"name": "Ace"}]))

    def test_resolve_reads_each_alias(self):
        names = ["Ace", "Bob"]
        found = {"s0": {"results": [candidate("Ace", "a")]}, "s1": {"results": []}}
        self.assertEqual(
            lookup.resolve(names, found), {"Ace": {"player_id": "a", "name": "Ace"}, "Bob": None}
        )

    def test_queries_put_every_name_in_one_document(self):
        document, variables = lookup.search_query(["A", "B"])
        self.assertIn("s1:playersPaginated(filter:{search:$n1}", document)
        self.assertEqual(variables, {"n0": "A", "n1": "B"})


def rating(season, end, rank=None, unranked=False, points=None, start="2026-01-01T00:00:00Z"):
    return {
        "rating": {"name": season, "startTime": start, "endTime": end},
        "rank": {"name": rank} if rank else None,
        "peakRank": {"name": rank} if rank else None,
        "unranked": unranked,
        "points": points,
    }


class ProfileTests(unittest.TestCase):
    def found(self, ratings):
        return {
            "seasons": [
                {"name": "S5", "endTime": "2026-11-03T10:00:00Z"},
                {"name": "S6", "endTime": None},
            ],
            "p0": {
                "ratings": ratings,
                "favHero": {"name": "Steel"},
                "favRole": "SUPPORT",
                "lastPlayedAt": "2026-11-10T00:00:00Z",
                "firstPlayedAt": "2022-12-01T00:00:00Z",
                "isNameConsole": False,
            },
        }

    def test_the_running_season_is_the_current_rank(self):
        found = self.found(
            [
                rating("S5", "2026-11-03T10:00:00Z", "Gold I"),
                rating("S6", None, "Diamond II", points=1890),
            ]
        )
        profile = lookup.profiles(["x"], found, NOW)["x"]
        self.assertEqual(profile["current"]["rank"], "Diamond II")
        self.assertEqual(profile["current"]["points"], 1890.0)
        self.assertEqual([s["name"] for s in profile["seasons"]], ["S5"])
        self.assertEqual(profile["main"], {"hero": "Steel", "role": "SUPPORT"})

    def test_no_entry_for_the_running_season_is_not_placed(self):
        found = self.found([rating("S5", "2026-11-03T10:00:00Z", "Gold I")])
        current = lookup.profiles(["x"], found, NOW)["x"]["current"]
        self.assertEqual(current, {"rank": None, "points": None, "season": "S6"})

    def test_an_unknown_player_is_left_out(self):
        self.assertEqual(lookup.profiles(["x"], {"p0": None}, NOW), {})


def match(mid, end, winner, rows):
    return {
        "id": mid,
        "endTime": end,
        "winningTeam": winner,
        "matchPlayers": [
            {
                "name": n,
                "team": t,
                "hero": {"name": h},
                "kills": 1,
                "deaths": 2,
                "assists": 3,
                "player": {"id": p},
            }
            for n, t, h, p in rows
        ],
    }


class MatchTests(unittest.TestCase):
    def test_my_matches_feed_the_day_and_met_before(self):
        first = match(
            "m1",
            "2026-11-18T10:00:00Z",
            "DAWN",
            [("Me", "DAWN", "Steel", "me"), ("Ann", "DUSK", "Kira", "ann")],
        )
        second = match(
            "m2",
            "2026-11-18T11:00:00Z",
            "DUSK",
            [("Me", "DAWN", "Steel", "me"), ("Ann", "DAWN", "Kira", "ann")],
        )
        found = {
            "me": {
                "matchesPaginated": {
                    "results": [
                        {"team": "DAWN", "kills": 5, "deaths": 1, "assists": 7, "match": first},
                        {"team": "DAWN", "kills": 2, "deaths": 4, "assists": 9, "match": second},
                    ]
                }
            }
        }
        summaries, details = lookup.my_matches(found)
        self.assertEqual([s["match_id"] for s in summaries], ["m2", "m1"])
        self.assertEqual([s["won"] for s in summaries], [False, True])
        met = core.encounters_for(("me", "ann"), "me", details)
        self.assertEqual(
            [(e.matches_ago, e.side, e.won) for e in met["ann"]],
            [(1, "with", False), (2, "against", True)],
        )
        self.assertEqual(
            lookup.known_names(details),
            {lookup.name_key("Me"): ("me", "Me"), lookup.name_key("Ann"): ("ann", "Ann")},
        )

    def test_rows_without_a_player_or_end_are_skipped(self):
        broken = {"id": "m", "endTime": None, "matchPlayers": [{"name": "x"}]}
        self.assertEqual(lookup.match_details(broken)["players"], [])
        found = {"me": {"matchesPaginated": {"results": [{"team": "DAWN", "match": broken}, None]}}}
        self.assertEqual(lookup.my_matches(found), ([], []))


class PointsTests(unittest.TestCase):
    def test_the_points_each_match_moved_me(self):
        document, variables = lookup.recent_query("me", 20, "13")
        self.assertIn("rating(ratingId:$r){points newPoints}", document)
        self.assertEqual(variables["r"], "13")
        self.assertNotIn("ratingId", lookup.recent_query("me", 20)[0])
        row = {
            "team": "DAWN",
            "kills": 1,
            "deaths": 1,
            "assists": 1,
            "rating": {"points": 1145.0, "newPoints": 1123.0},
            "match": {
                "id": "m",
                "endTime": "2026-11-18T10:00:00Z",
                "winningTeam": "DUSK",
                "matchPlayers": [],
            },
        }
        summaries, _ = lookup.my_matches({"me": {"matchesPaginated": {"results": [row]}}})
        self.assertEqual(summaries[0]["vp"], -22)

    def test_the_running_season(self):
        found = {
            "ratings": [
                {"id": "12", "endTime": "2026-11-03T10:00:00Z"},
                {"id": "13", "endTime": None},
            ]
        }
        self.assertEqual(lookup.running_season(found), "13")
        self.assertIsNone(lookup.running_season({"ratings": []}))


class ScriptTests(unittest.TestCase):
    def test_names_in_any_script_count_and_accents_fold(self):
        self.assertEqual(lookup.pick("Дима", [candidate("Дима", "d")])["player_id"], "d")
        self.assertNotEqual(lookup.name_key("XxДимаxX"), lookup.name_key("XxПетяxX"))
        self.assertEqual(lookup.name_key("GTamé52"), lookup.name_key("GTame52"))

    def test_the_same_spelling_beats_a_folded_lookalike(self):
        rows = [
            candidate("K.a.i", "dots", "2026-11-10T00:00:00Z"),
            candidate("Kai", "plain", "2026-01-01T00:00:00Z"),
        ]
        self.assertEqual(lookup.pick("Kai", rows)["player_id"], "plain")


class DocumentTests(unittest.TestCase):
    def test_every_query_is_balanced(self):
        documents = [
            lookup.search_query(["A", "B"])[0],
            lookup.profile_query(["p", "q"])[0],
            lookup.recent_query("me", 20)[0],
            lookup.recent_query("me", 20, "13")[0],
            lookup.match_query("m")[0],
            lookup.SEASONS_QUERY,
        ]
        for document in documents:
            with self.subTest(document=document[:40]):
                depth = 0
                for char in document:
                    depth += {"{": 1, "}": -1}.get(char, 0)
                    self.assertGreaterEqual(depth, 0)
                self.assertEqual(depth, 0)
                self.assertEqual(document.count("("), document.count(")"))


class SpellingTests(unittest.TestCase):
    def test_dashes_are_also_searched_as_underscores(self):
        self.assertEqual(lookup.search_spellings("ABC-Def"), ["ABC_Def"])
        self.assertIn("ABC_Def", lookup.search_spellings("ABC—Def"))
        self.assertIn("ABC-Def", lookup.search_spellings("ABC—Def"))  # an em dash is no name's

    def test_l_and_capital_i_are_swapped_only_where_case_says_so(self):
        self.assertEqual(lookup.search_spellings("pIayer"), ["player"])
        self.assertEqual(lookup.search_spellings("XlX"), ["XIX"])
        self.assertEqual(lookup.search_spellings("Ilya"), [])  # a capital at the start stays
        self.assertEqual(lookup.search_spellings("PLAIN"), [])

    def test_both_at_once_and_every_spelling_keeps_the_key(self):
        spellings = lookup.search_spellings("SmaIl-fry")
        self.assertEqual(spellings, ["SmaIl_fry", "Small-fry", "Small_fry"])
        for s in spellings:
            self.assertEqual(lookup.name_key(s), lookup.name_key("SmaIl-fry"))


class GapTests(unittest.TestCase):
    G = "�"

    def one(self, name):
        return [{"id": "p", "name": name, "lastPlayedAt": ""}]

    def test_a_gap_is_searched_by_the_longest_piece_and_matched_in_place(self):
        reading = "NAME" + self.G + "TWOLONGER"
        self.assertEqual(lookup.search_text(reading), "TWOLONGER")
        self.assertIsNotNone(lookup.pick(reading, self.one("Name🔥TwoLonger")))
        self.assertIsNone(lookup.pick(reading, self.one("NameTwoLonger")))  # no gap there

    def test_an_edge_symbol_may_be_read_or_missed(self):
        for reading in ("KOALAKAN" + self.G, "KOALAKAN"):
            self.assertIsNotNone(lookup.pick(reading, self.one("KoALaKaN™")))

    def test_nothing_to_search_by_when_only_gaps_remain(self):
        self.assertEqual(lookup.search_text(self.G), "")
        self.assertEqual(lookup.search_text("AB" + self.G), "")
        self.assertEqual(lookup.search_query([self.G + "ABCDEF"])[1], {"n0": "ABCDEF"})

    def test_cyrillic_is_matched_as_written(self):
        self.assertIsNotNone(lookup.pick("ТАХТА", self.one("Тахта")))

    def test_cyrillic_twins_of_latin_letters_match(self):
        # The model writes Cyrillic letters drawn like Latin ones as the Latin letter.
        self.assertIsNotNone(lookup.pick("TAXTA", self.one("Тахта")))
        self.assertIsNotNone(lookup.pick("Дима", self.one("Дима")))
        self.assertIsNotNone(lookup.pick("ДИMA", self.one("Дима")))
        self.assertIsNone(lookup.pick("DIMA", self.one("Дима")))  # Д and И have no twin

    def test_cyrillic_spelling_is_searched(self):
        self.assertEqual(lookup.search_spellings("ДИMA")[0], "ДИМА")
        self.assertEqual(lookup.search_spellings("TAXTA")[-1], "ТАХТА")  # look-alikes only: last
        self.assertNotIn("ВОВ", lookup.search_spellings("BOB_ONE"))  # a Latin word: no
        for text in ("ДИMA", "TAXTA"):
            for spelling in lookup.search_spellings(text):
                self.assertEqual(lookup.name_key(spelling), lookup.name_key(text))

    def test_a_mixed_script_name_is_searched_by_its_part_without_twins(self):
        reading = "Gnull_KOTЁHOK"  # read of "Gnull_КОТЁНОК": its twins come out Latin
        piece = next(s for s in lookup.search_spellings(reading) if s.startswith(lookup.PIECE))
        self.assertEqual(lookup.search_text(piece), "Gnull_")
        self.assertIsNotNone(lookup.pick(piece, self.one("Gnull_КОТЁНОК")))
        self.assertIsNotNone(lookup.pick(piece, self.one("Gnull_KОТЁНОК")))  # Latin K inside
        self.assertNotEqual(lookup.attempt_key(piece), lookup.attempt_key(reading))

    def test_ukrainian_i_is_searched_in_cyrillic(self):
        self.assertEqual(lookup.search_spellings("Пpивiт")[:2], ["Привiт", "Привіт"])
        # A Latin "i" in a mixed name stays Latin in the first Cyrillic spelling.
        self.assertEqual(lookup.search_spellings("gribni_KOTЁHOK")[0], "gribni_КОТЁНОК")

    def test_a_capital_i_inside_a_word_is_also_searched_as_l(self):
        self.assertIn("Alpha", lookup.search_spellings("AIpha"))

    def test_same_name_is_the_spelling_not_only_the_key(self):
        self.assertTrue(lookup.same_name("ZERO", "Zero"))
        self.assertFalse(lookup.same_name("Zer0", "Zero"))
        self.assertFalse(lookup.same_name("zero_", "Zero"))
        self.assertFalse(lookup.same_name("Ghost" + self.G, "Ghost"))
        self.assertTrue(lookup.same_name("TAXTA", "Тахта"))
        self.assertTrue(lookup.same_name("Ghost" + self.G, "Ghost★"))  # as the model writes it
        self.assertTrue(lookup.same_name("Joao", "João"))

    def test_a_name_with_a_letter_the_model_cannot_write_has_its_reading_key_too(self):
        self.assertIn(lookup.name_key("Bj" + self.G + "rn"), lookup.name_keys("Bjørn"))
        known = lookup.known_names([{"players": [{"name": "Bjørn", "player_id": "b"}]}])
        self.assertEqual(known[lookup.name_key("Bj" + self.G + "rn")][0], "b")

    def test_a_picked_known_name_with_an_emoji_matches_its_own_account(self):
        rows = self.one("Name") + [{"id": "e", "name": "Name🔥", "lastPlayedAt": ""}]
        self.assertEqual(lookup.pick("Name🔥", rows)["player_id"], "e")

    def test_namesakes_with_different_marks_are_not_told_apart(self):
        two = [
            {"id": "a", "name": "Name★", "lastPlayedAt": "2026"},
            {"id": "b", "name": "Name♥", "lastPlayedAt": "2025"},
        ]
        self.assertIsNone(lookup.pick("NAME" + self.G, two))
        self.assertIsNone(lookup.pick("NAME", two))

    def test_lower_case_cyrillic_letters_keep_their_script(self):
        rows = [
            {"id": "c", "name": "Niron_Кот", "lastPlayedAt": "2025"},
            {"id": "l", "name": "Niron_Kot", "lastPlayedAt": "2026"},
        ]
        self.assertEqual(lookup.pick("Niron_Koт", rows)["player_id"], "c")

    def test_a_name_as_the_model_writes_it(self):
        self.assertEqual(lookup.written("Кот 🔥🔥 x"), "Koт " + self.G + " x")

    def test_a_reading_without_a_mark_cannot_choose_between_plain_and_decorated(self):
        both = self.one("Name") + [{"id": "p2", "name": "Name★", "lastPlayedAt": "2026"}]
        self.assertIsNone(lookup.pick("NAME", both))
        self.assertEqual(lookup.pick("NAME" + self.G, both)["player_id"], "p2")
        self.assertIsNotNone(lookup.pick("NAME", self.one("Name")))

    def test_an_edge_mark_only_the_model_saw_matches_the_one_plain_name(self):
        self.assertIsNotNone(lookup.pick(self.G + "NAMEONE", self.one("NameOne")))
        two = self.one("NameOne") + [{"id": "p2", "name": "★NameOne★", "lastPlayedAt": "2026"}]
        self.assertIsNone(lookup.pick(self.G + "NAMEONE", two))  # an edge each: no unique

    def test_superscript_and_fullwidth_letters_are_gaps(self):
        self.assertEqual(lookup.gap_key("Foo ᵀᵀⱽ"), "F00" + self.G)
        self.assertEqual(lookup.search_text("Foo" + self.G), "Foo")
        self.assertEqual(lookup._unit("Ｒ"), self.G)
        self.assertEqual(lookup._unit("ş"), "s")

    def test_zero_width_and_combining_characters_are_left_out(self):
        self.assertEqual(lookup.gap_key("N0‍NAME★X"), lookup.gap_key("N0NAME" + self.G + "X"))
        self.assertEqual(lookup.gap_key("Café★"), lookup.gap_key("Café" + self.G))

    def test_attempts_differ_by_what_is_searched_and_matched(self):
        self.assertNotEqual(
            lookup.attempt_key("ABCDEF"), lookup.attempt_key("ABC" + self.G + "DEF")
        )
        self.assertEqual(lookup.attempt_key("AbcDef"), lookup.attempt_key("ABCDEF"))


if __name__ == "__main__":
    unittest.main()
