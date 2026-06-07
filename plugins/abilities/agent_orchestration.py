"""Agent Orchestration ability — SELF-CONTAINED drop-in.

Everything this capability needs lives in this file (plus its sibling
``agent_orchestration.skill.md``): the FEATURE descriptor, the tool handlers, the
spawn/run/re-wake runtime, its own storage (a lazily-created ``agent_spawns``
table — no core schema edit, mirroring how the optimizer self-creates
``optimizer_runs``), and a background follow-up-timer poller. Delete this file
(and the .skill.md) and the whole capability is gone; nothing in core references
it by name.

It is discovered generically by core via three optional module hooks (see
app/abilities/__init__.py "Self-contained abilities"):
  • FEATURE                    — catalog + UI + which tool names it gates
  • build_tools(...)           — its tool handlers (app/tools/loader.py injects them)
  • start_background()/stop_background() — its poller (app/main.py runs it)

What it does: lets an agent act as an orchestrator — spawn purpose-built helper
agents in their own saved sessions, converse with them, run them blocking or
forked, and oversee forked work with durable follow-up timers. Also still gates
the original `delegate_to_agent` / `list_delegatable_agents` / `run_optimizer`
tools (those handlers remain in core — this file only declares their names).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


FEATURE = {
    "id": "agent_orchestration",
    "display_name": "Agent Orchestration",
    "category": "ability",
    "status": "beta",
    "summary": "spawn, converse with & oversee helper agents; delegate; run the optimizer.",
    "tools": [
        # Spawn-and-oversee (handlers ship in THIS file via build_tools).
        "spawn_agent", "message_spawn", "read_spawn", "quote_spawn", "list_spawns",
        "stop_spawn", "schedule_spawn_check",
        # Hand off the whole session to another agent (handler in core).
        "delegate_to_agent", "list_delegatable_agents",
        # Run the prompt optimizer (handler in core).
        "run_optimizer",
    ],
    "group": "core",
    "icon": "share-2",
    "color": "#7dcfff",
    "description": (
        "Lets the agent orchestrate other agents: spawn purpose-built helpers "
        "with a system prompt it writes on the spot (or by cloning an existing "
        "agent), hold a real back-and-forth with them (saved like any chat), and "
        "run them blocking or forked. Forked helpers report back when they "
        "finish, and the agent can set durable follow-up timers to check on "
        "them. Also supports handing the whole session to another agent "
        "(delegate) and running the prompt optimizer. Switch off to remove it "
        "platform-wide."
    ),
    "simple": True,
    # Bundled skill (body in agent_orchestration.skill.md, found by convention).
    "skill_mode": "selectable",
    "skill_handle": "agent_orchestration_guide_v1",
    "skill_summary": "How to spawn helper agents (write their prompt or clone "
                     "one), run them blocking or forked, hold a real "
                     "conversation with them, and oversee forked work with "
                     "durable follow-up timers. Load this before spawning or "
                     "delegating.",
}


# ════════════════════════════════════════════════════════════════════════════
# Storage — a self-created `agent_spawns` table (no core schema edit).
# One row per spawned helper: links the orchestrator session to the helper's own
# session, tracks lifecycle status, and folds in a durable follow-up timer
# (next_check_at + check_note). Portable DDL (no SQL-function defaults) so it
# works through both the SQLite and Postgres backends via db._get_conn().
# ════════════════════════════════════════════════════════════════════════════

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS agent_spawns (
    id                       TEXT PRIMARY KEY,
    user_id                  TEXT,
    orchestrator_session_id  TEXT,
    orchestrator_agent_id    TEXT,
    spawn_session_id         TEXT,
    spawn_agent_id           TEXT,
    name                     TEXT,
    task                     TEXT,
    status                   TEXT,
    result_summary           TEXT,
    next_check_at            TEXT,
    check_note               TEXT,
    created_at               TEXT,
    updated_at               TEXT
)
"""
_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_agent_spawns_orch ON agent_spawns(orchestrator_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_spawns_session ON agent_spawns(spawn_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_spawns_check ON agent_spawns(next_check_at)",
]

_UPDATABLE = {"name", "task", "status", "result_summary", "next_check_at", "check_note"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retry_write(fn, attempts: int = 5, delay: float = 0.25):
    """Retry a DB write on transient SQLite 'locked' errors (no-op elsewhere)."""
    import time
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if "locked" in str(e).lower() and i < attempts - 1:
                time.sleep(delay * (i + 1))
            else:
                raise


def _ensure_table(conn) -> None:
    conn.execute(_CREATE_SQL)
    for stmt in _INDEX_SQL:
        try:
            conn.execute(stmt)
        except Exception:  # noqa: BLE001 — index is best-effort
            pass


def _db_conn():
    from app.db import get_db
    db = get_db()
    raw = getattr(db, "_get_conn", None)
    return raw() if raw else None


def _spawns_create(**f) -> dict:
    row_id = str(uuid.uuid4())
    now = _now_iso()

    def _do():
        conn = _db_conn()
        if conn is None:
            raise RuntimeError("backend has no raw connection")
        try:
            _ensure_table(conn)
            conn.execute(
                """INSERT INTO agent_spawns
                   (id, user_id, orchestrator_session_id, orchestrator_agent_id,
                    spawn_session_id, spawn_agent_id, name, task, status,
                    result_summary, next_check_at, check_note, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row_id, f.get("user_id"), f.get("orchestrator_session_id"),
                 f.get("orchestrator_agent_id"), f.get("spawn_session_id"),
                 f.get("spawn_agent_id"), f.get("name") or "", f.get("task") or "",
                 f.get("status") or "pending", None, None, None, now, now),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM agent_spawns WHERE id = ?", (row_id,)).fetchone()
            return dict(row)
        finally:
            conn.close()

    return _retry_write(_do)


def _spawns_get(spawn_id: str) -> Optional[dict]:
    conn = _db_conn()
    if conn is None:
        return None
    try:
        _ensure_table(conn)
        row = conn.execute("SELECT * FROM agent_spawns WHERE id = ?", (spawn_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _spawns_list(orchestrator_session_id: str) -> list:
    conn = _db_conn()
    if conn is None:
        return []
    try:
        _ensure_table(conn)
        rows = conn.execute(
            "SELECT * FROM agent_spawns WHERE orchestrator_session_id = ? ORDER BY created_at ASC",
            (orchestrator_session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _spawns_update(spawn_id: str, **fields) -> None:
    sets, params = [], []
    for k, v in fields.items():
        if k in _UPDATABLE:
            sets.append(f"{k} = ?")
            params.append(v)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now_iso())
    params.append(spawn_id)

    def _do():
        conn = _db_conn()
        if conn is None:
            return
        try:
            _ensure_table(conn)
            conn.execute(f"UPDATE agent_spawns SET {', '.join(sets)} WHERE id = ?", params)
            conn.commit()
        finally:
            conn.close()

    _retry_write(_do)


def _spawns_claim_due(now_iso: Optional[str] = None) -> list:
    """Rows whose follow-up timer elapsed; clear the timer so each fires once."""
    ts = now_iso or _now_iso()

    def _do():
        conn = _db_conn()
        if conn is None:
            return []
        try:
            _ensure_table(conn)
            rows = conn.execute(
                """SELECT * FROM agent_spawns
                   WHERE next_check_at IS NOT NULL AND next_check_at <= ?
                   ORDER BY next_check_at ASC""",
                (ts,),
            ).fetchall()
            claimed = [dict(r) for r in rows]
            for r in claimed:
                conn.execute(
                    "UPDATE agent_spawns SET next_check_at = NULL, updated_at = ? WHERE id = ?",
                    (ts, r["id"]),
                )
            conn.commit()
            return claimed
        finally:
            conn.close()

    return _retry_write(_do)


# ════════════════════════════════════════════════════════════════════════════
# Runtime — create helper agents, run them (wait/fork), re-wake the orchestrator.
# A helper is "run" by POSTing to the local /api/v1/chat for its session, exactly
# like the optimizer's _kickstart_planner. Because the helper's session is bound
# to its own agent, chat.py routes the message to that agent. The loopback POST
# must carry a JWT (see _internal_auth_headers) or chat.py's assert_caller_is
# rejects it 401 -> 500.
# ════════════════════════════════════════════════════════════════════════════

_SPAWN_PREAMBLE = (
    "You are a purpose-spawned helper agent created by an orchestrator agent to "
    "carry out a specific task. The 'user' in this conversation is the "
    "orchestrator, not a human. Work the task to completion, use your tools as "
    "needed, and report results clearly and concisely so the orchestrator can "
    "act on them. If you need a decision or more detail, ask in your reply.\n\n"
    "## Your task / directive\n"
)


def _chat_url() -> str:
    return f"http://127.0.0.1:{os.environ.get('PORT', '8080')}/api/v1/chat"


def _internal_auth_headers(user_id: str) -> dict:
    """Mint a short-lived JWT so this loopback POST passes the chat endpoint's
    caller-identity check (`assert_caller_is`). Without it the request carries no
    token and is rejected as 401 "Not authenticated", which the chat handler then
    surfaces as a 500 — silently killing every spawn/re-wake. We act strictly on
    behalf of the session's own user, so minting that user's own token here is
    legitimate. Applies to BOTH agent types we drive: spawned worker sessions
    (via _run_spawn_turn) and the orchestrator's own session (via
    _rewake_orchestrator)."""
    try:
        from app.auth.jwt import create_access_token
        token = create_access_token(username=user_id, user_id=user_id)
        return {"Authorization": f"Bearer {token}"}
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not mint internal auth token for %s: %s", user_id, e)
        return {}


async def _post_chat(user_id: str, session_id: str, message: str, timeout: float) -> str:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            _chat_url(),
            json={"message": message, "user_id": user_id, "session_id": session_id},
            headers=_internal_auth_headers(user_id),
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return ""
        return (data.get("reply") or data.get("response") or "") if isinstance(data, dict) else ""


def _iso_minutes_from_now(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=max(0.0, minutes))).isoformat()


async def _create_spawn(*, user_id, orchestrator_session_id, orchestrator_agent_id,
                        task, name="", system_prompt="", from_agent="") -> dict:
    """Materialize a helper agent + its own tagged session + an agent_spawns row."""
    from app.db import get_db
    db = get_db()

    template_id = (from_agent or "").strip() or "default"
    spawn_name = (name or "").strip() or (task[:40].strip() if task else "Helper")

    agent = await db.create_custom_agent(
        user_id=user_id, name=spawn_name,
        description=f"Spawned helper for orchestrator session {orchestrator_session_id[:12]}",
        template_id=template_id,
    )
    spawn_agent_id = agent["id"]

    if (system_prompt or "").strip():
        try:
            await db.upsert_slot(
                agent_id=spawn_agent_id, slot_name="orchestrator_directive",
                order_index=0, lock=True, merge_mode="replace",
                content=_SPAWN_PREAMBLE + system_prompt.strip(), updated_by="orchestrator",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not set spawn directive prompt: %s", e)

    spawn_session_id = f"spawn-{uuid.uuid4().hex[:10]}"
    meta = json.dumps({
        "source": "spawn",
        "orchestrator_session_id": orchestrator_session_id,
        "orchestrator_agent_id": orchestrator_agent_id,
        "spawn_name": spawn_name,
    })
    conn = _db_conn()
    if conn is not None:
        try:
            conn.execute(
                "INSERT INTO sessions (id, user_id, title, agent_id, metadata) VALUES (?, ?, ?, ?, ?)",
                (spawn_session_id, user_id, spawn_name[:60], spawn_agent_id, meta),
            )
            conn.commit()
        finally:
            conn.close()
    try:
        await db.bind_session_to_agent(spawn_session_id, spawn_agent_id)
    except Exception:  # noqa: BLE001
        pass

    return _spawns_create(
        user_id=user_id, orchestrator_session_id=orchestrator_session_id,
        orchestrator_agent_id=orchestrator_agent_id, spawn_session_id=spawn_session_id,
        spawn_agent_id=spawn_agent_id, name=spawn_name, task=task or "", status="pending",
    )


async def _rewake_orchestrator(user_id: str, orchestrator_session_id: str, note: str) -> None:
    """Re-engage the orchestrator with a system-style event note (fire-and-forget)."""
    try:
        await _post_chat(user_id, orchestrator_session_id, note, timeout=300.0)
    except Exception as e:  # noqa: BLE001
        logger.warning("Re-wake of orchestrator %s failed (non-fatal): %s",
                       orchestrator_session_id, e)


async def _run_spawn_turn(*, user_id, spawn_id, spawn_session_id, message, wait,
                         notify_on_done=True, wait_timeout=300.0) -> dict:
    """WAIT: await the turn, return the reply. FORK: return immediately; a
    background task settles the spawn and (if notify_on_done) re-wakes the
    orchestrator with the result."""
    _spawns_update(spawn_id, status="running")

    if wait:
        try:
            reply = await _post_chat(user_id, spawn_session_id, message, wait_timeout)
            _spawns_update(spawn_id, status="done", result_summary=(reply or "")[:2000])
            return {"status": "done", "reply": reply or ""}
        except Exception as e:  # noqa: BLE001
            logger.warning("Spawn %s wait-run failed: %s", spawn_id, e)
            _spawns_update(spawn_id, status="error", result_summary=str(e)[:2000])
            return {"status": "error", "error": str(e)}

    spawn = _spawns_get(spawn_id) or {}
    orch_session = spawn.get("orchestrator_session_id")
    spawn_name = spawn.get("name") or spawn_id

    async def _bg():
        try:
            reply = await _post_chat(user_id, spawn_session_id, message, wait_timeout)
            _spawns_update(spawn_id, status="done", result_summary=(reply or "")[:2000])
            if notify_on_done and orch_session:
                await _rewake_orchestrator(user_id, orch_session, (
                    f"[ORCHESTRATION EVENT] Spawn '{spawn_name}' (id {spawn_id}) finished.\n\n"
                    f"Result:\n{(reply or '').strip()[:2000]}\n\n"
                    "Decide what to do next: reply with message_spawn, read its full "
                    "transcript with read_spawn, spawn more helpers, or report back."
                ))
        except Exception as e:  # noqa: BLE001
            logger.warning("Spawn %s fork-run failed: %s", spawn_id, e)
            _spawns_update(spawn_id, status="error", result_summary=str(e)[:2000])
            if notify_on_done and orch_session:
                await _rewake_orchestrator(
                    user_id, orch_session,
                    f"[ORCHESTRATION EVENT] Spawn '{spawn_name}' (id {spawn_id}) errored: {e}",
                )

    asyncio.create_task(_bg())
    return {"status": "running"}


# ════════════════════════════════════════════════════════════════════════════
# Background service — durable follow-up-timer poller (discovered by app/main.py).
# Sibling in spirit to the automation scheduler: wakes every POLL_INTERVAL, claims
# spawns whose next_check_at elapsed, and re-wakes their orchestrator. DB-backed
# so timers survive a restart.
# ════════════════════════════════════════════════════════════════════════════

_POLL_INTERVAL = 20
_poll_task: Optional[asyncio.Task] = None
_poll_running = False


async def _poll_tick() -> None:
    now = _now_iso()
    try:
        due = _spawns_claim_due(now_iso=now)
    except Exception as e:  # noqa: BLE001
        logger.debug("spawn check claim failed (table not ready?): %s", e)
        return
    for spawn in due:
        try:
            name = spawn.get("name") or spawn.get("id")
            status = spawn.get("status") or "unknown"
            summary = (spawn.get("result_summary") or "").strip()[:1500]
            check_note = (spawn.get("check_note") or "").strip()
            note = (
                f"[ORCHESTRATION EVENT] Follow-up timer for spawn '{name}' "
                f"(id {spawn.get('id')}).\nCurrent status: {status}.\n"
            )
            if summary:
                note += f"Latest result/summary:\n{summary}\n"
            if check_note:
                note += f"\nYour reminder: {check_note}\n"
            note += ("\nDecide what to do: read_spawn for the full transcript, "
                     "message_spawn to nudge or redirect it, schedule_spawn_check to set "
                     "another timer, stop_spawn to halt it, or report back.")
            await _rewake_orchestrator(spawn.get("user_id"),
                                       spawn.get("orchestrator_session_id"), note)
        except Exception as e:  # noqa: BLE001
            logger.warning("Follow-up re-wake failed for spawn %s: %s", spawn.get("id"), e)


async def _poll_loop() -> None:
    await asyncio.sleep(3)
    while _poll_running:
        try:
            await _poll_tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("Orchestration poller tick failed: %s", e)
        try:
            await asyncio.sleep(_POLL_INTERVAL)
        except asyncio.CancelledError:
            raise


async def start_background() -> None:
    """Discovered + called by app/main.py at startup."""
    global _poll_task, _poll_running
    if _poll_task and not _poll_task.done():
        return
    _poll_running = True
    _poll_task = asyncio.create_task(_poll_loop(), name="orchestration_followup_loop")
    logger.info("Orchestration follow-up poller started (every %ss)", _POLL_INTERVAL)


async def stop_background() -> None:
    global _poll_running
    _poll_running = False
    if _poll_task:
        _poll_task.cancel()
        try:
            await _poll_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    logger.info("Orchestration follow-up poller stopped")


# ════════════════════════════════════════════════════════════════════════════
# Tools — build_tools() is discovered + called by app/tools/loader.py for every
# agent that has this ability enabled. Returns the spawn-and-oversee handlers.
# ════════════════════════════════════════════════════════════════════════════

_PIPELINE_TEMPLATES = ("opt_planner", "opt_closer")
_MAX_TRANSCRIPT = 40


def _err(msg: str, **extra) -> str:
    out = {"status": "error", "error": msg}
    out.update(extra)
    return json.dumps(out)


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, **_ctx):
    """Return {tool_name: handler} for the spawn-and-oversee tools.

    Recursion guard (the ability owns its own gating): returns {} inside a
    helper's own session (id prefix `spawn-`) so a helper can't spawn more
    helpers and cascade, and {} for optimizer pipeline sub-agents.
    """
    if not session_id or session_id.startswith("spawn-"):
        return {}
    if agent_template_id in _PIPELINE_TEMPLATES:
        return {}

    async def _owned_spawn(spawn_id: str) -> Optional[dict]:
        spawn = _spawns_get(spawn_id)
        if not spawn or spawn.get("orchestrator_session_id") != session_id:
            return None
        return spawn

    async def spawn_agent(task: str, name: str = "", system_prompt: str = "",
                          from_agent: str = "", wait: bool = False,
                          check_back_minutes: float = 0) -> str:
        """Create a brand-new helper agent and set it to work on a task.

        Give it an identity by writing ``system_prompt`` yourself, or cloning an
        existing agent via ``from_agent`` (a template id from
        list_delegatable_agents). The helper gets its OWN saved session (visible
        in the sidebar) and the whole exchange is recorded like any chat.
        wait=True blocks and returns the reply; wait=False forks it and you're
        re-woken with the result when it finishes. check_back_minutes>0 also sets
        a follow-up timer. Returns the spawn_id.
        """
        if not (task or "").strip() and not (system_prompt or "").strip():
            return _err("Provide a task (and/or a system_prompt) for the spawn.")
        try:
            spawn = await _create_spawn(
                user_id=user_id, orchestrator_session_id=session_id,
                orchestrator_agent_id=agent_id or None, task=task or "",
                name=name or "", system_prompt=system_prompt or "", from_agent=from_agent or "",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("spawn_agent create failed: %s", e)
            return _err(f"Could not create spawn: {e}")

        spawn_id = spawn["id"]
        if check_back_minutes and float(check_back_minutes) > 0:
            try:
                _spawns_update(spawn_id, next_check_at=_iso_minutes_from_now(float(check_back_minutes)),
                               check_note=f"Scheduled check-in on spawn '{spawn['name']}'.")
            except Exception:  # noqa: BLE001
                pass

        first_message = (task or system_prompt or "Begin your task.").strip()
        result = await _run_spawn_turn(user_id=user_id, spawn_id=spawn_id,
                                       spawn_session_id=spawn["spawn_session_id"],
                                       message=first_message, wait=bool(wait))
        out = {"status": "ok", "spawn_id": spawn_id, "name": spawn["name"],
               "spawn_session_id": spawn["spawn_session_id"],
               "mode": "wait" if wait else "fork", "run_status": result.get("status")}
        if wait:
            out["reply"] = result.get("reply", "")
            if result.get("status") == "error":
                out["error"] = result.get("error")
        else:
            out["note"] = ("Spawn is running on its own. You'll be re-woken with its result "
                           "when it finishes. Check anytime with read_spawn / list_spawns.")
        return json.dumps(out)

    async def message_spawn(spawn_id: str, message: str, wait: bool = False) -> str:
        """Send a follow-up message into a spawn's session to continue the
        conversation. wait=True blocks for the reply; wait=False forks."""
        spawn = await _owned_spawn(spawn_id)
        if not spawn:
            return _err(f"No spawn '{spawn_id}' owned by this session.")
        if not (message or "").strip():
            return _err("message is required.")
        result = await _run_spawn_turn(user_id=user_id, spawn_id=spawn_id,
                                       spawn_session_id=spawn["spawn_session_id"],
                                       message=message, wait=bool(wait))
        out = {"status": "ok", "spawn_id": spawn_id, "mode": "wait" if wait else "fork",
               "run_status": result.get("status")}
        if wait:
            out["reply"] = result.get("reply", "")
            if result.get("status") == "error":
                out["error"] = result.get("error")
        return json.dumps(out)

    async def read_spawn(spawn_id: str, limit: int = 20) -> str:
        """Read a spawn's transcript so far (most recent messages)."""
        spawn = await _owned_spawn(spawn_id)
        if not spawn:
            return _err(f"No spawn '{spawn_id}' owned by this session.")
        from app.db import get_db
        db = get_db()
        try:
            records = await db.fetch_interactions(user_id, spawn["spawn_session_id"])
        except Exception as e:  # noqa: BLE001
            return _err(f"Could not read spawn transcript: {e}")
        try:
            limit = max(1, min(int(limit), _MAX_TRANSCRIPT))
        except (TypeError, ValueError):
            limit = 20
        transcript = []
        for r in records[-limit:]:
            entry = {"role": getattr(r, "role", "") or "",
                     "content": (getattr(r, "content", "") or "")[:1500]}
            tn = getattr(r, "tool_name", None)
            if tn:
                entry["tool"] = tn
            transcript.append(entry)
        return json.dumps({"status": "ok", "spawn_id": spawn_id, "name": spawn.get("name"),
                           "spawn_status": spawn.get("status"), "transcript": transcript})

    async def quote_spawn(spawn_id: str, max_chars: int = 8000) -> str:
        """Return the helper's ACTUAL final answer, verbatim and untouched.

        Use this — never your memory — to report what a spawn produced. It pulls
        the helper's last real reply straight from its saved session and returns
        it word-for-word, plus a content fingerprint and the session id so the
        result is verifiable and can't be silently embellished. If the spawn
        produced nothing (stalled / interrupted / stopped), it says so explicitly
        so you don't invent a result.
        """
        spawn = await _owned_spawn(spawn_id)
        if not spawn:
            return _err(f"No spawn '{spawn_id}' owned by this session.")
        from app.db import get_db
        db = get_db()
        try:
            records = await db.fetch_interactions(user_id, spawn["spawn_session_id"])
        except Exception as e:  # noqa: BLE001
            return _err(f"Could not read spawn output: {e}")

        answers = [(getattr(r, "content", "") or "") for r in records
                   if getattr(r, "role", "") == "assistant"
                   and (getattr(r, "content", "") or "").strip()]
        spawn_status = spawn.get("status")
        if not answers:
            return json.dumps({
                "status": "ok", "spawn_id": spawn_id, "name": spawn.get("name"),
                "spawn_status": spawn_status, "produced_output": False,
                "spawn_session_id": spawn["spawn_session_id"], "final_answer": "",
                "guidance": ("This helper produced NO answer (it likely stalled, was "
                             "interrupted, or was stopped). Do NOT invent, summarize, "
                             "or guess a result for it. Re-run it (spawn again or "
                             "message_spawn) or report this spawn to the human as "
                             "FAILED / no output."),
            })

        final = answers[-1]
        try:
            cap = max(500, min(int(max_chars), 16000))
        except (TypeError, ValueError):
            cap = 8000
        full_len = len(final)
        body = final if full_len <= cap else (
            final[:cap] + f"\n\n[TRUNCATED — {full_len - cap} more characters. "
            "Open spawn_session_id to read the rest verbatim.]")
        fp = hashlib.sha256(final.encode("utf-8")).hexdigest()[:12]
        return json.dumps({
            "status": "ok", "spawn_id": spawn_id, "name": spawn.get("name"),
            "spawn_status": spawn_status, "produced_output": True,
            "spawn_session_id": spawn["spawn_session_id"], "answer_count": len(answers),
            "final_answer": body, "char_count": full_len, "fingerprint": f"sha256:{fp}",
            "guidance": ("This is the helper's ACTUAL words. When you report its result "
                         "to the human, quote from THIS text verbatim and attribute it "
                         "to the helper — do not paraphrase its numbers, counts, names, "
                         "severities, or claims from memory. The human can open "
                         "spawn_session_id to verify what you quote."),
        })

    async def list_spawns() -> str:
        """List the helper agents you've spawned in this session, with status,
        last result summary, and any pending follow-up timer."""
        try:
            rows = _spawns_list(session_id)
        except Exception as e:  # noqa: BLE001
            return _err(f"Could not list spawns: {e}")
        spawns = [{"spawn_id": r.get("id"), "name": r.get("name"), "status": r.get("status"),
                   "task": (r.get("task") or "")[:200],
                   "result_summary": (r.get("result_summary") or "")[:400],
                   "next_check_at": r.get("next_check_at")} for r in rows]
        return json.dumps({"status": "ok", "count": len(spawns), "spawns": spawns})

    async def stop_spawn(spawn_id: str) -> str:
        """Interrupt a running spawn and mark it stopped (transcript is kept)."""
        spawn = await _owned_spawn(spawn_id)
        if not spawn:
            return _err(f"No spawn '{spawn_id}' owned by this session.")
        from app.db import get_db
        db = get_db()
        try:
            await db.set_interrupt(spawn["spawn_session_id"])
        except Exception as e:  # noqa: BLE001
            logger.debug("set_interrupt on spawn failed: %s", e)
        _spawns_update(spawn_id, status="stopped")
        return json.dumps({"status": "ok", "spawn_id": spawn_id, "spawn_status": "stopped"})

    async def schedule_spawn_check(spawn_id: str, minutes: float, note: str = "") -> str:
        """Set a durable follow-up timer on a spawn. After ``minutes`` you're
        re-woken to check on it — even if still running — with your ``note``.
        Survives a server restart; fires once; replaces any pending timer."""
        spawn = await _owned_spawn(spawn_id)
        if not spawn:
            return _err(f"No spawn '{spawn_id}' owned by this session.")
        try:
            mins = float(minutes)
            if mins <= 0:
                return _err("minutes must be greater than 0.")
        except (TypeError, ValueError):
            return _err("minutes must be a number.")
        _spawns_update(spawn_id, next_check_at=_iso_minutes_from_now(mins),
                       check_note=(note or "").strip() or f"Scheduled check-in on spawn '{spawn['name']}'.")
        return json.dumps({"status": "ok", "spawn_id": spawn_id, "check_in_minutes": mins,
                           "note": "You'll be re-woken to check on this spawn when the timer elapses."})

    return {
        "spawn_agent": spawn_agent,
        "message_spawn": message_spawn,
        "read_spawn": read_spawn,
        "quote_spawn": quote_spawn,
        "list_spawns": list_spawns,
        "stop_spawn": stop_spawn,
        "schedule_spawn_check": schedule_spawn_check,
    }


DESTRUCTIVE = {"spawn_agent", "message_spawn", "stop_spawn"}

TOOL_SCHEMAS = {
    "spawn_agent": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "What the helper should do."},
            "name": {"type": "string", "description": "Optional short label for the helper/session."},
            "system_prompt": {"type": "string", "description": "Optional: the helper's full directive/identity, written by you. Leave blank to use a default general agent or clone via from_agent."},
            "from_agent": {"type": "string", "description": "Optional: template id of an existing agent to clone (see list_delegatable_agents). Ignored if system_prompt is given."},
            "wait": {"type": "boolean", "description": "True = block and return the helper's reply now. False (default) = fork and be re-woken when it finishes."},
            "check_back_minutes": {"type": "number", "description": "Optional: also set a follow-up timer (minutes) to re-check the helper even if still running."},
        },
        "required": ["task"],
    },
    "message_spawn": {
        "type": "object",
        "properties": {
            "spawn_id": {"type": "string", "description": "The spawn to message (from spawn_agent / list_spawns)."},
            "message": {"type": "string", "description": "The message to send into the spawn's session."},
            "wait": {"type": "boolean", "description": "True = block for the reply. False (default) = fork and be re-woken."},
        },
        "required": ["spawn_id", "message"],
    },
    "read_spawn": {
        "type": "object",
        "properties": {
            "spawn_id": {"type": "string", "description": "The spawn whose transcript to read."},
            "limit": {"type": "integer", "description": "How many recent messages to return (default 20, max 40)."},
        },
        "required": ["spawn_id"],
    },
    "quote_spawn": {
        "type": "object",
        "properties": {
            "spawn_id": {"type": "string", "description": "The spawn whose actual final answer you want, verbatim."},
            "max_chars": {"type": "integer", "description": "Max characters of the answer to return (default 8000, max 16000; longer answers are marked truncated)."},
        },
        "required": ["spawn_id"],
    },
    "list_spawns": {"type": "object", "properties": {}, "required": []},
    "stop_spawn": {
        "type": "object",
        "properties": {"spawn_id": {"type": "string", "description": "The spawn to interrupt and mark stopped."}},
        "required": ["spawn_id"],
    },
    "schedule_spawn_check": {
        "type": "object",
        "properties": {
            "spawn_id": {"type": "string", "description": "The spawn to set a follow-up timer on."},
            "minutes": {"type": "number", "description": "Minutes from now to be re-woken to check on it."},
            "note": {"type": "string", "description": "Optional reminder to yourself, delivered when the timer fires."},
        },
        "required": ["spawn_id", "minutes"],
    },
}
