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
        "stop_spawn", "schedule_spawn_check", "list_abilities", "promote_spawn",
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
    # ALWAYS-ON: the orchestration guidance is injected every turn so the agent
    # always knows it can (and should) use helpers — no load_skill step required.
    "skill_mode": "always_on",
    "skill_handle": "agent_orchestration_guide_v1",
    "skill_summary": "How to spawn helper agents (write their prompt or clone "
                     "one), run them blocking or forked, hold a real "
                     "conversation with them, and oversee forked work with "
                     "durable follow-up timers.",
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
    resume_attempts          INTEGER,
    heartbeat_at             TEXT,
    claim_token              TEXT,
    created_at               TEXT,
    updated_at               TEXT
)
"""
_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_agent_spawns_orch ON agent_spawns(orchestrator_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_spawns_session ON agent_spawns(spawn_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_spawns_check ON agent_spawns(next_check_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_spawns_hb ON agent_spawns(status, heartbeat_at)",
]

_UPDATABLE = {"name", "task", "status", "result_summary", "next_check_at",
              "check_note", "resume_attempts", "heartbeat_at"}


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
    # Best-effort add of columns introduced after a table's first creation
    # (older installs created the table without them). ALTER fails if it already
    # exists — that's fine.
    for col, decl in (("resume_attempts", "INTEGER"),
                      ("heartbeat_at", "TEXT"), ("claim_token", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE agent_spawns ADD COLUMN {col} {decl}")
        except Exception:  # noqa: BLE001
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


def _spawns_heartbeat(spawn_id: str, now_iso: Optional[str] = None) -> None:
    """Stamp a fresh ``heartbeat_at`` to prove this spawn's runner is alive. Called
    repeatedly by the runner while a spawn is queued/running. Cross-instance, this
    is the ONLY reliable liveness signal: a stale heartbeat means no live runner on
    any instance, so the row is safe to recover. Best-effort + quiet (a missed beat
    just shortens the liveness margin; the cutoff is many beats wide)."""
    ts = now_iso or _now_iso()

    def _do():
        conn = _db_conn()
        if conn is None:
            return
        try:
            conn.execute("UPDATE agent_spawns SET heartbeat_at = ? WHERE id = ?", (ts, spawn_id))
            conn.commit()
        finally:
            conn.close()

    try:
        _retry_write(_do)
    except Exception:  # noqa: BLE001 — a missed heartbeat is non-fatal
        pass


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


def _spawns_fail_orphans(now_iso: str, older_than_seconds: int, limit: int,
                         exclude_ids=()) -> list:
    """Fail — never re-run — forked spawns whose runner has died (e.g. a server
    restart/crash left them stuck 'running'/'queued'). A live runner proves itself
    by refreshing ``heartbeat_at`` every ``_HEARTBEAT_INTERVAL``s, so a row whose
    heartbeat is older than ``older_than_seconds`` has NO live runner on ANY
    instance and will never settle on its own.

    We deliberately flip such orphans to 'error' rather than re-executing their
    task: a re-run re-does everything the spawn already did, INCLUDING re-spawning
    the sub-helpers it created, so auto-re-running a self-orchestrating tree
    snowballs one restart into an exploding fan-out. The caller re-wakes the
    orchestrator instead, which can deliberately re-spawn anything it still needs.

    MULTI-INSTANCE SAFE: the flip is an atomic guarded UPDATE stamping a unique
    ``claim_token`` (``heartbeat_at <= cutoff`` re-checked at write time, and the
    status→'error' transition is itself the guard). Whoever's UPDATE lands first
    wins the row; every other instance's WHERE then misses. The token tells us
    which rows WE failed, so only we notify their orchestrator. ``exclude_ids`` is
    an extra in-process fast-skip for spawns with a live runner here; correctness
    rests on heartbeat + token. ``limit`` caps how many we fail per tick so a
    post-restart sweep doesn't fire a notification storm all at once."""
    cutoff = (datetime.fromisoformat(now_iso) - timedelta(seconds=older_than_seconds)).isoformat()
    excl = set(exclude_ids or ())
    token = uuid.uuid4().hex
    msg = ("This spawn was interrupted (most likely a server restart or crash) and "
           "did not finish. It was NOT automatically re-run, to avoid duplicating "
           "work it had already started. Re-spawn it if you still need the result; "
           "do not invent its output.")

    def _do():
        conn = _db_conn()
        if conn is None:
            return []
        try:
            _ensure_table(conn)
            # 1. Read a bounded batch of dead-runner candidates (cheap read).
            rows = conn.execute(
                """SELECT id FROM agent_spawns
                   WHERE status IN ('running', 'queued')
                     AND COALESCE(heartbeat_at, updated_at) <= ?
                   ORDER BY COALESCE(heartbeat_at, updated_at) ASC""",
                (cutoff,),
            ).fetchall()
            ids = [r["id"] for r in rows if r["id"] not in excl][:max(0, int(limit))]
            if not ids:
                return []
            # 2. Atomic fail-out: only rows STILL stale at write time get our token.
            ph = ",".join(["?"] * len(ids))
            conn.execute(
                f"""UPDATE agent_spawns
                    SET status = 'error', claim_token = ?, result_summary = ?, updated_at = ?
                    WHERE id IN ({ph})
                      AND status IN ('running', 'queued')
                      AND COALESCE(heartbeat_at, updated_at) <= ?""",
                (token, msg, now_iso, *ids, cutoff),
            )
            conn.commit()
            # 3. Fetch only what WE failed (our token).
            failed = conn.execute(
                "SELECT * FROM agent_spawns WHERE claim_token = ? AND status = 'error'", (token,)
            ).fetchall()
            return [dict(r) for r in failed]
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

# Tools that are inherently destructive regardless of an agent's own policy —
# mirror of app/agent/loop.py DESTRUCTIVE_TOOLS. Imported when available so the
# ceiling tracks core; the literal is the fallback if the import path changes.
try:  # pragma: no cover - import-shape guard
    from app.agent.loop import DESTRUCTIVE_TOOLS as _CORE_DESTRUCTIVE
    _HARDCODED_DESTRUCTIVE = set(_CORE_DESTRUCTIVE)
except Exception:  # noqa: BLE001
    _HARDCODED_DESTRUCTIVE = {"run_command", "restart_server"}


def _parse_json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
            return v if isinstance(v, list) else []
        except Exception:  # noqa: BLE001
            return []
    return []


async def _compute_clone_grants(master_agent_id: str, requested_abilities, allow_destructive):
    """Compute what a clone is allowed, clamped so it can NEVER be looser than its
    master — the ceiling. Returns (granted_abilities, deny_tools, ask_tools).

    Two axes:
      • Ability axis — a clone only gets abilities the master itself has enabled;
        the requested list is intersected with the master's enabled abilities.
        Recursion is therefore opt-in: a clone gets the spawn tools only if the
        orchestrator explicitly lists 'agent_orchestration' (and has it itself).
      • Per-tool permission axis — for every tool the clone might run, its level is
        at most the master's level on the deny < ask < auto scale. A tool the
        master must CONFIRM (ask) can never be auto for the clone: it is blocked
        outright unless the orchestrator explicitly opts it in via allow_destructive
        (and even then it stays ask, never auto). A tool the master has blocked
        (deny) is blocked for the clone too. This is also the unattended-destructive
        policy: a clone runs with no human to confirm, so confirm-tools are off by
        default.
    """
    from app.db import get_db
    db = get_db()
    requested = {a.strip() for a in (requested_abilities or []) if isinstance(a, str) and a.strip()}
    opted_in = {t.strip() for t in (allow_destructive or []) if isinstance(t, str) and t.strip()}

    master = await db.get_agent_by_id(master_agent_id) or {}
    try:
        conns = await db.get_agent_connections(master_agent_id)
    except Exception:  # noqa: BLE001
        conns = []
    master_abilities = {
        c.get("connection_type") for c in conns
        if c.get("section") == "ability" and c.get("enabled")
    }
    granted = sorted(requested & master_abilities)

    master_deny = set(_parse_json_list(master.get("allowed_tools")))
    sp = master.get("safety_policy")
    if isinstance(sp, str):
        try:
            sp = json.loads(sp or "{}")
        except Exception:  # noqa: BLE001
            sp = {}
    master_ask = set(_parse_json_list((sp or {}).get("destructive_tools"))) | set(_HARDCODED_DESTRUCTIVE)

    clone_deny = sorted(master_deny | (master_ask - opted_in))
    clone_ask = sorted(master_ask & opted_in)
    return granted, clone_deny, clone_ask


async def _borrowed_prompt(from_agent: str) -> str:
    """Best-effort: assemble another agent/template's prompt text so the
    orchestrator can SEE and reuse it inside the clone's directive. Returns '' on
    any failure (the borrow is optional, never fatal)."""
    src = (from_agent or "").strip()
    if not src:
        return ""
    from app.db import get_db
    db = get_db()
    try:
        slots = await db.resolve_prompts(src, user_id=None)
    except Exception:  # noqa: BLE001
        return ""
    parts = []
    for s in slots or []:
        if s.get("slot_name") == "__skills__":
            continue
        c = (s.get("content") or "").strip()
        if c:
            parts.append(c)
    return "\n\n".join(parts).strip()


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


def _age_seconds(now_dt: datetime, iso: Optional[str]) -> Optional[int]:
    """Whole seconds between an ISO timestamp and now (None if unparseable)."""
    if not iso:
        return None
    try:
        return max(0, int((now_dt - datetime.fromisoformat(iso)).total_seconds()))
    except Exception:  # noqa: BLE001
        return None


def _spawn_health(status: Optional[str], since_heartbeat: Optional[int]) -> tuple:
    """Map (status, seconds-since-last-heartbeat) → (stalled?, human health line).

    `stalled` is True when a spawn that should have a live runner ('running' or
    'queued') has gone silent past the orphan window — i.e. no heartbeat from ANY
    instance — so it is almost certainly interrupted/dead, not progressing. The
    background recovery will re-run or fail it; meanwhile the orchestrator should
    treat it as NOT a result."""
    st = status or "unknown"
    stale = st in ("running", "queued") and since_heartbeat is not None and \
        since_heartbeat >= _ORPHAN_RUNNING_SECONDS
    if st == "done":
        return False, "done — finished; call quote_spawn for its actual result"
    if st == "error":
        return False, "failed — did not complete; there is no result to report"
    if st == "stopped":
        return False, "stopped — you halted it; transcript kept, no completed result"
    if st == "pending":
        return False, "pending — created but not started yet"
    if st == "queued":
        if stale:
            return True, (f"stalled — queued but no heartbeat for {since_heartbeat}s; its runner "
                          "is likely dead. Being auto-recovered/failed. NOT a result.")
        return False, "queued — waiting for a run slot (normal under load); not a result yet"
    if st == "running":
        if stale:
            return True, (f"stalled — 'running' but silent for {since_heartbeat}s; treat as "
                          "interrupted/failed, NOT progress. Being auto-recovered/failed.")
        return False, "running — actively working; not finished, not a result yet"
    return False, st


async def _create_spawn(*, user_id, orchestrator_session_id, orchestrator_agent_id,
                        task, name="", system_prompt="", from_agent="",
                        abilities=None, allow_destructive=None, output_contract="") -> dict:
    """Materialize a CLONE helper + its own tagged session + an agent_spawns row.

    The helper is an ephemeral clone of the orchestrator (built from scratch on
    the app-global baseline), with abilities OFF except those explicitly granted
    and clamped to the orchestrator's own ceiling (see _compute_clone_grants)."""
    from app.db import get_db
    db = get_db()

    spawn_name = (name or "").strip() or (task[:40].strip() if task else "Helper")

    # Ceiling: clamp requested abilities + tool permissions to the orchestrator's.
    granted, clone_deny, clone_ask = ([], [], [])
    if orchestrator_agent_id:
        try:
            granted, clone_deny, clone_ask = await _compute_clone_grants(
                orchestrator_agent_id, abilities, allow_destructive)
        except Exception as e:  # noqa: BLE001
            logger.warning("Clone grant computation failed (defaulting to no abilities): %s", e)

    agent = await db.create_clone_agent(
        user_id=user_id, master_agent_id=orchestrator_agent_id or "",
        name=spawn_name,
        description=f"Clone helper for orchestrator session {orchestrator_session_id[:12]}",
        abilities=granted, allowed_tools=clone_deny, destructive_ask=clone_ask,
    )
    spawn_agent_id = agent["id"]

    # Build the clone's directive slot from (optional) borrowed prompt text, the
    # orchestrator's custom system_prompt, and an optional output contract. A
    # fire-and-forget worker with none of these gets no slot — it runs on the
    # app-global baseline + the task message alone.
    directive_parts = []
    borrowed = await _borrowed_prompt(from_agent) if (from_agent or "").strip() else ""
    if borrowed:
        directive_parts.append(
            f"## Reference identity borrowed from '{from_agent.strip()}'\n{borrowed}")
    if (system_prompt or "").strip():
        directive_parts.append(system_prompt.strip())
    if (output_contract or "").strip():
        directive_parts.append(
            "## Required output shape\n"
            "Return your result in exactly this shape/length:\n" + output_contract.strip())
    if directive_parts:
        try:
            await db.upsert_slot(
                agent_id=spawn_agent_id, slot_name="orchestrator_directive",
                order_index=0, lock=True, merge_mode="replace",
                content=_SPAWN_PREAMBLE + "\n\n".join(directive_parts), updated_by="orchestrator",
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

    row = _spawns_create(
        user_id=user_id, orchestrator_session_id=orchestrator_session_id,
        orchestrator_agent_id=orchestrator_agent_id, spawn_session_id=spawn_session_id,
        spawn_agent_id=spawn_agent_id, name=spawn_name, task=task or "", status="pending",
    )
    # Echo the (clamped) grants so the orchestrator can see what the ceiling allowed.
    row["granted_abilities"] = granted
    row["confirm_tools"] = clone_ask
    return row


async def _rewake_orchestrator(user_id: str, orchestrator_session_id: str, note: str) -> None:
    """Re-engage the orchestrator with a system-style event note (fire-and-forget)."""
    try:
        await _post_chat(user_id, orchestrator_session_id, note, timeout=300.0)
    except Exception as e:  # noqa: BLE001
        logger.warning("Re-wake of orchestrator %s failed (non-fatal): %s",
                       orchestrator_session_id, e)


async def _heartbeat_loop(spawn_id: str) -> None:
    """Refresh a spawn's heartbeat every _HEARTBEAT_INTERVAL while its runner is
    alive (covers the whole life of the runner coroutine: queue-wait + run), so a
    live spawn is never recovered as 'dead' — on this OR any other instance."""
    try:
        while True:
            _spawns_heartbeat(spawn_id)
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.debug("heartbeat loop for spawn %s stopped: %s", spawn_id, e)


async def _run_spawn_turn(*, user_id, spawn_id, spawn_session_id, message, wait,
                         notify_on_done=True, wait_timeout=300.0) -> dict:
    """WAIT: await the turn inline (bypasses the run-queue — see _run_slots),
    return the reply. FORK: return immediately as 'queued'; a background task
    waits for a run slot, executes the turn, settles the spawn, and (if
    notify_on_done) re-wakes the orchestrator with the result."""
    if wait:
        # Blocking runs execute inline under the caller and do NOT take a run
        # slot: they don't multiply concurrency, and throttling them would
        # deadlock a deep blocking chain. Tracked as live so recovery won't
        # double-run a legitimately long one.
        _spawns_update(spawn_id, status="running")
        _LIVE_RUNS.add(spawn_id)
        _spawns_heartbeat(spawn_id)
        hb = asyncio.create_task(_heartbeat_loop(spawn_id))
        try:
            reply = await _post_chat(user_id, spawn_session_id, message, wait_timeout)
            _spawns_update(spawn_id, status="done", result_summary=(reply or "")[:2000])
            return {"status": "done", "reply": reply or ""}
        except Exception as e:  # noqa: BLE001
            logger.warning("Spawn %s wait-run failed: %s", spawn_id, e)
            _spawns_update(spawn_id, status="error", result_summary=str(e)[:2000])
            return {"status": "error", "error": str(e)}
        finally:
            hb.cancel()
            try:
                await hb
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            _LIVE_RUNS.discard(spawn_id)

    spawn = _spawns_get(spawn_id) or {}
    orch_session = spawn.get("orchestrator_session_id")
    spawn_name = spawn.get("name") or spawn_id

    # Mark queued up-front: it has a live runner (below) but may sit waiting for a
    # run slot under heavy fan-out. 'queued' (not 'running') keeps the orphan
    # sweep from mistaking a slot-waiting spawn for a dead one.
    _spawns_update(spawn_id, status="queued")

    async def _bg():
        _LIVE_RUNS.add(spawn_id)
        _spawns_heartbeat(spawn_id)  # prove life immediately, even while queued
        hb = asyncio.create_task(_heartbeat_loop(spawn_id))
        try:
            # Drain through the global run-queue: unlimited spawns may be queued,
            # but only _MAX_CONCURRENT_SPAWN_RUNS execute their (heavy) loop at
            # once. The slot is held ONLY for the spawn's own turn — released
            # before the re-wake so the orchestrator's reaction isn't gated by it.
            async with _run_slots():
                _spawns_update(spawn_id, status="running")
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
        finally:
            hb.cancel()
            try:
                await hb
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            _LIVE_RUNS.discard(spawn_id)

    asyncio.create_task(_bg())
    return {"status": "queued"}


# ════════════════════════════════════════════════════════════════════════════
# Background service — durable follow-up-timer poller (discovered by app/main.py).
# Sibling in spirit to the automation scheduler: wakes every POLL_INTERVAL, claims
# spawns whose next_check_at elapsed, and re-wakes their orchestrator. DB-backed
# so timers survive a restart.
# ════════════════════════════════════════════════════════════════════════════

_POLL_INTERVAL = 20

# ── Spawn self-healing tunables ──────────────────────────────────────────────
# The core run self-healing (session_runs + boot resume) does NOT cover spawn
# turns: forked spawns run via the plain /api/v1/chat path and never register a
# session_runs row, so a restart orphans them with no recovery. This ability
# therefore recovers its OWN forked spawns from the same poller — but recovery
# here means FAIL-AND-NOTIFY, never re-run. A spawn proven dead (a stale heartbeat
# ⇒ no live runner on any instance) is flipped to 'error' and its orchestrator is
# re-woken so it can deliberately re-spawn the work. We never re-execute the task
# automatically: a re-run re-does everything the spawn already did, INCLUDING
# re-spawning the sub-helpers it created, so auto-re-running a self-orchestrating
# tree snowballs one restart into an exploding fan-out (observed: 6 → 120+ spawns).
_ORPHAN_RUNNING_SECONDS = 180   # 'running'/'queued' + heartbeat idle this long ⇒ runner is dead
_RECOVER_PER_TICK = 3           # fail at most this many orphans per tick (avoid a notify storm)

# ── Spawn run-queue (concurrency throttle) ───────────────────────────────────
# Spawns can spawn — self-orchestration is unlimited in DEPTH and BREADTH (an
# agent may fork as many helpers as it likes, and those helpers may fork their
# own). What is bounded is how many forked spawn loops EXECUTE at once. Each
# forked spawn runs a full agent loop (its own LLM calls + parallel tool batches)
# via a loopback into this same server process; letting dozens run simultaneously
# exhausts memory and hard-kills the process (observed: ~6 crashes/16min under a
# heavy fan-out). So forked execution drains through a global semaphore: requests
# are never capped, but only this many run concurrently — the rest sit 'queued'
# and start as slots free up. Blocking (wait=True) spawns BYPASS the semaphore:
# they run inline under their caller, so they don't multiply concurrency, and
# throttling them would deadlock a deep blocking chain (each level would hold a
# slot waiting for a deeper level that can't get one). Tune via env.
_MAX_CONCURRENT_SPAWN_RUNS = max(1, int(os.environ.get("WEBAGENT_SPAWN_CONCURRENCY", "3") or 3))
_RUN_SLOTS: Optional[asyncio.Semaphore] = None
# How often a live runner refreshes its heartbeat. Must be comfortably smaller
# than _ORPHAN_RUNNING_SECONDS so a healthy spawn beats several times within the
# window and is never mistaken for dead.
_HEARTBEAT_INTERVAL = max(5, int(os.environ.get("WEBAGENT_SPAWN_HEARTBEAT", "20") or 20))
# spawn_ids whose runner coroutine is alive THIS process (queued-and-waiting or
# actively running). Recovery skips these: a spawn that's stuck 'running'/'queued'
# but NOT in this set has no live runner (e.g. a restart killed it) ⇒ orphaned.
# This also prevents wrongly re-running a legitimately long live spawn.
_LIVE_RUNS: set = set()


def _run_slots() -> asyncio.Semaphore:
    """Lazily create the run-queue semaphore so it binds to the running loop."""
    global _RUN_SLOTS
    if _RUN_SLOTS is None:
        _RUN_SLOTS = asyncio.Semaphore(_MAX_CONCURRENT_SPAWN_RUNS)
    return _RUN_SLOTS


_poll_task: Optional[asyncio.Task] = None
_poll_running = False


async def _resume_orphaned_spawns(now_iso: str) -> None:
    """Recover forked spawns whose runner died (server restart, crash): they sit at
    'running'/'queued' with a stale heartbeat and no live coroutine. We FAIL them
    honestly and re-wake their orchestrator — we never re-run the task, because a
    re-run re-does everything the spawn already did (including re-spawning its
    sub-helpers), which snowballs one restart into an exploding fan-out. The
    orchestrator can deliberately re-spawn anything it still needs."""
    live = set(_LIVE_RUNS)
    try:
        failed = _spawns_fail_orphans(now_iso, _ORPHAN_RUNNING_SECONDS,
                                      _RECOVER_PER_TICK, exclude_ids=live)
    except Exception as e:  # noqa: BLE001
        logger.debug("orphan fail-out sweep failed: %s", e)
        return
    for dead in failed:
        orch = dead.get("orchestrator_session_id")
        logger.info("Failed orphaned spawn %s '%s' (interrupted; not re-run)",
                    dead.get("id"), dead.get("name"))
        if not orch:
            continue
        try:
            await _rewake_orchestrator(dead.get("user_id"), orch, (
                f"[ORCHESTRATION EVENT] Spawn '{dead.get('name') or dead.get('id')}' "
                f"(id {dead.get('id')}) FAILED to complete — it was interrupted (likely a "
                "server restart) and was NOT automatically re-run. Re-spawn it if you still "
                "need it, or report it as failed. Do not invent its result."))
        except Exception as e:  # noqa: BLE001
            logger.warning("Re-wake after fail-out failed for spawn %s: %s", dead.get("id"), e)


async def _poll_tick() -> None:
    now = _now_iso()
    # Self-heal forked spawns orphaned by a dead runner (e.g. a server restart).
    try:
        await _resume_orphaned_spawns(now)
    except Exception as e:  # noqa: BLE001
        logger.debug("orphaned-spawn recovery failed: %s", e)
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

    Self-orchestration is UNLIMITED: a helper (its own session id is prefixed
    `spawn-`) gets the full spawn toolset too, so it can spawn its own
    sub-helpers, to any depth and breadth. Run-away cascades are prevented not by
    forbidding nesting but by the global run-queue (see _run_slots): unlimited
    spawns may be requested/queued, but only a few execute their heavy loop at
    once, so a deep/wide tree drains safely instead of crashing the process.
    Optimizer pipeline sub-agents are still excluded (they aren't orchestrators).
    """
    if not session_id:
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
                          check_back_minutes: float = 0, abilities: Optional[list] = None,
                          allow_destructive: Optional[list] = None,
                          output_contract: str = "") -> str:
        """Spawn a fresh CLONE of yourself to work on a task.

        A clone is built FROM SCRATCH — it does NOT inherit your persona. It runs
        on the platform's app-global baseline plus whatever directive you give it
        here, and it starts with EVERY ability OFF.

        - ``system_prompt`` — the clone's identity/directive, written by you. Leave
          blank for a bare fire-and-forget worker (baseline + task only).
        - ``abilities`` — a list of ability ids to switch ON for the clone (see
          ``list_abilities``). You MUST pick deliberately; nothing is on by default.
          A clone can only spawn its OWN sub-helpers if you include
          ``"agent_orchestration"`` here. You can never grant an ability you don't
          have yourself (the ceiling).
        - ``allow_destructive`` — list of tool names the clone may use even though
          they normally require confirmation. Off by default: a clone has no human
          to confirm, so confirm-tools are blocked unless you opt them in here. You
          can never give a clone looser permission on a tool than you have.
        - ``output_contract`` — optional: the exact shape/length you want the
          result in, so it comes back small and uniform.
        - ``from_agent`` — optional: another agent/template id whose prompt text is
          pulled in for the clone to reuse as reference identity. (To hand the whole
          chat to a real other agent instead, use delegate_to_agent.)
        - ``wait=True`` blocks and returns the reply; ``wait=False`` (default) forks
          and re-wakes you with the result. ``check_back_minutes>0`` sets a timer.

        Returns the spawn_id.
        """
        if not (task or "").strip() and not (system_prompt or "").strip():
            return _err("Provide a task (and/or a system_prompt) for the spawn.")
        try:
            spawn = await _create_spawn(
                user_id=user_id, orchestrator_session_id=session_id,
                orchestrator_agent_id=agent_id or None, task=task or "",
                name=name or "", system_prompt=system_prompt or "", from_agent=from_agent or "",
                abilities=abilities or [], allow_destructive=allow_destructive or [],
                output_contract=output_contract or "",
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
               "granted_abilities": spawn.get("granted_abilities", []),
               "confirm_tools": spawn.get("confirm_tools", []),
               "mode": "wait" if wait else "fork", "run_status": result.get("status")}
        if wait:
            out["reply"] = result.get("reply", "")
            if result.get("status") == "error":
                out["error"] = result.get("error")
        else:
            out["note"] = ("Spawn forked — it's queued in the run-queue and will run on its "
                           "own (status goes queued → running → done). You'll be re-woken with "
                           "its result when it finishes; no need to wait or poll. 'queued' just "
                           "means it's waiting its turn under load — that's normal, not a "
                           "failure. Check anytime with list_spawns.")
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
        a health read (is it alive / stalled / done?), age, last result summary,
        and any pending follow-up timer."""
        try:
            rows = _spawns_list(session_id)
        except Exception as e:  # noqa: BLE001
            return _err(f"Could not list spawns: {e}")
        now_dt = datetime.now(timezone.utc)
        spawns = []
        for r in rows:
            status = r.get("status")
            # Prefer the heartbeat (refreshed during a run); fall back to updated_at
            # for legacy rows with no heartbeat — same basis the recovery uses.
            since_hb = _age_seconds(now_dt, r.get("heartbeat_at") or r.get("updated_at"))
            stalled, health = _spawn_health(status, since_hb)
            spawns.append({
                "spawn_id": r.get("id"), "name": r.get("name"), "status": status,
                "health": health, "stalled": stalled,
                "seconds_since_heartbeat": since_hb,
                "seconds_in_state": _age_seconds(now_dt, r.get("updated_at")),
                "age_seconds": _age_seconds(now_dt, r.get("created_at")),
                "task": (r.get("task") or "")[:200],
                "result_summary": (r.get("result_summary") or "")[:400],
                "next_check_at": r.get("next_check_at"),
            })
        return json.dumps({
            "status": "ok", "count": len(spawns), "spawns": spawns,
            "guidance": ("`health`/`stalled` tell you a spawn's liveness. Only a 'done' spawn "
                         "has a result (read it with quote_spawn). 'running'/'queued' are "
                         "in-progress — never report them as results or narrate what they're "
                         "'doing'. A 'stalled' spawn is effectively dead — it's being "
                         "auto-recovered or failed; don't wait on it, and don't invent its output."),
        })

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

    async def promote_spawn(spawn_id: str, name: str = "", description: str = "") -> str:
        """Promote a throwaway clone into a PERMANENT agent so it survives the
        cleanup that normally reaps clones with this session. Use it when a clone
        you spawned turned out genuinely reusable and you want to keep its prompt +
        abilities as a real fleet agent. Keeps the clone's directive, granted
        abilities and transcript; flips it out of 'clone' status so it appears in
        the agent roster and is no longer cascade-deleted."""
        spawn = await _owned_spawn(spawn_id)
        if not spawn:
            return _err(f"No spawn '{spawn_id}' owned by this session.")
        spawn_agent_id = spawn.get("spawn_agent_id")
        if not spawn_agent_id:
            return _err("This spawn has no agent to promote.")
        from app.db import get_db
        db = get_db()
        try:
            agent = await db.get_agent_by_id(spawn_agent_id) or {}
            if agent.get("status") != "clone":
                return _err("That spawn's agent is not a clone (already promoted or gone).")
            try:
                meta = json.loads(agent.get("metadata") or "{}")
            except Exception:  # noqa: BLE001
                meta = {}
            meta["kind"] = "promoted_clone"  # keep clone_of for lineage
            meta["owner_user_id"] = user_id
            new_name = (name or "").strip() or (spawn.get("name") or agent.get("name") or "Promoted clone")
            new_desc = (description or "").strip() or "Promoted from a spawned clone."
            conn = _db_conn()
            if conn is None:
                return _err("Backend unavailable.")
            try:
                conn.execute(
                    "UPDATE agents SET status='active', name=?, description=?, metadata=?, updated_at=? "
                    "WHERE id=? AND status='clone'",
                    (new_name, new_desc, json.dumps(meta), _now_iso(), spawn_agent_id),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("promote_spawn failed: %s", e)
            return _err(f"Could not promote spawn: {e}")
        return json.dumps({"status": "ok", "spawn_id": spawn_id, "agent_id": spawn_agent_id,
                           "name": new_name,
                           "note": "Clone promoted to a permanent agent — it now appears in the "
                                   "roster and will NOT be reaped when this session is deleted."})

    async def list_abilities() -> str:
        """List the abilities you can switch on for a clone — the ones YOU have
        enabled (your ceiling). A clone starts with everything OFF; pass the ids
        you need in spawn_agent(abilities=[...]). You can't grant what you lack."""
        from app.db import get_db
        db = get_db()
        try:
            conns = await db.get_agent_connections(agent_id) if agent_id else []
        except Exception:  # noqa: BLE001
            conns = []
        mine = {c.get("connection_type") for c in conns
                if c.get("section") == "ability" and c.get("enabled")}
        try:
            from app.abilities import ui_catalog
            cat = ui_catalog() or {}
        except Exception:  # noqa: BLE001
            cat = {}
        meta_map = cat.get("abilities") or {}
        out = []
        for aid in sorted(mine):
            meta = meta_map.get(aid) or {}
            out.append({
                "id": aid,
                "display_name": meta.get("display_name") or aid,
                "description": (meta.get("description") or meta.get("summary") or "")[:300],
                "tools": meta.get("tools") or [],
            })
        return json.dumps({
            "status": "ok", "count": len(out), "grantable_abilities": out,
            "guidance": ("These are the only abilities you can switch on for a clone — you "
                         "can't grant what you don't have. Pass the ids you need in "
                         "spawn_agent(abilities=[...]); everything is OFF unless you list it. "
                         "Include 'agent_orchestration' ONLY if the clone must spawn its own "
                         "sub-helpers (recursion is opt-in)."),
        })

    return {
        "spawn_agent": spawn_agent,
        "message_spawn": message_spawn,
        "read_spawn": read_spawn,
        "quote_spawn": quote_spawn,
        "list_spawns": list_spawns,
        "stop_spawn": stop_spawn,
        "schedule_spawn_check": schedule_spawn_check,
        "list_abilities": list_abilities,
        "promote_spawn": promote_spawn,
    }


# No host-side confirmation on the orchestration tools themselves: creating /
# messaging / stopping helpers is frictionless. Anything risky a HELPER does runs
# inside that helper's own session, where that agent's own tool gating asks for
# confirmation — so the guard sits where the real side effect happens, not on the
# act of spawning.
DESTRUCTIVE: set = set()

TOOL_SCHEMAS = {
    "spawn_agent": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "What the helper should do."},
            "name": {"type": "string", "description": "Optional short label for the helper/session."},
            "system_prompt": {"type": "string", "description": "Optional: the clone's directive/identity, written by you. Leave blank for a bare fire-and-forget worker (runs on the app-global baseline + the task only)."},
            "abilities": {"type": "array", "items": {"type": "string"}, "description": "Ability ids to switch ON for the clone (see list_abilities). EVERYTHING is OFF unless listed. Clamped to abilities you have yourself. Include 'agent_orchestration' only if the clone must spawn its own sub-helpers."},
            "allow_destructive": {"type": "array", "items": {"type": "string"}, "description": "Tool names the clone may use even though they normally require confirmation. Off by default (a clone has no human to confirm). Can never exceed your own permission for a tool."},
            "output_contract": {"type": "string", "description": "Optional: the exact shape/length you want the result in, so it returns small and uniform."},
            "from_agent": {"type": "string", "description": "Optional: another agent/template id whose prompt text is pulled in as reference identity for the clone. To hand the whole chat to a real agent instead, use delegate_to_agent."},
            "wait": {"type": "boolean", "description": "True = block and return the helper's reply now. False (default) = fork and be re-woken when it finishes."},
            "check_back_minutes": {"type": "number", "description": "Optional: also set a follow-up timer (minutes) to re-check the helper even if still running."},
        },
        "required": ["task"],
    },
    "list_abilities": {"type": "object", "properties": {}, "required": []},
    "promote_spawn": {
        "type": "object",
        "properties": {
            "spawn_id": {"type": "string", "description": "The spawn whose clone agent to keep permanently."},
            "name": {"type": "string", "description": "Optional name for the promoted agent (defaults to the spawn's name)."},
            "description": {"type": "string", "description": "Optional description for the promoted agent."},
        },
        "required": ["spawn_id"],
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
