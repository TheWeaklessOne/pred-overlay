"""Queue counts on the menu strip are the mean of the latest reads."""

import unittest

from predoverlay.overlay import core


def read(players):
    return {"EUROPE": {"RANKED": (players, players // 2)}}


class Average(unittest.TestCase):
    def test_a_single_read_stands_as_it_is(self):
        self.assertEqual(core.average_queues([read(40)]), read(40))

    def test_a_mode_missing_from_a_read_counts_as_empty(self):
        reads = [{"EUROPE": {"RANKED": (40, 20), "ARAM": (8, 4)}}, read(20)]
        self.assertEqual(
            core.average_queues(reads),
            {"EUROPE": {"RANKED": (30, 15), "ARAM": (4, 2)}},
        )

    def feed(self, state, players, at):
        core.reduce(state, ("queues", read(players)), at)
        return state.queues["EUROPE"]["RANKED"][0]

    def test_a_formed_match_does_not_halve_the_number(self):
        state = core.State()
        for index, players in enumerate((40, 44, 42)):
            self.feed(state, players, 30.0 * index)
        self.assertEqual(self.feed(state, 10, 90.0), 34)

    def test_only_the_latest_reads_count(self):
        state = core.State()
        for index in range(core.QUEUES_WINDOW):
            self.feed(state, 100, 30.0 * index)
        last = 30.0 * core.QUEUES_WINDOW
        for offset in range(core.QUEUES_WINDOW):
            shown = self.feed(state, 20, last + 30.0 * offset)
        self.assertEqual(shown, 20)

    def test_reads_from_before_a_match_are_dropped(self):
        state = core.State()
        self.feed(state, 100, 0.0)
        self.assertEqual(self.feed(state, 20, core.QUEUES_STALE + 1), 20)


if __name__ == "__main__":
    unittest.main()
