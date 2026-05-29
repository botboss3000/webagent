"""Full-screen, keyboard-driven chat client for the webagent server.

Mirrors the webapp's right-side web chat inside the TUI:
  * Agent + Session pickers (keyboard-invoked list overlays).
  * Live token-by-token streaming.
  * Tool calls rendered as expandable blocks (name + args + result).
  * Agent loop steps shown as dim status lines.
  * A multi-line editor input with full Windows-style editing.
  * A welcome animation on an empty session + a "thinking" animation while
    the agent is working.
  * Per-response stats (model · tokens · time · cost) from loop metadata.

No on-screen buttons — everything is a keyboard shortcut, shown in the hint
bar. Talks to the SAME local server the launcher starts (HTTP + SSE); it is
the only thing that can run the agent loop and read local.db.

Glyphs are ASCII-only (no emoji) so they render in any Windows console font.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Optional

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Collapsible, Input, Label, ListItem, ListView, Static, TextArea

from .api_client import WebAgentClient, WebAgentError
from .config import LauncherConfig
from .glyphs import G
from .palette import build_palette_from_config
from .server import ServerController
from .stage import AnimatedStage

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


class ChatInput(TextArea):
    """Multi-line message editor.

    Inherits all of TextArea's Windows-style editing: Ctrl+C/V/X copy/cut/
    paste, Ctrl+Z/Y undo/redo, Ctrl+arrows word-skip, Home/End, mouse
    selection. We override:
      * Enter            -> send (posts Submitted)
      * Shift/Ctrl+Enter -> newline (terminal permitting; Ctrl+J always works)
      * Ctrl+A           -> select all (Windows convention, not line-start)
      * Ctrl+Up / Down   -> document start / end
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

    async def _on_key(self, event: events.Key) -> None:
        # NOTE: base TextArea._on_key is async — we MUST await super().
        key = event.key
        if key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self, self.text))
            return
        if key in ("shift+enter", "ctrl+enter", "ctrl+j"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
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
        Binding("escape", "back", "Home", priority=True),
        Binding("f2", "pick_agent", "Agent", priority=True),
        Binding("f3", "pick_session", "Session", priority=True),
        Binding("ctrl+n", "new_session", "New session", priority=True),
        Binding("ctrl+g", "new_agent", "New agent", priority=True),
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
        self._welcome: Optional[AnimatedStage] = None
        self._thinking: Optional[AnimatedStage] = None
        self._pending_attachments: list[dict[str, Any]] = []
        # per-turn stat accumulators
        self._t_model = ""
        self._t_in = 0
        self._t_out = 0
        self._t_ms = 0
        self._t_cost = 0.0
        self._t_has_cost = False

    # ── layout ─────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Static("connecting...", id="chat-status")
        with Container(id="chat-body"):
            yield VerticalScroll(id="chat-log")
        yield ChatInput(id="chat-input", soft_wrap=True, tab_behavior="focus")
        yield Static(self._hints_idle(), id="chat-hints")

    def on_mount(self) -> None:
        self.query_one("#chat-input", ChatInput).focus()
        self.set_interval(2.0, self._refresh_status)
        self._show_welcome()
        if self._autostart:
            self.run_worker(self._init(), group="init", exclusive=True)

    # ── welcome / thinking animations ──────────────────────────────────
    def _new_stage(self, *, show_logo: bool, fps: int) -> AnimatedStage:
        return AnimatedStage(
            palette=build_palette_from_config(self.cfg),
            char_ramp=self.cfg.char_ramp,
            fps=fps,
            style=self.cfg.animation_style if self.cfg.animation_style != "off" else "plasma",
            speed=self.cfg.theme_speed,
            intensity=self.cfg.animation_intensity,
            show_logo=show_logo,
        )

    def _show_welcome(self) -> None:
        """Logo animation shown on an empty session; removed once chatting."""
        try:
            body = self.query_one("#chat-body", Container)
            self.query_one("#chat-log", VerticalScroll).display = False
        except Exception:
            return
        if self._welcome is None:
            self._welcome = self._new_stage(show_logo=True, fps=self.cfg.fps)
            self._welcome.id = "chat-welcome"
            body.mount(self._welcome)

    def _hide_welcome(self) -> None:
        if self._welcome is not None:
            try:
                self._welcome.remove()
            except Exception:
                pass
            self._welcome = None
        try:
            self.query_one("#chat-log", VerticalScroll).display = True
        except Exception:
            pass

    def _start_thinking(self) -> None:
        if self._thinking is not None:
            return
        try:
            self._thinking = self._new_stage(show_logo=False, fps=min(self.cfg.fps, 20))
            self._thinking.id = "chat-thinking"
            self.mount(self._thinking, before="#chat-input")
        except Exception:
            self._thinking = None

    def _stop_thinking(self) -> None:
        if self._thinking is not None:
            try:
                self._thinking.remove()
            except Exception:
                pass
            self._thinking = None

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
    def _server_dot(self) -> tuple[str, str]:
        st = self.controller.state.status if self.controller else "running"
        if st == "running":
            return f"{G.DOT_LIVE} live", GREEN
        if st in ("starting", "stopping"):
            return f"{G.DOT_WARN} {st}", AMBER
        return f"{G.DOT_DEAD} {st}", RED

    def _refresh_status(self) -> None:
        dot, color = self._server_dot()
        t = Text()
        t.append(self.agent_name or "agent", style=f"bold {GREEN}")
        t.append("  -  ", style=DIM)
        t.append(self.session_title or "new session", style=CYAN)
        t.append("  -  ", style=DIM)
        t.append(dot, style=color)
        if self.is_processing:
            t.append("  -  ", style=DIM)
            t.append(f"{G.THINKING} thinking...", style=AMBER)
            if self._t_in or self._t_out:
                t.append(
                    f"  {_fmt_tokens(self._t_in)} in / {_fmt_tokens(self._t_out)} out",
                    style=DIM,
                )
        try:
            self.query_one("#chat-status", Static).update(t)
        except Exception:
            pass

    def _status(self, msg: str) -> None:
        try:
            self.query_one("#chat-status", Static).update(Text(msg, style=AMBER))
        except Exception:
            pass

    def _hints_idle(self) -> str:
        att = ""
        if self._pending_attachments:
            att = f"{G.IMAGE} {len(self._pending_attachments)}  -  "
        return (att + "Enter send  -  Shift+Enter newline  -  F2 agent  -  "
                "F3 session  -  Ctrl+N new  -  Ctrl+G new-agent  -  Esc home")

    def _set_hints(self, text: str) -> None:
        try:
            self.query_one("#chat-hints", Static).update(text)
        except Exception:
            pass

    # ── log helpers ────────────────────────────────────────────────────
    def _log(self) -> VerticalScroll:
        return self.query_one("#chat-log", VerticalScroll)

    def _mount(self, widget) -> None:
        # Real content arriving → ensure the transcript (not welcome) is shown.
        self._hide_welcome()
        try:
            self._log().mount(widget)
            self._log().scroll_end(animate=False)
        except Exception:
            pass

    def _clear_log(self) -> None:
        try:
            self._log().remove_children()
        except Exception:
            pass
        self._cur_assistant = None
        self._cur_text = ""
        self._pending_tools.clear()

    def _info(self, msg: str) -> None:
        self._mount(Static(Text(f"{G.BULLET} " + msg, style=DIM), classes="msg-pipe"))

    def _add_user(self, content: str) -> None:
        t = Text()
        t.append(f"{G.USER}\n", style=f"bold {CYAN}")
        t.append(content)
        self._mount(Static(t, classes="msg-user"))

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
        self._mount_stats()
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

    def _mount_stats(self) -> None:
        """Dim one-line summary built from this turn's loop metadata."""
        parts: list[str] = []
        if self._t_model:
            parts.append(self._t_model)
        if self._t_in or self._t_out:
            parts.append(f"{_fmt_tokens(self._t_in)} in / {_fmt_tokens(self._t_out)} out")
        if self._t_ms:
            parts.append(f"{self._t_ms / 1000:.1f}s")
        if self._t_has_cost and self._t_cost:
            parts.append(f"${self._t_cost:.4f}")
        if not parts:
            return
        self._mount(Static(Text("  " + "  -  ".join(parts), style=DIM), classes="msg-stats"))

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
        self._mount(col)

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
        if m:
            self._t_model = m
        for k in ("input_tokens", "output_tokens"):
            v = ev.get(k)
            if isinstance(v, (int, float)):
                if k == "input_tokens":
                    self._t_in += int(v)
                else:
                    self._t_out += int(v)
        dur = ev.get("duration_ms")
        if isinstance(dur, (int, float)):
            self._t_ms += int(dur)
        for ck in ("cost", "total_cost", "amount"):
            cv = ev.get(ck)
            if isinstance(cv, (int, float)) and cv:
                self._t_cost += float(cv)
                self._t_has_cost = True
                break

    def _add_pipe(self, ev: dict[str, Any]) -> None:
        step = ev.get("step", "")
        if step == "agent_assigned":
            self.resolved_agent_id = ev.get("agent_id", "") or self.resolved_agent_id
            text = "agent ready"
        elif step == "load_context":
            text = f"context loaded ({ev.get('count', 0)})"
        elif step == "memory_search_start":
            text = "searching memory..."
        elif step == "memory_search_end":
            text = f"memory: {ev.get('results_count', 0)} result(s)"
        elif step == "memory_search_skip":
            text = "memory search skipped"
        elif step == "turn_start":
            text = f"turn {ev.get('turn', '?')}/{ev.get('max_turns', '?')}"
        elif step == "llm_call_start":
            text = f"llm call - {ev.get('model', '')}"
        elif step == "llm_call_end":
            return  # captured for stats; no visible line
        else:
            return
        self._mount(Static(Text(f"{G.BULLET} " + text, style=DIM), classes="msg-pipe"))

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
        self.query_one("#chat-input", ChatInput).text = ""
        self._autosize_input()
        self._send_worker = self.run_worker(self._send(text), group="turn", exclusive=True)

    @on(ChatInput.ImagesDropped, "#chat-input")
    def _on_images(self, event: ChatInput.ImagesDropped) -> None:
        if self.client is None:
            return
        self.run_worker(self._attach_images(event.paths), group="attach", exclusive=False)

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
            ta.styles.height = max(1, min(3, lines)) + 2  # + round border
        except Exception:
            pass

    async def _send(self, text: str) -> None:
        self.is_processing = True
        self._t_model = ""
        self._t_in = self._t_out = self._t_ms = 0
        self._t_cost = 0.0
        self._t_has_cost = False
        att_ids = [a["id"] for a in self._pending_attachments if a.get("id")]
        self._pending_attachments = []
        self._set_hints("thinking...   Esc to stop")
        self._start_thinking()
        shown = text if text else "(image)"
        if att_ids:
            shown = (text + "\n" if text else "") + f"[{len(att_ids)} image attached]"
        self._add_user(shown)
        self._refresh_status()
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
        except Exception as e:  # noqa: BLE001
            self._finalize_error(f"{G.WARN} stream error: {e}")
        finally:
            self.is_processing = False
            self._pending_tools.clear()
            self._stop_thinking()
            self._set_hints(self._hints_idle())
            self._refresh_status()
            self.cfg.last_session_id = self.session_id
            self.cfg.last_agent_ref = (
                f"agent:{self.agent_value}" if self.agent_kind == "agent"
                else f"template:{self.agent_value}"
            )
            self.cfg.save()

    def _handle_event(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        if t in ("pipeline", "billing"):
            self._capture_stats(ev)
        if t == "stream":
            self._append_assistant(ev.get("content", "") or "")
        elif t == "tool_call":
            self._add_tool_call(ev.get("tool", "tool"), ev.get("args"))
        elif t == "tool_result":
            self._fill_tool_result(
                ev.get("tool", "tool"), ev.get("result", ""),
                ev.get("duration_ms"), ev.get("error"),
            )
        elif t == "pipeline":
            self._add_pipe(ev)
            self._refresh_status()
        elif t == "response":
            self._finalize_assistant(ev.get("content", "") or "")
        elif t == "error":
            self._finalize_error(f"{G.WARN} {ev.get('message', 'error')}")
        elif t == "interrupted":
            msg = ev.get("message") or ""
            self._finalize_error(f"(interrupted{': ' + msg if msg else ''})", style=AMBER)

    # ── actions ────────────────────────────────────────────────────────
    def action_back(self) -> None:
        if self.is_processing:
            self.action_stop()
            return
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
        self._stop_thinking()
        self._set_hints(self._hints_idle())
        self._refresh_status()
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

    # ── pickers (workers) ──────────────────────────────────────────────
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
        await self._load_history(choice)
        self._refresh_status()

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

    def _new_session(self, announce: bool = True) -> None:
        if self.is_processing:
            self.action_stop()
        self.session_id = str(uuid.uuid4())
        self.session_title = ""
        self._pending_attachments = []
        self.cfg.last_session_id = ""
        self.cfg.save()
        self._clear_log()
        self._show_welcome()
        self._refresh_status()
        self._set_hints(self._hints_idle())
        try:
            self.query_one("#chat-input", ChatInput).focus()
        except Exception:
            pass

    async def _load_history(self, session_id: str) -> None:
        rows = await self.client.load_history(session_id)
        self._clear_log()
        if not rows:
            self._show_welcome()
            return
        self._hide_welcome()
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
        self._mount(col)

    # ── cleanup ────────────────────────────────────────────────────────
    def on_unmount(self) -> None:
        # The client is cached on the App and reused; don't close it here.
        self._stop_thinking()
