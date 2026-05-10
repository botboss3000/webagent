"""
Analyzer phase — aggressively finds optimization opportunities.
After every interaction, flags every skill used in the current session
for potential improvement. Low thresholds (intensity 5).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.db import get_db

logger = logging.getLogger(__name__)


async def analyze_skills(
    user_id: str,
    session_id: str,
    min_sample: int = 1,
    target_metrics: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Aggressive analysis: flag every skill touched in this session.
    Even skills with no failures get reviewed for verbosity, conciseness,
    and clarity. Low sample thresholds mean single-use skills get optimized.
    """
    db = get_db()
    opportunities: List[Dict[str, Any]] = []

    try:
        # ── Always scan ALL skills in DB (not just current session) ──
        conn = None
        try:
            raw = getattr(db, '_get_conn', None)
            if raw:
                conn = raw()

                # Scan all executable skills for this user
                all_skills = conn.execute(
                    """SELECT s.name, s.description,
                              COUNT(se.id) as exec_count,
                              SUM(CASE WHEN se.success=1 THEN 1 ELSE 0 END) as ok_count
                       FROM skills s
                       LEFT JOIN skill_executions se ON se.skill_id = s.id AND se.user_id = ?
                       WHERE s.user_id = ? AND s.is_active = 1
                       GROUP BY s.id""",
                    (user_id, user_id),
                ).fetchall()

                for name, desc, exec_count, ok_count in all_skills:
                    if not name:
                        continue
                    total_exec = exec_count or 0
                    success_count = ok_count or 0

                    # Always flag — even with 0 executions (new skill, review it)
                    issue_parts = []
                    priority = 40

                    if total_exec == 0:
                        issue_parts.append("never used — review for correctness")
                        priority = 25
                    else:
                        failure_rate = (1 - success_count / total_exec) * 100 if total_exec > 0 else 0
                        if failure_rate > 0:
                            issue_parts.append(f"{failure_rate:.0f}% failure ({success_count}/{total_exec})")
                            priority += int(failure_rate * 1.5)
                        if total_exec >= 3 and failure_rate == 0:
                            # Perfect record — still review for improvement
                            issue_parts.append(f"{total_exec} successful — review for conciseness")
                            priority = 20

                    opportunities.append({
                        "skill_name": name,
                        "category": "tool",
                        "issue_type": "review",
                        "issue_desc": "; ".join(issue_parts) if issue_parts else "review for conciseness",
                        "expected_gain": "Improve reliability, reduce tokens, sharpen instructions.",
                        "priority": priority,
                    })

        except Exception as e:
            logger.debug("Analyzer: DB query issue: %s", e)
        finally:
            if conn and hasattr(conn, 'close'):
                conn.close()

        # ── Also check context_document skills (behavioral) ──
        try:
            raw = getattr(db, '_get_conn', None)
            if raw:
                conn = raw()
                # Get agent_id for this user
                agent_row = conn.execute(
                    "SELECT id FROM agents WHERE user_id = ? LIMIT 1",
                    (user_id,),
                ).fetchone()
                if agent_row:
                    agent_id = agent_row[0]
                    skill_docs = conn.execute(
                        """SELECT title, content FROM context_documents
                           WHERE context_type = 'skills' AND agent_id = ?""",
                        (agent_id,),
                    ).fetchall()
                    for title, content in skill_docs:
                        if not title or not content:
                            continue
                        token_est = len(content.split())
                        if token_est > 60:  # > 60 words = verbose
                            opportunities.append({
                                "skill_name": title,
                                "category": "behavioral",
                                "issue_type": "verbose",
                                "issue_desc": f"~{token_est} words — candidate for trimming",
                                "expected_gain": "Shorten instructions. Fewer tokens per system prompt.",
                                "priority": 40 + min(token_est // 10, 30),
                                "turn_count": 0,
                            })
                        else:
                            # Always flag for review even if short
                            opportunities.append({
                                "skill_name": title,
                                "category": "behavioral",
                                "issue_type": "review",
                                "issue_desc": "Review for clarity and effectiveness",
                                "expected_gain": "Sharpen instructions. Ensure agent follows them correctly.",
                                "priority": 25,
                                "turn_count": 0,
                            })
                if conn:
                    conn.close()
        except Exception as e:
            logger.debug("Analyzer: behavioral scan issue: %s", e)

    except Exception as e:
        logger.warning("Analyzer: error scanning skills: %s", e)

    opportunities.sort(key=lambda o: o["priority"], reverse=True)
    return opportunities[:8]  # top 8
