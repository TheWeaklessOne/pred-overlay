import math
import os
import unittest

from predoverlay.overlay import roster, scoreboard
from predoverlay.overlay.scoreboard import Box

# Never a window on the real screen (the game may be up): later tests reuse this app.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SURE, UNSURE, DOUBTFUL = math.log(0.995), math.log(0.7), math.log(0.3)


class FakeNames:
    """The overlay's model, answering from a table: {line: (text, log-probability)}."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def read(self, lines, known=None):
        self.asked.append(dict(lines))
        self.known = known
        return {key: self.answers[line] for key, line in lines.items() if line in self.answers}


def no_ocr(png):
    raise RuntimeError("No Windows OCR language is installed.")


class RosterTests(unittest.TestCase):
    def run_reader(self, boxes, names, read):
        reader = roster.Reader(read=read, names=names)
        got = []
        reader._run(lambda: boxes, got.append)
        return reader, got[0]

    def test_a_sure_reading_is_two_versions_and_ocr_is_not_asked(self):
        asked = []
        boxes = {("ally", 0): Box("line0", lambda: asked.append(1) or ["png"])}
        _, texts = self.run_reader(
            boxes, FakeNames({"line0": ("ABC_DEF", SURE)}), lambda png: ["X"]
        )
        self.assertEqual(texts[("ally", 0)], ["ABC_DEF", "ABC_DEF"])
        self.assertEqual(asked, [])

    def test_an_unsure_reading_is_one_version_next_to_ocr(self):
        boxes = {("ally", 0): Box("line0", lambda: ["a", "b"])}
        lines = {"a": ["ABC-DEF", "HERO"], "b": [""]}
        _, texts = self.run_reader(
            boxes, FakeNames({"line0": ("ABC_DEF", UNSURE)}), lambda png: lines[png]
        )
        self.assertEqual(texts[("ally", 0)], ["ABC_DEF", "ABC-DEF", ""])

    def test_the_header_is_ocr_alone(self):
        names = FakeNames({})
        boxes = {("anchor", 0): Box(None, lambda: ["h"])}
        _, texts = self.run_reader(boxes, names, lambda png: ["ALLY"])
        self.assertEqual(texts[("anchor", 0)], ["ALLY"])
        self.assertEqual(names.asked, [])  # no name line: the model is not asked

    def test_without_ocr_the_model_reads_alone(self):
        boxes = {
            ("ally", 0): Box("line0", lambda: ["a"]),
            ("ally", 1): Box("line1", lambda: ["b"]),
        }
        names = FakeNames({"line0": ("SURE ONE", SURE), "line1": ("MAYBE", UNSURE)})
        reader, texts = self.run_reader(boxes, names, no_ocr)
        self.assertEqual(texts, {("ally", 0): ["SURE ONE", "SURE ONE"], ("ally", 1): ["MAYBE"]})
        self.assertIsNone(reader.error)
        self.assertIn("No Windows OCR", reader.note)

    def test_a_doubtful_reading_is_left_out_but_keeps_its_place(self):
        boxes = {("ally", 0): Box("line0", lambda: ["a"])}
        _, texts = self.run_reader(
            boxes, FakeNames({"line0": ("QQQ", DOUBTFUL)}), lambda png: ["ABC"]
        )
        self.assertEqual(texts[("ally", 0)], ["", "ABC"])  # OCR stays version 1

    def test_ocr_is_not_asked_again_after_it_failed(self):
        calls = []

        def failing(png):
            calls.append(png)
            raise RuntimeError("No Windows OCR language is installed.")

        boxes = {("ally", i): Box(f"line{i}", lambda: ["a"]) for i in range(3)}
        names = FakeNames({f"line{i}": ("MAYBE", UNSURE) for i in range(3)})
        self.run_reader(boxes, names, failing)
        self.assertEqual(len(calls), 1)

    def test_without_model_or_ocr_the_reading_fails_as_before(self):
        reader, texts = self.run_reader({("ally", 0): ["a"]}, None, no_ocr)
        self.assertIsNone(texts)
        self.assertIn("No Windows OCR", reader.error)

    def test_plain_png_lists_still_work(self):
        _, texts = self.run_reader({("ally", 0): ["a"]}, None, lambda png: ["NAME"])
        self.assertEqual(texts, {("ally", 0): ["NAME"]})


class BoardTests(unittest.TestCase):
    NAMES = {"ally": {"p1": "ABC_DEF", "p2": "XYZ"}, "enemy": {}}

    def read(self, boxes, names, read):
        reader = scoreboard.Reader(read=read, clock=lambda: 0.0, names=names)
        reader.match_id = "m"
        reader._run("m", boxes, self.NAMES, size=2)
        return reader

    def test_a_sure_name_is_the_models_and_the_hero_ocrs(self):
        boxes = {("ally", 0): Box("l0", lambda: ["a"]), ("ally", 1): Box("l1", lambda: ["b"])}
        lines = {"a": ["ABC-DEF", "KALLARI"], "b": ["XYZ", "GRUX"]}
        names = FakeNames({"l0": ("ABC_DEF", SURE), "l1": ("XYZ", SURE)})
        reader = self.read(boxes, names, lambda png: lines[png])
        self.assertEqual(reader.rows[("ally", 0)]["player_id"], "p1")
        self.assertEqual(reader.rows[("ally", 0)]["hero"], "KALLARI")
        row = reader.read_rows[("ally", 0)]
        self.assertEqual(
            (row["text"], row["texts"]), ("ABC_DEF", ["ABC_DEF"] * 2)
        )  # the model's, sure

    def test_an_unsure_name_loses_to_an_ocr_reading_nearer_the_roster(self):
        boxes = {("ally", 0): Box("l0", lambda: ["a"])}
        names = FakeNames({"l0": ("QQQ", UNSURE)})
        reader = self.read(boxes, names, lambda png: ["XYZ", "GRUX"])
        self.assertEqual(reader.rows[("ally", 0)]["player_id"], "p2")
        self.assertEqual(reader.read_rows[("ally", 0)]["texts"], ["QQQ", "XYZ"])  # both versions

    def test_without_ocr_names_still_match_and_no_error_stops_the_reader(self):
        boxes = {("ally", 0): Box("l0", lambda: ["a"]), ("ally", 1): Box("l1", lambda: ["b"])}
        names = FakeNames({"l0": ("ABC_DEF", SURE), "l1": ("XYZ", UNSURE)})
        reader = self.read(boxes, names, no_ocr)
        self.assertEqual({r["player_id"] for r in reader.rows.values()}, {"p1", "p2"})
        self.assertIsNone(reader.error)
        self.assertIn("No Windows OCR", reader.note)

    def test_specks_read_as_punctuation_are_no_board(self):
        # The end cinematic before the board: the model may write "-" or "." for specks.
        boxes = {("ally", i): Box(f"l{i}", lambda: ["a"]) for i in range(2)}
        boxes.update({("enemy", i): Box(f"e{i}", lambda: ["a"]) for i in range(3)})
        names = FakeNames({k: ("-", SURE) for k in ("l0", "l1", "e0", "e1", "e2")})
        reader = self.read(boxes, names, lambda png: [])
        self.assertFalse(reader.seen)
        self.assertEqual(reader.result[0], 0)

    def test_a_line_too_small_to_trust_is_never_sure(self):
        boxes = {"a": Box("l0", lambda: ["x"], False), "b": Box("l1", lambda: ["x"])}
        names = FakeNames({"l0": ("ABC", SURE), "l1": ("ABC", SURE)})
        found = scoreboard.model_readings(names, boxes)
        self.assertFalse(scoreboard.sure(found["a"]))
        self.assertTrue(scoreboard.usable(found["a"]))
        self.assertTrue(scoreboard.sure(found["b"]))

    def test_a_doubtful_reading_of_a_small_line_is_left_out(self):
        boxes = {"a": Box("l0", lambda: ["x"], False)}
        found = scoreboard.model_readings(FakeNames({"l0": ("ABC", UNSURE)}), boxes)
        self.assertFalse(scoreboard.usable(found["a"]))

    def test_a_key_that_is_not_bytes_is_one_png(self):
        self.assertEqual(scoreboard.versions_of("row0"), ["row0"])
        self.assertEqual(scoreboard.versions_of(b"png"), [b"png"])


try:
    from PySide6.QtGui import QColor, QGuiApplication, QImage

    from predoverlay.overlay import namereader
except ImportError:  # pragma: no cover - no Qt here
    namereader = None


@unittest.skipIf(namereader is None, "PySide6 missing")
class PrepareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QGuiApplication.instance() or QGuiApplication([])

    def test_a_line_becomes_one_padded_grey_input(self):
        image = QImage(200, 34, QImage.Format.Format_RGB32)
        image.fill(QColor(20, 20, 20))
        for x in range(10, 60):
            for y in range(10, 24):
                image.setPixelColor(x, y, QColor(230, 230, 230))
        x = namereader.prepare(image)
        self.assertEqual(len(x), namereader.H * namereader.W)
        self.assertAlmostEqual(max(x), 1.0, places=2)  # text at the top of the stretch
        self.assertEqual(x[namereader.W - 1], 0.0)  # padded on the right

    def test_the_draft_line_is_cut_from_the_box_at_any_size(self):
        box = QImage(371 * 3 // 4, 64 * 3 // 4, QImage.Format.Format_RGB32)  # 1080p
        part = namereader.line(box, "draft", 64)
        self.assertEqual((part.width(), part.height()), (278, 24))

    def test_greedy_decoding_and_its_path_probability(self):
        classes = len(namereader.CHARSET) + 1
        a, b = namereader.CHARSET.index("A") + 1, namereader.CHARSET.index("B") + 1
        path = [a, a, 0, b, 0, 0]
        logp = []
        for k in path:
            row = [-20.0] * classes
            row[k] = math.log(0.9)
            logp += row
        text, total = namereader.decode(logp, len(path), classes)
        self.assertEqual(text, "AB")
        self.assertAlmostEqual(total, 6 * math.log(0.9))

    def test_no_model_file_means_unavailable_not_a_crash(self):
        reader = namereader.NameReader(path="missing.onnx")
        self.assertEqual(reader.read({"k": QImage(10, 10, QImage.Format.Format_RGB32)}), {})
        self.assertIn("FileNotFoundError", reader.error)

    def test_a_closed_reader_stays_closed(self):
        reader = namereader.NameReader()
        reader.close()
        self.assertFalse(reader.available())
        self.assertEqual(reader.read({"k": QImage(10, 10, QImage.Format.Format_RGB32)}), {})
        self.assertIsNone(reader.session)

    def test_edge_gaps_stay_after_decoding(self):
        classes = len(namereader.CHARSET) + 1
        gap, a = namereader.CHARSET.index(namereader.GAP) + 1, namereader.CHARSET.index("A") + 1
        logp = []
        for k in (gap, 0, a, a, 0, 0):
            row = [-20.0] * classes
            row[k] = 0.0
            logp += row
        self.assertEqual(namereader.decode(logp, 6, classes)[0], namereader.GAP + "A")


DATA = __import__("pathlib").Path(__file__).parent / "data"


@unittest.skipIf(namereader is None, "PySide6 missing")
class ShippedModelTests(unittest.TestCase):
    """The model in assets/ loads through Windows ML and reads made-up names drawn in the
    game's fonts (by the training project, not part of this repository), with this charset."""

    @classmethod
    def setUpClass(cls):
        cls.app = QGuiApplication.instance() or QGuiApplication([])
        cls.reader = namereader.NameReader()
        if not cls.reader.available():
            raise unittest.SkipTest(f"no Windows ML here: {cls.reader.error}")

    @classmethod
    def tearDownClass(cls):
        cls.reader.close()

    def test_it_has_this_charsets_classes(self):
        self.assertEqual(self.reader.classes, len(namereader.CHARSET) + 1)

    def test_it_reads_a_tab_line_and_a_loading_line_surely(self):
        lines = {
            "tab": QImage(str(DATA / "namereader_tab.png")),
            "loading": QImage(str(DATA / "namereader_loading.png")),
        }
        found = self.reader.read(lines)
        self.assertEqual(found["tab"][0], "PREDOVERLAY7")
        self.assertEqual(found["loading"][0], "Test_Name42")
        self.assertTrue(all(scoreboard.sure(r) for r in found.values()), found)


def lines_of(path, classes, p=0.9):
    """Log-probabilities of a line whose best path is ``path`` (class ids), each step
    ``p`` likely and the rest of it spread evenly over the other classes."""
    rest = math.log((1 - p) / (classes - 1))
    logp = []
    for k in path:
        row = [rest] * classes
        row[k] = math.log(p)
        logp += row
    return logp


@unittest.skipIf(namereader is None, "PySide6 missing")
class ScoreTests(unittest.TestCase):
    """The CTC scores: what a text is worth, and when a known name beats the reading."""

    C = None

    def setUp(self):
        self.classes = len(namereader.CHARSET) + 1
        self.k = {c: namereader.CHARSET.index(c) + 1 for c in "ABCDEX"}

    def test_the_score_of_one_path_text_matches_by_hand(self):
        # Two steps, "A" then blank: paths spelling "A" are A-blank, A-A, blank-A.
        logp = lines_of([self.k["A"], 0], self.classes, 0.9)
        rest = math.log(0.1 / (self.classes - 1))
        want = math.log(
            math.exp(math.log(0.9) * 2)  # A, blank
            + math.exp(math.log(0.9) + rest)  # A, A
            + math.exp(rest + rest)  # blank, A
        )
        self.assertAlmostEqual(namereader.score(logp, 2, self.classes, "A"), want, places=6)
        self.assertIsNone(namereader.score(logp, 2, self.classes, "☃"))  # not in the charset

    def test_the_text_is_worth_at_least_its_best_path(self):
        logp = lines_of([self.k["A"], self.k["A"], 0, self.k["B"]], self.classes, 0.8)
        reading = namereader.decode(logp, 4, self.classes)
        text, value = namereader.likely(logp, 4, self.classes, reading)
        self.assertEqual(text, "AB")
        self.assertGreater(value, reading[1])

    def test_a_known_name_must_beat_the_reading_itself(self):
        path = [self.k["A"], 0, self.k["B"], 0, self.k["C"]]
        logp = lines_of(path, self.classes, 0.75)
        reading = namereader.likely(logp, 5, self.classes, namereader.decode(logp, 5, self.classes))
        # "AB" fits the line far worse than "ABC" does: never taken over it.
        self.assertFalse(namereader.choose(logp, 5, self.classes, reading, ["AB"])[2])
        self.assertTrue(namereader.choose(logp, 5, self.classes, reading, ["ABC", "XED"])[2])

    def test_names_far_from_the_reading_are_not_scored(self):
        self.assertFalse(namereader._near("ABCDEFGHIJKLMN", "ABC"))
        self.assertTrue(namereader._near("ABCD", "ABC"))


if __name__ == "__main__":
    unittest.main()
