"""Reusable TUI widgets for the launcher.

`Slider` — a focusable, mouse-draggable horizontal slider. Textual 8.x ships
no built-in slider, so this is a small custom one. It supports:

- click-to-set and click-drag the handle (mouse),
- Left/Right to step, Home/End to jump to min/max (keyboard, when focused),

and posts a `Slider.Changed` message carrying the new value. The owning panel
listens for that and pushes the change into the animation live.
"""

from __future__ import annotations

from typing import Callable

from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget

# Fixed column widths for the label and the value readout; the track fills the
# space between them.
_LABEL_W = 11
_VALUE_W = 8

# Track glyphs.
_FILLED = "━"
_TRACK = "─"
_HANDLE = "●"

# Accent colors (launcher green family — this is the TUI's own theme, not the
# web app's light/dark design system).
_C_FILLED = "#39ff14"
_C_HANDLE = "#aaffaa"
_C_TRACK = "#2a2a55"
_C_LABEL = "#aaaaff"
_C_VALUE = "#d8d8f0"
_C_DIM = "#5a5a8a"


class Slider(Widget, can_focus=True):
    """A one-line labeled slider: ``Label  ━━━●────  value``."""

    DEFAULT_CSS = """
    Slider {
        height: 1;
        width: 100%;
        padding: 0;
        margin: 0;
    }
    Slider:focus {
        text-style: bold;
    }
    """

    BINDINGS = [
        Binding("left,down",  "step(-1)", "−", show=False),
        Binding("right,up",   "step(1)",  "+", show=False),
        Binding("home",       "to_min",   "Min", show=False),
        Binding("end",        "to_max",   "Max", show=False),
    ]

    value: reactive[float] = reactive(0.0)

    class Changed(Message):
        """Posted whenever the slider value changes (by mouse or key)."""

        def __init__(self, slider: "Slider", value: float) -> None:
            super().__init__()
            self.slider = slider
            self.value = value

        @property
        def control(self) -> "Slider":
            return self.slider

    def __init__(
        self,
        label: str,
        value: float,
        minimum: float,
        maximum: float,
        step: float,
        formatter: Callable[[float], str] | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._label = label
        self._min = float(minimum)
        self._max = float(maximum)
        self._step = float(step)
        self._format = formatter or (lambda v: f"{v:.2f}")
        self._grabbed = False
        self.value = self._clamp_snap(float(value))

    # ── value helpers ──────────────────────────────────────────────────
    def _clamp_snap(self, v: float) -> float:
        v = max(self._min, min(self._max, v))
        steps = round((v - self._min) / self._step)
        v = self._min + steps * self._step
        return round(v, 4)

    def _set_value(self, v: float, notify: bool = True) -> None:
        v = self._clamp_snap(v)
        if v != self.value:
            self.value = v
            if notify:
                self.post_message(self.Changed(self, v))

    def watch_value(self) -> None:  # reactive → repaint on any value change
        self.refresh()

    # ── geometry shared by render + mouse mapping ──────────────────────
    def _track_geom(self) -> tuple[int, int]:
        """Return (track_start_col, track_width) for the current widget width."""
        total = max(self.size.width, _LABEL_W + _VALUE_W + 6)
        track_start = _LABEL_W + 1
        track_w = max(4, total - _LABEL_W - _VALUE_W - 2)
        return track_start, track_w

    def _value_from_x(self, x: int) -> float:
        track_start, track_w = self._track_geom()
        rel = max(0, min(track_w - 1, x - track_start))
        frac = rel / max(1, track_w - 1)
        return self._min + frac * (self._max - self._min)

    # ── mouse ──────────────────────────────────────────────────────────
    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.focus()
        self._grabbed = True
        self.capture_mouse()
        self._set_value(self._value_from_x(event.x))
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._grabbed:
            self._set_value(self._value_from_x(event.x))
            event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._grabbed:
            self._grabbed = False
            self.release_mouse()
            event.stop()

    # ── keyboard ───────────────────────────────────────────────────────
    def action_step(self, direction: int) -> None:
        self._set_value(self.value + direction * self._step)

    def action_to_min(self) -> None:
        self._set_value(self._min)

    def action_to_max(self) -> None:
        self._set_value(self._max)

    # ── render ─────────────────────────────────────────────────────────
    def render(self) -> Text:
        track_start, track_w = self._track_geom()
        frac = 0.0
        span = self._max - self._min
        if span > 0:
            frac = (self.value - self._min) / span
        handle_pos = int(round(frac * (track_w - 1)))

        focused = self.has_focus
        c_handle = "#ffffff" if focused else _C_HANDLE
        c_filled = _C_FILLED
        c_track = _C_DIM if focused else _C_TRACK

        text = Text(no_wrap=True, overflow="crop")
        text.append(
            self._label.ljust(_LABEL_W)[:_LABEL_W] + " ",
            style=Style(color=_C_LABEL, bold=focused),
        )
        for i in range(track_w):
            if i == handle_pos:
                text.append(_HANDLE, style=Style(color=c_handle, bold=True))
            elif i < handle_pos:
                text.append(_FILLED, style=Style(color=c_filled))
            else:
                text.append(_TRACK, style=Style(color=c_track))
        text.append(
            " " + self._format(self.value).rjust(_VALUE_W)[:_VALUE_W],
            style=Style(color=_C_VALUE, bold=focused),
        )
        return text
