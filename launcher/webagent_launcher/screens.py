"""Modal/secondary screens: first-run setup, settings, confirmation dialog."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Select, Static

from .config import LauncherConfig
from .palette import PRESETS, Preset, apply_preset_to_config, build_palette_from_config
from .ascii_anim import ANIM_STYLES, ANIM_LABELS
from .widgets import Slider


# ── first-run setup ────────────────────────────────────────────────────
class SetupScreen(ModalScreen[bool]):
    """Asks for the path to the webagent project."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True, show=True),
    ]

    def action_cancel(self) -> None:
        self.dismiss(False)

    def __init__(self, cfg: LauncherConfig) -> None:
        super().__init__()
        self.cfg = cfg

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-panel"):
            yield Label("[b]webagent launcher — first-run setup[/b]", classes="label")
            yield Label(
                "Enter the path to your [b]webagent project folder[/b] "
                "(the directory containing [i]run.py[/i] and [i]app/[/i]).",
                classes="label",
            )
            yield Input(
                value=self.cfg.project_path or str(Path.cwd()),
                placeholder=r"C:\Users\You\Projects\webagent",
                id="path-input",
            )
            yield Label("", id="setup-error", classes="label")
            with Horizontal():
                yield Button("Save", variant="primary", id="setup-save")
                yield Button("Cancel", variant="default", id="setup-cancel")

    @on(Button.Pressed, "#setup-save")
    def _save(self) -> None:
        path = self.query_one("#path-input", Input).value.strip().strip('"')
        p = Path(path)
        err = self.query_one("#setup-error", Label)
        if not p.is_dir():
            err.update(f"[red]Not a directory:[/red] {path}")
            return
        if not (p / "run.py").exists():
            err.update(f"[red]No run.py found in[/red] {path}")
            return
        if not (p / "app").is_dir():
            err.update(f"[red]No app/ folder in[/red] {path}")
            return
        self.cfg.project_path = str(p)
        self.cfg.save()
        self.dismiss(True)

    @on(Button.Pressed, "#setup-cancel")
    def _cancel(self) -> None:
        self.dismiss(False)


# ── inline settings panel ───────────────────────────────────────────────
class SettingsPanel(Vertical):
    """Theme + animation settings, mounted INLINE where the log pane sits.

    Replaces the old centered modal so the animation stage above stays fully
    visible and updates live as the controls change. The owning app toggles
    this panel's visibility and handles saving/closing via callbacks.
    """

    def __init__(
        self,
        cfg: LauncherConfig,
        on_change: Callable[[], None],
        on_close: Callable[[], None],
    ) -> None:
        super().__init__(id="settings-panel")
        self.cfg = cfg
        self._on_change = on_change
        self._on_close = on_close
        self._preset_idx = self._guess_preset_idx()

    def _guess_preset_idx(self) -> int:
        for i, p in enumerate(PRESETS):
            if p.color_a.lower() == self.cfg.theme_color_a.lower() and p.mode == self.cfg.theme_mode:
                return i
        return 0

    def compose(self) -> ComposeResult:
        # Sticky header so the Close button is ALWAYS visible
        with Horizontal(id="settings-header"):
            yield Static("[b]Theme & Animation[/b]", id="settings-title")
            yield Button("Close (Esc)", variant="primary", id="settings-close")

        # Scrollable body. Style + the three sliders sit at the TOP so the most
        # used controls (the "it's too much" knobs) are visible without
        # scrolling; the preset list and ramp live below and scroll if needed.
        with VerticalScroll(id="settings-body"):
            yield Label("Animation style (Off = stop it):", classes="label")
            yield Select(
                [(ANIM_LABELS.get(s, s.title()), s) for s in ANIM_STYLES],
                value=self.cfg.animation_style if self.cfg.animation_style in ANIM_STYLES else "plasma",
                id="anim-style",
                allow_blank=False,
            )

            yield Label("Motion (drag, or ←/→ when focused):", classes="label")
            yield Slider(
                "Speed", self.cfg.theme_speed, 0.05, 5.0, 0.05,
                formatter=lambda v: f"{v:.2f}x", id="speed-slider",
            )
            yield Slider(
                "Intensity", self.cfg.animation_intensity, 0.0, 2.0, 0.05,
                formatter=lambda v: f"{v:.2f}", id="intensity-slider",
            )
            yield Slider(
                "FPS", float(self.cfg.fps), 4, 60, 2,
                formatter=lambda v: f"{int(round(v))}", id="fps-slider",
            )

            yield Label("Color preset (↑/↓ or click):", classes="label")
            yield ListView(
                *[ListItem(Label(f"  {p.name}")) for p in PRESETS],
                id="preset-list",
                initial_index=self._preset_idx,
            )

            yield Label("Character ramp (typed):", classes="label")
            yield Input(value=self.cfg.char_ramp, id="ramp-input")

    def focus_first(self) -> None:
        """Focus the Speed slider when the panel opens so ←/→ adjust motion
        immediately (the most common 'it's too much' tweak)."""
        try:
            self.query_one("#speed-slider", Slider).focus()
        except Exception:
            pass

    def sync_from_config(self) -> None:
        """Refresh every control from cfg. Call this after the app's Space/C
        cycle shortcuts so the open panel doesn't show stale values."""
        try:
            sel = self.query_one("#anim-style", Select)
            if self.cfg.animation_style in ANIM_STYLES and sel.value != self.cfg.animation_style:
                sel.value = self.cfg.animation_style
            # Sliders: update without re-posting Changed (no feedback loop).
            self.query_one("#speed-slider", Slider)._set_value(self.cfg.theme_speed, notify=False)
            self.query_one("#intensity-slider", Slider)._set_value(self.cfg.animation_intensity, notify=False)
            self.query_one("#fps-slider", Slider)._set_value(float(self.cfg.fps), notify=False)
            # Preset highlight (guard prevents a redundant re-apply).
            self._preset_idx = self._guess_preset_idx()
            self.query_one("#preset-list", ListView).index = self._preset_idx
        except Exception:
            pass

    # ── widget events ──────────────────────────────────────────────
    @on(ListView.Highlighted, "#preset-list")
    def _preset_highlighted(self, event: ListView.Highlighted) -> None:
        idx = event.list_view.index or 0
        if idx != self._preset_idx:
            self._preset_idx = idx
            preset: Preset = PRESETS[idx]
            apply_preset_to_config(preset, self.cfg)
            self._notify()

    @on(Select.Changed, "#anim-style")
    def _anim_changed(self, event: Select.Changed) -> None:
        if event.value:
            self.cfg.animation_style = str(event.value)
            self._notify()

    @on(Slider.Changed, "#speed-slider")
    def _speed_changed(self, event: Slider.Changed) -> None:
        self.cfg.theme_speed = round(event.value, 2)
        self._notify()

    @on(Slider.Changed, "#intensity-slider")
    def _intensity_changed(self, event: Slider.Changed) -> None:
        self.cfg.animation_intensity = round(event.value, 2)
        self._notify()

    @on(Slider.Changed, "#fps-slider")
    def _fps_changed(self, event: Slider.Changed) -> None:
        self.cfg.fps = int(round(event.value))
        self._notify()

    @on(Input.Changed, "#ramp-input")
    def _ramp_changed(self, event: Input.Changed) -> None:
        if len(event.value) >= 2:
            self.cfg.char_ramp = event.value
            self._notify()

    @on(Button.Pressed, "#settings-close")
    def _close(self) -> None:
        self._on_close()

    # ── helpers ────────────────────────────────────────────────────
    def _notify(self) -> None:
        try:
            self._on_change()
        except Exception:
            pass


# ── confirm modal ──────────────────────────────────────────────────────
class ConfirmModal(ModalScreen[bool]):
    """Yes/No confirmation for destructive actions."""

    BINDINGS = [
        Binding("escape", "no",  "No",  priority=True, show=True),
        Binding("n",      "no",  "No",  priority=True, show=False),
        Binding("y",      "yes", "Yes", priority=True, show=True),
    ]

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-panel"):
            yield Label(f"[b]{self._title}[/b]", classes="label")
            yield Static(self._body)
            with Horizontal():
                yield Button("Yes (y)", variant="error", id="yes")
                yield Button("No (esc)", variant="default", id="no")

    @on(Button.Pressed, "#yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no(self) -> None:
        self.dismiss(False)
