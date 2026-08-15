"""Run the locally installed Codex CLI in non-interactive JSON mode."""
import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys as _sys
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

from plugins.engines._heartbeat import start_heartbeat

logger = logging.getLogger(__name__)

ENGINE_ID = "codex"
_POLL_TIMEOUT = 0.4


def _pick_port() -> int:
    """Pick a free loopback port for the MCP HTTP server."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port

ENGINE_ID = "codex"
_POLL_TIMEOUT = 0.4

# ── Live model catalog (codex debug models) ─────────────────────────────────
# The Codex CLI bundles the authoritative model list; `codex debug models`
# renders it as JSON. We surface it (filtered to user-selectable models) so the
# chat footer can show the REAL options without an admin maintaining them.
_MODEL_CATALOG_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_MODEL_CATALOG_TTL = 900.0  # 15 min

# Friendly labels for reasoning-effort values the CLI may report.
_EFFORT_LABELS = {
    "minimal": "Minimal", "low": "Low", "medium": "Medium", "high": "High",
    "xhigh": "X-High", "max": "Max", "ultra": "Ultra",
}


def model_catalog(force: bool = False) -> Optional[List[Dict[str, Any]]]:
    """Return the live Codex model catalog straight from the CLI.

    ``codex debug models`` emits the bundled catalog (slug, display name,
    blurb, per-model reasoning levels + default). We keep only the models a
    user may actually pick (visibility == "list" — the -wm routing aliases and
    the auto-review model are hidden). Cached ~15 min per process (pass
    ``force=True`` to bypass the cache and re-query the CLI — what the agent
    Config tab's "Query CLI for latest model options" button does). Returns
    None on ANY failure (CLI missing, parse error, …) so callers can fall back
    to a curated list instead of erroring."""
    now = time.monotonic()
    cached = _MODEL_CATALOG_CACHE.get("data")
    if not force and cached is not None and now - _MODEL_CATALOG_CACHE.get("ts", 0.0) < _MODEL_CATALOG_TTL:
        return cached
    try:
        codex = shutil.which("codex")
        if not codex:
            return None
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        r = subprocess.run(
            [codex, "debug", "models"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, creationflags=flags,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout or "{}")
        raw = data.get("models") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return None
        models: List[Dict[str, Any]] = []
        for m in raw:
            if not isinstance(m, dict) or str(m.get("visibility") or "") != "list":
                continue
            slug = str(m.get("slug") or "").strip()
            if not slug:
                continue
            efforts = []
            for e in (m.get("supported_reasoning_levels") or []):
                if isinstance(e, dict):
                    v = str(e.get("effort") or "").strip()
                    if v:
                        efforts.append({"v": v, "label": _EFFORT_LABELS.get(v, v)})
            models.append({
                "v": slug,
                "label": str(m.get("display_name") or slug),
                "sub": str(m.get("description") or "").strip(),
                "efforts": efforts,
                "default_effort": str(m.get("default_reasoning_level") or "").strip(),
            })
        if models:
            _MODEL_CATALOG_CACHE.update({"ts": now, "data": models})
            return models
    except Exception as e:
        logger.warning("codex get_model_catalog failed: %s", e)
    return None



def _cfg(agent_rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = (agent_rec or {}).get("metadata") or {}
    if isinstance(raw, str):
        try: raw = json.loads(raw)
        except (TypeError, ValueError): raw = {}
    c = raw.get("codex_code", {}) if isinstance(raw, dict) else {}
    c = c if isinstance(c, dict) else {}
    cfg = {k: str(c.get(k) or "").strip() for k in ("model", "extra_flags", "effort")}
    cfg["append_persona"] = bool(c.get("append_persona", False))
    cfg["mcp_enabled"] = bool(c.get("mcp_enabled", False))
    cfg["mcp_tool_allowlist"] = str(c.get("mcp_tool_allowlist") or "").strip()
    return cfg


def _text(value: Any) -> str:
    if isinstance(value, str): return value
    if isinstance(value, dict): return str(value.get("text") or value.get("content") or "")
    if isinstance(value, list): return "\n".join(_text(x) for x in value)
    return str(value or "")


async def stream(*, user_id: str, session_id: str, agent_id: str, user_message: Any,
                 agent_rec: Dict[str, Any], db: Any, system_prompt: str = "",
                 channel: Optional[str] = None, parent_interaction_id: Optional[str] = None,
                 interrupt_event=None, execution_mode: str = "ask",
                 persona_prompt: Optional[str] = None, **_kwargs) -> AsyncGenerator[Dict[str, Any], None]:
    """Translate `codex exec --json` records into the normal chat event vocabulary.

    Codex's own saved login/API-key configuration is deliberately reused; credentials
    are never read or forwarded by WebAgent.
    """
    cfg = _cfg(agent_rec)
    # Remote clients do not have a meaningful local working directory. Always
    # anchor Codex to the live checkout that is running WebAgent, including when
    # old agent metadata still contains a custom `folder` value.
    from app.util.paths import project_root
    folder = project_root()
    if not os.path.isdir(folder):
        yield {"type": "error", "level": "agent", "message": f"Codex working folder does not exist: {folder}"}; return
    codex = shutil.which("codex")
    if not codex:
        yield {"type": "error", "level": "agent", "message": "Codex is not installed or is not on PATH on the machine running this turn."}; return
    prompt = _text(user_message).strip()
    if not prompt:
        yield {"type": "response", "level": "agent", "content": "I didn't get a message to send to Codex."}; return
    try:
        prior_thread_id = await db.get_session_codex_id(session_id)
    except (AttributeError, TypeError):
        prior_thread_id = None

    # One-shot "compact & restart" (/compact): if a recap has been armed for this
    # session, DON'T resume the old Codex thread — start a fresh one and seed it
    # with the recap so continuity survives at a fraction of the size. Consumed
    # (cleared) below once the fresh thread has actually produced an id.
    reseed_ctx: Optional[str] = None
    try:
        reseed_ctx = await db.get_session_codex_reseed(session_id)
    except Exception:
        reseed_ctx = None
    if reseed_ctx:
        prior_thread_id = None  # force a brand-new `codex exec` (no resume)
        prompt = (
            "<conversation_recap>\n"
            "You are continuing an earlier conversation in a FRESH session. The "
            "earlier turns are condensed below for context only — treat this as "
            "background, not as a new instruction or something the user just said. "
            "After reading it, answer the user's actual message that follows the "
            "recap.\n\n"
            f"{reseed_ctx}\n"
            "</conversation_recap>\n\n"
            f"{prompt}"
        )

    # Build the CLI invocation. `codex exec` and `codex exec resume` accept
    # different flag sets (resume supports only -c/-m/--json — no --cd, no
    # --sandbox), so options are placed per subcommand and before positionals,
    # matching `codex exec [OPTIONS] [PROMPT]` / `codex exec resume [OPTIONS]
    # [SESSION_ID] [PROMPT]`. The working folder is anchored via Popen(cwd=...),
    # so `--cd` is not needed (and would be rejected on resume).
    cmd = [codex, "exec"]
    if prior_thread_id:
        cmd += ["resume"]
    cmd += ["--json"]
    if cfg["model"]: cmd += ["--model", cfg["model"]]
    # Reasoning effort → config override (no CLI flag): model_reasoning_effort.
    if cfg["effort"]: cmd += ["-c", "model_reasoning_effort=" + json.dumps(cfg["effort"])]
    if cfg["append_persona"] and system_prompt.strip():
        # `-c` is a first-class Codex config override. JSON quotes the prompt as
        # one TOML string while keeping it out of shell parsing.
        _pers = (persona_prompt or "").strip() or system_prompt
        cmd += ["-c", "developer_instructions=" + json.dumps(_pers)]
    # Sandbox: `--sandbox` exists on plain `exec` only; `resume` takes config
    # overrides, so map the mode onto the config key there. Codex uses its own
    # engine-specific mode set (the chat pill shows Ask/Wkspc/Auto for codex
    # agents):
    #   ask   → read-only            (no writes at all)
    #   wkspc → workspace-write      (writes inside the repo only)
    #   auto  → danger-full-access   (unrestricted)
    # Legacy 'plan' (the pre-wkspc read-only slot) stays an alias for read-only
    # so existing saved sessions don't silently become writable; anything unknown
    # also falls back to read-only (safest).
    _sb = ("danger-full-access" if execution_mode == "auto"
           else ("workspace-write" if execution_mode == "wkspc" else "read-only"))
    if prior_thread_id:
        cmd += ["-c", "sandbox_mode=" + json.dumps(_sb)]
    else:
        cmd += ["--sandbox", _sb]
    if cfg["extra_flags"]: cmd += cfg["extra_flags"].split()

    # ── MCP bridge (exposes WebAgent tools to the local CLI) ───────────────────
    # Codex 0.144.5 cannot spawn stdio MCP child processes reliably on Windows
    # (space-containing paths and long arg arrays get mangled; `-c mcp_servers.*`
    # command overrides drop the args; config.toml registrations get rewritten).
    # The robust path is a streamable-HTTP server WE hold: start app/mcp/server.py
    # on 127.0.0.1 and point Codex at it with
    # `-c mcp_servers.webagent.url="http://127.0.0.1:PORT"` — no child spawn, no
    # paths, and it works for both `codex exec` and `codex exec resume`.
    mcp_proc: Optional[subprocess.Popen] = None
    if cfg["mcp_enabled"]:
        # Translate the codex-specific modes onto the MCP gate (auto/ask/plan):
        # ask (read-only) → plan (destructive tools dropped from the schema),
        # wkspc → ask (destructive visible but blocked at call time), auto → auto;
        # legacy 'plan' → plan. Unknown → plan (read-only, safest).
        _mcp_mode = {"auto": "auto", "wkspc": "ask", "ask": "plan", "plan": "plan"}.get(
            str(execution_mode or "").strip().lower(), "plan")
        _mcp_server = os.path.join(folder, "app", "mcp", "server.py")
        _mcp_port = _pick_port()
        _mcp_args = [
            _sys.executable, _mcp_server,
            "--transport", "http", "--port", str(_mcp_port),
            "--user-id", user_id,
            "--agent-id", agent_id,
            "--session-id", session_id,
            "--mode", _mcp_mode,
        ]
        if cfg["mcp_tool_allowlist"]:
            _mcp_args += ["--allowed-tools", cfg["mcp_tool_allowlist"]]
        if agent_rec and agent_rec.get("template_id"):
            _mcp_args += ["--agent-template-id", str(agent_rec["template_id"])]

        _mcp_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            mcp_proc = subprocess.Popen(
                _mcp_args,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_mcp_flags,
            )
        except Exception as _me:
            logger.warning("codex: MCP server failed to start: %s", _me)
            mcp_proc = None

        if mcp_proc is not None:
            cmd += ["-c", f'mcp_servers.webagent.url="http://127.0.0.1:{_mcp_port}"']

    if prior_thread_id:
        cmd.append(prior_thread_id)
    cmd.append(prompt)
    loop = asyncio.get_running_loop(); q: asyncio.Queue = asyncio.Queue(); holder = {}
    def worker():
        try:
            proc = subprocess.Popen(cmd, cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
            holder["proc"] = proc
            for line in proc.stdout or []: loop.call_soon_threadsafe(q.put_nowait, ("line", line))
            err = (proc.stderr.read() if proc.stderr else "")
            loop.call_soon_threadsafe(q.put_nowait, ("exit", (proc.wait(), err)))
        except Exception as exc: loop.call_soon_threadsafe(q.put_nowait, ("fail", str(exc)))
    import threading; threading.Thread(target=worker, daemon=True).start()
    # Background heartbeat keeps the liveness watchdog alive during the
    # long-running child-process wait. See plugins/engines/_heartbeat.py.
    _hb = start_heartbeat(db, session_id)
    captured_thread_id = None
    held_text = ""            # narration not yet flushed to a bubble
    last_asst_id: Optional[str] = None
    pending_tools: Dict[str, Dict[str, Any]] = {}

    async def persist(content: str, parent: Optional[str] = None,
                      message_phase: str = "progress") -> str:
        return await db.insert_interaction(user_id, session_id, role="assistant", content=content,
            parent_id=parent or parent_interaction_id, channel=channel,
            metadata=json.dumps({"provider": "codex", "engine": ENGINE_ID,
                                 "message_phase": message_phase}),
            output_data=json.dumps({"role": "assistant", "content": content, "tool_calls": []}),
            sender_id=agent_id, receiver_id=user_id)

    async def flush_held(as_final: bool = False):
        """Flush held narration as a chat bubble. Non-final flushes become an
        intermediate step (stream + agent_step_end); the final one becomes the
        turn's response."""
        nonlocal held_text, last_asst_id
        if not held_text.strip():
            return
        txt, held_text = held_text, ""
        if as_final:
            asst_id = await persist(txt, message_phase="main")
            yield {"type": "db", "level": "db", "op": "insert_interaction", "role": "assistant", "id": asst_id}
            yield {"type": "response", "level": "agent", "message_phase": "main",
                   "content": txt, "asst_id": asst_id}
            return
        asst_id = await persist(txt, parent=last_asst_id)
        last_asst_id = asst_id
        yield {"type": "db", "level": "db", "op": "insert_interaction", "role": "assistant", "id": asst_id}
        yield {"type": "stream", "level": "agent", "content": txt, "asst_id": asst_id}
        yield {"type": "agent_step_end", "level": "agent", "message_phase": "progress",
               "asst_id": asst_id, "content": txt}

    async def record_tool_result(tool_name: str, args: dict, result_text: str,
                                 success: bool, tool_call_id: str, parent: Optional[str]):
        try:
            await db.insert_interaction(
                user_id, session_id, role="tool", content=result_text[:20000],
                parent_id=parent or parent_interaction_id, tool_call_id=tool_call_id or None,
                tool_name=tool_name, channel=channel,
                metadata=json.dumps({"success": success, "duration_ms": 0,
                                     "input_params": args,
                                     "error_message": None if success else "tool error"}),
                output_data=json.dumps({"role": "tool", "content": result_text[:20000],
                                        "tool_call_id": tool_call_id, "name": tool_name,
                                        "success": success}),
                sender_id=agent_id, receiver_id=agent_id,
            )
        except Exception as _pe:
            logger.debug("codex tool-row persist failed: %s", _pe)

    try:
        while True:
            if interrupt_event is not None and interrupt_event.is_set():
                p = holder.get("proc")
                if p and p.poll() is None: p.terminate()
                if mcp_proc is not None and mcp_proc.poll() is None:
                    try: mcp_proc.terminate()
                    except Exception: pass
                # Persist the thread id before bailing out so the next turn resumes
                # the same Codex session instead of starting fresh.
                if captured_thread_id:
                    try: await db.set_session_codex_id(session_id, str(captured_thread_id))
                    except (AttributeError, TypeError): pass
                yield {"type": "interrupted", "level": "agent", "message": "Stopped by user."}; return
            try: kind, payload = await asyncio.wait_for(q.get(), timeout=_POLL_TIMEOUT)
            except asyncio.TimeoutError:
                # Check if the codex subprocess has died unexpectedly
                proc = holder.get("proc")
                if proc is not None and proc.poll() is not None:
                    logger.warning("codex: subprocess exited while waiting for output (code %s)", proc.returncode)
                    try:
                        while True:  # drain any stray exit event
                            _k, _p = q.get_nowait()
                            if _k == "exit":
                                kind, payload = _k, _p
                                break
                    except asyncio.QueueEmpty:
                        kind, payload = "exit", (proc.returncode, "")
                    break
                # Check if MCP server is still alive during idle periods
                if mcp_proc is not None and mcp_proc.poll() is not None:
                    logger.warning("codex: MCP server exited early (code %s)", mcp_proc.returncode)
                    mcp_proc = None
                continue
            if kind == "fail": yield {"type": "error", "level": "agent", "message": f"Could not start Codex: {payload}"}; return
            if kind == "line":
                try: rec = json.loads(payload)
                except (TypeError, ValueError): continue
                if not isinstance(rec, dict): continue
                rtype = rec.get("type")
                if rtype == "thread.started":
                    captured_thread_id = rec.get("thread_id") or captured_thread_id
                    # Persist the thread id immediately so a later interrupt /
                    # freeze / resume picks up the same Codex session instead of
                    # starting a fresh one (the exit-time persist is a no-op then).
                    if captured_thread_id:
                        try: await db.set_session_codex_id(session_id, str(captured_thread_id))
                        except (AttributeError, TypeError): pass
                    continue
                item = rec.get("item")
                if not isinstance(item, dict): continue
                itype = item.get("type")
                iid = str(item.get("id") or "")
                if rtype == "item.started" and itype in ("command_execution", "mcp_tool_call"):
                    # A tool round begins — flush any held narration as its own bubble.
                    async for ev in flush_held():
                        yield ev
                    if itype == "command_execution":
                        tname, targs = "shell", {"command": item.get("command") or ""}
                    else:
                        tname = f"{item.get('server') or 'mcp'}:{item.get('tool') or '?'}"
                        targs = item.get("arguments") or {}
                    tool_asst_id = await db.insert_interaction(
                        user_id, session_id, role="assistant", content="",
                        parent_id=last_asst_id or parent_interaction_id, channel=channel,
                        metadata=json.dumps({
                            "provider": "codex", "engine": ENGINE_ID,
                            "message_phase": "progress",
                        }),
                        output_data=json.dumps({
                            "role": "assistant", "content": "",
                            "tool_calls": [{
                                "id": iid, "type": "function",
                                "function": {
                                    "name": tname,
                                    "arguments": json.dumps(targs, ensure_ascii=False),
                                },
                            }],
                        }),
                        sender_id=agent_id, receiver_id=user_id,
                    )
                    last_asst_id = tool_asst_id
                    pending_tools[iid] = {
                        "name": tname, "args": targs, "parent": tool_asst_id,
                    }
                    yield {
                        "type": "db", "level": "db", "op": "insert_interaction",
                        "role": "assistant", "id": tool_asst_id,
                    }
                    yield {
                        "type": "tool_call", "level": "agent", "tool": tname,
                        "args": targs, "tool_call_id": iid,
                    }
                    continue
                if rtype == "item.completed":
                    if itype == "agent_message":
                        text = _text(item.get("text") or item.get("content")).strip()
                        if text:
                            if held_text.strip():
                                async for ev in flush_held():
                                    yield ev
                            held_text = text
                        continue
                    if itype in ("command_execution", "mcp_tool_call"):
                        pinfo = pending_tools.pop(iid, None) or {"name": "tool", "args": {}, "parent": None}
                        if itype == "command_execution":
                            success = item.get("exit_code") in (None, 0)
                            result_text = str(item.get("aggregated_output") or "")
                            if not success:
                                result_text = (result_text + f"\n[exit code {item.get('exit_code')}]").strip()
                        else:
                            success = not bool(item.get("error"))
                            result_text = (json.dumps(item.get("result"), ensure_ascii=False)
                                           if item.get("result") is not None
                                           else json.dumps(item.get("error"), ensure_ascii=False)
                                           if item.get("error") else "(no result)")
                        await record_tool_result(pinfo["name"], pinfo["args"], result_text,
                                                 success, iid, pinfo["parent"])
                        yield {"type": "tool_result", "level": "agent", "tool": pinfo["name"],
                               "result": result_text[:4000], "duration_ms": 0,
                               "error": not success, "tool_call_id": iid}
                        continue
            continue
        rc, err = payload
        if captured_thread_id:
            try: await db.set_session_codex_id(session_id, str(captured_thread_id))
            except (AttributeError, TypeError): pass
            # Consume a one-shot reseed only once the fresh thread actually produced
            # an id — so a failed restart retries the recap next turn instead of
            # losing it.
            if reseed_ctx:
                try: await db.clear_session_codex_reseed(session_id)
                except Exception: pass
        if held_text.strip():
            async for ev in flush_held(as_final=True):
                yield ev
        else:
            message = (f"Codex couldn't complete the request:\n\n{err.strip()[:1500]}"
                       if err.strip() else f"Codex exited without a reply (code {rc}).")
            asst_id = await persist(message, message_phase="main")
            yield {"type": "db", "level": "db", "op": "insert_interaction", "role": "assistant", "id": asst_id}
            yield {"type": "response", "level": "agent", "message_phase": "main",
                   "content": message, "asst_id": asst_id}
        # Clean up MCP resources
        if mcp_proc is not None and mcp_proc.poll() is None:
            try: mcp_proc.terminate()
            except Exception: pass
        return

    finally:
        _hb.cancel()
        if mcp_proc is not None and mcp_proc.poll() is None:
            try: mcp_proc.terminate()
            except Exception: pass


async def compact_restart(
    db: Any, user_id: str, session_id: str, agent_id: str,
    info: Optional[Dict[str, Any]] = None,
) -> str:
    """Engine /compact override for a Local Codex agent (compact & restart).

    Mirror of the Claude engine's compact_restart: Context Control has just folded
    the older turns (``info`` is its result, or None when the chat was short enough
    that nothing needed folding). We build a compact recap of the WHOLE conversation
    so far (summary cars + verbatim tail) straight from WebAgent's own interactions
    table, arm it as a one-shot reseed, and forget the current Codex thread id — so
    the user's NEXT message starts a fresh, lighter `codex exec` thread seeded with
    the recap. Returns the user-facing chat reply. Failure-safe: any error returns a
    plain message and leaves the existing thread untouched."""
    try:
        from app.agent.session_history import build_openai_history_from_session, build_reseed_context
        # No agent_id on purpose: Context Control's write side already ran via the
        # forced compaction above; here we only want the passive read (cars + tail).
        msgs = await build_openai_history_from_session(db, user_id, session_id)
        seed = build_reseed_context(msgs)
        if not seed:
            return (
                "Nothing to carry over yet — this conversation is empty, so there's "
                "no context to compact. Send a message first, then run `/compact`."
            )
        await db.set_session_codex_reseed(session_id, seed)
    except Exception as e:
        logger.warning("codex compact_restart failed for %s: %s", session_id, e)
        return f"Couldn't prepare a fresh Codex session — {e}"
    folded = (info or {}).get("summarised_rows") or 0
    head = (
        f"✅ **Compacted — ready to restart.** Folded {folded} older message(s) into "
        "a summary and prepared a recap of this conversation."
        if folded else
        "✅ **Ready to restart with a clean session.** This conversation was short "
        "enough to keep in full, and a recap is prepared."
    )
    return (
        f"{head} Your **next message** starts a **fresh Codex thread** seeded with "
        "that recap — Codex's own runaway context is reset, while WebAgent keeps the "
        "full transcript (nothing is lost). It then resumes that lighter thread "
        "normally until you run `/compact` again."
    )
