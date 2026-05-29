"""Combined animation + logo widget.

Single render pass paints the procedural ASCII background AND stamps the
`webagent` block-letter logo on top — guarantees the logo is visible while
the animation flows around it. Avoids the layer/transparency dance that
doesn't reliably composite in terminal cells.
"""

from __future__ import annotations

import math
import random
import time
from typing import Callable, List

from rich.style import Style
from rich.text import Text
from textual.app import RenderResult
from textual.widget import Widget

from .ascii_anim import ANIM_STYLES, PLASMA, FLOWFIELD, RINGS, STATIC
from .palette import PaletteFn, rainbow_palette


# Block-letter logo for "webagent" — `█` and spaces only.
_LOGO_LINES = [
    "██   ██ ███████ ██████   █████   ██████  ███████ ███    ██ ████████",
    "██   ██ ██      ██   ██ ██   ██ ██       ██      ████   ██    ██   ",
    "██ █ ██ █████   ██████  ███████ ██   ███ █████   ██ ██  ██    ██   ",
    "███████ ██      ██   ██ ██   ██ ██    ██ ██      ██  ██ ██    ██   ",
    "██   ██ ███████ ██████  ██   ██  ██████  ███████ ██    ███    ██   ",
]
_LOGO_H = len(_LOGO_LINES)
_LOGO_W = max(len(line) for line in _LOGO_LINES)


class AnimatedStage(Widget):
    """Background animation with the logo + tagline composited into one render."""

    DEFAULT_CSS = """
    AnimatedStage {
        width: 100%;
        height: 100%;
        background: #08081a;
    }
    """

    def __init__(
        self,
        palette: PaletteFn | None = None,
        char_ramp: str = " .:-=+*#%@",
        fps: int = 24,
        style: str = PLASMA,
        speed: float = 1.0,
        intensity: float = 1.0,
        show_logo: bool = True,
    ) -> None:
        super().__init__()
        self._palette: PaletteFn = palette or rainbow_palette()
        self._ramp = char_ramp if len(char_ramp) >= 2 else " .:-=+*#%@"
        self._fps = max(4, min(60, fps))
        self._style = style if style in ANIM_STYLES else PLASMA
        self._speed = speed
        self._intensity = intensity
        self._show_logo = show_logo
        self._t0 = time.monotonic()
        self._noise: List[List[float]] | None = None
        self._noise_size: tuple[int, int] = (0, 0)

    # ── live config ───────────────────────────────────────────────────
    def set_palette(self, palette: PaletteFn) -> None:
        self._palette = palette

    def set_style(self, style: str) -> None:
        if style in ANIM_STYLES:
            self._style = style
            self._noise = None

    def set_speed(self, speed: float) -> None:
        self._speed = max(0.05, min(5.0, speed))

    def set_intensity(self, intensity: float) -> None:
        self._intensity = max(0.0, min(2.0, intensity))

    def set_ramp(self, ramp: str) -> None:
        if len(ramp) >= 2:
            self._ramp = ramp

    def set_fps(self, fps: int) -> None:
        self._fps = max(4, min(60, fps))
        # caller is expected to reset the interval

    # ── lifecycle ─────────────────────────────────────────────────────
    def on_mount(self) -> None:
        self._timer = self.set_interval(1.0 / self._fps, self.refresh)

    # ── field samplers (copied from AsciiAnimation; trivial / no shared state) ─
    def _sample_plasma(self, x: float, y: float, t: float) -> float:
        s = (
            math.sin(x * 6.0 + t * 1.3)
            + math.sin(y * 5.0 - t * 0.9)
            + math.sin((x + y) * 4.0 + t * 0.6)
            + math.sin(math.sqrt(x * x + y * y) * 8.0 - t * 1.1)
        )
        return (s + 4.0) / 8.0

    def _sample_flowfield(self, x: float, y: float, t: float) -> float:
        a = math.sin(x * 4.0 + math.cos(y * 3.0 + t * 0.5) * 2.0 + t * 0.3)
        b = math.sin(y * 5.0 + math.cos(x * 2.5 - t * 0.4) * 2.0 - t * 0.6)
        v = (a + b + 2.0) / 4.0
        return abs(math.sin(v * math.pi * 3.0 + t * 0.4))

    def _sample_rings(self, x: float, y: float, t: float) -> float:
        cx, cy = 0.5 + 0.2 * math.sin(t * 0.3), 0.5 + 0.2 * math.cos(t * 0.25)
        dx = x - cx
        dy = (y - cy) * 0.5
        d = math.sqrt(dx * dx + dy * dy)
        return 0.5 + 0.5 * math.sin(d * 24.0 - t * 2.0)

    def _ensure_static_noise(self, w: int, h: int) -> List[List[float]]:
        if self._noise is None or self._noise_size != (w, h):
            rng = random.Random(0xBEEF)
            self._noise = [[rng.random() for _ in range(w)] for _ in range(h)]
            self._noise_size = (w, h)
        return self._noise

    def _sample_static(self, x: float, y: float, t: float, w: int, h: int) -> float:
        noise = self._ensure_static_noise(w, h)
        dx = int((t * 6.0 * self._speed) % w)
        dy = int((t * 2.0 * self._speed) % h)
        ix = int(x * (w - 1) + dx) % w
        iy = int(y * (h - 1) + dy) % h
        nx = (ix + 1) % w
        a = noise[iy][ix]
        b = noise[iy][nx]
        u = ((x * (w - 1) + dx) % 1.0)
        return a * (1 - u) + b * u

    # ── render ────────────────────────────────────────────────────────
    def render(self) -> RenderResult:
        size = self.size
        w = max(2, size.width)
        h = max(2, size.height)
        t = (time.monotonic() - self._t0) * self._speed

        # Compute logo placement (centered in the stage area)
        if self._show_logo and h > _LOGO_H:
            logo_h = _LOGO_H
            logo_top = max(0, (h - logo_h) // 2)
            logo_left = max(0, (w - _LOGO_W) // 2)
        else:
            logo_top = -1
            logo_left = -1
            logo_h = 0

        text = Text(no_wrap=True, overflow="crop")
        ramp = self._ramp
        ramp_max = len(ramp) - 1
        palette = self._palette
        intensity_scale = self._intensity

        if self._style == PLASMA:
            sampler: Callable[[float, float, float], float] = self._sample_plasma
        elif self._style == FLOWFIELD:
            sampler = self._sample_flowfield
        elif self._style == RINGS:
            sampler = self._sample_rings
        else:
            sampler = lambda x, y, tt: self._sample_static(x, y, tt, w, h)

        # Render every cell. Logo glyphs overstamp the bg in the same pass.
        for row in range(h):
            yn = row / max(1, h - 1)
            # Pre-compute logo row index for this terminal row (if any)
            in_logo_row = (logo_top >= 0
                           and logo_top <= row < logo_top + logo_h)
            logo_row_idx = row - logo_top if in_logo_row else -1

            for col in range(w):
                xn = col / max(1, w - 1)
                v_raw = sampler(xn, yn, t)
                v = max(0.0, min(1.0, v_raw * intensity_scale))

                # Logo overstamp?
                if in_logo_row:
                    lx = col - logo_left
                    if 0 <= lx < _LOGO_W and _LOGO_LINES[logo_row_idx][lx] == "█":
                        # Bright logo block
                        ly_n = logo_row_idx / max(1, _LOGO_H - 1)
                        lx_n = lx / max(1, _LOGO_W - 1)
                        r, g, b = palette(1.0, lx_n, ly_n, t)
                        text.append("█", style=Style(color=f"rgb({r},{g},{b})", bold=True))
                        continue

                # Plain animation cell (dimmer where logo is to make logo pop)
                if in_logo_row:
                    v *= 0.45  # dim around the logo for contrast
                ch_idx = min(ramp_max, int(v * (ramp_max + 0.999)))
                ch = ramp[ch_idx]
                r, g, b = palette(v, xn, yn, t)
                text.append(ch, style=Style(color=f"rgb({r},{g},{b})"))

            if row < h - 1:
                text.append("\n")
        return text
