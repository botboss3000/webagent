"""
Optimizer Runner — orchestrates the analyze→propose→deploy pipeline.
Called after every agent completion (all transport types).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.optimizer.config import load_config, update_state, get_intensity_thresholds
from app.optimizer.analyzer import analyze_skills
from app.optimizer.proposer import propose_improvement
from app.optimizer.deployer import deploy_improvements

logger = logging.getLogger(__name__)

# ── Track which sessions have been optimized recently to avoid duplicates ──
_recently_seen: Dict[str, float] = {}
_RECENT_WINDOW_SEC = 30  # don't re-analyze same session within 30 seconds


async def run_optimizer_async(
    user_id: str,
    session_id: str,
    channel: str = "ui",
) -> Optional[str]:
    """
    Run the full optimizer pipeline for a user session.

    This is called as a fire-and-forget async task after every agent
    turn completes (streaming, WebSocket, buffered).

    Returns optimizer session_id if created, or None if skipped.
    """
    now = datetime.now(timezone.utc).isoformat()

    # ── Dedup ──
    if session_id in _recently_seen:
        elapsed = asyncio.get_event_loop().time() - _recently_seen[session_id]
        if elapsed < _RECENT_WINDOW_SEC:
            logger.debug("Optimizer: skipping session %s (ran %.0fs ago)", session_id, elapsed)
            return None
    _recently_seen[session_id] = asyncio.get_event_loop().time()

    # ── Load config ──
    cfg = load_config()
    if cfg.get("mode") != "live":
        return None  # scheduled mode — only runs on timer

    intensity = cfg.get("intensity", 3)
    thresholds = get_intensity_thresholds(intensity)
    min_sample = cfg.get("per_user", {}).get("min_sample", 3)
    target_metrics = cfg.get("target_metrics", [])

    optimizer_session_id = f"optimizer-{session_id}"

    # ── Create optimizer session ──
    try:
        _create_optimizer_session(user_id, optimizer_session_id, session_id)
    except Exception as e:
        logger.warning("Optimizer: failed to create session: %s", e)

    # ── Log optimizer run start ──
    run_id = str(uuid.uuid4())
    _log_run(run_id, "running", cfg, optimizer_session_id)

    # ── Phase 1: Analyze ──
    opportunities = []
    try:
        opportunities = await analyze_skills(
            user_id=user_id,
            session_id=session_id,
            min_sample=min_sample,
            target_metrics=target_metrics,
        )
        _insert_opt_interaction(user_id, optimizer_session_id,
                                'assistant', 'optimizer:analyzer',
                                f"Scanned skills. Found {len(opportunities)} opportunities.")
    except Exception as e:
        logger.error("Optimizer: analyzer failed: %s", e)
        _insert_opt_interaction(user_id, optimizer_session_id,
                                'assistant', 'optimizer:analyzer',
                                f"Analyzer failed: {e}")
        _log_run(run_id, "failed", cfg, optimizer_session_id,
                 errors=[f"analyzer: {e}"])
        return optimizer_session_id

    if not opportunities:
        _insert_opt_interaction(user_id, optimizer_session_id,
                                'assistant', 'optimizer:analyzer',
                                'No optimization opportunities found. All skills are healthy.')
        _log_run(run_id, "success", cfg, optimizer_session_id,
                 summary="No optimization opportunities found.",
                 skills_analyzed=0,
                 proposals_generated=0,
                 proposals_deployed=0)
        update_state(last_run_at=now, last_run_status="success",
                    improvements_deployed=cfg.get("state", {}).get("improvements_deployed", 0))
        return optimizer_session_id

    # ── Phase 2: Propose ──
    proposals: List[Dict[str, Any]] = []
    for opp in opportunities[:3]:  # limit to 3 per run
        skill_name = opp["skill_name"]
        issue_desc = opp["issue_desc"]
        issue_type = opp["issue_type"]

        # Get current skill content — try context_documents first, then skills table
        current_content = _get_skill_content(user_id, skill_name)
        if not current_content:
            # Try getting from skills table
            current_content = _get_skill_code(user_id, skill_name)
        if not current_content:
            # Generate synthetic content for tools with no instructions
            current_content = f"Tool: {skill_name}\nIssue: {issue_desc}\nThis tool needs reliability improvements."

        logger.info("Optimizer: proposing improvement for %s (content length %d)", skill_name, len(current_content) if current_content else 0)

        try:
            proposal = await propose_improvement(
                skill_name=skill_name,
                skill_content=current_content,
                issue_desc=issue_desc,
                issue_type=issue_type,
            )
            if proposal:
                proposals.append(proposal)
                if proposal.get("improved"):
                    _insert_opt_interaction(user_id, optimizer_session_id,
                                            'assistant', 'optimizer:proposer',
                                            f"Proposed v2 for {skill_name}.\nReasoning: {proposal.get('reasoning', '')[:200]}")
        except Exception as e:
            logger.warning("Optimizer: proposer failed for %s: %s", skill_name, e)

    if not proposals:
        _insert_opt_interaction(user_id, optimizer_session_id,
                                'assistant', 'optimizer:proposer',
                                f"Analyzed {len(opportunities)} opportunities but LLM returned no improvements.")
        _log_run(run_id, "success", cfg, optimizer_session_id,
                 summary=f"Analyzed {len(opportunities)} opportunities but proposer returned no improvements.",
                 skills_analyzed=len(opportunities),
                 proposals_generated=0,
                 proposals_deployed=0)
        update_state(last_run_at=now, last_run_status="success",
                    improvements_deployed=cfg.get("state", {}).get("improvements_deployed", 0))
        return optimizer_session_id

    # ── Phase 3: Deploy ──
    deployed = 0
    try:
        _create_skill_improvements_table()
        deployed = await deploy_improvements(
            user_id=user_id,
            session_id=session_id,
            proposals=proposals,
            optimizer_session_id=optimizer_session_id,
        )
        _insert_opt_interaction(user_id, optimizer_session_id,
                                'assistant', 'optimizer:deployer',
                                f"Deployed {deployed} improvements.")
    except Exception as e:
        logger.error("Optimizer: deployer failed: %s", e)
        _log_run(run_id, "failed", cfg, optimizer_session_id,
                 skills_analyzed=len(opportunities),
                 proposals_generated=len(proposals),
                 proposals_deployed=0,
                 errors=[f"deployer: {e}"])
        return optimizer_session_id

    # ── Success ──
    old_total = cfg.get("state", {}).get("improvements_deployed", 0)
    new_total = old_total + deployed
    _log_run(run_id, "success", cfg, optimizer_session_id,
             skills_analyzed=len(opportunities),
             proposals_generated=len(proposals),
             proposals_deployed=deployed,
             summary=f"Deployed {deployed} improvements from {len(proposals)} proposals across {len(opportunities)} analyzed skills.")
    update_state(last_run_at=now, last_run_status="success",
                improvements_deployed=new_total)

    logger.info("Optimizer: run %s complete — %d deployed", run_id, deployed)
    return optimizer_session_id


# ── Internal helpers ──

def _get_skill_content(user_id: str, skill_name: str) -> Optional[str]:
    """Read current skill content from context_documents (behavioral skills)."""
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, '_get_conn', None)
        if raw:
            conn = raw()
            # Map user_id to agent_id
            agent_row = conn.execute(
                "SELECT id FROM agents WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if not agent_row:
                conn.close()
                return None
            agent_id = agent_row[0]
            row = conn.execute(
                """SELECT content FROM context_documents
                   WHERE context_type = 'skills' AND (title = ? OR title LIKE ?)
                   AND agent_id = ?
                   LIMIT 1""",
                (skill_name, f"%{skill_name}%", agent_id),
            ).fetchone()
            conn.close()
            if row:
                return row[0]
    except Exception as e:
        logger.debug("_get_skill_content: %s", e)
    return None


def _get_skill_code(user_id: str, skill_name: str) -> Optional[str]:
    """Read skill code from the skills table (executable tools)."""
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, '_get_conn', None)
        if raw:
            conn = raw()
            row = conn.execute(
                "SELECT name, description, code FROM skills WHERE name = ? AND user_id = ? LIMIT 1",
                (skill_name, user_id),
            ).fetchone()
            conn.close()
            if row:
                name, desc, code = row
                return f"Tool: {name}\nDescription: {desc or 'N/A'}\nCode: {code or '(no code)'}"
    except Exception as e:
        logger.debug("_get_skill_code: %s", e)
    return None


def _create_optimizer_session(user_id: str, optimizer_session_id: str,
                               original_session_id: str) -> None:
    """Create a session for the optimizer run."""
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, '_get_conn', None)
        if not raw:
            return
        conn = raw()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT OR IGNORE INTO sessions
               (id, user_id, title, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (optimizer_session_id, user_id,
             f"Optimizer — {original_session_id[:12]}",
             now, now),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("_create_optimizer_session: %s", e)


def _log_run(run_id: str, status: str, config: dict, session_id: str,
             skills_analyzed: int = 0, proposals_generated: int = 0,
             proposals_deployed: int = 0, summary: str = "",
             errors: Optional[List[str]] = None) -> None:
    """Log or update optimizer run in the DB."""
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, '_get_conn', None)
        if not raw:
            return
        conn = raw()
        now = datetime.now(timezone.utc).isoformat()

        # Ensure table exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS optimizer_runs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at TEXT,
                blocked_skill TEXT,
                blocked_reason TEXT,
                blocked_action TEXT,
                blocked_at TEXT,
                skills_analyzed INTEGER DEFAULT 0,
                proposals_generated INTEGER DEFAULT 0,
                proposals_deployed INTEGER DEFAULT 0,
                proposals_rejected INTEGER DEFAULT 0,
                errors TEXT,
                summary TEXT,
                config_snapshot TEXT,
                session_id TEXT
            )
        """)

        # Check if run exists
        existing = conn.execute(
            "SELECT id FROM optimizer_runs WHERE id = ?", (run_id,)
        ).fetchone()

        err_json = json.dumps(errors) if errors else None
        config_json = json.dumps(config)
        completed_at = now if status in ("success", "failed") else None

        if existing:
            conn.execute(
                """UPDATE optimizer_runs
                   SET status = ?, completed_at = ?,
                       skills_analyzed = ?, proposals_generated = ?,
                       proposals_deployed = ?, errors = ?, summary = ?
                   WHERE id = ?""",
                (status, completed_at,
                 skills_analyzed, proposals_generated,
                 proposals_deployed, err_json, summary, run_id),
            )
        else:
            conn.execute(
                """INSERT INTO optimizer_runs
                   (id, status, started_at, completed_at,
                    skills_analyzed, proposals_generated, proposals_deployed,
                    errors, summary, config_snapshot, session_id)
                   VALUES (?, ?, ?, ?,
                           ?, ?, ?,
                           ?, ?, ?, ?)""",
                (run_id, status, now, completed_at,
                 skills_analyzed, proposals_generated, proposals_deployed,
                 err_json, summary, config_json, session_id),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("_log_run: %s", e)


def _create_skill_improvements_table() -> None:
    """Ensure skill_improvements table exists."""
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, '_get_conn', None)
        if raw:
            conn = raw()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS skill_improvements (
                    id TEXT PRIMARY KEY,
                    skill_id TEXT,
                    old_version TEXT,
                    new_version TEXT,
                    opportunity_type TEXT,
                    old_metrics TEXT,
                    expected_deltas TEXT,
                    actual_deltas TEXT,
                    validation_result TEXT DEFAULT 'pass',
                    proposer_reasoning TEXT,
                    diff_summary TEXT,
                    user_feedback_ref TEXT,
                    deployed_at TEXT NOT NULL DEFAULT (datetime('now')),
                    rolled_back_at TEXT
                )
            """)
            conn.commit()
            conn.close()
    except Exception as e:
        logger.warning("_create_skill_improvements_table: %s", e)


def _insert_opt_interaction(user_id: str, session_id: str,
                             role: str, source: str, content: str) -> None:
    """Insert an interaction into the optimizer session with proper source label."""
    try:
        from app.db import get_db
        db = get_db()
        # Use the DB's insert_interaction — but that asserts session ownership.
        # Instead, insert directly into the optimizer session.
        raw = getattr(db, '_get_conn', None)
        if raw:
            import uuid
            conn = raw()
            intr_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO interactions
                   (id, session_id, role, content, source, channel, created_at)
                   VALUES (?, ?, ?, ?, ?, 'optimizer', ?)""",
                (intr_id, session_id, role, content, source, now),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.debug("_insert_opt_interaction: %s", e)
