"""Headless engine driver — run the LINKED checkout's real agent brain.

This file is shipped with the TUI but is **executed by the linked webAgent
checkout's own Python interpreter** (its ``.venv``), with the checkout as the
working directory, so ``import app...`` resolves to the checkout's code and its
installed dependencies. That is the whole point of "Option A": the terminal
becomes a *different front door* to the same app — it drives the app's real
``stream_agent_events`` loop directly, in the app's own environment, against the
app's own database — **without** standing up the FastAPI web server. So it keeps
working to install, run, diagnose and recover the server even when the server is
down.

It is **never imported** by the TUI process itself (the TUI's lean venv has none
of the app's heavy deps). The TUI copies/locates this file and launches it as a
subprocess; see ``tui_app/app_engine.py`` for the parent side.

Protocol — newline-delimited JSON over stdio (one JSON object per line):

  parent → driver (stdin):
    {"op": "turn", "request_id": "...", "session_id": "...",
     "message": "...", "execution_mode": "ask|plan|auto",
     "template_id": "server-manager"}
    {"op": "shutdown"}

  driver → parent (stdout):
    {"event": "ready"}                              once, after a clean boot
    {"event": "fatal", "message": "..."}            boot failed → driver exits 1
    {"event": "loop", "request_id": "...", "payload": {<raw loop event>}}
    {"event": "turn_done", "request_id": "..."}     a turn finished cleanly
    {"event": "turn_error", "request_id": "...", "message": "..."}

The driver boots ONCE (import app, apply provider config, open the DB), then
serves turns until it is told to shut down or its stdin closes. Any text the app
prints to its own stdout would corrupt the JSON stream, so the driver redirects
the app's stdout to stderr and keeps stdout exclusively for protocol lines.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid


def _emit(obj: dict) -> None:
    """Write one protocol line to the REAL stdout and flush immediately."""
    try:
        sys.__stdout__.write(json.dumps(obj, default=str) + "\n")
        sys.__stdout__.flush()
    except (OSError, ValueError):
        pass


async def _boot():
    """Import the checkout's app and bring up provider config + DB.

    Returns the live ``db`` handle. Raises on any failure — the caller turns
    that into a single ``fatal`` line so the TUI can fall back to its own brain.
    """
    # Keep the app's own prints/logs off the protocol channel: anything the app
    # writes to stdout goes to stderr instead. Protocol lines use __stdout__.
    sys.stdout = sys.stderr

    # This file ships in the TUI tree but runs under the checkout's interpreter
    # with the checkout as cwd. Python puts the SCRIPT's dir (tui_app/) on
    # sys.path[0] — and that dir contains the TUI's own ``app.py``, which would
    # SHADOW the checkout's ``app/`` package on ``import app``. Strip the script
    # dir from sys.path entirely, then make the checkout (cwd) the import root,
    # so ``import app`` can only ever resolve to the linked checkout.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [
        p for p in sys.path
        if os.path.abspath(p) != script_dir or p in ("", ".")
    ]
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    # The checkout root is our cwd (the TUI launches us there). Load its .env so
    # LLM_API_KEY/SUPABASE/etc. are present exactly as the server would see them.
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.getcwd(), ".env"))
    except Exception:
        pass  # .env is optional; provider.json may carry the key instead.

    from app.provider_boot import apply_provider_config
    apply_provider_config()

    from app.db import get_db
    return get_db()


async def _ensure_session(db, user_id: str, session_id: str) -> None:
    """Create the session row if it doesn't exist yet (backend-agnostic)."""
    raw = db.get_raw_client()
    try:
        existing = raw.table("sessions").select("id").eq("id", session_id).execute()
        if getattr(existing, "data", None):
            return
    except Exception:
        # If the existence check isn't supported on this backend, the insert
        # below is written to be idempotent-ish; a duplicate just errors and we
        # carry on (the row already exists).
        pass
    try:
        raw.table("sessions").insert(
            {"id": session_id, "user_id": user_id, "title": "Server Manager", "pinned": 1}
        ).execute()
    except Exception:
        pass  # Already exists / created concurrently — fine.


async def _run_turn(db, req: dict) -> None:
    """Run one user turn through the app's real loop and stream events out."""
    from app.agent.loop import stream_agent_events
    from app.agent.prompts import build_system_prompt, append_skills_section
    from app.agent.session_history import build_openai_history_from_session

    rid = req.get("request_id") or ""
    user_id = "admin"  # The TUI is tied to the admin user (by design).
    session_id = req.get("session_id") or str(uuid.uuid4())
    template_id = req.get("template_id") or "server-manager"
    message = req.get("message") or ""
    execution_mode = req.get("execution_mode") or "ask"

    # 1. Make sure the session exists, then materialize the Server Manager agent
    #    for the admin user and bind it to the session.
    await _ensure_session(db, user_id, session_id)
    agent = await db.get_or_resolve_session_agent(
        session_id=session_id, user_id=user_id, template_id=template_id
    )
    agent_id = agent["id"]

    # 2. Persist the user message exactly like the chat endpoint does (the loop
    #    does NOT record the incoming user_message itself), then exclude it from
    #    the history we build so it isn't duplicated.
    user_interaction_id = await db.insert_interaction(
        user_id, session_id, "user", message, channel="tui_manager"
    )

    # 3. Build the system prompt + prior history the same way the browser does.
    context_docs = agent.get("context_documents", []) or []
    system_prompt = await build_system_prompt(
        context_docs, brain_context=None, user_id=user_id, agent_id=agent_id
    )
    try:
        system_prompt = await append_skills_section(system_prompt, agent, session_id)
    except Exception:
        pass  # Skills are best-effort; never block a turn on them.

    history = await build_openai_history_from_session(
        db, user_id, session_id,
        exclude_interaction_ids={user_interaction_id} if user_interaction_id else set(),
        agent_id=agent_id,
    )

    # 4. Drive the real loop and forward every event verbatim to the parent.
    async for event in stream_agent_events(
        user_id=user_id,
        session_id=session_id,
        user_message=message,
        system_prompt=system_prompt,
        agent_id=agent_id,
        history=history,
        max_turns=int(agent.get("max_turn_count") or 0),
        channel="tui_manager",
        db=db,
        agent_template_id=template_id,
        allowed_tools=None,
        execution_mode=execution_mode,
    ):
        _emit({"event": "loop", "request_id": rid, "payload": event})


async def _stdin_lines():
    """Yield decoded stdin lines without blocking the event loop."""
    loop = asyncio.get_event_loop()
    # On Windows the ProactorEventLoop cannot connect_read_pipe a piped stdin: it
    # attaches a transport that then fails ASYNCHRONOUSLY with "[WinError 6] The
    # handle is invalid" inside the loop's read callback — which no try/except
    # around connect_read_pipe/readline can catch, so the driver would hang
    # forever after "ready" without ever seeing a turn. Read via a blocking
    # thread there; only POSIX uses the native non-blocking pipe transport.
    if sys.platform != "win32":
        reader = asyncio.StreamReader()
        try:
            await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
            )
        except (NotImplementedError, OSError):
            reader = None  # fall through to the thread reader below
        if reader is not None:
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                yield raw.decode("utf-8", errors="replace")
            return
    # Windows (or a POSIX platform without connect_read_pipe): block on
    # readline in a worker thread so the event loop stays responsive.
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            return
        yield line


async def _main() -> int:
    try:
        db = await _boot()
    except Exception as e:  # noqa: BLE001 — report any boot failure verbatim.
        import traceback
        _emit({"event": "fatal", "message": f"{e}", "traceback": traceback.format_exc()})
        return 1

    _emit({"event": "ready"})

    async for line in _stdin_lines():
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        op = req.get("op")
        if op == "shutdown":
            break
        if op != "turn":
            continue
        rid = req.get("request_id") or ""
        try:
            await _run_turn(db, req)
            _emit({"event": "turn_done", "request_id": rid})
        except Exception as e:  # noqa: BLE001 — one bad turn must not kill the driver.
            import traceback
            _emit({"event": "turn_error", "request_id": rid,
                   "message": f"{e}", "traceback": traceback.format_exc()})
            _emit({"event": "turn_done", "request_id": rid})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        sys.exit(0)
