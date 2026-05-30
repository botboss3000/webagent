"""Inline first-run setup + install panel, settings panel, and modal dialogs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable, Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Label, ListItem, ListView, RichLog, Select, Static

from .config import LauncherConfig, default_install_dir
from .palette import PRESETS, Preset, apply_preset_to_config, build_palette_from_config
from .ascii_anim import ANIM_STYLES, ANIM_LABELS
from .stage import AnimatedStage
from .widgets import Slider


# ── first-run setup + install (INLINE, in the log slot) ──────────────────
_MANUAL_INSTALL_TEXT = (
    "Prefer to install it yourself? Do this in a terminal:\n"
    "\n"
    "1. Get the code (pick one):\n"
    "     git clone https://github.com/botboss3000/webagent.git C:\\webagent\n"
    "   or download the ZIP from github.com/botboss3000/webagent and unzip it.\n"
    "\n"
    "2. Install uv (Astral's Python manager), in PowerShell:\n"
    "     irm https://astral.sh/uv/install.ps1 | iex\n"
    "\n"
    "3. Install the dependencies:\n"
    "     cd C:\\webagent\n"
    "     uv sync\n"
    "\n"
    "4. Install the browser the agent's browser tool uses:\n"
    "     uv run playwright install chromium\n"
    "\n"
    "5. Start the server:\n"
    "     uv run python run.py\n"
    "   then open http://localhost:8080 in your browser.\n"
    "\n"
    "To use THIS launcher with that folder afterwards: open the Install tab,\n"
    "type the folder path, and press Download & Install — it detects the\n"
    "existing checkout and just finishes setup (no re-download)."
)


def install_status_msg(raw: str) -> str:
    """Live status line for the destination box (markup string).

    Reflects what's at the typed path: nothing entered, already a webAgent
    install (will update), a brand-new path, an empty folder, or a folder that
    already holds other files (the one real warning).
    """
    raw = (raw or "").strip().strip('"')
    if not raw:
        return "[#ffb000]Enter a destination — e.g. C:\\webagent[/]"
    p = Path(raw).expanduser()
    if (p / "run.py").exists() and (p / "app").is_dir():
        return "[#5dff4d]webAgent is already here — Install updates it in place.[/]"
    if not p.exists():
        return "[#8a8aa8]New location — a fresh install is created here.[/]"
    try:
        empty = not any(p.iterdir())
    except OSError:
        empty = False
    if empty:
        return "[#5dff4d]Empty folder — ready for a fresh install.[/]"
    return "[#ffb000]This folder already has other files in it.[/]"


class SetupPanel(Vertical):
    """First-run setup, install, manual instructions, and a Doctor — mounted
    INLINE where the log pane sits, so the animated WEBAGENT logo above stays
    visible the whole time. No modal.

    Four sub-views swap inside the panel (the app's footer tabs pick between the
    first three; the install flow shows the fourth):
      • form     — a destination field + a single Download & Install button.
      • manual   — copy-paste steps to install by hand.
      • doctor   — environment diagnostics (git / uv / internet / disk / …).
      • progress — a live install log. **No Cancel** — once started it runs to
                   completion. On success the app hides the panel (it "clears");
                   on failure the user can Retry or change the location.

    The app shows/hides the panel and learns the outcome via ``on_done(ok)``.
    """

    def __init__(
        self,
        cfg: LauncherConfig,
        on_done: Callable[[bool], None],
        on_cancel: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(id="setup-panel-inline")
        self.cfg = cfg
        self._on_done = on_done
        self._on_cancel = on_cancel
        self._task: Optional[asyncio.Task] = None
        self._doc_task: Optional[asyncio.Task] = None
        self._target = ""
        self._installing = False
        self._repoint = False  # True when changing an existing target (Cancel allowed)

    def compose(self) -> ComposeResult:
        # ── install form: destination on the left, button on the right ──
        with Vertical(id="setup-form"):
            yield Static("[b]Install webAgent[/b]", id="setup-heading")
            yield Static("", id="setup-help")
            yield Static("Install location", id="setup-dest-label")
            with Horizontal(id="setup-form-row"):
                yield Input(value=str(default_install_dir()), id="path-input")
                yield Button("Download & Install", variant="primary", id="setup-go")
                yield Button("Current Folder", id="setup-current")
                yield Button("Cancel", id="setup-cancel")
            yield Static("", id="setup-error")
        # ── manual install instructions ──
        with VerticalScroll(id="setup-manual"):
            yield Static("[b]Manual install[/b] — do it yourself", classes="setup-subhead")
            yield Static(_MANUAL_INSTALL_TEXT, id="setup-manual-text")
        # ── health check / diagnostics (re-runs each time the tab is opened) ──
        with Vertical(id="setup-doctor"):
            yield Static("[b]Health Check[/b] — environment report", classes="setup-subhead")
            yield RichLog(highlight=False, markup=False, wrap=True, id="setup-doctor-log")
        # ── live install progress (no Cancel) ──
        with Vertical(id="setup-progress"):
            yield Static("[b]Installing webAgent…[/b]", id="setup-progress-title")
            yield RichLog(highlight=False, markup=False, wrap=True, id="setup-log")
            with Horizontal(id="setup-progress-actions"):
                yield Button("Retry", variant="primary", id="setup-retry", disabled=True)
                yield Button("Change location", id="setup-back", disabled=True)

    # ── sub-view switching ──────────────────────────────────────────────
    _SUBVIEWS = ("form", "manual", "doctor", "progress")

    def _show_subview(self, name: str) -> None:
        for sv in self._SUBVIEWS:
            try:
                self.query_one(f"#setup-{sv}").display = (sv == name)
            except Exception:
                pass

    def start(self, repoint: bool = False) -> None:
        """Enter the install form. ``repoint`` = changing an existing target
        (Cancel is offered). Pre-fill the box with THIS exe's current target (its
        saved address), or suggest C:\\webagent if it has never been pointed."""
        self._repoint = repoint
        try:
            self.query_one("#path-input", Input).value = (
                self.cfg.project_path or str(default_install_dir())
            )
            self.query_one("#setup-cancel", Button).display = repoint
            # "Current Folder" — a one-click shortcut to this exe's saved target.
            # Only meaningful once it HAS a target; hidden on a never-pointed exe.
            self.query_one("#setup-current", Button).display = bool(
                (self.cfg.project_path or "").strip()
            )
        except Exception:
            pass
        self.show_install()

    def show_install(self) -> None:
        """The Install tab: the form normally, or the live log if mid-install."""
        if self._installing:
            self._show_subview("progress")
            return
        self._show_subview("form")
        self.query_one("#setup-help", Static).update(
            "This webAgent points at the folder below. Download & Install creates "
            "(or updates) it there, along with Python and everything it needs. "
            "Point it anywhere you like — each .exe remembers its own target."
        )
        inp = self.query_one("#path-input", Input)
        if not inp.value:
            inp.value = str(default_install_dir())
        self._update_status()
        try:
            inp.focus()
        except Exception:
            pass

    @on(Input.Changed, "#path-input")
    def _path_changed(self) -> None:
        self._update_status()

    def _update_status(self) -> None:
        """Live line under the box + adaptive primary-button label."""
        try:
            st = self.query_one("#setup-error", Static)
            raw = self.query_one("#path-input", Input).value.strip().strip('"')
        except Exception:
            return
        st.update(install_status_msg(raw))
        # Already an install → 'Use this folder' (adopt + ready); else download.
        try:
            p = Path(raw).expanduser() if raw else None
            is_inst = bool(p and (p / "run.py").exists() and (p / "app").is_dir())
            self.query_one("#setup-go", Button).label = "Use this folder" if is_inst else "Download & Install"
        except Exception:
            pass

    @on(Button.Pressed, "#setup-cancel")
    def _cancel(self) -> None:
        if self._on_cancel is not None:
            self._on_cancel()

    @on(Button.Pressed, "#setup-current")
    def _use_current(self) -> None:
        """One-click: drop THIS exe's saved target into the box and keep it as
        the app's location — no typing, no re-download. If that folder is still a
        real install we just adopt it and hand back to the app (which leaves a
        healthy same-folder server running instead of bouncing it); if it has
        gone missing, fall back to a normal install there."""
        cur = (self.cfg.project_path or "").strip().strip('"')
        if not cur:
            return
        self.query_one("#path-input", Input).value = cur  # show it in the box
        self._update_status()
        p = Path(cur).expanduser()
        if (p / "run.py").exists() and (p / "app").is_dir():
            self.cfg.project_path = str(p)
            self.cfg.save()
            self._on_done(True)  # save as the app's location + switch to it
        else:
            self._begin_install(str(p))  # not set up anymore → install it

    def show_manual(self) -> None:
        self._show_subview("manual")

    def show_doctor(self) -> None:
        self._show_subview("doctor")
        if self._doc_task is None or self._doc_task.done():
            self._doc_task = asyncio.create_task(self._run_doctor())

    # ── doctor ──────────────────────────────────────────────────────────
    def _doc_log(self, line: str) -> None:
        try:
            self.query_one("#setup-doctor-log", RichLog).write(line)
        except Exception:
            pass

    async def _run_doctor(self) -> None:
        from . import bootstrap

        try:
            self.query_one("#setup-doctor-log", RichLog).clear()
        except Exception:
            pass
        raw = self.query_one("#path-input", Input).value.strip().strip('"')
        target = Path(raw).expanduser() if raw else default_install_dir()
        try:
            await bootstrap.run_diagnostics(target, self._doc_log)
        except Exception as e:
            self._doc_log(f"diagnostics error: {e}")

    # ── confirm (form) ──────────────────────────────────────────────────
    @on(Button.Pressed, "#setup-go")
    def _go(self) -> None:
        path = self.query_one("#path-input", Input).value.strip().strip('"')
        err = self.query_one("#setup-error", Static)
        if not path:
            err.update("[red]Enter a destination folder.[/red]")
            return
        p = Path(path).expanduser()
        # Only sanity check: the location must be reachable so we can create it.
        # An existing webAgent folder is adopted/re-synced; a brand-new path is
        # created. (No "must be empty" rule.)
        anc = p
        while not anc.exists() and anc.parent != anc:
            anc = anc.parent
        if not anc.exists():
            err.update(f"[red]That location isn't reachable:[/red] {path}")
            return
        self._begin_install(str(p))

    # ── install (no cancel) ─────────────────────────────────────────────
    def _begin_install(self, target: str) -> None:
        self._target = target
        self._installing = True
        self._show_subview("progress")
        self.query_one("#setup-progress-title", Static).update(
            "[b]Installing webAgent…[/b]  (this can take a few minutes — keep this window open)"
        )
        for bid in ("setup-retry", "setup-back"):
            self.query_one(f"#{bid}", Button).disabled = True
        try:
            self.query_one("#setup-log", RichLog).clear()
        except Exception:
            pass
        self._task = asyncio.create_task(self._run_install(target))

    def _log(self, line: str) -> None:
        try:
            self.query_one("#setup-log", RichLog).write(line)
        except Exception:
            pass

    async def _run_install(self, target: str) -> None:
        from . import bootstrap

        try:
            ok = await bootstrap.install(Path(target), self._log)
        except Exception as e:  # never let an install crash take down the app
            self._log(f"[install] unexpected error: {e}")
            ok = False

        self._installing = False
        if ok:
            self.cfg.project_path = str(Path(target).expanduser())
            self.cfg.save()
            self._log("[install] done — launching…")
            self._on_done(True)  # app hides this panel (it "clears")
        else:
            self.query_one("#setup-progress-title", Static).update(
                "[b]Install failed[/b] — Retry, or change the location"
            )
            self.query_one("#setup-retry", Button).disabled = False
            self.query_one("#setup-back", Button).disabled = False

    @on(Button.Pressed, "#setup-retry")
    def _retry(self) -> None:
        if self._target:
            self._begin_install(self._target)

    @on(Button.Pressed, "#setup-back")
    def _back(self) -> None:
        self.show_install()


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


# ── full-screen first-run installer / re-point ──────────────────────────
class InstallScreen(Screen):
    """Full-screen wrapper around :class:`SetupPanel`.

    Replaces the old trick of repurposing the home footer buttons as installer
    tabs. The animated logo stays on top (so it's visible all through the
    install, exactly as before), the SetupPanel body fills the middle, and a
    footer row carries the installer tabs (Install / Manual Install / Health
    Check) plus Theme + Quit.

    The launcher pushes this screen when there's no valid project (first run) or
    when re-pointing at a new folder. It hands the outcome back through the
    ``on_done(ok)`` / ``on_cancel()`` callbacks — the SAME callables the old
    inline flow used — so the app's commit-target logic is unchanged. The
    SetupPanel internals and ``bootstrap`` are deliberately left untouched.
    """

    BINDINGS = [Binding("escape", "esc", "Cancel", show=True)]

    def __init__(
        self,
        cfg: LauncherConfig,
        on_done: Callable[[bool], None],
        on_cancel: Optional[Callable[[], None]] = None,
        repoint: bool = False,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self._on_done_cb = on_done
        self._on_cancel_cb = on_cancel
        self._repoint = repoint
        # SetupPanel keeps its own callbacks; we forward them to the app.
        self._panel = SetupPanel(cfg, self._panel_done, self._panel_cancel)

    def compose(self) -> ComposeResult:
        stage = AnimatedStage(
            palette=build_palette_from_config(self.cfg),
            char_ramp=self.cfg.char_ramp,
            fps=self.cfg.fps,
            style=self.cfg.animation_style,
            speed=self.cfg.theme_speed,
            intensity=self.cfg.animation_intensity,
        )
        stage.id = "install-stage"
        with Vertical(id="install-root"):
            yield stage
            yield self._panel
            with Horizontal(id="install-tabs"):
                yield Button("Install", id="inst-install", classes="primary")
                yield Button("Manual Install", id="inst-manual", classes="muted")
                yield Button("Health Check", id="inst-doctor", classes="muted")
                yield Button("Theme", id="inst-theme", classes="muted")
                yield Button("Quit", id="inst-quit", classes="muted")

    def on_mount(self) -> None:
        # The panel is display:none by default (it used to share the home slot);
        # it always shows inside this dedicated screen.
        self._panel.display = True
        self._panel.start(repoint=self._repoint)

    @on(Button.Pressed)
    def _tabs(self, event: Button.Pressed) -> None:
        # Only the footer-tab buttons are handled here; the SetupPanel's own
        # buttons (#setup-go, #setup-cancel, …) are handled inside the panel and
        # simply bubble through this (no-op) for non-tab ids.
        bid = event.button.id or ""
        if bid == "inst-install":
            self._panel.show_install()
        elif bid == "inst-manual":
            self._panel.show_manual()
        elif bid == "inst-doctor":
            self._panel.show_doctor()
        elif bid == "inst-theme":
            self.app.action_cycle_theme()
        elif bid == "inst-quit":
            self.app.run_worker(self.app.action_request_quit())

    def _panel_done(self, ok: bool) -> None:
        if self._on_done_cb is not None:
            self._on_done_cb(ok)

    def _panel_cancel(self) -> None:
        if self._on_cancel_cb is not None:
            self._on_cancel_cb()

    def action_esc(self) -> None:
        # Esc cancels a re-point (returns to chat); on first-run there's nothing
        # to return to, so it quits the launcher.
        if self._repoint and self._on_cancel_cb is not None:
            self._on_cancel_cb()
        else:
            self.app.run_worker(self.app.action_request_quit())
