"""Full-screen, keyboard-driven chat client for the webagent server.

Mirrors the webapp's right-side web chat inside the TUI:
  * Agent + Session pickers (keyboard-invoked list overlays).
  * Live token-by-token streaming.
  * Tool calls rendered as expandable blocks (name + args + result).
  * Agent loop steps shown as dim status lines (filterable; hidden by default).
  * A multi-line editor input with full Windows-style editing.
  * A STATIC ascii banner pinned to the top of the transcript (scrolls up with
    the conversation) instead of a background animation.
  * A little ascii guy who walks on the input "pill" while the loop is working.
  * A session HUD above the input: total tokens, running cost, context gauge.

No on-screen buttons — everything is a keyboard shortcut, shown in the hint
bar. Talks to the SAME local server the launcher starts (HTTP + SSE).

Command keys are all Ctrl-prefixed so they never collide with typing:
  Ctrl+~ / Ctrl+`  home          Ctrl+Q  go back to last screen
  Ctrl+! / Ctrl+1  agent picker  Ctrl+@ / Ctrl+2  session picker
  Ctrl+# / Ctrl+3  new session   Ctrl+$ / Ctrl+4  new agent
  Ctrl+F           filter        Esc              exit
  Ctrl+C/V/Z       copy/paste/undo (editor)

Glyphs are ASCII-only (no emoji) so they render in any Windows console font.
"""

from __future__ import annotations

import colorsys
import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import (
    Collapsible, Input, Label, ListItem, ListView, SelectionList, Static, TextArea,
)
from textual.widgets.selection_list import Selection

from .api_client import WebAgentClient, WebAgentError
from .config import LauncherConfig
from .glyphs import EMOJI, G
from .server import ServerController
from .stage import _LOGO_LINES

# Palette (terminal-only; the dark/light rule applies to the web ui/, not here)
GREEN = "#39ff14"
DIM = "#6a6a8a"
RED = "#ff5577"
CYAN = "#5dd6ff"
AMBER = "#ffb000"

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

_ADMIN_TEMPLATES = {
    "admin-agent": "Admin coding agent (shell - files)",
    "integration-admin-agent": "Integration admin",
}

# ── interaction filter categories (Ctrl+F) ──────────────────────────────
# (key, label, default_visible). Memory + loop chatter default to hidden so the
# transcript stays clean; the live HUD line shows progress instead. Tool blocks
# stay visible. User + assistant messages are always shown (not filterable).
_FILTER_CATS: list[tuple[str, str, bool]] = [
    ("tools",  "Tool calls & results", True),
    ("loop",   "Loop steps (turn / llm / context)", False),
    ("memory", "Memory search", False),
]

# Best-effort context-window sizes by model-name substring (lower-cased). Used
# only to draw the context gauge's "/ max"; unknown models show the live size
# alone. Order matters — first substring hit wins.
_CTX_MAX: list[tuple[str, int]] = [
    ("o1", 200000), ("o3", 200000), ("gpt-4.1", 1000000), ("gpt-4o", 128000),
    ("gpt-4", 128000), ("gpt-3.5", 16385), ("claude", 200000),
    ("gemini-1.5", 1000000), ("gemini", 1000000), ("deepseek", 128000),
    ("llama-3", 128000), ("llama", 128000), ("qwen", 32768),
    ("mistral", 32768), ("mixtral", 32768), ("command-r", 128000),
    ("grok", 131072),
]


def _parse_agent_ref(ref: str) -> tuple[str, str, str]:
    """('template'|'agent', value, display_name) from a stored ref string."""
    ref = (ref or "").strip()
    if ref.startswith("agent:"):
        v = ref[6:]
        return "agent", v, v[:8]
    if ref.startswith("template:"):
        v = ref[9:]
        return "template", v, _ADMIN_TEMPLATES.get(v, v)
    if ref in _ADMIN_TEMPLATES:
        return "template", ref, _ADMIN_TEMPLATES[ref]
    if ref:  # bare template id
        return "template", ref, ref
    return "template", "admin-agent", _ADMIN_TEMPLATES["admin-agent"]


def _extract_image_paths(text: str) -> list[str]:
    """Pull existing image file paths out of pasted/dropped text.

    Terminals deliver an OS file drop as pasted text — usually the path,
    sometimes quoted, sometimes several separated by spaces/newlines.
    """
    if not text:
        return []
    raw = text.strip()
    candidates: list[str] = []
    # newline-separated first (multi-file drop)
    for line in raw.splitlines():
        line = line.strip().strip('"').strip("'")
        if line:
            candidates.append(line)
    # if a single line held several quoted paths, also try space splitting
    if len(candidates) == 1 and " " in candidates[0] and '"' not in raw:
        parts = [p for p in candidates[0].split(" ") if p]
        if all(Path(p.strip('"').strip("'")).suffix.lower() in _IMAGE_EXTS for p in parts):
            candidates = [p.strip('"').strip("'") for p in parts]
    out: list[str] = []
    for c in candidates:
        p = Path(c)
        if p.suffix.lower() in _IMAGE_EXTS and p.is_file():
            out.append(str(p))
    return out


def _fmt_tokens(n: Any) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _context_max(model: str) -> Optional[int]:
    """Known context-window size for a model name, or None if unrecognised."""
    m = (model or "").lower()
    for sub, mx in _CTX_MAX:
        if sub in m:
            return mx
    return None


def _agent_color(name: str) -> str:
    """Stable accent colour derived from the agent name (per-agent tint)."""
    name = (name or "agent").strip().lower()
    h = (sum(ord(c) * (i + 1) for i, c in enumerate(name)) % 360) / 360.0
    r, g, b = colorsys.hsv_to_rgb(h, 0.55, 1.0)
    return "#{:02x}{:02x}{:02x}".format(int(r * 255), int(g * 255), int(b * 255))


class ChatInput(TextArea):
    """Multi-line message editor.

    Inherits all of TextArea's Windows-style editing: Ctrl+C/V/X copy/cut/
    paste, Ctrl+Z/Y undo/redo, Ctrl+arrows word-skip, Home/End, mouse
    selection. We override:
      * Enter            -> send (posts Submitted)
      * Ctrl+Enter / Ctrl+J -> newline (Shift+Enter is unreliable across
                              terminals — most send a plain Enter — so it is no
                              longer advertised; Ctrl+J always inserts a line.)
      * Ctrl+A           -> select all (Windows convention, not line-start)
      * Up / Down (single-line) -> recall previous / next sent message
      * Paste of an image path -> ImagesDropped (drag-to-attach)
    """

    BINDINGS = [
        Binding("ctrl+a", "select_all", "Select all", show=False),
        Binding("ctrl+up", "doc_start", "Doc start", show=False),
        Binding("ctrl+down", "doc_end", "Doc end", show=False),
        Binding("ctrl+home", "doc_start", "Doc start", show=False),
        Binding("ctrl+end", "doc_end", "Doc end", show=False),
    ]

    class Submitted(Message):
        def __init__(self, widget: "ChatInput", value: str) -> None:
            self.input = widget
            self.value = value
            super().__init__()

        @property
        def control(self) -> "ChatInput":
            return self.input

    class ImagesDropped(Message):
        def __init__(self, widget: "ChatInput", paths: list[str]) -> None:
            self.input = widget
            self.paths = paths
            super().__init__()

        @property
        def control(self) -> "ChatInput":
            return self.input

    class HistoryNav(Message):
        """Up/Down on a single-line input → recall sent messages."""

        def __init__(self, widget: "ChatInput", delta: int) -> None:
            self.input = widget
            self.delta = delta
            super().__init__()

        @property
        def control(self) -> "ChatInput":
            return self.input

    async def _on_key(self, event: events.Key) -> None:
        # NOTE: base TextArea._on_key is async — we MUST await super().
        key = event.key
        if key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self, self.text))
            return
        if key in ("ctrl+enter", "ctrl+j", "shift+enter"):
            # Insert a newline. shift+enter only reaches us on terminals that
            # distinguish it; where it doesn't, it arrives as plain Enter above.
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if key in ("up", "down") and "\n" not in self.text:
            # Single-line: there is nowhere to move the cursor vertically, so
            # repurpose Up/Down as shell-style history recall.
            event.stop()
            event.prevent_default()
            self.post_message(self.HistoryNav(self, -1 if key == "up" else 1))
            return
        await super()._on_key(event)

    async def _on_paste(self, event: events.Paste) -> None:
        # Only intercept when the paste is an image-file drop; otherwise let
        # the base class do its normal (undo-aware) text insert.
        imgs = _extract_image_paths(event.text)
        if imgs:
            event.stop()
            event.prevent_default()
            self.post_message(self.ImagesDropped(self, imgs))
            return
        await super()._on_paste(event)

    def action_doc_start(self) -> None:
        try:
            self.move_cursor((0, 0))
        except Exception:
            pass

    def action_doc_end(self) -> None:
        try:
            self.move_cursor(self.document.end)
        except Exception:
            pass


class WalkerBar(Widget):
    """A one-row strip above the input where a tiny ascii guy reacts to the loop.

    States:  idle (blank) · walk (streaming) · work (tool running) ·
             cheer (reply done) · trip (error).  The walker only animates while
    a turn is active, so it costs nothing at rest.
    """

    DEFAULT_CSS = """
    WalkerBar { height: 1; width: 100%; }
    """

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self._state = "idle"
        self._pos = 0
        self._frame = 0
        self._timer = None

    def on_mount(self) -> None:
        # ~8 fps, paused until a turn starts.
        self._timer = self.set_interval(0.12, self._tick, pause=True)

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self._frame = 0
        # Keep _pos continuous so resuming "walk" after a tool doesn't teleport
        # the guy back to the left edge.
        if self._timer is not None:
            if state == "idle":
                self._timer.pause()
            else:
                self._timer.resume()
        self.refresh()

    def _tick(self) -> None:
        self._frame += 1
        if self._state == "walk":
            self._pos += 1
        self.refresh()

    def render(self) -> Text:
        w = max(6, self.size.width)
        if self._state == "idle":
            return Text(" " * w)

        emoji = EMOJI
        if self._state == "walk":
            sprite = "🚶" if emoji else ("o/" if self._frame % 2 == 0 else "o\\")
            color = GREEN
            span = max(1, w - 3)
            pos = self._pos % span
        elif self._state == "work":
            spin = "|/-\\"[self._frame % 4]
            sprite = ("🔧" if emoji else "o" + spin)
            color = AMBER
            span = max(1, w - 3)
            pos = self._pos % span
        elif self._state == "cheer":
            sprite = "🙌" if emoji else "\\o/"
            color = GREEN
            span = max(1, w - 3)
            pos = self._pos % span
        else:  # trip
            sprite = "💥" if emoji else "x_"
            color = RED
            span = max(1, w - 3)
            pos = self._pos % span

        line = Text(no_wrap=True, overflow="crop")
        if pos:
            line.append(" " * pos)
        line.append(sprite, style=f"bold {color}")
        tail = w - pos - len(sprite)
        if tail > 0:
            line.append(" " * tail)
        return line


class ListPicker(ModalScreen[Optional[str]]):
    """Generic keyboard list picker. Returns the chosen item's key (or None)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, items: list[tuple[str, str]]) -> None:
        super().__init__()
        self._title = title
        self._items = items

    def compose(self) -> ComposeResult:
        rows: list[ListItem] = []
        for key, label in self._items:
            li = ListItem(Label(label))
            li._pick_key = key  # type: ignore[attr-defined]
            rows.append(li)
        with Vertical(id="picker-panel"):
            yield Static(self._title, id="picker-title")
            yield ListView(*rows, id="picker-list")

    def on_mount(self) -> None:
        self.query_one("#picker-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(getattr(event.item, "_pick_key", None))

    def action_cancel(self) -> None:
        self.dismiss(None)


class FilterModal(ModalScreen[Optional[set[str]]]):
    """Scrollable checkbox list (like the pickers) to show/hide interaction
    categories. Returns the set of enabled category keys, or None if cancelled."""

    BINDINGS = [Binding("escape", "done", "Apply")]

    def __init__(self, categories: list[tuple[str, str]], enabled: set[str]) -> None:
        super().__init__()
        self._categories = categories
        self._enabled = enabled

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-panel"):
            yield Static("Show / hide interactions  -  Space toggles  -  Esc applies",
                         id="picker-title")
            yield SelectionList(
                *[Selection(label, key, key in self._enabled)
                  for key, label in self._categories],
                id="filter-list",
            )

    def on_mount(self) -> None:
        self.query_one("#filter-list", SelectionList).focus()

    def action_done(self) -> None:
        sel = set(self.query_one("#filter-list", SelectionList).selected)
        self.dismiss(sel)


class CredentialsModal(ModalScreen[Optional[tuple[str, str]]]):
    """Prompt for login when the default admin/admin fails."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, username: str) -> None:
        super().__init__()
        self._username = username

    def compose(self) -> ComposeResult:
        with Vertical(id="creds-panel"):
            yield Static("Server login", classes="label")
            yield Input(value=self._username, placeholder="username", id="creds-user")
            yield Input(placeholder="password", password=True, id="creds-pass")
            yield Static("Enter to sign in - Esc to cancel", classes="dim")

    def on_mount(self) -> None:
        self.query_one("#creds-pass", Input).focus()

    @on(Input.Submitted)
    def _submit(self) -> None:
        u = self.query_one("#creds-user", Input).value.strip()
        p = self.query_one("#creds-pass", Input).value
        self.dismiss((u, p))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChatScreen(Screen):
    """The chat surface. Pushed over the launcher's home/control view."""

    BINDINGS = [
        Binding("escape", "back", "Exit", priority=True),
        # Navigation. Each binds the shifted symbol AND the plain key so it
        # fires whether the terminal reports e.g. "ctrl+!" or "ctrl+1".
        Binding("ctrl+tilde,ctrl+shift+tilde,ctrl+shift+grave_accent,ctrl+grave_accent",
                "go_home", "Home", priority=True),
        # Ctrl+Q ("go back to last") is handled at the app level so it toggles
        # home/chat from either screen — see LauncherApp.action_go_back_last.
        Binding("ctrl+exclamation_mark,ctrl+shift+1,ctrl+1",
                "pick_agent", "Agent", priority=True),
        Binding("ctrl+at,ctrl+shift+2,ctrl+2",
                "pick_session", "Session", priority=True),
        Binding("ctrl+number_sign,ctrl+shift+3,ctrl+3",
                "new_session", "New session", priority=True),
        Binding("ctrl+dollar_sign,ctrl+shift+4,ctrl+4",
                "new_agent", "New agent", priority=True),
        Binding("ctrl+f", "filter", "Filter", priority=True),
        # Swallow the home screen's single-letter server shortcuts so they can't
        # fire while chat is open and focus is off the input. When the input IS
        # focused, the printable key is consumed for typing before reaching here.
        *[Binding(k, "noop", show=False) for k in
          ("q", "l", "r", "b", "d", "p", "f", "t", "c", "space")],
    ]

    def __init__(
        self,
        cfg: LauncherConfig,
        controller: ServerController | None,
        *,
        autostart: bool = True,
    ) -> None:
        super().__init__()
        self.cfg = cfg
        self.controller = controller
        self._autostart = autostart  # False in tests → skip the connect worker
        self.client: Optional[WebAgentClient] = None
        self.ready = False
        kind, value, name = _parse_agent_ref(cfg.last_agent_ref or cfg.default_agent_ref)
        self.agent_kind = kind
        self.agent_value = value
        self.agent_name = name
        self.resolved_agent_id = ""
        self.session_id = ""
        self.session_title = ""
        self.is_processing = False
        self._send_worker = None
        self._cur_assistant: Optional[Static] = None
        self._cur_text = ""
        self._pending_tools: list[dict[str, Any]] = []
        self._banner: Optional[Static] = None
        self._walker: Optional[WalkerBar] = None
        self._pending_attachments: list[dict[str, Any]] = []
        # message history recall (Up/Down on a single-line input)
        self._history: list[str] = []
        self._hist_idx: Optional[int] = None
        # interaction filter
        self._filter: dict[str, bool] = {k: d for k, _, d in _FILTER_CATS}
        self._cat_widgets: dict[str, list[Widget]] = {k: [] for k, _, _ in _FILTER_CATS}
        # per-turn / live state
        self._t_model = ""
        self._turn = 0
        self._max_turns = 0
        self._activity = ""
        self._proc_start = 0.0
        # session-total accumulators
        self._s_in = 0
        self._s_out = 0
        self._s_cost = 0.0
        self._s_has_cost = False
        self._ctx_tokens = 0
        self._ctx_max: Optional[int] = None

    # ── layout ─────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Static("connecting...", id="chat-status")
        with Container(id="chat-body"):
            yield VerticalScroll(id="chat-log")
        yield Static("", id="chat-hud")
        yield WalkerBar(id="chat-walker")
        yield ChatInput(id="chat-input", soft_wrap=True, tab_behavior="focus")
        yield Static(self._hints_idle(), id="chat-hints")

    def on_mount(self) -> None:
        self._walker = self.query_one("#chat-walker", WalkerBar)
        self._mount_banner()
        self.query_one("#chat-input", ChatInput).focus()
        # One timer drives both the server-dot refresh and the live HUD clock.
        self.set_interval(1.0, self._tick)
        self._update_hud()
        if self._autostart:
            self.run_worker(self._init(), group="init", exclusive=True)

    # ── static banner (top of transcript, scrolls with content) ─────────
    def _build_banner_text(self) -> Text:
        accent = self._agent_color()
        t = Text(no_wrap=True, overflow="crop")
        for line in _LOGO_LINES:
            t.append(line + "\n", style=f"bold {accent}")
        t.append("\n")
        t.append(self.agent_name or "agent", style=f"bold {CYAN}")
        t.append("  -  ", style=DIM)
        t.append(self._t_model or "model: (pending)", style=DIM)
        if self.session_title:
            t.append("  -  ", style=DIM)
            t.append(self.session_title, style=DIM)
        t.append("  -  ", style=DIM)
        t.append(datetime.now().strftime("%Y-%m-%d"), style=DIM)
        return t

    def _agent_color(self) -> str:
        return _agent_color(self.agent_name)

    def _mount_banner(self) -> None:
        """(Re)create the static banner as the first child of the transcript."""
        try:
            log = self._log()
        except Exception:
            return
        self._banner = Static(self._build_banner_text(), classes="chat-banner")
        log.mount(self._banner)

    def _update_banner(self) -> None:
        if self._banner is None:
            return
        try:
            self._banner.update(self._build_banner_text())
        except Exception:
            pass

    # ── init / connect ─────────────────────────────────────────────────
    async def _init(self) -> None:
        self._status("starting server...")
        ok = await ServerController.wait_until_ready(timeout=60.0)
        if not ok:
            self._status("server not ready - press Esc, start it, then reopen chat")
            return

        cached = getattr(self.app, "_chat_client", None)
        if isinstance(cached, WebAgentClient) and cached.token:
            self.client = cached
        else:
            self.client = WebAgentClient()
            try:
                await self.client.login(self.cfg.chat_username, self.cfg.chat_password)
            except WebAgentError:
                creds = await self.app.push_screen_wait(
                    CredentialsModal(self.cfg.chat_username)
                )
                if not creds:
                    self._status("login cancelled - press Esc")
                    return
                try:
                    await self.client.login(creds[0], creds[1])
                except WebAgentError as e:
                    self._status(f"login failed: {e}")
                    return
                self.cfg.chat_username, self.cfg.chat_password = creds
                self.cfg.save()
            self.app._chat_client = self.client  # type: ignore[attr-defined]

        self.ready = True
        if self.cfg.last_session_id:
            self.session_id = self.cfg.last_session_id
            await self._load_history(self.session_id)
        else:
            self._new_session(announce=False)
        self._refresh_status()
        self.query_one("#chat-input", ChatInput).focus()

    # ── status / hints ─────────────────────────────────────────────────
    def notify_server_state(self) -> None:
        """Hook the launcher calls the instant the server's state changes, so the
        dot flips immediately rather than waiting for the next 1 Hz poll."""
        self._refresh_status()

    def _server_dot(self) -> tuple[str, str]:
        # Prefer our injected controller; fall back to the app's live one so a
        # missing reference can't pin the dot on a permanent "live".
        ctrl = self.controller
        if ctrl is None:
            try:
                ctrl = getattr(self.app, "controller", None)
            except Exception:
                ctrl = None
        st = ctrl.state.status if ctrl else "running"
        if st == "running":
            return f"{G.DOT_LIVE} live", GREEN
        if st == "starting":
            return f"{G.DOT_WARN} reconnecting", AMBER
        if st == "stopping":
            return f"{G.DOT_WARN} stopping", AMBER
        return f"{G.DOT_DEAD} disconnected", RED

    def _refresh_status(self) -> None:
        dot, color = self._server_dot()
        t = Text()
        t.append(self.agent_name or "agent", style=f"bold {self._agent_color()}")
        t.append("  -  ", style=DIM)
        t.append(self.session_title or "new session", style=CYAN)
        t.append("  -  ", style=DIM)
        t.append(dot, style=color)
        try:
            self.query_one("#chat-status", Static).update(t)
        except Exception:
            pass

    def _status(self, msg: str) -> None:
        try:
            self.query_one("#chat-status", Static).update(Text(msg, style=AMBER))
        except Exception:
            pass

    def _tick(self) -> None:
        """1 Hz: refresh the server dot and (while working) the HUD clock."""
        self._refresh_status()
        if self.is_processing:
            self._update_hud()

    def _ctx_gauge(self) -> Text:
        """Context-window usage bar; colour shifts green -> amber -> red."""
        g = Text()
        used = self._ctx_tokens
        if used <= 0:
            g.append("ctx --", style=DIM)
            return g
        mx = self._ctx_max
        if not mx:
            g.append(f"ctx {_fmt_tokens(used)}", style=DIM)
            return g
        frac = max(0.0, min(1.0, used / mx))
        cells = 12
        filled = int(round(frac * cells))
        if frac >= 0.85:
            col = RED
        elif frac >= 0.6:
            col = AMBER
        else:
            col = GREEN
        g.append("ctx ", style=DIM)
        g.append("[", style=DIM)
        g.append("#" * filled, style=col)
        g.append("." * (cells - filled), style=DIM)
        g.append("] ", style=DIM)
        g.append(f"{_fmt_tokens(used)}/{_fmt_tokens(mx)} ", style=DIM)
        g.append(f"{int(frac * 100)}%", style=col)
        if frac >= 0.85:
            g.append("  full - Ctrl+# for new", style=RED)
        return g

    def _update_hud(self) -> None:
        """Session HUD shown directly above the input. Session totals + context
        gauge come FIRST so they always stay visible; the live activity trails
        at the end where it can safely truncate on a narrow terminal."""
        t = Text(no_wrap=True, overflow="crop")
        # session totals (must stay visible)
        t.append("session ", style=DIM)
        t.append(
            f"{_fmt_tokens(self._s_in)} in / {_fmt_tokens(self._s_out)} out",
            style=f"bold {GREEN}",
        )
        if self._s_has_cost:
            t.append(f" - ${self._s_cost:.4f}", style=DIM)
        t.append("  |  ", style=DIM)
        t.append_text(self._ctx_gauge())
        # live activity (safe to crop on the right; the walker carries the
        # "it's moving" signal, so no spinner glyph is needed here)
        if self.is_processing:
            t.append("  |  ", style=DIM)
            if self._activity:
                t.append(self._activity, style=AMBER)
            if self._turn:
                t.append(f" - turn {self._turn}", style=DIM)
                if self._max_turns:
                    t.append(f"/{self._max_turns}", style=DIM)
            if self._proc_start:
                t.append(f" - {time.monotonic() - self._proc_start:.1f}s", style=DIM)
        try:
            self.query_one("#chat-hud", Static).update(t)
        except Exception:
            pass

    def _hints_idle(self) -> str:
        att = ""
        if self._pending_attachments:
            att = f"{G.IMAGE} {len(self._pending_attachments)}  -  "
        return (att + "Ctrl+! agent  -  Ctrl+@ session  -  Ctrl+# new  -  "
                "Ctrl+F filter  -  Ctrl+J newline  -  Ctrl+~ home  -  Esc exit")

    def _set_hints(self, text: str) -> None:
        try:
            self.query_one("#chat-hints", Static).update(text)
        except Exception:
            pass

    # ── log helpers ────────────────────────────────────────────────────
    def _log(self) -> VerticalScroll:
        return self.query_one("#chat-log", VerticalScroll)

    def _mount(self, widget) -> None:
        try:
            self._log().mount(widget)
            self._log().scroll_end(animate=False)
        except Exception:
            pass

    def _mount_cat(self, widget, cat: str) -> None:
        """Mount a categorised (filterable) widget, honouring current filter."""
        widget.add_class(f"cat-{cat}")
        widget.display = self._filter.get(cat, True)
        self._cat_widgets.setdefault(cat, []).append(widget)
        self._mount(widget)

    def _clear_log(self) -> None:
        try:
            self._log().remove_children()
        except Exception:
            pass
        self._cur_assistant = None
        self._cur_text = ""
        self._pending_tools.clear()
        for cat in self._cat_widgets:
            self._cat_widgets[cat] = []
        # The static banner always sits at the top of a fresh transcript.
        self._mount_banner()

    def _info(self, msg: str) -> None:
        self._mount(Static(Text(f"{G.BULLET} " + msg, style=DIM), classes="msg-pipe"))

    def _add_user(self, content: str) -> None:
        t = Text()
        t.append(f"{G.USER}\n", style=f"bold {CYAN}")
        t.append(content)
        w = Static(t, classes="msg-user")
        self._mount(w)
        # per-agent tint on the left bar
        try:
            w.styles.border_left = ("thick", self._agent_color())
        except Exception:
            pass

    def _append_assistant(self, delta: str) -> None:
        if self._cur_assistant is None:
            self._cur_text = ""
            self._cur_assistant = Static("", classes="msg-agent")
            self._mount(self._cur_assistant)
        self._cur_text += delta
        self._cur_assistant.update(self._cur_text)
        self._log().scroll_end(animate=False)

    def _finalize_assistant(self, content: str) -> None:
        text = content or self._cur_text or ""
        target = self._cur_assistant
        if target is None:
            target = Static("", classes="msg-agent")
            self._mount(target)
        try:
            from rich.markdown import Markdown
            target.update(Markdown(text) if text.strip() else Text("(no reply)", style=DIM))
        except Exception:
            target.update(text)
        self._cur_assistant = None
        self._cur_text = ""
        self._update_hud()
        self._log().scroll_end(animate=False)

    def _finalize_error(self, msg: str, style: str = RED) -> None:
        target = self._cur_assistant
        if target is None:
            target = Static("", classes="msg-agent")
            self._mount(target)
        body = self._cur_text + ("\n\n" if self._cur_text else "")
        target.update(Text(body) + Text(msg, style=style))
        target.add_class("msg-error")
        self._cur_assistant = None
        self._cur_text = ""

    # ── tool blocks ────────────────────────────────────────────────────
    def _fmt_args(self, args: Any) -> str:
        try:
            if isinstance(args, (dict, list)):
                return json.dumps(args, indent=2, ensure_ascii=False)
        except Exception:
            pass
        return str(args)

    def _preview(self, args: Any) -> str:
        try:
            if isinstance(args, dict):
                parts = []
                for k, v in list(args.items())[:2]:
                    sv = str(v)
                    parts.append(f"{k}={sv[:24] + '...' if len(sv) > 24 else sv}")
                s = ", ".join(parts)
            else:
                s = str(args)
        except Exception:
            s = ""
        return s[:48] + "..." if len(s) > 48 else s

    def _add_tool_call(self, tool: str, args: Any) -> None:
        self._cur_assistant = None
        self._cur_text = ""
        args_text = self._fmt_args(args)
        body = Static(args_text, classes="tool-body")
        col = Collapsible(body, title=f"{G.TOOL} {tool}( {self._preview(args)} )", collapsed=True)
        col.add_class("tool-block")
        self._pending_tools.append({"tool": tool, "body": body, "col": col, "args": args_text})
        self._mount_cat(col, "tools")

    def _fill_tool_result(self, tool: str, result: Any, duration_ms: Any, error: Any) -> None:
        entry = None
        for e in self._pending_tools:
            if e["tool"] == tool:
                entry = e
                break
        if entry is None:
            return
        self._pending_tools.remove(entry)
        out = result if isinstance(result, str) else self._fmt_args(result)
        if len(out) > 4000:
            out = out[:4000] + "\n... (truncated)"
        divider = "\n" + "-" * 24 + "\n"
        entry["body"].update(entry["args"] + divider + out)
        mark = G.ERR if error else G.OK
        dur = f" - {duration_ms}ms" if duration_ms else ""
        try:
            entry["col"].title = f"{G.TOOL} {tool}  {mark}{dur}"
        except Exception:
            pass

    # ── pipeline (loop) lines + stat capture ────────────────────────────
    def _capture_stats(self, ev: dict[str, Any]) -> None:
        m = ev.get("model")
        if m and m != self._t_model:
            self._t_model = m
            self._ctx_max = _context_max(m)
            self._update_banner()  # surface the model in the banner identity line
        in_v = ev.get("input_tokens")
        if isinstance(in_v, (int, float)):
            self._s_in += int(in_v)
            self._ctx_tokens = int(in_v)  # latest call = current context size
        out_v = ev.get("output_tokens")
        if isinstance(out_v, (int, float)):
            self._s_out += int(out_v)
        for ck in ("cost", "total_cost", "amount"):
            cv = ev.get(ck)
            if isinstance(cv, (int, float)) and cv:
                self._s_cost += float(cv)
                self._s_has_cost = True
                break

    def _add_pipe(self, ev: dict[str, Any]) -> None:
        step = ev.get("step", "")
        cat = "loop"
        if step == "agent_assigned":
            self.resolved_agent_id = ev.get("agent_id", "") or self.resolved_agent_id
            text = "agent ready"
        elif step == "load_context":
            text = f"context loaded ({ev.get('count', 0)})"
        elif step == "memory_search_start":
            text = "searching memory..."
            cat = "memory"
        elif step == "memory_search_end":
            text = f"memory: {ev.get('results_count', 0)} result(s)"
            cat = "memory"
        elif step == "memory_search_skip":
            text = "memory search skipped"
            cat = "memory"
        elif step == "turn_start":
            self._turn = ev.get("turn", 0) or self._turn
            self._max_turns = ev.get("max_turns", 0) or self._max_turns
            text = f"turn {ev.get('turn', '?')}/{ev.get('max_turns', '?')}"
        elif step == "llm_call_start":
            text = f"llm call - {ev.get('model', '')}"
        elif step == "llm_call_end":
            return  # captured for stats; no visible line
        else:
            return
        # live activity (the "collapsed" one-line view, shown in the HUD)
        self._activity = text
        # detailed line in the transcript (filterable; hidden by default)
        self._mount_cat(
            Static(Text(f"{G.BULLET} " + text, style=DIM), classes="msg-pipe"), cat
        )

    # ── sending ────────────────────────────────────────────────────────
    @on(ChatInput.Submitted, "#chat-input")
    def _on_submit(self, event: ChatInput.Submitted) -> None:
        text = (event.value or "").strip()
        if not text and not self._pending_attachments:
            return
        if not self.ready or self.client is None:
            self._info("still connecting to the server...")
            return
        if self.is_processing:
            return
        if text:
            self._history.append(text)
        self._hist_idx = None
        self.query_one("#chat-input", ChatInput).text = ""
        self._autosize_input()
        self._send_worker = self.run_worker(self._send(text), group="turn", exclusive=True)

    @on(ChatInput.ImagesDropped, "#chat-input")
    def _on_images(self, event: ChatInput.ImagesDropped) -> None:
        if self.client is None:
            return
        self.run_worker(self._attach_images(event.paths), group="attach", exclusive=False)

    @on(ChatInput.HistoryNav, "#chat-input")
    def _on_history_nav(self, event: ChatInput.HistoryNav) -> None:
        if not self._history:
            return
        if self._hist_idx is None:
            self._hist_idx = len(self._history)
        self._hist_idx = max(0, min(len(self._history), self._hist_idx + event.delta))
        ta = self.query_one("#chat-input", ChatInput)
        ta.text = "" if self._hist_idx >= len(self._history) else self._history[self._hist_idx]
        ta.move_cursor(ta.document.end)
        self._autosize_input()

    async def _attach_images(self, paths: list[str]) -> None:
        for p in paths:
            try:
                att = await self.client.upload_image(p, self.session_id)
                if att.get("id"):
                    self._pending_attachments.append(att)
                    self._info(f"attached image: {att.get('original_name', Path(p).name)}")
            except WebAgentError as e:
                self._info(f"attach failed: {e}")
        self._set_hints(self._hints_idle())

    @on(TextArea.Changed, "#chat-input")
    def _on_changed(self, _event: TextArea.Changed) -> None:
        self._autosize_input()

    def _autosize_input(self) -> None:
        try:
            ta = self.query_one("#chat-input", ChatInput)
            lines = ta.text.count("\n") + 1
            ta.styles.height = max(1, min(5, lines)) + 2  # + round border
        except Exception:
            pass

    async def _send(self, text: str) -> None:
        self.is_processing = True
        self._turn = 0
        self._max_turns = 0
        self._activity = "thinking..."
        self._proc_start = time.monotonic()
        if self._walker is not None:
            self._walker.set_state("walk")
        att_ids = [a["id"] for a in self._pending_attachments if a.get("id")]
        self._pending_attachments = []
        self._set_hints("working...   Esc to stop")
        shown = text if text else "(image)"
        if att_ids:
            shown = (text + "\n" if text else "") + f"[{len(att_ids)} image attached]"
        self._add_user(shown)
        self._refresh_status()
        self._update_hud()
        kwargs: dict[str, Any] = {}
        if self.agent_kind == "agent":
            kwargs["agent_id"] = self.agent_value
        else:
            kwargs["agent_template_id"] = self.agent_value
        if att_ids:
            kwargs["attachment_ids"] = att_ids
        try:
            async for ev in self.client.stream_chat(text or "(see attached image)",
                                                    self.session_id, **kwargs):
                self._handle_event(ev)
        except WebAgentError as e:
            self._finalize_error(f"{G.WARN} {e}")
            self._walker_pose("trip")
        except Exception as e:  # noqa: BLE001
            self._finalize_error(f"{G.WARN} stream error: {e}")
            self._walker_pose("trip")
        finally:
            self.is_processing = False
            self._activity = ""
            self._pending_tools.clear()
            self._walker_rest_soon()
            self._set_hints(self._hints_idle())
            self._refresh_status()
            self._update_hud()
            self.cfg.last_session_id = self.session_id
            self.cfg.last_agent_ref = (
                f"agent:{self.agent_value}" if self.agent_kind == "agent"
                else f"template:{self.agent_value}"
            )
            self.cfg.save()

    def _walker_pose(self, pose: str) -> None:
        if self._walker is not None:
            self._walker.set_state(pose)

    def _walker_rest_soon(self) -> None:
        """Let the final pose (cheer/trip) linger briefly, then go idle."""
        def _rest() -> None:
            if self._walker is not None and not self.is_processing:
                self._walker.set_state("idle")
        try:
            self.set_timer(0.9, _rest)
        except Exception:
            _rest()

    def _handle_event(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        if t in ("pipeline", "billing"):
            self._capture_stats(ev)
        if t == "stream":
            self._append_assistant(ev.get("content", "") or "")
            self._walker_pose("walk")
        elif t == "tool_call":
            self._add_tool_call(ev.get("tool", "tool"), ev.get("args"))
            self._activity = f"running {ev.get('tool', 'tool')}..."
            self._walker_pose("work")
            self._update_hud()
        elif t == "tool_result":
            self._fill_tool_result(
                ev.get("tool", "tool"), ev.get("result", ""),
                ev.get("duration_ms"), ev.get("error"),
            )
            self._walker_pose("walk")
        elif t == "pipeline":
            self._add_pipe(ev)
            self._update_hud()
        elif t == "response":
            self._finalize_assistant(ev.get("content", "") or "")
            self._walker_pose("cheer")
        elif t == "error":
            self._finalize_error(f"{G.WARN} {ev.get('message', 'error')}")
            self._walker_pose("trip")
        elif t == "interrupted":
            msg = ev.get("message") or ""
            self._finalize_error(f"(interrupted{': ' + msg if msg else ''})", style=AMBER)

    # ── actions ────────────────────────────────────────────────────────
    def action_back(self) -> None:
        # First Esc stops a running turn; a second Esc leaves to Home.
        if self.is_processing:
            self.action_stop()
            return
        self.app.pop_screen()

    def action_go_home(self) -> None:
        if self.is_processing:
            self.action_stop()
        self.app.pop_screen()

    def action_stop(self) -> None:
        if not self.is_processing:
            return
        if self._send_worker is not None:
            try:
                self._send_worker.cancel()
            except Exception:
                pass
        if self.client is not None:
            self.run_worker(self.client.interrupt(self.session_id))
        self.is_processing = False
        self._activity = ""
        self._walker_pose("idle")
        self._set_hints(self._hints_idle())
        self._refresh_status()
        self._update_hud()
        self._info("(stopped)")

    def action_new_session(self) -> None:
        self._new_session()

    def action_noop(self) -> None:
        pass

    def action_pick_agent(self) -> None:
        if self.ready:
            self.run_worker(self._open_agent_picker(), group="ui", exclusive=True)

    def action_pick_session(self) -> None:
        if self.ready:
            self.run_worker(self._open_session_picker(), group="ui", exclusive=True)

    def action_new_agent(self) -> None:
        if self.ready:
            self.run_worker(self._open_template_picker(), group="ui", exclusive=True)

    def action_filter(self) -> None:
        self.run_worker(self._open_filter(), group="ui", exclusive=True)

    # ── pickers (workers) ──────────────────────────────────────────────
    async def _open_filter(self) -> None:
        cats = [(k, label) for k, label, _ in _FILTER_CATS]
        enabled = {k for k, v in self._filter.items() if v}
        chosen = await self.app.push_screen_wait(FilterModal(cats, enabled))
        if chosen is None:
            return
        self._apply_filter(chosen)

    def _apply_filter(self, enabled: set[str]) -> None:
        for k, _, _ in _FILTER_CATS:
            on = k in enabled
            self._filter[k] = on
            for w in self._cat_widgets.get(k, []):
                try:
                    w.display = on
                except Exception:
                    pass
        try:
            self._log().scroll_end(animate=False)
        except Exception:
            pass

    async def _open_agent_picker(self) -> None:
        customs = await self.client.list_custom_agents()
        items: list[tuple[str, str]] = [
            ("template:admin-agent", f"{G.ADMIN} " + _ADMIN_TEMPLATES["admin-agent"]),
            ("template:integration-admin-agent", f"{G.PLUG} " + _ADMIN_TEMPLATES["integration-admin-agent"]),
        ]
        for a in customs:
            items.append((f"agent:{a['id']}", f"{G.AGENT} " + (a.get("name") or a["id"][:8])))
        items.append(("__new__", f"{G.NEW} New agent from template..."))
        choice = await self.app.push_screen_wait(ListPicker("Select agent", items))
        if not choice:
            return
        if choice == "__new__":
            await self._open_template_picker()
            return
        self._set_agent(choice)
        self._new_session()

    async def _open_template_picker(self) -> None:
        templates = await self.client.list_templates()
        items = [(t["id"], t.get("name") or t["id"]) for t in templates if t.get("id")]
        if not items:
            self._info("no templates available")
            return
        tid = await self.app.push_screen_wait(ListPicker("New agent from template", items))
        if not tid:
            return
        name = next((t.get("name") for t in templates if t.get("id") == tid), None) or tid
        try:
            agent = await self.client.create_agent(name=name, template_id=tid)
        except WebAgentError as e:
            self._info(f"could not create agent: {e}")
            return
        if agent.get("id"):
            self._set_agent(f"agent:{agent['id']}")
            self.agent_name = agent.get("name") or self.agent_name
            self._new_session()

    async def _open_session_picker(self) -> None:
        agent_id = self.resolved_agent_id if self.agent_kind == "template" else self.agent_value
        sessions = await self.client.list_sessions(agent_id or None)
        items: list[tuple[str, str]] = [("__new__", f"{G.NEW} New session")]
        for s in sessions:
            items.append((s["id"], s.get("title") or s["id"][:8]))
        choice = await self.app.push_screen_wait(ListPicker("Sessions", items))
        if not choice:
            return
        if choice == "__new__":
            self._new_session()
            return
        if self.is_processing:
            self.action_stop()
        title = next((s.get("title") for s in sessions if s["id"] == choice), "") or ""
        self.session_id = choice
        self.session_title = title
        self._reset_session_stats()
        await self._load_history(choice)
        self._refresh_status()
        self._update_hud()

    # ── agent / session state ──────────────────────────────────────────
    def _set_agent(self, ref: str) -> None:
        kind, value, name = _parse_agent_ref(ref)
        self.agent_kind = kind
        self.agent_value = value
        self.agent_name = name
        self.resolved_agent_id = value if kind == "agent" else ""
        self.cfg.last_agent_ref = ref
        self.cfg.save()
        self._refresh_status()
        self._update_banner()

    def _reset_session_stats(self) -> None:
        self._s_in = self._s_out = 0
        self._s_cost = 0.0
        self._s_has_cost = False
        self._ctx_tokens = 0
        self._turn = 0
        self._max_turns = 0

    def _new_session(self, announce: bool = True) -> None:
        if self.is_processing:
            self.action_stop()
        self.session_id = str(uuid.uuid4())
        self.session_title = ""
        self._pending_attachments = []
        self._reset_session_stats()
        self.cfg.last_session_id = ""
        self.cfg.save()
        self._clear_log()
        self._update_banner()
        self._refresh_status()
        self._update_hud()
        self._set_hints(self._hints_idle())
        try:
            self.query_one("#chat-input", ChatInput).focus()
        except Exception:
            pass

    async def _load_history(self, session_id: str) -> None:
        rows = await self.client.load_history(session_id)
        self._clear_log()
        for row in rows:
            role = row.get("role")
            content = row.get("content") or ""
            if role == "user":
                self._add_user(content)
            elif role == "assistant":
                content = self._strip_toolcall_suffix(content)
                if content.strip():
                    self._mount_agent_final(content)
            elif role == "tool":
                name = row.get("tool_name") or "tool"
                out = row.get("output") or content
                self._mount_history_tool(name, out)
        self._update_banner()
        self._log().scroll_end(animate=False)

    @staticmethod
    def _strip_toolcall_suffix(text: str) -> str:
        idx = text.find("\n\n[Tool calls: ")
        return text[:idx] if idx != -1 else text

    def _mount_agent_final(self, content: str) -> None:
        w = Static("", classes="msg-agent")
        self._mount(w)
        try:
            from rich.markdown import Markdown
            w.update(Markdown(content))
        except Exception:
            w.update(content)

    def _mount_history_tool(self, name: str, out: Any) -> None:
        text = out if isinstance(out, str) else self._fmt_args(out)
        if len(text) > 4000:
            text = text[:4000] + "\n... (truncated)"
        col = Collapsible(Static(text, classes="tool-body"), title=f"{G.TOOL} {name}", collapsed=True)
        col.add_class("tool-block")
        self._mount_cat(col, "tools")

    # ── cleanup ────────────────────────────────────────────────────────
    def on_unmount(self) -> None:
        # The client is cached on the App and reused; don't close it here.
        if self._walker is not None:
            self._walker.set_state("idle")
