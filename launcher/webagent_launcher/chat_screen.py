"""Full-screen, keyboard-driven chat client for the webagent server.

Mirrors the webapp's right-side web chat inside the TUI:
  * Agent + Session pickers (keyboard-invoked list overlays).
  * Live token-by-token streaming.
  * Tool calls rendered as expandable blocks (name + args + result).
  * Agent loop steps shown as dim status lines.

No on-screen buttons — everything is a keyboard shortcut, shown in the hint
bar. Talks to the SAME local server the launcher starts (HTTP + SSE); it is
the only thing that can run the agent loop and read local.db.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Collapsible, Input, Label, ListItem, ListView, Static

from .api_client import WebAgentClient, WebAgentError
from .config import LauncherConfig
from .server import ServerController

# Palette (terminal-only; the dark/light rule applies to the web ui/, not here)
GREEN = "#39ff14"
DIM = "#6a6a8a"
RED = "#ff5577"
CYAN = "#5dd6ff"
AMBER = "#ffb000"

_ADMIN_TEMPLATES = {
    "admin-agent": "Admin coding agent (shell · files)",
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
            yield Static("Enter to sign in · Esc to cancel", classes="dim")

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
        Binding("ctrl+a", "pick_agent", "Agent", priority=True),
        Binding("ctrl+s", "pick_session", "Session", priority=True),
        Binding("ctrl+n", "new_session", "New session", priority=True),
        Binding("ctrl+g", "new_agent", "New agent", priority=True),
        Binding("ctrl+x", "stop", "Stop", priority=True),
        # Swallow the home screen's single-letter server shortcuts so they can't
        # fire while chat is open and focus is off the input. When the input IS
        # focused, the printable key is consumed for typing before reaching here.
        *[Binding(k, "noop", show=False) for k in
          ("q", "l", "r", "s", "b", "d", "p", "f", "t", "c", "space")],
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

    # ── layout ─────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Static("connecting…", id="chat-status")
        yield VerticalScroll(id="chat-log")
        yield Input(placeholder="Message  (Enter to send)", id="chat-input")
        yield Static(self._hints_idle(), id="chat-hints")

    def on_mount(self) -> None:
        self.query_one("#chat-input", Input).focus()
        self.set_interval(2.0, self._refresh_status)
        if self._autostart:
            self.run_worker(self._init(), group="init", exclusive=True)

    # ── init / connect ─────────────────────────────────────────────────
    async def _init(self) -> None:
        self._status("starting server…")
        ok = await ServerController.wait_until_ready(timeout=60.0)
        if not ok:
            self._status("server not ready — press Esc, start it, then reopen chat")
            return

        # Reuse a client cached on the App across home/chat toggles (no re-login).
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
                    self._status("login cancelled — press Esc")
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
        # Restore the last conversation if it belongs to the current agent path.
        if self.cfg.last_session_id:
            self.session_id = self.cfg.last_session_id
            await self._load_history(self.session_id)
        else:
            self._new_session(announce=False)
        self._refresh_status()
        self._info("Ready. Type below. Ctrl+A agent · Ctrl+S session · Esc home.")
        self.query_one("#chat-input", Input).focus()

    # ── status / hints ─────────────────────────────────────────────────
    def _server_dot(self) -> tuple[str, str]:
        st = self.controller.state.status if self.controller else "running"
        if st == "running":
            return "● live", GREEN
        if st in ("starting", "stopping"):
            return "● " + st, AMBER
        return "● " + st, RED

    def _refresh_status(self) -> None:
        dot, color = self._server_dot()
        t = Text()
        t.append(self.agent_name or "agent", style=f"bold {GREEN}")
        t.append("  ·  ", style=DIM)
        t.append(self.session_title or "new session", style=CYAN)
        t.append("  ·  ", style=DIM)
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

    def _hints_idle(self) -> str:
        return ("Enter send  ·  Ctrl+A agent  ·  Ctrl+S session  ·  Ctrl+N new  ·  "
                "Ctrl+G new-agent  ·  Esc home")

    def _set_hints(self, text: str) -> None:
        try:
            self.query_one("#chat-hints", Static).update(text)
        except Exception:
            pass

    # ── log helpers ────────────────────────────────────────────────────
    def _log(self) -> VerticalScroll:
        return self.query_one("#chat-log", VerticalScroll)

    def _mount(self, widget) -> None:
        # Defensive: a session/agent switch can clear the log while a turn is
        # still mounting widgets — swallow the resulting mount race rather than
        # crash the screen.
        try:
            self._log().mount(widget)
            self._log().scroll_end(animate=False)
        except Exception:
            pass

    def _clear_log(self) -> None:
        self._log().remove_children()
        self._cur_assistant = None
        self._cur_text = ""
        self._pending_tools.clear()

    def _info(self, msg: str) -> None:
        self._mount(Static(Text("· " + msg, style=DIM), classes="msg-pipe"))

    def _add_user(self, content: str) -> None:
        t = Text()
        t.append("you\n", style=f"bold {CYAN}")
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
                    parts.append(f"{k}={sv[:24] + '…' if len(sv) > 24 else sv}")
                s = ", ".join(parts)
            else:
                s = str(args)
        except Exception:
            s = ""
        return s[:48] + "…" if len(s) > 48 else s

    def _add_tool_call(self, tool: str, args: Any) -> None:
        # Close the current assistant bubble so post-tool text starts fresh below.
        self._cur_assistant = None
        self._cur_text = ""
        args_text = self._fmt_args(args)
        body = Static(args_text, classes="tool-body")
        col = Collapsible(body, title=f"🔧 {tool}( {self._preview(args)} )", collapsed=True)
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
            out = out[:4000] + "\n… (truncated)"
        divider = "\n" + "─" * 24 + "\n"
        entry["body"].update(entry["args"] + divider + out)
        mark = "✗" if error else "✓"
        dur = f" · {duration_ms}ms" if duration_ms else ""
        try:
            entry["col"].title = f"🔧 {tool} {mark}{dur}"
        except Exception:
            pass

    # ── pipeline (loop) lines ──────────────────────────────────────────
    def _add_pipe(self, ev: dict[str, Any]) -> None:
        step = ev.get("step", "")
        if step == "agent_assigned":
            self.resolved_agent_id = ev.get("agent_id", "") or self.resolved_agent_id
            text = "agent ready"
        elif step == "load_context":
            text = f"context loaded ({ev.get('count', 0)})"
        elif step == "memory_search_start":
            text = "searching memory…"
        elif step == "memory_search_end":
            text = f"memory: {ev.get('results_count', 0)} result(s)"
        elif step == "memory_search_skip":
            text = "memory search skipped"
        elif step == "turn_start":
            text = f"turn {ev.get('turn', '?')}/{ev.get('max_turns', '?')}"
        elif step == "llm_call_start":
            text = f"llm call · {ev.get('model', '')}"
        else:
            return  # skip noisy/internal steps
        self._mount(Static(Text("· " + text, style=DIM), classes="msg-pipe"))

    # ── sending ────────────────────────────────────────────────────────
    @on(Input.Submitted, "#chat-input")
    def _on_submit(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        if not text:
            return
        if not self.ready or self.client is None:
            self._info("still connecting to the server…")
            return
        if self.is_processing:
            return
        self.query_one("#chat-input", Input).value = ""
        # Own group so picker/init workers never cancel an in-flight turn.
        self._send_worker = self.run_worker(self._send(text), group="turn", exclusive=True)

    async def _send(self, text: str) -> None:
        self.is_processing = True
        self._set_hints("streaming…   Ctrl+X / Esc to stop")
        self._add_user(text)
        kwargs: dict[str, Any] = {}
        if self.agent_kind == "agent":
            kwargs["agent_id"] = self.agent_value
        else:
            kwargs["agent_template_id"] = self.agent_value
        try:
            async for ev in self.client.stream_chat(text, self.session_id, **kwargs):
                self._handle_event(ev)
        except WebAgentError as e:
            self._finalize_error(f"⚠ {e}")
        except Exception as e:  # noqa: BLE001
            self._finalize_error(f"⚠ stream error: {e}")
        finally:
            self.is_processing = False
            self._pending_tools.clear()
            self._set_hints(self._hints_idle())
            # Remember this conversation so reopening restores it.
            self.cfg.last_session_id = self.session_id
            self.cfg.last_agent_ref = (
                f"agent:{self.agent_value}" if self.agent_kind == "agent"
                else f"template:{self.agent_value}"
            )
            self.cfg.save()

    def _handle_event(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
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
        elif t == "response":
            self._finalize_assistant(ev.get("content", "") or "")
        elif t == "error":
            self._finalize_error(f"⚠ {ev.get('message', 'error')}")
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
        self._set_hints(self._hints_idle())
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
            ("template:admin-agent", "🛠  " + _ADMIN_TEMPLATES["admin-agent"]),
            ("template:integration-admin-agent", "🔌  " + _ADMIN_TEMPLATES["integration-admin-agent"]),
        ]
        for a in customs:
            items.append((f"agent:{a['id']}", "🤖  " + (a.get("name") or a["id"][:8])))
        items.append(("__new__", "＋  New agent from template…"))
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
        items: list[tuple[str, str]] = [("__new__", "＋  New session")]
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
        self.cfg.last_session_id = ""
        self.cfg.save()
        self._clear_log()
        self._refresh_status()
        if announce:
            self._info(f"new session · {self.agent_name}")
        self.query_one("#chat-input", Input).focus()

    async def _load_history(self, session_id: str) -> None:
        rows = await self.client.load_history(session_id)
        self._clear_log()
        if not rows:
            self._info("empty session — start typing")
            return
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
            text = text[:4000] + "\n… (truncated)"
        col = Collapsible(Static(text, classes="tool-body"), title=f"🔧 {name}", collapsed=True)
        col.add_class("tool-block")
        self._mount(col)

    # ── cleanup ────────────────────────────────────────────────────────
    def on_unmount(self) -> None:
        # The client is cached on the App and reused; don't close it here.
        pass
