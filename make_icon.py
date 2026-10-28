"""
make_icon.py
============
Generates a simple, clean placeholder app icon (scissors = крой/cutting theme)
on a rounded blue→indigo gradient tile. Outputs:
  * app.ico          (multi-size: 16/32/48/64/128/256 — for PyInstaller --icon)
  * app_preview.png  (256px, for eyeballing the design)

Run:  python make_icon.py
"""
from __future__ import annotations

import math

from PIL import Image, ImageDraw

S = 1024  # supersample canvas; downscaled at the end for crisp anti-aliasing
WHITE = (255, 255, 255, 255)
GOLD = (255, 201, 92, 255)       # pivot screw accent
GRAD_TOP = (59, 130, 246)        # blue-500
GRAD_BOTTOM = (26, 35, 110)      # deep indigo


def _gradient_tile() -> Image.Image:
    """Rounded square with a vertical blue→indigo gradient."""
    grad = Image.new("RGB", (1, S))
    for y in range(S):
        t = y / (S - 1)
        grad.putpixel((0, y), tuple(
            int(GRAD_TOP[i] * (1 - t) + GRAD_BOTTOM[i] * t) for i in range(3)))
    grad = grad.resize((S, S)).convert("RGBA")

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [int(0.05 * S), int(0.05 * S), int(0.95 * S), int(0.95 * S)],
        radius=int(0.20 * S), fill=255)

    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile.paste(grad, (0, 0), mask)
    return tile


def _edge_point(center, toward, r):
    """Point on the circle of radius r around `center`, facing `toward`."""
    dx, dy = toward[0] - center[0], toward[1] - center[1]
    d = math.hypot(dx, dy) or 1
    return (center[0] + dx / d * r, center[1] + dy / d * r)


def _stroke(draw, p1, p2, w):
    """Thick line with round caps."""
    draw.line([p1, p2], fill=WHITE, width=w)
    r = w / 2
    for p in (p1, p2):
        draw.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=WHITE)


def _draw_scissors(img: Image.Image) -> None:
    d = ImageDraw.Draw(img)

    def P(fx, fy):
        return (fx * S, fy * S)

    pivot = P(0.50, 0.575)
    tip_l, tip_r = P(0.30, 0.20), P(0.70, 0.20)        # blade tips (top)
    ring_l, ring_r = P(0.375, 0.775), P(0.625, 0.775)  # handle centers (bottom)
    blade_w = int(0.052 * S)
    ring_rad = int(0.090 * S)
    ring_w = int(0.040 * S)

    # Two crossing arms: tip -> pivot -> opposite ring edge.
    _stroke(d, tip_r, pivot, blade_w)
    _stroke(d, pivot, _edge_point(ring_l, pivot, ring_rad), blade_w)
    _stroke(d, tip_l, pivot, blade_w)
    _stroke(d, pivot, _edge_point(ring_r, pivot, ring_rad), blade_w)

    # Handle rings.
    for c in (ring_l, ring_r):
        d.ellipse([c[0] - ring_rad, c[1] - ring_rad, c[0] + ring_rad, c[1] + ring_rad],
                  outline=WHITE, width=ring_w)

    # Pivot screw (gold accent).
    pr = int(0.030 * S)
    d.ellipse([pivot[0] - pr, pivot[1] - pr, pivot[0] + pr, pivot[1] + pr], fill=GOLD)


def main():
    img = _gradient_tile()
    _draw_scissors(img)

    img.save("app_icon_1024.png")  # full-res source for the macOS .icns (CI)
    icon = img.resize((256, 256), Image.LANCZOS)
    icon.save("app_preview.png")
    icon.save("app.ico", format="ICO",
              sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Wrote app.ico, app_preview.png and app_icon_1024.png")


if __name__ == "__main__":
    main()
