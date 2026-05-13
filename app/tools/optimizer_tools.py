"""
Optimizer tools for the Planner and Finalizer subagents.
Loaded into the webAgent's toolset when the user is chatting in an optimizer session.
"""

from __future__ import annotations

import json, logging, sqlite3, sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


async def run_worker_trials(changes_json: str, user_id: str, session_id: str) -> str:
    """Run worker trials as isolated subprocesses. Each worker spawns its own
    Python process with its own event loop, DB connection, and HTTP client.
    No deadlock because the subprocess is fully independent from the parent.
    """
    import json, os, asyncio
    
    changes = json.loads(changes_json) if isinstance(changes_json, str) else changes_json
    if not isinstance(changes, list):
        changes = [changes]
    
    # Get the original user message from the optimizer session
    import sqlite3
    db = sqlite3.connect("app/db/local.db")
    try:
        sys_rows = db.execute(
            "SELECT content FROM interactions WHERE session_id=? AND source='optimizer:init' ORDER BY created_at ASC LIMIT 1",
            (session_id,),
        ).fetchall()
        original_message = "hi"
        for r in sys_rows:
            content = r[0]
            if "[user]" in content:
                for line in content.split('\\n'):
                    if line.strip().startswith('[user]'):
                        original_message = line.split(']', 1)[-1].strip()
                        break
    finally:
        db.close()
    
    # Spawn subprocesses for each change (runs in parallel)
    results = []
    for change in changes:
        changes_arg = json.dumps([change])
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "app/optimizer/worker_runner.py",
                changes_arg, user_id, original_message, "120",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=130.0)
            if proc.returncode == 0 and stdout:
                trial_results = json.loads(stdout.decode())
                results.extend(trial_results)
            else:
                error_msg = stderr.decode()[:200] if stderr else "process failed"
                results.append({
                    "element": change.get("element", "unknown"),
                    "message": f"Worker subprocess failed: {error_msg}",
                    "estimated_turns": 99, "estimated_tokens": 999,
                    "estimated_time_ms": 99999, "success_likely": False,
                    "confidence": 0.0, "reasoning": error_msg
                })
        except asyncio.TimeoutError:
            proc.kill()
            results.append({
                "element": change.get("element", "unknown"),
                "message": "Worker timed out after 130s",
                "estimated_turns": 99,
                "estimated_tokens": 999,
                "estimated_time_ms": 130000,
                "success_likely": False,
                "confidence": 0.0,
                "reasoning": "Worker subprocess timed out"
            })
    
    return json.dumps(results, indent=2)


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

    # Resolve real user_id from optimizer agent user_id (opt_role_realuser -> realuser)
    real_user_id = user_id
    if user_id.startswith('opt_'):
        parts = user_id.split('_', 2)
        if len(parts) == 3:
            real_user_id = parts[2]

    changes = json.loads(changes_json)
    db = sqlite3.connect("app/db/local.db")
    c = db.cursor()

    deployed = []
    for ch in changes:
        element = ch.get("element", "")
        new_content = ch.get("new_content", "")
        element_type = ch.get("element_type", "system_prompt")

        if element_type == "system_prompt":
            cur = c.execute("SELECT system_prompt FROM agents WHERE user_id=? LIMIT 1", (real_user_id,))
            row = cur.fetchone()
            old = row[0] if row else ""
            new_full = old + "\n\n" + new_content if old else new_content
            c.execute("UPDATE agents SET system_prompt=?, updated_at=datetime('now') WHERE user_id=?", (new_full, real_user_id))
            deployed.append(f"Updated system_prompt: {element}")
        elif element_type == "context_document":
            agent = c.execute("SELECT id FROM agents WHERE user_id=? LIMIT 1", (real_user_id,)).fetchone()
            if agent:
                existing = c.execute("SELECT id FROM context_documents WHERE agent_id=? AND title=?", (agent[0], element)).fetchone()
                if existing:
                    c.execute("UPDATE context_documents SET content=?, updated_at=datetime('now') WHERE id=?", (new_content, existing[0]))
                else:
                    c.execute("INSERT INTO context_documents (id,agent_id,context_type,title,content,tags,created_at,updated_at) VALUES (?,(SELECT id FROM agents WHERE user_id=? LIMIT 1),'skills',?,?,'[]',datetime('now'),datetime('now'))",
                              (str(uuid.uuid4()), real_user_id, element, new_content))
                deployed.append(f"Updated context document: {element}")

    c.execute("INSERT INTO skill_improvements (id,skill_id,old_version,new_version,opportunity_type,proposer_reasoning,deployed_at) VALUES (?,?,?,?,?,?,datetime('now'))",
              (str(uuid.uuid4()), 'optimizer', '1', '2', 'optimizer', 'Deployed by optimizer Finalizer'))

    db.commit()
    db.close()
    return f"Deployed {len(deployed)} changes: " + "; ".join(deployed)