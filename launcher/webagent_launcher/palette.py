"""Color palette system.

Each palette is a callable: (intensity, x_norm, y_norm, t_seconds) -> (r, g, b).

- intensity: 0.0..1.0 — brightness sampled from the animation field
- x_norm, y_norm: 0.0..1.0 — cell position in the grid
- t_seconds: animation time, used by rainbow / animated modes

This indirection lets the same animation widget plug into any palette
without caring how the colors are produced.
"""

from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass
from typing import Callable, List, Tuple

RGB = Tuple[int, int, int]
PaletteFn = Callable[[float, float, float, float], RGB]


# ── hex helpers ────────────────────────────────────────────────────────
def hex_to_rgb(h: str) -> RGB:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return (255, 255, 255)
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return (255, 255, 255)


def rgb_to_hex(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _clamp(v: float) -> int:
    return max(0, min(255, int(v)))


def _lerp(a: RGB, b: RGB, t: float) -> RGB:
    t = max(0.0, min(1.0, t))
    return (
        _clamp(a[0] + (b[0] - a[0]) * t),
        _clamp(a[1] + (b[1] - a[1]) * t),
        _clamp(a[2] + (b[2] - a[2]) * t),
    )


def _multiply(rgb: RGB, k: float) -> RGB:
    return (_clamp(rgb[0] * k), _clamp(rgb[1] * k), _clamp(rgb[2] * k))


def _hsv(h: float, s: float, v: float) -> RGB:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return (_clamp(r * 255), _clamp(g * 255), _clamp(b * 255))


# ── palette factories ──────────────────────────────────────────────────
def solid_palette(color: RGB) -> PaletteFn:
    def fn(intensity: float, x: float, y: float, t: float) -> RGB:
        return _multiply(color, intensity)
    return fn


def two_color_gradient(a: RGB, b: RGB, angle_deg: float = 90.0) -> PaletteFn:
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    def fn(intensity: float, x: float, y: float, t: float) -> RGB:
        # Project (x, y) onto the gradient axis, range ~[-0.71, 0.71]
        proj = (x - 0.5) * cos_a + (y - 0.5) * sin_a
        u = (proj + 0.71) / 1.42  # back to 0..1
        return _multiply(_lerp(a, b, u), intensity)
    return fn


def three_color_gradient(a: RGB, b: RGB, c: RGB) -> PaletteFn:
    def fn(intensity: float, x: float, y: float, t: float) -> RGB:
        if x < 0.5:
            base = _lerp(a, b, x * 2.0)
        else:
            base = _lerp(b, c, (x - 0.5) * 2.0)
        return _multiply(base, intensity)
    return fn


def rainbow_palette(speed: float = 1.0, spatial: float = 1.0) -> PaletteFn:
    """Animated hue cycle. `spatial` controls how much hue varies across grid."""
    def fn(intensity: float, x: float, y: float, t: float) -> RGB:
        hue = (t * 0.1 * speed) + (x + y) * 0.5 * spatial
        return _multiply(_hsv(hue, 0.85, 1.0), intensity)
    return fn


def pulse_palette(color: RGB, speed: float = 1.0) -> PaletteFn:
    """Single color whose brightness pulses over time (subtle alive feel)."""
    def fn(intensity: float, x: float, y: float, t: float) -> RGB:
        pulse = 0.7 + 0.3 * math.sin(t * 2.0 * speed)
        return _multiply(color, intensity * pulse)
    return fn


def fire_palette() -> PaletteFn:
    """Classic black → red → orange → yellow → white ramp keyed on intensity."""
    stops = [
        (0.00, (0, 0, 0)),
        (0.25, (90, 0, 0)),
        (0.50, (200, 40, 0)),
        (0.75, (255, 160, 0)),
        (1.00, (255, 240, 200)),
    ]

    def fn(intensity: float, x: float, y: float, t: float) -> RGB:
        v = max(0.0, min(1.0, intensity))
        for i in range(len(stops) - 1):
            v0, c0 = stops[i]
            v1, c1 = stops[i + 1]
            if v <= v1:
                u = (v - v0) / max(1e-6, (v1 - v0))
                return _lerp(c0, c1, u)
        return stops[-1][1]
    return fn


def ice_palette() -> PaletteFn:
    stops = [
        (0.00, (0, 0, 0)),
        (0.30, (10, 30, 80)),
        (0.60, (40, 130, 220)),
        (0.90, (180, 230, 255)),
        (1.00, (255, 255, 255)),
    ]

    def fn(intensity: float, x: float, y: float, t: float) -> RGB:
        v = max(0.0, min(1.0, intensity))
        for i in range(len(stops) - 1):
            v0, c0 = stops[i]
            v1, c1 = stops[i + 1]
            if v <= v1:
                u = (v - v0) / max(1e-6, (v1 - v0))
                return _lerp(c0, c1, u)
        return stops[-1][1]
    return fn


# ── presets the user can cycle through ────────────────────────────────
@dataclass
class Preset:
    name: str
    mode: str          # the config theme_mode this corresponds to
    color_a: str       # for save/restore
    color_b: str = ""
    color_c: str = ""
    builder: Callable[[], PaletteFn] = lambda: solid_palette((255, 255, 255))


PHOSPHOR = "#39ff14"
AMBER = "#ffb000"
CYAN = "#00e5ff"
MAGENTA = "#ff00ff"
HOT_PINK = "#ff2d95"
ELECTRIC_BLUE = "#1e90ff"
LIME = "#a3ff00"


PRESETS: List[Preset] = [
    Preset("Phosphor",      "solid",    PHOSPHOR,
           builder=lambda: solid_palette(hex_to_rgb(PHOSPHOR))),
    Preset("Amber CRT",     "solid",    AMBER,
           builder=lambda: solid_palette(hex_to_rgb(AMBER))),
    Preset("Cyan",          "solid",    CYAN,
           builder=lambda: solid_palette(hex_to_rgb(CYAN))),
    Preset("Magenta",       "solid",    MAGENTA,
           builder=lambda: solid_palette(hex_to_rgb(MAGENTA))),
    Preset("Sunset",        "gradient", "#ff4d00", "#ffb800",
           builder=lambda: two_color_gradient(hex_to_rgb("#ff4d00"), hex_to_rgb("#ffb800"), 45.0)),
    Preset("Vaporwave",     "gradient", HOT_PINK, CYAN,
           builder=lambda: two_color_gradient(hex_to_rgb(HOT_PINK), hex_to_rgb(CYAN), 135.0)),
    Preset("Neon Tide",     "gradient", "#7c00ff", "#00ffe0",
           builder=lambda: two_color_gradient(hex_to_rgb("#7c00ff"), hex_to_rgb("#00ffe0"), 90.0)),
    Preset("Tricolor",      "gradient", "#ff2d95", "#9d00ff", "#00e5ff",
           builder=lambda: three_color_gradient(
               hex_to_rgb("#ff2d95"), hex_to_rgb("#9d00ff"), hex_to_rgb("#00e5ff"))),
    Preset("Rainbow",       "rainbow",  "#ff0000",
           builder=lambda: rainbow_palette(speed=1.0, spatial=1.0)),
    Preset("Rainbow slow",  "rainbow",  "#ff0000",
           builder=lambda: rainbow_palette(speed=0.4, spatial=0.6)),
    Preset("Fire",          "ramp",     "#000000",
           builder=fire_palette),
    Preset("Ice",           "ramp",     "#000000",
           builder=ice_palette),
    Preset("Lime pulse",    "pulse",    LIME,
           builder=lambda: pulse_palette(hex_to_rgb(LIME), speed=1.0)),
]


def build_palette_from_config(cfg) -> PaletteFn:
    """Build palette callable from a LauncherConfig instance.

    If theme_mode matches one of the preset modes via stored colors, we still
    reconstruct deterministically from the raw color fields so user tweaks persist.
    """
    mode = (cfg.theme_mode or "rainbow").lower()
    a = hex_to_rgb(cfg.theme_color_a)
    b = hex_to_rgb(cfg.theme_color_b)
    c = hex_to_rgb(cfg.theme_color_c)
    speed = float(cfg.theme_speed or 1.0)

    if mode == "solid":
        return solid_palette(a)
    if mode == "gradient":
        return two_color_gradient(a, b, 90.0)
    if mode == "tri":
        return three_color_gradient(a, b, c)
    if mode == "rainbow":
        return rainbow_palette(speed=speed, spatial=1.0)
    if mode == "pulse":
        return pulse_palette(a, speed=speed)
    if mode == "fire":
        return fire_palette()
    if mode == "ice":
        return ice_palette()
    return rainbow_palette(speed=speed)


def apply_preset_to_config(preset: Preset, cfg) -> None:
    """Persist a preset selection back into LauncherConfig fields."""
    cfg.theme_mode = preset.mode
    if preset.color_a:
        cfg.theme_color_a = preset.color_a
    if preset.color_b:
        cfg.theme_color_b = preset.color_b
    if preset.color_c:
        cfg.theme_color_c = preset.color_c


def palette_from_theme(app) -> PaletteFn:
    """Build an animation palette from the app's ACTIVE Textual theme.

    Maps the theme's primary -> secondary into a gradient so the ASCII banner
    agrees with the chrome. For light themes we keep the saturated theme hues
    (never the light background) so the art stays legible on a light surface.
    """
    from .theme_colors import chrome_colors  # local import: theme_colors is dep-free

    c = chrome_colors(app)
    a = hex_to_rgb(c["primary"])
    b = hex_to_rgb(c["secondary"])
    return two_color_gradient(a, b, 90.0)
