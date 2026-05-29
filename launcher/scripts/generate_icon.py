"""Render the Lucide `bot` icon to webagent.ico (multi-resolution).

Re-renders the SVG paths from `ui/favicon.svg` at each target resolution
with Pillow's ImageDraw (no SVG parser needed — paths are simple lines
and a rounded rect). Each size is freshly rasterized so small sizes stay
crisp instead of being a blurry downscale of the largest.

Output: launcher/assets/webagent.ico
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "webagent.ico"

# Icon background (matches launcher Screen background) + foreground stroke
BG = (10, 10, 20, 255)        # #0a0a14, opaque
FG = (255, 255, 255, 255)     # white
ACCENT = (57, 255, 20, 255)   # phosphor green for eyes — optional accent

# SVG viewBox is 0 0 24 24, stroke-width 2, round caps + joins
VIEW = 24
STROKE_SVG = 2.0


def _scale(p: Tuple[float, float], s: float) -> Tuple[float, float]:
    return (p[0] * s, p[1] * s)


def _line(draw: ImageDraw.ImageDraw, a: Tuple[float, float], b: Tuple[float, float],
          stroke_px: float, color=FG) -> None:
    # Round-capped line: use Pillow's line + filled circles at endpoints to mimic round caps.
    draw.line([a, b], fill=color, width=max(1, int(round(stroke_px))))
    r = stroke_px / 2.0
    for cx, cy in (a, b):
        draw.ellipse(
            (cx - r, cy - r, cx + r, cy + r),
            fill=color,
        )


def _rounded_rect(draw: ImageDraw.ImageDraw, x: float, y: float, w: float, h: float,
                  r: float, stroke_px: float, color=FG) -> None:
    # Pillow's rounded_rectangle with outline=color, width=stroke_px (no fill).
    draw.rounded_rectangle(
        (x, y, x + w, y + h),
        radius=r,
        outline=color,
        width=max(1, int(round(stroke_px))),
    )


def render_at(size: int) -> Image.Image:
    """Render the bot icon at `size`x`size` pixels with transparent background."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Optional dark rounded-square background — gives the .exe a tile look
    # in the taskbar/file explorer. Use a slightly smaller inset so it
    # doesn't kiss the cell edges.
    bg_inset = max(1, int(size * 0.04))
    bg_radius = max(2, int(size * 0.20))
    draw.rounded_rectangle(
        (bg_inset, bg_inset, size - bg_inset - 1, size - bg_inset - 1),
        radius=bg_radius,
        fill=BG,
    )

    # Drawing area: leave ~12% padding inside the background
    pad = int(size * 0.12)
    draw_size = size - 2 * pad

    s = draw_size / VIEW                              # SVG-unit → pixel scale
    stroke_px = max(1.0, STROKE_SVG * s)              # ~2/24 of icon size
    ox, oy = pad, pad                                 # origin offset

    def P(x: float, y: float) -> Tuple[float, float]:
        return (ox + x * s, oy + y * s)

    # 1. Antenna: M12 8 V4 H8 (polyline)
    _line(draw, P(12, 8), P(12, 4), stroke_px)
    _line(draw, P(12, 4), P(8, 4), stroke_px)

    # 2. Main body: rect x=4 y=8 w=16 h=12 rx=2
    body_x, body_y = P(4, 8)
    body_w = 16 * s
    body_h = 12 * s
    body_r = 2 * s
    _rounded_rect(draw, body_x, body_y, body_w, body_h, body_r, stroke_px)

    # 3. Left ear: M2 14 h2
    _line(draw, P(2, 14), P(4, 14), stroke_px)
    # 4. Right ear: M20 14 h2
    _line(draw, P(20, 14), P(22, 14), stroke_px)

    # 5. Eyes (use accent color so the icon has a hint of personality)
    eye_color = ACCENT
    _line(draw, P(9, 13), P(9, 15), stroke_px, color=eye_color)
    _line(draw, P(15, 13), P(15, 15), stroke_px, color=eye_color)

    return img


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    # Render each size separately so small frames stay crisp instead of
    # being a fuzzy downscale of the 256 master.
    images = {s: render_at(s) for s in sizes}

    # Pillow's ICO writer: pass the LARGEST image with sizes= listing all
    # target frames, and append_images= with the separately-rendered crisp
    # versions. Pillow keeps the explicit append_images frames instead of
    # downscaling the master, when the size matches.
    base = images[256]
    base.save(
        OUT,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=[images[s] for s in sizes if s != 256],
    )
    # Verify the file is multi-frame
    from PIL import Image as _Image
    with _Image.open(OUT) as verify:
        embedded = sorted(verify.ico.sizes()) if hasattr(verify, "ico") else []
    print(f"[icon] wrote {OUT}  ({OUT.stat().st_size / 1024:.1f} KB)")
    print(f"[icon] embedded sizes: {embedded}")

    # Also dump a 256x256 PNG preview for sanity
    preview = OUT.with_name("webagent_256.png")
    images[256].save(preview, format="PNG")
    print(f"[icon] preview {preview}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
