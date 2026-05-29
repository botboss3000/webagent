"""Modal/secondary screens: first-run setup, install progress, settings, confirm."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, RichLog, Select, Static

from .config import LauncherConfig, default_install_dir
from .palette import PRESETS, Preset, apply_preset_to_config, build_palette_from_config
from .ascii_anim import ANIM_STYLES, ANIM_LABELS
from .widgets import Slider


# ── first-run setup ────────────────────────────────────────────────────
@dataclass
class SetupResult:
    """What the user chose on the first-run screen.

    action == "install"  → download + install fresh into ``path`` (app pushes
                            InstallScreen next).
    action == "existing" → ``path`` is an already-present project; cfg is saved.
    """

    action: str
    path: str


class SetupScreen(ModalScreen[Optional["SetupResult"]]):
    """First run: install webAgent fresh (download everything) OR point the
    launcher at a webagent folder that already exists on the machine."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True, show=True),
    ]

    def action_cancel(self) -> None:
        self.dismiss(None)

    def __init__(self, cfg: LauncherConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.mode = "install"  # "install" | "existing"

    def compose(self) -> ComposeResult:
        with Vertical(id="setup-panel"):
            yield Label("[b]webagent — first-run setup[/b]", classes="label")
            with Horizontal(id="setup-modes"):
                yield Button("Download & Install", id="mode-install")
                yield Button("Use existing folder", id="mode-existing")
            yield Label("", id="setup-help", classes="help")
            yield Input(value=str(default_install_dir()), id="path-input")
            yield Label("", id="setup-error", classes="label")
            with Horizontal(id="setup-actions"):
                yield Button("Install", variant="primary", id="setup-go")
                yield Button("Cancel", variant="default", id="setup-cancel")

    def on_mount(self) -> None:
        self._apply_mode()

    # ── mode toggle ────────────────────────────────────────────────────
    def _apply_mode(self) -> None:
        help_lbl = self.query_one("#setup-help", Label)
        inp = self.query_one("#path-input", Input)
        go = self.query_one("#setup-go", Button)
        mi = self.query_one("#mode-install", Button)
        me = self.query_one("#mode-existing", Button)
        self.query_one("#setup-error", Label).update("")
        if self.mode == "install":
            help_lbl.update(
                "Downloads webAgent and every dependency into a new folder. "
                "Nothing needs to be installed beforehand — this can take a few "
                "minutes and a few hundred MB the first time."
            )
            inp.value = str(default_install_dir())
            go.label = "Install here"
            mi.add_class("active")
            me.remove_class("active")
        else:
            help_lbl.update(
                "Point the launcher at a webAgent folder you already have "
                "(it must contain run.py and an app/ folder)."
            )
            inp.value = self.cfg.project_path or str(Path.cwd())
            go.label = "Use this folder"
            me.add_class("active")
            mi.remove_class("active")

    @on(Button.Pressed, "#mode-install")
    def _pick_install(self) -> None:
        self.mode = "install"
        self._apply_mode()

    @on(Button.Pressed, "#mode-existing")
    def _pick_existing(self) -> None:
        self.mode = "existing"
        self._apply_mode()

    # ── confirm ────────────────────────────────────────────────────────
    @on(Button.Pressed, "#setup-go")
    def _go(self) -> None:
        path = self.query_one("#path-input", Input).value.strip().strip('"')
        err = self.query_one("#setup-error", Label)
        if not path:
            err.update("[red]Enter a folder path.[/red]")
            return
        p = Path(path).expanduser()

        if self.mode == "existing":
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
            self.dismiss(SetupResult("existing", str(p)))
            return

        # install mode — make sure we can reach the target and it isn't a
        # non-empty unrelated folder.
        anc = p
        while not anc.exists() and anc.parent != anc:
            anc = anc.parent
        if not anc.exists():
            err.update(f"[red]Cannot reach[/red] {path}")
            return
        already_project = (p / "run.py").exists()
        if p.exists() and any(p.iterdir()) and not already_project:
            err.update(f"[red]Folder isn't empty — pick a new folder:[/red] {path}")
            return
        self.dismiss(SetupResult("install", str(p)))

    @on(Button.Pressed, "#setup-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


# ── install progress ────────────────────────────────────────────────────
class InstallScreen(ModalScreen[bool]):
    """Runs the download + dependency install with a live progress log.

    dismiss(True)  → installed OK, project_path saved; app proceeds to launch.
    dismiss(False) → user cancelled after a failure.
    """

    # Esc is swallowed while work is in flight; the action buttons (Continue /
    # Retry / Cancel) are the only way out and only light up when work stops.
    BINDINGS = [Binding("escape", "noop", show=False)]

    def action_noop(self) -> None:
        pass

    def __init__(self, cfg: LauncherConfig, target: str) -> None:
        super().__init__()
        self.cfg = cfg
        self.target = target
        self._task: Optional[asyncio.Task] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="install-panel"):
            yield Label("[b]Installing webAgent…[/b]", id="install-title", classes="label")
            yield RichLog(highlight=False, markup=False, wrap=True, id="install-log")
            with Horizontal(id="install-actions"):
                yield Button("Continue ▶", variant="primary", id="install-continue", disabled=True)
                yield Button("Retry", variant="default", id="install-retry", disabled=True)
                yield Button("Cancel", variant="default", id="install-cancel", disabled=True)

    def on_mount(self) -> None:
        self._start()

    def _start(self) -> None:
        self.query_one("#install-title", Label).update("[b]Installing webAgent…[/b]")
        for bid in ("install-continue", "install-retry", "install-cancel"):
            self.query_one(f"#{bid}", Button).disabled = True
        self._task = asyncio.create_task(self._run())

    def _log(self, line: str) -> None:
        try:
            self.query_one("#install-log", RichLog).write(line)
        except Exception:
            pass

    async def _run(self) -> None:
        from . import bootstrap

        try:
            ok = await bootstrap.install(Path(self.target), self._log)
        except Exception as e:  # never let an install crash take down the app
            self._log(f"[install] unexpected error: {e}")
            ok = False

        title = self.query_one("#install-title", Label)
        if ok:
            self.cfg.project_path = str(Path(self.target).expanduser())
            self.cfg.save()
            title.update("[b]✔ Install complete[/b] — press Continue to launch")
            btn = self.query_one("#install-continue", Button)
            btn.disabled = False
            btn.focus()
        else:
            title.update("[b]✘ Install failed[/b] — review the log, then Retry or Cancel")
            self.query_one("#install-retry", Button).disabled = False
            self.query_one("#install-cancel", Button).disabled = False

    @on(Button.Pressed, "#install-continue")
    def _continue(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#install-retry")
    def _retry(self) -> None:
        self._start()

    @on(Button.Pressed, "#install-cancel")
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
