"""
Optimizer tools for the Planner and Finalizer subagents.
Loaded into the webAgent's toolset when the user is chatting in an optimizer session.
"""

from __future__ import annotations

import json, logging, sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


async def run_worker_trials(changes_json: str, user_id: str, session_id: str) -> str:
    """Run worker trials against proposed changes and return results as JSON string."""
    from app.optimizer.worker import run_trials

    changes = json.loads(changes_json)

    # Get transcript from current optimizer session for context
    db = sqlite3.connect("app/db/local.db")
    try:
        rows = db.execute(
            "SELECT content FROM interactions WHERE session_id=? ORDER BY created_at ASC",
            (session_id,),
        ).fetchall()
        transcript = [r[0] for r in rows[-25:]]
    finally:
        db.close()

    trials = await run_trials(user_id, changes, transcript, trials_per_change=2)
    return json.dumps(trials, indent=2, default=str)


async def handoff_to_finalizer(summary: str, user_id: str, session_id: str) -> str:
    """Hand off the optimization to the Finalizer agent for review.
    Sets session metadata so future messages in this session route to the Finalizer agent.
    """
    db = sqlite3.connect("app/db/local.db")
    try:
        meta_row = db.execute(
            "SELECT metadata FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        metadata = json.loads(meta_row[0]) if meta_row and meta_row[0] else {}
        metadata["opt_role"] = "finalizer"
        metadata["planner_summary"] = summary
        db.execute(
            "UPDATE sessions SET metadata=? WHERE id=?",
            (json.dumps(metadata), session_id),
        )
        db.commit()
    finally:
        db.close()
    return f"Handed off to Finalizer. Summary: {summary}"


async def deploy_optimization(changes_json: str, user_id: str, session_id: str) -> str:
    """Deploy approved optimization changes to the target user's agent."""
    import uuid

    changes = json.loads(changes_json)
    db = sqlite3.connect("app/db/local.db")
    c = db.cursor()

    deployed = []
    for ch in changes:
        element = ch.get("element", "")
        new_content = ch.get("new_content", "")
        element_type = ch.get("element_type", "system_prompt")

        if element_type == "system_prompt":
            cur = c.execute("SELECT system_prompt FROM agents WHERE user_id=? LIMIT 1", (user_id,))
            row = cur.fetchone()
            old = row[0] if row else ""
            new_full = old + "\n\n" + new_content if old else new_content
            c.execute("UPDATE agents SET system_prompt=?, updated_at=datetime('now') WHERE user_id=?", (new_full, user_id))
            deployed.append(f"Updated system_prompt: {element}")
        elif element_type == "context_document":
            agent = c.execute("SELECT id FROM agents WHERE user_id=? LIMIT 1", (user_id,)).fetchone()
            if agent:
                existing = c.execute("SELECT id FROM context_documents WHERE agent_id=? AND title=?", (agent[0], element)).fetchone()
                if existing:
                    c.execute("UPDATE context_documents SET content=?, updated_at=datetime('now') WHERE id=?", (new_content, existing[0]))
                else:
                    c.execute("INSERT INTO context_documents (id,agent_id,context_type,title,content,tags,created_at,updated_at) VALUES (?,(SELECT id FROM agents WHERE user_id=? LIMIT 1),'skills',?,?,'[]',datetime('now'),datetime('now'))",
                              (str(uuid.uuid4()), user_id, element, new_content))
                deployed.append(f"Updated context document: {element}")

    c.execute("INSERT INTO skill_improvements (id,skill_id,old_version,new_version,opportunity_type,proposer_reasoning,deployed_at) VALUES (?,?,?,?,?,?,datetime('now'))",
              (str(uuid.uuid4()), 'optimizer', '1', '2', 'optimizer', 'Deployed by optimizer Finalizer'))

    db.commit()
    db.close()
    return f"Deployed {len(deployed)} changes: " + "; ".join(deployed)