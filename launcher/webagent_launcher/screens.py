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
from .ascii_anim import ANIM_STYLES


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


# ── settings screen ────────────────────────────────────────────────────
class SettingsScreen(ModalScreen[bool]):
    """Theme + animation settings. Live-updates the parent app via callback."""

    BINDINGS = [
        Binding("escape", "close", "Close", priority=True, show=True),
        Binding("q",      "close", "Close", priority=True, show=False),
        Binding("left,h",  "prev_preset", "Prev preset"),
        Binding("right,l", "next_preset", "Next preset"),
        Binding("up,k",    "prev_anim",   "Prev animation"),
        Binding("down,j",  "next_anim",   "Next animation"),
        Binding("comma",                "speed_down",     "Slower"),
        Binding("full_stop",            "speed_up",       "Faster"),
        Binding("left_square_bracket",  "intensity_down", "Less"),
        Binding("right_square_bracket", "intensity_up",   "More"),
        Binding("minus",                "fps_down",       "FPS-"),
        Binding("equals_sign",          "fps_up",         "FPS+"),
        Binding("plus",                 "fps_up",         "FPS+"),
    ]

    def action_close(self) -> None:
        try:
            self.cfg.save()
        except Exception:
            pass
        self.dismiss(True)

    def __init__(self, cfg: LauncherConfig, on_change: Callable[[], None]) -> None:
        super().__init__()
        self.cfg = cfg
        self._on_change = on_change
        self._preset_idx = self._guess_preset_idx()

    def _guess_preset_idx(self) -> int:
        for i, p in enumerate(PRESETS):
            if p.color_a.lower() == self.cfg.theme_color_a.lower() and p.mode == self.cfg.theme_mode:
                return i
        return 0

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-panel"):
            # Sticky header so the Close button is ALWAYS visible
            with Horizontal(id="settings-header"):
                yield Static("[b]Theme & Animation[/b]", id="settings-title")
                yield Button("Close (Esc)", variant="primary", id="settings-close")

            # Scrollable body — content can grow without hiding the header
            with VerticalScroll(id="settings-body"):
                yield Label("Color preset (Left/Right or click):", classes="label")
                yield ListView(
                    *[ListItem(Label(f"  {p.name}")) for p in PRESETS],
                    id="preset-list",
                    initial_index=self._preset_idx,
                )

                yield Label("Animation style:", classes="label")
                yield Select(
                    [(s.title(), s) for s in ANIM_STYLES],
                    value=self.cfg.animation_style if self.cfg.animation_style in ANIM_STYLES else "plasma",
                    id="anim-style",
                    allow_blank=False,
                )

                yield Label(
                    f"Speed: [b]{self.cfg.theme_speed:.2f}[/b]  (',' slower  '.' faster)",
                    id="speed-label",
                    classes="label",
                )
                yield Label(
                    f"Intensity: [b]{self.cfg.animation_intensity:.2f}[/b]  ('[' less  ']' more)",
                    id="intensity-label",
                    classes="label",
                )
                yield Label(
                    f"FPS: [b]{self.cfg.fps}[/b]  ('-' slower  '+' faster)",
                    id="fps-label",
                    classes="label",
                )

                yield Label("Character ramp (typed):", classes="label")
                yield Input(value=self.cfg.char_ramp, id="ramp-input")

    # ── actions ────────────────────────────────────────────────────
    def action_prev_preset(self) -> None:
        self._preset_idx = (self._preset_idx - 1) % len(PRESETS)
        self.query_one("#preset-list", ListView).index = self._preset_idx
        self._apply_preset()

    def action_next_preset(self) -> None:
        self._preset_idx = (self._preset_idx + 1) % len(PRESETS)
        self.query_one("#preset-list", ListView).index = self._preset_idx
        self._apply_preset()

    def action_prev_anim(self) -> None:
        cur = self.cfg.animation_style if self.cfg.animation_style in ANIM_STYLES else "plasma"
        idx = ANIM_STYLES.index(cur)
        new = ANIM_STYLES[(idx - 1) % len(ANIM_STYLES)]
        self.cfg.animation_style = new
        self.query_one("#anim-style", Select).value = new
        self._notify()

    def action_next_anim(self) -> None:
        cur = self.cfg.animation_style if self.cfg.animation_style in ANIM_STYLES else "plasma"
        idx = ANIM_STYLES.index(cur)
        new = ANIM_STYLES[(idx + 1) % len(ANIM_STYLES)]
        self.cfg.animation_style = new
        self.query_one("#anim-style", Select).value = new
        self._notify()

    def action_speed_up(self) -> None:
        self.cfg.theme_speed = min(5.0, round(self.cfg.theme_speed + 0.1, 2))
        self.query_one("#speed-label", Label).update(
            f"Speed: [b]{self.cfg.theme_speed:.2f}[/b]  (',' slower  '.' faster)"
        )
        self._notify()

    def action_speed_down(self) -> None:
        self.cfg.theme_speed = max(0.05, round(self.cfg.theme_speed - 0.1, 2))
        self.query_one("#speed-label", Label).update(
            f"Speed: [b]{self.cfg.theme_speed:.2f}[/b]  (',' slower  '.' faster)"
        )
        self._notify()

    def action_intensity_up(self) -> None:
        self.cfg.animation_intensity = min(2.0, round(self.cfg.animation_intensity + 0.1, 2))
        self.query_one("#intensity-label", Label).update(
            f"Intensity: [b]{self.cfg.animation_intensity:.2f}[/b]  ('[' less  ']' more)"
        )
        self._notify()

    def action_intensity_down(self) -> None:
        self.cfg.animation_intensity = max(0.0, round(self.cfg.animation_intensity - 0.1, 2))
        self.query_one("#intensity-label", Label).update(
            f"Intensity: [b]{self.cfg.animation_intensity:.2f}[/b]  ('[' less  ']' more)"
        )
        self._notify()

    def action_fps_up(self) -> None:
        self.cfg.fps = min(60, self.cfg.fps + 2)
        self.query_one("#fps-label", Label).update(
            f"FPS: [b]{self.cfg.fps}[/b]  ('-' slower  '+' faster)"
        )
        self._notify()

    def action_fps_down(self) -> None:
        self.cfg.fps = max(4, self.cfg.fps - 2)
        self.query_one("#fps-label", Label).update(
            f"FPS: [b]{self.cfg.fps}[/b]  ('-' slower  '+' faster)"
        )
        self._notify()

    # ── widget events ──────────────────────────────────────────────
    @on(ListView.Highlighted, "#preset-list")
    def _preset_highlighted(self, event: ListView.Highlighted) -> None:
        idx = event.list_view.index or 0
        if idx != self._preset_idx:
            self._preset_idx = idx
            self._apply_preset()

    @on(Select.Changed, "#anim-style")
    def _anim_changed(self, event: Select.Changed) -> None:
        if event.value:
            self.cfg.animation_style = str(event.value)
            self._notify()

    @on(Input.Changed, "#ramp-input")
    def _ramp_changed(self, event: Input.Changed) -> None:
        if len(event.value) >= 2:
            self.cfg.char_ramp = event.value
            self._notify()

    @on(Button.Pressed, "#settings-close")
    def _close(self) -> None:
        self.action_close()

    # ── helpers ────────────────────────────────────────────────────
    def _apply_preset(self) -> None:
        preset: Preset = PRESETS[self._preset_idx]
        apply_preset_to_config(preset, self.cfg)
        self._notify()

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
