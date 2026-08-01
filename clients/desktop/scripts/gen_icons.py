#!/usr/bin/env python3
"""One-off icon generator for Engram Tray.

Draws a simple filled-circle "node" mark (dark-friendly, works on light/dark
trays) plus an "unread" variant with a small red badge dot. Produces the
full icon set Tauri v2 expects in src-tauri/icons/, and two small tray-only
PNGs embedded at compile time via include_bytes! in tray.rs.

Run once with: python scripts/gen_icons.py
"""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ICONS = os.path.join(HERE, "..", "src-tauri", "icons")
os.makedirs(ICONS, exist_ok=True)

# Brand-ish teal/violet "node" mark on transparent background.
FILL = (108, 99, 255, 255)      # violet-blue, reads fine on light+dark trays
RING = (255, 255, 255, 235)
BADGE = (235, 64, 64, 255)


def draw_mark(size: int, badge: bool) -> Image.Image:
    # Supersample for smooth edges.
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = s * 0.12
    d.ellipse([pad, pad, s - pad, s - pad], fill=FILL)
    ring_w = max(2, int(s * 0.045))
    d.ellipse([pad, pad, s - pad, s - pad], outline=RING, width=ring_w)
    # inner dot to read as a "node"
    cx = cy = s / 2
    r = s * 0.14
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=RING)
    if badge:
        br = s * 0.22
        bx = s - br * 1.1
        by = br * 1.1
        d.ellipse([bx - br, by - br, bx + br, by + br], fill=(0, 0, 0, 0))
        d.ellipse([bx - br * 0.82, by - br * 0.82, bx + br * 0.82, by + br * 0.82], fill=BADGE)
    return img.resize((size, size), Image.LANCZOS)


def save_set(base: Image.Image, prefix: str):
    sizes = {
        "32x32.png": 32,
        "128x128.png": 128,
        "128x128@2x.png": 256,
        f"{prefix}.png": 256,
    }
    for name, sz in sizes.items():
        base.resize((sz, sz), Image.LANCZOS).save(os.path.join(ICONS, name))


# --- main app icon set (normal state) ---
normal_256 = draw_mark(256, badge=False)
normal_256.save(os.path.join(ICONS, "icon.png"))
for name, sz in {"32x32.png": 32, "128x128.png": 128, "128x128@2x.png": 256}.items():
    normal_256.resize((sz, sz), Image.LANCZOS).save(os.path.join(ICONS, name))

# Windows .ico (multi-size) + macOS .icns from the same source.
ico_sizes = [16, 24, 32, 48, 64, 128, 256]
normal_256.save(
    os.path.join(ICONS, "icon.ico"),
    sizes=[(s, s) for s in ico_sizes],
)
normal_256.save(os.path.join(ICONS, "icon.icns"))

# --- tray-only PNGs (small, embedded via include_bytes!) ---
draw_mark(32, badge=False).save(os.path.join(ICONS, "tray-normal.png"))
draw_mark(32, badge=True).save(os.path.join(ICONS, "tray-unread.png"))

print("wrote icons to", os.path.abspath(ICONS))
