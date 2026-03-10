#!/usr/bin/env python3
"""Generate extension icons — a simple vinyl label disc.

Run from the firefox-ext directory:
    python3 make_icons.py
Requires Pillow (already a project dependency).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw
import math

BG      = (26,  26,  26, 255)   # dark grey background
DISC    = (214, 132,  74, 255)  # burnt orange — dt_label accent colour
GROOVE  = (160,  90,  40, 255)  # darker ring
LABEL   = (230, 200, 170, 255)  # pale cream label centre
HOLE    = (26,  26,  26, 255)   # transparent centre hole


def draw_icon(size):
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    cx = cy = size / 2
    r  = size * 0.46   # outer disc radius

    # Outer disc
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DISC)

    # Two groove rings
    for frac in (0.78, 0.62):
        rg = r * frac
        w  = max(1, size // 16)
        draw.ellipse([cx - rg, cy - rg, cx + rg, cy + rg], outline=GROOVE, width=w)

    # Centre label circle
    rl = r * 0.38
    draw.ellipse([cx - rl, cy - rl, cx + rl, cy + rl], fill=LABEL)

    # Centre hole
    rh = r * 0.10
    draw.ellipse([cx - rh, cy - rh, cx + rh, cy + rh], fill=HOLE)

    return img


if __name__ == "__main__":
    out_dir = os.path.join(os.path.dirname(__file__), "icons")
    os.makedirs(out_dir, exist_ok=True)

    for size in (16, 32, 48, 96):
        img = draw_icon(size)
        path = os.path.join(out_dir, f"icon-{size}.png")
        img.save(path)
        print(f"  {path}")

    print("Done.")
