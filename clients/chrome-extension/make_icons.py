"""One-off icon generator for the Engram Notifications extension.

Run once with: uv run --no-sync python make_icons.py
Emits icons/icon16.png, icons/icon48.png, icons/icon128.png — solid rounded
squares with an "E" mark. Not part of the extension's runtime; re-run only if
you want to regenerate the icons.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).parent / "icons"
BG = (58, 92, 214)  # indigo
FG = (255, 255, 255)


def make_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = max(2, size // 5)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=BG)

    font = None
    for font_name in ("arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(font_name, int(size * 0.62))
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    text = "E"
    bbox = draw.textbbox((0, 0), text, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]),
        text,
        fill=FG,
        font=font,
    )
    return img


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    for size in (16, 48, 128):
        make_icon(size).save(OUT_DIR / f"icon{size}.png")
        print(f"wrote icons/icon{size}.png")


if __name__ == "__main__":
    main()
