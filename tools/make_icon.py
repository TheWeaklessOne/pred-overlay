"""Build predoverlay/assets/icon.ico and icon.png from assets/icon-source.png.

The source is a 1024 px square; the icon gets rounded corners and every size
Windows asks for (tray 16-32 px, Explorer up to 256 px).

    python tools/make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def rounded(image, radius=0.2):
    size = image.size[0]
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), round(size * radius), 255)
    out = image.copy()
    out.putalpha(mask)
    return out


def main():
    source = Image.open(ROOT / "assets" / "icon-source.png").convert("RGBA")
    side = min(source.size)
    source = source.crop((0, 0, side, side)).resize((1024, 1024), Image.LANCZOS)
    icon = rounded(source)
    out = ROOT / "predoverlay" / "assets"
    out.mkdir(parents=True, exist_ok=True)
    icon.resize((256, 256), Image.LANCZOS).save(out / "icon.png")
    frames = [icon.resize((s, s), Image.LANCZOS) for s in SIZES]
    frames[-1].save(out / "icon.ico", sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    print("wrote", out / "icon.ico")


if __name__ == "__main__":
    main()
