"""Which game screen is showing, from a few pixels (no OCR): the loading screen today.

The loading screen prints its role labels between the two teams; MIDLANE sits in the
middle column whichever way the columns run. Its white glyphs (and the midlane icon
above them) are matched against masks taken from 2026-11-19 frames. On the captures of
2026-11-19..21, scaled to 1440p, 1080p and 720p as Qt does it, every loading screen
scored 0.73 or more and no draft, TAB or end frame more than 0.09 (the practice mode's
loading screen, which has no cards, gives 0).

Boxes are in design pixels of a 2560x1440 screen; a grab of any size is scaled to the
mask's size (half the box), so other resolutions read the same way (not measured).
"""

# name: (design box x, y, w, h), mask width, height, mask bits as hex (row by row)
ANCHORS = {
    "label": (
        (1560, 684, 200, 48),
        100,
        24,
        (
            "802000000000000000000000080000000000000000000000000000000000000000000000"
            "000000000000000000000000000000000000000000000000000000000000000000000000"
            "000000000000000000000000000000000000000000000000001044f84018308f80000000"
            "00018c484401c3088000000000001dc48440143888000000000001744824024388800000"
            "0000001244824020248f0000000000010448240222288000000000001044844062238800"
            "000000000104484407e2188000000000001044f87c41208f800000000000000000000000"
            "000000000000000000000000000000000000000000000000000000000000000000000000"
            "000000000000000000000000000000000000000000000000000000000000000000000000"
            "0000000000000"
        ),
    ),
    "icon": (
        (1610, 610, 100, 70),
        50,
        35,
        (
            "4000000000003040000000000e140000000007870000000001f1c0000000002670000000"
            "00009800000000002600000000001b000000000007800000000001e00000000800300040"
            "0002000c0010000080030008000020004002000004000000800001000000200000408000"
            "1800001810020e000007060107000000f080c38000001e3061e0000003de1df00000007f"
            "87f80000000fc1fc00000003f03e000000007c0f000000000e03c0000"
        ),
    ),
}

WHITE = 225  # a glyph pixel: every channel above this
LOADING = 0.4  # the best anchor's overlap with its mask for "the loading screen"
REGION = (1560, 610, 200, 122)  # the box around every anchor: one small grab holds them


def _mask(hexed, width, height):
    bits = bin(int("".join(hexed), 16))[2:].zfill(width * height)
    return [bit == "1" for bit in bits[-width * height :]]


_MASKS = {name: _mask(hx, w, h) for name, (_, w, h, hx) in ANCHORS.items()}


def white(image, width, height):
    """The image scaled to width x height (smooth), as a list of glyph-white flags."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    small = image.scaled(
        width,
        height,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ).convertToFormat(QImage.Format.Format_RGB32)
    stride = small.bytesPerLine()
    data = bytes(small.constBits())[: stride * height]
    out = []
    for y in range(height):
        row = data[y * stride : y * stride + width * 4]
        out.extend(min(row[i], row[i + 1], row[i + 2]) > WHITE for i in range(0, width * 4, 4))
    return out


def overlap(a, b):
    both = sum(1 for x, y in zip(a, b) if x and y)
    either = sum(1 for x, y in zip(a, b) if x or y)
    return both / either if either else 0.0


def loading_score(region):
    """The loading screen's anchors against a grab of REGION (a QImage): 0..1."""
    rx, ry, rw, rh = REGION
    sx, sy = region.width() / rw, region.height() / rh
    best = 0.0
    for name, ((x, y, w, h), mw, mh, _) in ANCHORS.items():
        box = (round((x - rx) * sx), round((y - ry) * sy), round(w * sx), round(h * sy))
        best = max(best, overlap(white(region.copy(*box), mw, mh), _MASKS[name]))
    return best


def is_loading(region):
    return loading_score(region) >= LOADING
