"""
Optimizer Runner — creates sessions, hands off to chat.py routing.
"""

from __future__ import annotations

import asyncio, json, logging, uuid
from datetime import datetime, timezone
from typing import Any, Dict

from app.optimizer.config import load_config, update_state, get_intensity_thresholds
from app.optimizer.prefilter import prefilter

logger = logging.getLogger(__name__)
_recently_seen: Dict[str, float] = {}
_RECENT_WINDOW_SEC = 60


async def run_optimizer_async(user_id, session_id, channel="ui", criteria="", feedback="", skill_name="", force=False):
    """
    Lightweight entry point for /optimize and live mode.

    1. Dedup + mode check.
    2. Create optimizer session (opt_role='planner' → chat.py routes to opt_planner agent).
    3. Run prefilter on target session.
    4. Insert prefilter stats + user feedback as init interactions.
    5. Return session_id for the user to open.

    No direct LLM calls. No iteration loop. No auto-deploy.
    The Planner / Finalizer agents handle those via tools in chat flow.
    """
    now = datetime.now(timezone.utc).isoformat()
    from app.optimizer.templates import seed_optimizer_templates
    seed_optimizer_templates()

    # Skip Worker test sessions to prevent recursive optimizer cascades
    if session_id.startswith('worker-'):
        return None

    # Skip dedup check when force=True (/optimize command)
    if not force:
        if session_id in _recently_seen:
            if asyncio.get_event_loop().time() - _recently_seen[session_id] < _RECENT_WINDOW_SEC and not criteria:
                return None
    _recently_seen[session_id] = asyncio.get_event_loop().time()

    cfg = load_config()
    # Bypass mode check when force=True (used by /optimize command)
    if not force:
        if cfg.get("mode") != "live":
            return None

    run_id = str(uuid.uuid4())
    opt_sid = f"optimizer-{str(uuid.uuid4())[:8]}"

    # ── Create optimizer session ──
    _ensure_session(user_id, opt_sid, session_id)
    _store_optimizer_metadata(user_id, opt_sid, session_id)  # sets opt_role='planner'

    # ── Prefilter target session ──
    pf = await prefilter(user_id, session_id)
    turns = pf.get("turns", 1)
    tokens_est = pf.get("tokens", 100)

    # ── Insert init interactions ──
    # NOTE: role='assistant' so build_openai_history_from_session includes it
    # (system-role interactions are skipped by history builder).
    transcript_text = "\n".join(pf.get("transcript", [])[:30])
    docs_text = ""
    for d in pf.get("context_docs", []):
        docs_text += f"- [{d.get('type','?')}] {d.get('title','?')}: {d.get('excerpt','')[:100]}\n"
    _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:init",
                    f"📊 **Optimization Analysis**\n\n"
                    f"**Target session:** `{session_id}`\n"
                    f"**Stats:** {turns} turns, ~{tokens_est} tokens\n\n"
                    f"**Transcript:**\n{transcript_text}\n\n"
                    f"**Context documents:**\n{docs_text or '(none)'}\n\n"
                    f"Review this session. Use `list_agent_context_documents` and `session_search` to dive deeper. "
                    f"Propose changes by discussing with me.")

    _insert_opt_msg(user_id, opt_sid, "assistant", "optimizer:prefilter",
                    f"Stats: {turns} turns, ~{tokens_est} tokens.")

    if feedback:
        _insert_opt_msg(user_id, opt_sid, "user", "optimizer:user_feedback",
                        f"User feedback: {feedback}")

    # ── Log and return ──
    _log_start(run_id, cfg, opt_sid)
    _log_complete(run_id, "running", cfg, opt_sid,
                  summary=f"Session created. {turns} turns, ~{tokens_est} tokens in target session.")
    return opt_sid


# ── Helpers ──

def _retry_db_write(fn, max_attempts=5, delay=0.3):
    import time
    for a in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            if "locked" in str(e).lower() and a < max_attempts - 1:
                time.sleep(delay * (a + 1))
            else:
                raise


def _store_optimizer_metadata(uid, sid, target_sid):
    """Store the original target session in optimizer session metadata."""
    from app.db import get_db
    db = get_db()
    raw = getattr(db, '_get_conn', None)
    if raw:
        meta = json.dumps({"opt_role": "planner", "target_session": target_sid})

        def _do():
            c = raw()
            c.execute(
                "UPDATE sessions SET metadata=? WHERE id=?",
                (meta, sid),
            )
            c.commit()
            c.close()

        _retry_db_write(_do)


def _ensure_session(uid, sid, orig):
    from app.db import get_db
    db = get_db()
    raw = getattr(db, '_get_conn', None)
    if raw:
        def _do():
            c = raw()
            c.execute(
                "INSERT OR IGNORE INTO sessions (id,user_id,title,created_at,updated_at) "
                "VALUES (?,?,?,datetime('now'),datetime('now'))",
                (sid, uid, f"Optimizer \u2014 {orig[:12]}"),
            )
            c.commit()
            c.close()

        _retry_db_write(_do)


def _insert_opt_msg(uid, sid, role, source, content):
    from app.db import get_db
    db = get_db()
    raw = getattr(db, '_get_conn', None)
    if raw:
        def _do():
            c = raw()
            c.execute(
                "INSERT INTO interactions (id,session_id,role,content,source,channel,created_at) "
                "VALUES (?,?,?,?,?,'optimizer',datetime('now'))",
                (str(uuid.uuid4()), sid, role, content, source),
            )
            c.commit()
            c.close()

        _retry_db_write(_do)


def _log_start(rid, cfg, sid):
    _log_run(rid, "running", cfg, sid)


def _log_complete(rid, st, cfg, sid, **kw):
    _log_run(rid, st, cfg, sid, **kw)


def _log_run(rid, status, config, sid, skills_analyzed=0, proposals_generated=0, proposals_deployed=0, summary="", errors=None):
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, '_get_conn', None)
        if not raw:
            return
        now = datetime.now(timezone.utc).isoformat()
        completed = now if status in ("success", "failed") else None
        errs = json.dumps(errors) if errors else None
        cfg_json = json.dumps(config)

        def _do():
            c = raw()
            c.execute(
                "CREATE TABLE IF NOT EXISTS optimizer_runs "
                "(id TEXT PRIMARY KEY, status TEXT DEFAULT 'running', started_at TEXT, "
                "completed_at TEXT, skills_analyzed INTEGER DEFAULT 0, "
                "proposals_generated INTEGER DEFAULT 0, proposals_deployed INTEGER DEFAULT 0, "
                "errors TEXT, summary TEXT, config_snapshot TEXT, session_id TEXT)"
            )
            ex = c.execute("SELECT id FROM optimizer_runs WHERE id=?", (rid,)).fetchone()
            if ex:
                c.execute(
                    "UPDATE optimizer_runs SET status=?,completed_at=?,skills_analyzed=?,"
                    "proposals_generated=?,proposals_deployed=?,errors=?,summary=? WHERE id=?",
                    (status, completed, skills_analyzed, proposals_generated, proposals_deployed, errs, summary, rid),
                )
            else:
                c.execute(
                    "INSERT INTO optimizer_runs (id,status,started_at,completed_at,skills_analyzed,"
                    "proposals_generated,proposals_deployed,errors,summary,config_snapshot,session_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, status, now, completed, skills_analyzed, proposals_generated, proposals_deployed, errs, summary, cfg_json, sid),
                )
            c.commit()
            c.close()

        _retry_db_write(_do)
    except Exception as e:
        logger.warning("_log_run: %s", e)
