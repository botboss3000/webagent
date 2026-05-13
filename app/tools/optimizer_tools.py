"""
Optimizer tools for the Planner and Finalizer subagents.
Loaded into the webAgent's toolset when the user is chatting in an optimizer session.
"""

from __future__ import annotations

import asyncio, json, logging, sqlite3, sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


async def run_worker_trials(changes_json: str, user_id: str, session_id: str) -> str:
    """
    Test proposed changes by creating per-worker temp DB files and calling LLM directly.
    No httpx POST to self — no lock contention on local.db.

    For each change:
    1. Create temp DB (app/db/test_<uuid>.db)
    2. Initialize schema via SCHEMA_SQL
    3. Copy real agent + context docs from local.db (read-only)
    4. Create session in temp db
    5. Build system prompt with build_system_prompt()
    6. Make direct LLM call via OpenRouter client (same as main loop)
    7. Store response in temp db
    8. Calculate metrics

    Returns JSON string with results for all changes.
    Each result includes "test_db_path" so Finalizer can read trial data.
    """
    import json, uuid, time, os, re, sqlite3, logging
    import traceback as _tb
    from datetime import datetime, timezone

    try:
        changes = json.loads(changes_json) if isinstance(changes_json, str) else changes_json
    except Exception as e:
        logging.error(f"run_worker_trials JSON parse: {e}\n{_tb.format_exc()}")
        return json.dumps({"status": "error", "message": f"JSON parse: {e}"})
    logging.warning(f"run_worker_trials: {len(changes) if isinstance(changes, list) else 0} changes, user={user_id}, session={session_id}")
    if not isinstance(changes, list):
        changes = [changes]

    # Resolve real user_id from optimizer agent user_id (opt_role_realuser -> realuser)
    real_user_id = user_id
    if user_id.startswith('opt_'):
        parts = user_id.split('_', 2)
        if len(parts) == 3:
            real_user_id = parts[2]
    logging.warning(f"run_worker_trials: real_user_id={real_user_id}")

    # Determine paths (app/tools/optimizer_tools.py -> app/db/)
    _here = os.path.dirname(os.path.abspath(__file__))          # .../app/tools
    _project_root = os.path.normpath(os.path.join(_here, "..", ".."))  # project root
    _db_dir = os.path.normpath(os.path.join(_here, "..", "db"))       # .../app/db
    _local_path = os.path.join(_db_dir, "local.db")

    # ── Read from local.db (read-only connection) ──
    _local_conn = sqlite3.connect(_local_path)
    _local_conn.execute("PRAGMA journal_mode=WAL")
    _local_conn.execute("PRAGMA busy_timeout=10000")

    try:
        # Read original user message from optimizer:init interaction
        original_message = "what time is it in Detroit?"
        init_row = _local_conn.execute(
            "SELECT content FROM interactions WHERE session_id=? AND source='optimizer:init' ORDER BY created_at ASC LIMIT 1",
            (session_id,),
        ).fetchone()
        if init_row:
            content = init_row[0]
            user_msgs = re.findall(r'\[user\]\s*(.+)', content)
            if user_msgs:
                original_message = user_msgs[0].strip()
        logging.warning(f"run_worker_trials: original_message={original_message}")

        # Read real agent (read-only — no write lock)
        real_agent = _local_conn.execute(
            "SELECT id, system_prompt FROM agents WHERE user_id=? LIMIT 1",
            (real_user_id,)
        ).fetchone()
        if not real_agent:
            return json.dumps({"status": "error", "message": "No real agent found for user"})

        real_agent_id = real_agent[0]
        base_prompt = real_agent[1] or ""

        # Read context documents (read-only — no write lock)
        _context_rows = _local_conn.execute(
            "SELECT context_type, title, content, tags FROM context_documents WHERE agent_id=? ORDER BY context_type, title",
            (real_agent_id,)
        ).fetchall()
    finally:
        _local_conn.close()

    # Build context docs list for prompt builder
    base_context_docs = []
    for ct, title, content, tags in _context_rows:
        try:
            tags_parsed = json.loads(tags) if tags else []
        except (json.JSONDecodeError, TypeError):
            tags_parsed = []
        base_context_docs.append({
            "context_type": ct,
            "title": title,
            "content": content,
            "tags": tags_parsed,
        })

    # ── LLM client setup (same pattern as loop.py _get_client) ──
    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    llm_base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
    llm_api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
    llm_model = os.environ.get("LLM_MODEL") or os.environ.get("OPENROUTER_MODEL") or "deepseek/deepseek-v4-flash"

    _llm_client = AsyncOpenAI(base_url=llm_base_url, api_key=llm_api_key, timeout=60.0)

    from app.agent.prompts import build_system_prompt
    from app.db.local import SCHEMA_SQL
    from app.tools.loader import load_tools

    results = []
    _now_iso = lambda: datetime.now(timezone.utc).isoformat()

    for ci, change in enumerate(changes):
        element = change.get("element", "unknown")
        new_content = change.get("new_content", "")
        element_type = change.get("element_type", "system_prompt")
        trial_id = str(uuid.uuid4())[:8]
        test_uid = f"worker-test-{trial_id}"
        test_agent_id = str(uuid.uuid4())
        temp_db_name = f"test_{uuid.uuid4().hex}.db"
        temp_db_path = os.path.join(_db_dir, temp_db_name)

        trial_entry = None
        temp_conn = None

        try:
            # 1. Create temp DB and init schema
            os.makedirs(_db_dir, exist_ok=True)
            temp_conn = sqlite3.connect(temp_db_path)
            temp_conn.execute("PRAGMA journal_mode=WAL")
            temp_conn.execute("PRAGMA busy_timeout=10000")
            temp_conn.executescript(SCHEMA_SQL)
            now = _now_iso()

            # 2. Build trial prompt: base + proposed change
            trial_prompt = base_prompt
            if element_type == "system_prompt" and new_content:
                trial_prompt += "\n\n" + new_content
            elif element_type == "context_document" and new_content:
                trial_prompt += f"\n\nINSTRUCTION: {new_content}"

            # 3. Insert agent into temp db
            temp_conn.execute(
                "INSERT INTO agents (id, user_id, system_prompt, status, metadata, created_at, updated_at) VALUES (?, ?, ?, 'active', '{}', ?, ?)",
                (test_agent_id, test_uid, trial_prompt, now, now)
            )

            # 4. Copy context documents into temp db
            for ct, title, content, tags in _context_rows:
                temp_conn.execute(
                    "INSERT INTO context_documents (id, agent_id, context_type, title, content, tags, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid.uuid4()), test_agent_id, ct, title, content, tags, now, now)
                )

            # 5. Create session in temp db
            test_session_id = f"trial-{trial_id}"
            temp_conn.execute(
                "INSERT INTO sessions (id, user_id, title, metadata, created_at, updated_at) VALUES (?, ?, ?, '{}', ?, ?)",
                (test_session_id, test_uid, f"Worker trial: {element}", now, now)
            )

            temp_conn.commit()

            # 6. Build system prompt using the app's prompt builder
            system_prompt = await build_system_prompt(
                base_context_docs,
                brain_context=None,
                user_id=test_uid,
                agent_system_prompt=trial_prompt,
            )

            # 7. Store user message in temp db
            user_msg_id = str(uuid.uuid4())
            temp_conn.execute(
                "INSERT INTO interactions (id, session_id, role, content, source, created_at) VALUES (?, ?, 'user', ?, 'worker_trial', ?)",
                (user_msg_id, test_session_id, original_message, now)
            )
            temp_conn.commit()

            # 8. Make direct LLM call (no httpx POST to self)
            start_time = time.time()
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": original_message},
            ]

            # Load tools and build tool_definitions (same pattern as main loop)
            tools = await load_tools(real_user_id)
            tool_definitions = []
            for name, info in tools.items():
                description = info.handler.__doc__ if hasattr(info, 'handler') and info.handler.__doc__ else f"Execute {name}"
                description = description.split("\n")[0]
                tool_definitions.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": info.parameters if hasattr(info, 'parameters') else {"type": "object", "properties": {}, "required": []},
                    },
                })

            response = await _llm_client.chat.completions.create(
                model=llm_model,
                messages=messages,
                tools=tool_definitions if tool_definitions else None,
                tool_choice="auto" if tool_definitions else None,
                temperature=0.0,
                max_tokens=4096,
                stream=False,
            )

            elapsed = time.time() - start_time
            msg = response.choices[0].message if response.choices else None
            reply_text = msg.content or "" if msg else ""
            if msg and msg.tool_calls:
                for tc in msg.tool_calls:
                    reply_text += f"\n[Tool call: {tc.function.name}({tc.function.arguments})]"
            input_tokens = response.usage.prompt_tokens if response.usage else 0
            output_tokens = response.usage.completion_tokens if response.usage else 0
            token_total = (input_tokens or 0) + (output_tokens or 0)

            # 9. Store assistant response in temp db
            asst_msg_id = str(uuid.uuid4())
            temp_conn.execute(
                "INSERT INTO interactions (id, session_id, parent_id, role, content, source, metadata, created_at) VALUES (?, ?, ?, 'assistant', ?, 'worker_trial', ?, ?)",
                (asst_msg_id, test_session_id, user_msg_id, reply_text,
                 json.dumps({"input_tokens": input_tokens, "output_tokens": output_tokens}), now)
            )
            temp_conn.commit()

            # 10. Build result with test_db_path
            success = bool(reply_text and not reply_text.startswith("Error:"))
            confidence = 0.8 if success else 0.2
            rel_db_path = os.path.relpath(temp_db_path, _project_root).replace("\\", "/")

            trial_entry = {
                "element": element,
                "element_type": element_type,
                "new_content": new_content,
                "success": success,
                "reply": reply_text[:300],
                "turn_count": 2,
                "token_estimate": max(token_total, 10),
                "duration_ms": int(elapsed * 1000),
                "confidence": confidence,
                "test_db_path": rel_db_path,
                "trial_transcript": [
                    {"role": "user", "content": original_message[:200]},
                    {"role": "assistant", "content": reply_text[:200]},
                ],
            }

            # Check time format criteria (for the time query test case)
            if "time" in original_message.lower():
                has_24hr = bool(re.search(r'\b(1[3-9]|2[0-3]):[0-5][0-9]\b', reply_text))
                has_pm_am = 'pm' in reply_text.lower() or 'a.m.' in reply_text.lower()
                if has_24hr and not has_pm_am:
                    trial_entry["criteria_met"] = True
                    trial_entry["reasoning"] = "Response uses 24-hour format (no AM/PM)"
                elif has_pm_am:
                    trial_entry["criteria_met"] = False
                    trial_entry["reasoning"] = "Response still uses 12-hour format (AM/PM)"
                else:
                    trial_entry["criteria_met"] = False
                    trial_entry["reasoning"] = "Could not determine time format"

        except Exception as e:
            tb = _tb.format_exc()
            logging.error(f"Worker trial {ci} for {element} crashed: {e}\n{tb}")
            trial_entry = {
                "element": element,
                "error": f"{e}",
                "traceback": tb,
                "confidence": 0.0,
                "success": False,
                "reply": f"Error: {e}",
            }
        finally:
            if temp_conn:
                try:
                    temp_conn.close()
                except Exception:
                    pass

        if trial_entry:
            results.append(trial_entry)

    return json.dumps(results, indent=2)


async def handoff_to_finalizer(summary: str = "", user_id: str = "", session_id: str = "",
                                judging_criteria: str = "", baseline_transcript: str = "",
                                worker_results: str = "") -> str:
    """Hand off the optimization to the Finalizer agent for review.
    Sets session metadata so future messages in this session route to the Finalizer agent.
    Stores judging criteria, baseline transcript, and worker trial results so the
    Finalizer can evaluate without seeing the Planner's conversation.
    """
    db = sqlite3.connect("app/db/local.db")
    try:
        meta_row = db.execute(
            "SELECT metadata FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        metadata = json.loads(meta_row[0]) if meta_row and meta_row[0] else {}
        metadata["opt_role"] = "finalizer"
        metadata["planner_summary"] = summary
        metadata["judging_criteria"] = judging_criteria
        metadata["baseline_transcript"] = baseline_transcript
        metadata["worker_results"] = worker_results

        # Extract test_db_paths from worker_results so Finalizer can read raw trial data
        test_db_paths = []
        try:
            worker_data = json.loads(worker_results) if isinstance(worker_results, str) else worker_results
            if isinstance(worker_data, list):
                for entry in worker_data:
                    if isinstance(entry, dict) and entry.get("test_db_path"):
                        test_db_paths.append(entry["test_db_path"])
        except (json.JSONDecodeError, TypeError):
            pass
        if test_db_paths:
            metadata["test_db_paths"] = test_db_paths

        db.execute(
            "UPDATE sessions SET metadata=? WHERE id=?",
            (json.dumps(metadata), session_id),
        )
        db.commit()
    finally:
        db.close()
    # Auto-kickstart the Finalizer
    try:
        import httpx, os, logging as _log
        port = os.environ.get('PORT', '8080')
        _log.warning(f"Kickstarting Finalizer for session {session_id}")
        asyncio.create_task(_fire_finalizer(user_id, session_id, summary))
    except Exception:
        pass
    return f"Handed off to Finalizer. Summary: {summary}"


def _fire_finalizer(user_id: str, session_id: str, summary: str) -> None:
    """Fire the Finalizer via API to auto-evaluate."""
    import httpx, os, asyncio, logging as _log
    async def _run():
        try:
            port = os.environ.get('PORT', '8080')
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"http://127.0.0.1:{port}/api/v1/chat",
                    json={
                        "message": f"Please evaluate the optimization results and recommend next steps.",
                        "user_id": user_id,
                        "session_id": session_id,
                    },
                )
                _log.warning(f"Kickstart Finalizer responded: {resp.status_code}")
        except Exception as e:
            _log.warning(f"Kickstart Finalizer failed: {e}")
    asyncio.create_task(_run())


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
    deployed = []

    from app.db import get_db
    backend = get_db()
    async with backend._write_lock:
        raw = backend._get_conn()
        c = raw.cursor()

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

        raw.commit()
        raw.close()

    # Cleanup: remove temp trial databases after successful deployment
    import glob as _glob, os as _os
    _here = _os.path.dirname(_os.path.abspath(__file__))
    _db_dir = _os.path.normpath(_os.path.join(_here, "..", "db"))
    for _f in _glob.glob(_os.path.join(_db_dir, "test_*.db")):
        try:
            _os.remove(_f)
        except Exception:
            pass
    for _f in _glob.glob(_os.path.join(_db_dir, "test_*.db-wal")):
        try:
            _os.remove(_f)
        except Exception:
            pass
    for _f in _glob.glob(_os.path.join(_db_dir, "test_*.db-shm")):
        try:
            _os.remove(_f)
        except Exception:
            pass

    return f"Deployed {len(deployed)} changes: " + "; ".join(deployed)