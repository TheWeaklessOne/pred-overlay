"""Draw the README's screenshots from the app's own preview: made-up players, no game.

    python tools/make_screenshots.py

Writes docs/screenshots/panel.png (the loading-screen panel) and card.png (the TAB
card). Nothing is shown on screen. Run it again after a change to what the panel or the
card draws, so the pictures never show what the app no longer does.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter  # noqa: E402

from predoverlay.overlay import paint, preview  # noqa: E402
from predoverlay.overlay.rows import row  # noqa: E402

SCALE = 1.5  # the pictures are drawn larger than on a 1440p screen, to stay sharp
MARGIN = 24
BACKGROUND = QColor(20, 26, 36)  # a dark map behind the panel


def save(name, width, height, draw):
    image = QImage(
        round(width * SCALE) + 2 * MARGIN,
        round(height * SCALE) + 2 * MARGIN,
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    image.fill(BACKGROUND)
    painter = QPainter(image)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
    painter.translate(MARGIN, MARGIN)
    painter.scale(SCALE, SCALE)
    draw(painter)
    painter.end()
    path = ROOT / "docs" / "screenshots" / f"{name}.png"
    image.save(str(path))
    print(f"{path.relative_to(ROOT)}: {image.width()}x{image.height()}")


def main():
    app = QGuiApplication(sys.argv[:1])  # noqa: F841  (fonts need it; no window is made)
    view = preview.preview_view("loading")
    motion = paint.Motion()
    allies, enemies = [row(p) for p in view.allies], [row(p) for p in view.enemies]
    width, height = paint.stack_size(len(allies), len(enemies))
    label = " · ".join(m for m in (view.mode_name, view.mode) if m)
    save(
        "panel",
        width,
        height,
        lambda p: paint.paint_stack(p, width, allies, enemies, motion, mode=label),
    )
    data, seen = enemies[1], {"hero": "GRUX", "role": "OFFLANE"}
    save("card", *paint.card_size(data, seen), lambda p: paint.paint_card(p, data, motion, seen))


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "windows" if os.name == "nt" else "offscreen")
    main()
