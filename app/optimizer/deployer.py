"""
Deployer phase — writes improved skill versions to the database.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.db import get_db

logger = logging.getLogger(__name__)


async def deploy_improvements(
    user_id: str,
    session_id: str,
    proposals: List[Dict[str, Any]],
    optimizer_session_id: str,
) -> int:
    """
    Write approved proposals to the skills table and log improvements.

    Returns number of skills deployed.
    """
    db = get_db()
    deployed = 0

    for proposal in proposals:
        if not proposal.get("improved"):
            continue

        skill_name = proposal.get("skill_name", "")
        new_content = proposal.get("new_content", "")
        reasoning = proposal.get("reasoning", "")

        if not skill_name or not new_content:
            continue

        try:
            # ── Update skill in context_documents (behavioral) ──
            # For behavioral skills, we update context_documents with context_type="skills"
            conn = None
            try:
                raw = getattr(db, '_get_conn', None)
                if raw:
                    conn = raw()
                    # Check if skill exists as a context document
                    existing = conn.execute(
                        """SELECT id, content FROM context_documents
                           WHERE context_type = 'skills' AND title = ?""",
                        (skill_name,),
                    ).fetchone()

                    # Get agent_id from user_id
                    agent_id = None
                    agent_row = conn.execute(
                        "SELECT id FROM agents WHERE user_id = ? LIMIT 1",
                        (user_id,),
                    ).fetchone()
                    if agent_row:
                        agent_id = agent_row[0]

                    if existing:
                        old_content = existing[1]
                        doc_id = existing[0]
                        conn.execute(
                            """UPDATE context_documents
                               SET content = ?, updated_at = datetime('now')
                               WHERE id = ?""",
                            (new_content, doc_id),
                        )
                        conn.commit()
                        deployed += 1
                        logger.info("Deployer: updated behavioral skill '%s' (%s)", skill_name, doc_id)
                    else:
                        # Insert as new behavioral skill
                        doc_id = str(uuid.uuid4())
                        now = datetime.now(timezone.utc).isoformat()
                        conn.execute(
                            """INSERT INTO context_documents
                               (id, agent_id, context_type, title, content, tags, created_at, updated_at)
                               VALUES (?, ?, 'skills', ?, ?, '[]', ?, ?)""",
                            (doc_id, agent_id or user_id, skill_name, new_content, now, now),
                        )
                        conn.commit()
                        deployed += 1
                        logger.info("Deployer: created behavioral skill '%s' (%s)", skill_name, doc_id)

                    # ── Log the improvement ──
                    _log_improvement(conn, skill_name, "1", "2",
                                     "behavioral", reasoning, optimizer_session_id)

            except Exception as e:
                logger.error("Deployer: failed to deploy %s: %s", skill_name, e)
            finally:
                if conn and hasattr(conn, 'close'):
                    conn.close()

        except Exception as e:
            logger.error("Deployer: unexpected error for %s: %s", skill_name, e)

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
