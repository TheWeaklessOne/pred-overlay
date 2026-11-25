"""Player names read by the overlay's own model: a small CRNN trained on the game's fonts.

The model (assets/namereader.onnx, about 3.5 MB) reads one name line of the draft,
loading, TAB or end screen and runs through Windows ML (Windows.AI.MachineLearning,
part of Windows 10 1809 and later): no runtime to ship, and no Windows OCR language
needed for the names. It was trained on synthetic lines only, drawn in the fonts
identified from the screens (Josefin Sans on the draft, Barlow elsewhere) over
backgrounds cut from them (by a separate training project, not part of this repository).

Each reading comes with the log-probability of its text (the CTC sum over every path
that spells it): a reading the model is sure of is taken as it is; one it is less sure
of is one more version next to Windows OCR's; one below even that is left out
(roster.Reader, scoreboard.Reader). Pure Python and Qt: no numpy.

The model file is ONNX opset 9 (IR 4), what Windows ML runs from Windows 10 2004 on.
"""

import array
import atexit
import contextlib
import math
import os
import threading
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

from ..errors import describe
from .charset import CHARSET, GAP, V6  # noqa: F401
from .lookup import written
from .scoreboard import SURE, Box, _key, sure  # noqa: F401 (the readers' side of it)

MODEL = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "namereader.onnx")
H, W = 32, 448  # model input: one grey line, scaled to 32 px high, padded right
# The name line inside each reader's box, in design pixels: (offset from the top, height).
LINE = {"draft": (18, 32), "loading": (0, 34), "tab": (0, 34), "end": (0, 34)}
# Lines per evaluation: Windows ML gains nothing from bigger batches here and keeps the
# memory of the biggest one for good (10 lines: about 70 MB more than 4).
BATCH = 4


def line(part, kind, design_h):
    """The name line of a reader's box image; ``design_h`` is the box's height in design
    pixels (the image may be bigger or smaller: another screen size)."""
    top, height = LINE[kind]
    pixels = part.height() / max(design_h, 1)
    return part.copy(0, round(top * pixels), part.width(), max(1, round(height * pixels)))


def prepare(image):
    """A line image -> the model's input (H * W floats): grey, 32 px high, stretched to
    the 5th..99.5th percentile of its brightness, padded with black on the right."""
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    width = min(W, max(8, round(gray.width() * H / max(gray.height(), 1))))
    gray = gray.scaled(
        width, H, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    )
    stride = gray.bytesPerLine()
    data = bytes(gray.constBits())[: stride * H]
    rows = [data[y * stride : y * stride + width] for y in range(H)]
    counts = [0] * 256
    for row in rows:
        for value, n in _histogram(row):
            counts[value] += n
    lo, hi = _percentile(counts, 0.05), _percentile(counts, 0.995)
    span = max(hi - lo, 8.0)
    table = [min(max((v - lo) / span, 0.0), 1.5) for v in range(256)]
    out = array.array("f")
    pad = array.array("f", [0.0]) * (W - width)
    for row in rows:
        out.extend(map(table.__getitem__, row))
        out.extend(pad)
    return out


def _histogram(row):
    return ((value, row.count(value)) for value in set(row))


def _percentile(counts, q):
    """The q-quantile of a 256-bin histogram (the value at rank q * (n - 1))."""
    rank, seen = q * (sum(counts) - 1), 0
    for value, n in enumerate(counts):
        seen += n
        if seen > rank:
            return float(value)
    return 255.0


def decode(logp, steps, classes, chars=CHARSET):
    """Greedy CTC over one line's log-probabilities -> (text, log-probability of the path).
    GAP marks stay, at the ends too: an emoji at a name's edge is what tells it from a
    plain namesake (lookup.pick)."""
    text, prev, total = [], 0, 0.0
    for t in range(steps):
        row = logp[t * classes : (t + 1) * classes]
        best = max(row)
        k = row.index(best)
        total += best
        if k != prev and k != 0:
            text.append(chars[k - 1])
        prev = k
    return "".join(text).strip(), total


NEG = -1e30
# A known name is taken for a line the model was unsure of when it is at least this
# likely and this many nats ahead of the next known name.
PICK, MARGIN = math.log(0.2), 5.0


def _add(a, b):
    """log(exp(a) + exp(b))."""
    if a < b:
        a, b = b, a
    return a if b <= NEG else a + math.log1p(math.exp(b - a))


def score(logp, steps, classes, text, chars=CHARSET):
    """log P(text | line): the CTC forward sum over every path spelling ``text``; None
    for a text with a character the model does not know."""
    labels = [chars.find(c) + 1 for c in text]
    if not labels or 0 in labels:
        return None
    ext = [0]
    for k in labels:
        ext += [k, 0]
    size = len(ext)
    alpha = [NEG] * size
    alpha[0], alpha[1] = logp[0], logp[ext[1]]
    skip = [s > 1 and ext[s] != 0 and ext[s] != ext[s - 2] for s in range(size)]
    for t in range(1, steps):
        base, new = t * classes, [NEG] * size
        for s in range(min(size, 2 * t + 2)):
            a = alpha[s]
            if s:
                a = _add(a, alpha[s - 1])
                if skip[s]:
                    a = _add(a, alpha[s - 2])
            if a > NEG:
                new[s] = a + logp[base + ext[s]]
        alpha = new
    return _add(alpha[-1], alpha[-2])


def likely(logp, steps, classes, reading, chars=CHARSET):
    """(text, log P(text | line)) for a greedy reading. Its best path is a lower bound;
    the sum over every path spelling it is what the text is worth (a long name read
    right is spread over many paths)."""
    text, path = reading
    if not text or path >= SURE:
        return text, path
    total = score(logp, steps, classes, text, chars)
    return text, path if total is None else max(path, total)


def _near(name, text):
    """A known name worth scoring against a reading: about as long. Names far longer or
    shorter cannot win, and each score costs a few ms of pure Python."""
    return abs(len(name) - len(text)) <= max(3, len(text) // 3)


def choose(logp, steps, classes, reading, names, chars=CHARSET):
    """The model's reading of a line, or one of ``names`` (as the screen spells them) when
    the model was unsure and that name is clearly the likeliest: (text, logp, picked).

    ``reading`` is (text, log P(text)). A name is taken when it is at least PICK likely,
    at least as likely as the reading itself, and MARGIN ahead of every name with
    another key."""
    text, own = reading
    if own >= SURE or not names:
        return text, own, False
    best = {}
    for name in dict.fromkeys(names):
        # Scored as the model writes it (Cyrillic twins as Latin letters, an emoji as GAP).
        shown = written(name)
        if not shown or not _near(shown, text):
            continue
        value, key = score(logp, steps, classes, shown, chars), _key(name)
        if value is not None and value > best.get(key, (NEG, ""))[0]:
            best[key] = (value, name)
    ranked = sorted(best.values(), reverse=True)
    if ranked and ranked[0][0] >= max(PICK, own):
        runner = ranked[1][0] if len(ranked) > 1 else NEG
        if ranked[0][0] - runner >= MARGIN:
            return ranked[0][1], ranked[0][0], True
    return text, own, False


@contextlib.contextmanager
def _quiet_stderr():
    """Native writes to the process's stderr go nowhere for a moment (a console run)."""
    try:
        saved = os.dup(2)
    except OSError:  # no stderr (the tray app): nothing to quiet
        yield
        return
    try:
        with open(os.devnull, "wb") as null:
            os.dup2(null.fileno(), 2)
            yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


class NameReader:
    """The model, loaded on first use on the caller's (worker) thread; ``read`` is safe
    to call from several threads. ``error`` says why it is unavailable."""

    def __init__(self, path=MODEL):
        self.path = path
        self.session = None
        self.error = None  # why the model did not load
        self.failure = None  # the first reading that raised: "Type: message"
        self.closed = False
        self.classes = None
        self.lock = threading.Lock()

    def _load(self):
        from winrt.windows.ai.machinelearning import (
            LearningModel,
            LearningModelDevice,
            LearningModelDeviceKind,
            LearningModelSession,
        )

        with _quiet_stderr():  # Windows ML's runtime prints its CPU survey there (UTF-16)
            model = LearningModel.load_from_file_path(os.path.abspath(self.path))
            device = LearningModelDevice(LearningModelDeviceKind.CPU)  # faster than DirectML
            self.session = LearningModelSession(model, device)
            self._run([array.array("f", [0.0]) * (H * W)])  # the first runs are slow: warm up

    def available(self):
        with self.lock:
            if self.closed:
                return False
            if self.session is None and self.error is None:
                try:
                    if not os.path.exists(self.path):
                        raise FileNotFoundError(self.path)
                    self._load()
                    if self.closed:  # close() gave up waiting while this loaded
                        self.session = None
                except Exception as exc:  # no Windows ML (old Windows), a damaged file
                    self.session, self.error = None, f"{type(exc).__name__}: {exc}"
            return self.session is not None

    def warm(self, log=None):
        """Load the model on a thread of its own (the game just started: its first reading
        should not pay for it) and log once how that went."""

        def load():
            began = time.monotonic()
            if self.available():
                if log:
                    log(
                        f"name model ready: {self.classes} classes, {time.monotonic() - began:.2f} s"
                    )
            elif log and self.error:
                log(f"name model not loaded ({self.error}); names by Windows OCR alone")

        if not self.closed and self.session is None and self.error is None:
            threading.Thread(target=load, daemon=True, name="pred-namemodel").start()

    def _run(self, inputs, names=None):
        from winrt.windows.ai.machinelearning import LearningModelBinding, TensorFloat

        x = array.array("f")
        for item in inputs:
            x.extend(item)
        binding = LearningModelBinding(self.session)
        shape = array.array("q", [len(inputs), 1, H, W])
        binding.bind("x", TensorFloat.create_from_shape_array_and_data_array(shape, x))
        result = self.session.evaluate(binding, "names")
        out = result.outputs["logp"].as_(TensorFloat)
        n, steps, classes = list(out.shape)
        if classes - 1 not in (len(CHARSET), V6):  # v7, or v6 (no Cyrillic, no GAP)
            raise ValueError(f"model has {classes} classes, expected {len(CHARSET) + 1}")
        chars = CHARSET[: classes - 1]
        self.classes = classes
        flat = array.array("f")
        flat.frombytes(bytes(memoryview(out.create_reference())))
        size = steps * classes
        names = names or [()] * n
        found = []
        for i in range(n):
            logp = flat[i * size : (i + 1) * size].tolist()
            reading = likely(logp, steps, classes, decode(logp, steps, classes, chars), chars)
            found.append(choose(logp, steps, classes, reading, names[i], chars))
        return found

    def close(self):
        """Let the session go while Python still runs: WinRT freed at interpreter
        teardown crashes the process on its way out."""
        self.closed = True  # no later reading loads it again
        if self.lock.acquire(timeout=2):  # a reading on a worker may be under way
            try:
                self.session = None
            finally:
                self.lock.release()

    def read(self, lines, names=None):
        """{key: line QImage} -> {key: (text, log-probability, picked)}; {} when unavailable.

        ``names`` ({key: [name, ...]}, spelled as the screen shows them) are the names a
        line may hold: one the model was unsure of becomes the likeliest of them, if it
        is clearly ahead (``picked``)."""
        if not lines or not self.available():
            return {}
        keys = list(lines)
        known = [(names or {}).get(k, ()) for k in keys]
        found = {}
        try:
            inputs = [prepare(lines[k]) for k in keys]
            with self.lock:
                if self.session is None:  # closed meanwhile
                    return {}
                try:
                    for i in range(0, len(keys), BATCH):
                        batch = self._run(inputs[i : i + BATCH], known[i : i + BATCH])
                        found.update(zip(keys[i : i + BATCH], batch))
                finally:
                    if self.closed:  # close() gave up waiting for this reading
                        self.session = None
        except Exception as exc:
            self.failure = self.failure or describe(exc)
            raise
        return found


READER = NameReader()  # one model for every reader
atexit.register(READER.close)
