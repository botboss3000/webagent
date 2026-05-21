"""
Deployer phase — writes improved skill versions into the agent's prompt slots.

Behavioral skills land in the 'skills' slot on the user's default agent
(admin-base row). The Optimizer's improvement log is a separate table.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.db import get_db

logger = logging.getLogger(__name__)


async def deploy_improvements(
    user_id: str,
    session_id: str,
    proposals: List[Dict[str, Any]],
    optimizer_session_id: str,
) -> int:
    """Apply approved proposals to the user's agent prompt slots."""
    db = get_db()
    deployed = 0

    # Resolve the user's default agent.
    raw = getattr(db, "_get_conn", None)
    if not raw:
        return 0
    conn = raw()
    try:
        agent_row = conn.execute(
            """SELECT id FROM agents
               WHERE EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?)
               ORDER BY is_user_default DESC, created_at ASC
               LIMIT 1""",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()
    if not agent_row:
        logger.warning("Deployer: no agent found for user %s", user_id)
        return 0
    agent_id = agent_row[0]

    for proposal in proposals:
        if not proposal.get("improved"):
            continue

        skill_name = proposal.get("skill_name", "")
        new_content = proposal.get("new_content", "")
        reasoning = proposal.get("reasoning", "")

        if not skill_name or not new_content:
            continue

        try:
            # Behavioral skill writes go into the 'skills' slot (admin base).
            slots = await db.list_slots(agent_id)
            skills_slot = next((s for s in slots if s["slot_name"] == "skills"), None)
            if skills_slot is None:
                logger.warning("Deployer: agent %s has no 'skills' slot — skipping", agent_id[:8])
                continue
            await db.upsert_slot(
                agent_id=agent_id,
                slot_name="skills",
                order_index=int(skills_slot.get("order_index") or 40),
                lock=bool(skills_slot.get("lock")),
                merge_mode=skills_slot.get("merge_mode") or "replace",
                content=new_content,
                updated_by="optimizer",
            )
            deployed += 1

            conn = raw()
            try:
                _log_improvement(conn, skill_name, "1", "2",
                                 "behavioral", reasoning, optimizer_session_id)
            finally:
                conn.close()
            logger.info("Deployer: updated 'skills' slot for agent %s (proposal '%s')",
                        agent_id[:8], skill_name)
        except Exception as e:
            logger.error("Deployer: failed to deploy %s: %s", skill_name, e)

    return deployed


def _log_improvement(conn, skill_name: str, old_ver: str, new_ver: str,
                     opp_type: str, reasoning: str, optimizer_session_id: str) -> None:
    """Insert a row into skill_improvements table (creates if not exists)."""
    try:
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
        imp_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO skill_improvements
               (id, skill_id, old_version, new_version, opportunity_type,
                proposer_reasoning, deployed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (imp_id, skill_name, old_ver, new_ver, opp_type, reasoning, now),
        )
        conn.commit()
    except Exception as e:
        logger.warning("Deployer: failed to log improvement: %s", e)
