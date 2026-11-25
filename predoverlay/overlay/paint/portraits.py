"""Hero art: read from disk once, resampled once per size on the screen.

A file still downloading is looked for again a little later; until it is
there the panel draws a tile with the hero's initials.
"""

import re
import time

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QImage, QLinearGradient, QPainter, QPen

from ...portraits import path as portrait_path
from .common import BAR_W, FADE_FOCUS, FADE_W, PORTRAIT, col, font, text

PORTRAIT_LOOK_AGAIN = 2.0  # seconds before a missing portrait's file is looked for again
_SOURCES = {}  # icon -> the whole portrait
_SCALED = {}  # (icon, width, height) -> sized for the screen
_ABSENT = {}  # icon -> when to look for its file again


def look_for_art():
    """New portraits are on disk: what was missing may be there now."""
    _ABSENT.clear()


def _source(icon):
    """The portrait from disk, once; a file still downloading is looked for again soon."""
    if icon in _SOURCES:
        return _SOURCES[icon]
    if not icon or time.monotonic() < _ABSENT.get(icon, 0.0):
        return None
    image = QImage(str(portrait_path(icon)))
    if image.isNull():
        _ABSENT[icon] = time.monotonic() + PORTRAIT_LOOK_AGAIN
        return None
    _SOURCES[icon] = image
    return image


def _portrait_image(icon, pixels):
    """The portrait resampled once to its size on the screen; None without its file."""
    key = (icon, pixels, pixels)
    if key not in _SCALED:
        source = _source(icon)
        if source is None:
            return None
        _SCALED[key] = source.scaled(
            pixels,
            pixels,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return _SCALED[key]


def _fade_image(icon, width, height):
    """The art at the row's edge: zoomed past the row's height, cut to it around the face,
    then dissolved to the right so it has no hard edge against the plate."""
    key = (icon, width, height)
    if key not in _SCALED:
        source = _source(icon)
        if source is None:
            return None
        square = source.scaled(
            width,
            width,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        image = square.copy(0, round((width - height) * FADE_FOCUS), width, height)
        image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        q = QPainter(image)
        q.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        q.fillRect(image.rect(), _fade_mask(width))
        q.end()
        _SCALED[key] = image
    return _SCALED[key]


def _fade_mask(width, left=0.0):
    mask = QLinearGradient(left, 0, left + width, 0)
    mask.setColorAt(0.0, QColor(0, 0, 0, 255))
    mask.setColorAt(0.42, QColor(0, 0, 0, 255))
    mask.setColorAt(0.58, QColor(0, 0, 0, 200))
    mask.setColorAt(0.74, QColor(0, 0, 0, 96))
    mask.setColorAt(0.88, QColor(0, 0, 0, 24))
    mask.setColorAt(1.0, QColor(0, 0, 0, 0))
    return mask


def _initials(hero):
    words = [
        w for w in re.split(r"[^A-Z0-9]+", hero.upper()) if w and w not in ("THE", "LT", "AND")
    ]
    return "".join(w[0] for w in words[:2]) if len(words) > 1 else (words or [hero])[0][:2]


def _tile(p, rect, hero):
    """No art for this hero: a clean tile with its initials."""
    fill = QLinearGradient(0, rect.top(), 0, rect.bottom())
    fill.setColorAt(0.0, col("hilite"))
    fill.setColorAt(1.0, col("row_c"))
    p.fillRect(rect, fill)
    if hero:
        f = font(int(rect.height() * 0.38), 600, 0.04)
        base = rect.center().y() + QFontMetricsF(f).capHeight() / 2
        text(p, rect.center().x(), base, _initials(hero), f, col("grey1"), "c")


def _portrait(p, rect, icon, hero, tone=None):
    """A square hero portrait; ``tone`` draws the thin result border inside its edge."""
    pixels = max(1, round(rect.width() * p.worldTransform().m11()))
    image = _portrait_image(icon, pixels)
    if image is None:
        _tile(p, rect, hero)
    else:
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(rect, image)
        p.restore()
    if tone is not None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(col("bg", 0.75), 1.0))  # the game's dark gap inside the frame
        p.drawRect(rect.adjusted(2.5, 2.5, -2.5, -2.5))
        p.setPen(QPen(col(tone), 2.0))
        p.drawRect(rect.adjusted(1, 1, -1, -1))


def _main(p, rect, data):
    """The MAIN portrait at the row's edge, dissolving into the plate. Soft and unframed:
    only the past match's portrait gets a frame. No art yet: the same shape, empty."""
    main = data["main"] or {}
    box = QRectF(rect.left() + BAR_W, rect.top(), FADE_W, rect.height())
    scale = p.worldTransform().m11()
    image = _fade_image(main.get("icon"), round(FADE_W * scale), round(rect.height() * scale))
    if image is not None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(box, image)
        p.restore()
        return
    p.fillRect(box, _empty_fill(box))
    if main.get("hero"):
        f = font(14, 600, 0.04)
        base = box.center().y() + QFontMetricsF(f).capHeight() / 2
        text(p, box.left() + PORTRAIT / 2, base, _initials(main["hero"]), f, col("grey1"), "c")


def _empty_fill(box):
    fill = QLinearGradient(box.left(), 0, box.right(), 0)
    fill.setColorAt(0.0, col("border", 0.42))
    fill.setColorAt(0.46, col("border", 0.30))
    fill.setColorAt(1.0, col("border", 0.0))
    return fill
