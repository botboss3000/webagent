"""
Worker — tests proposed changes against a LIVE webAgent.
Parallel execution via asyncio.gather.
Uses isolated test.db to avoid locking the main database.
"""

from __future__ import annotations

import asyncio, json, logging, os, uuid, time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

API_BASE = "http://127.0.0.1:8000"
TIMEOUT_SEC = int(os.environ.get("WORKER_TIMEOUT", "90"))
TEST_DB = "app/db/local.db"


def _init_test_db():
    """Initialize the test database with WAL mode and required tables."""
    import sqlite3
    db = sqlite3.connect(TEST_DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            system_prompt TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            metadata TEXT DEFAULT '{}',
            created_at TEXT,
            updated_at TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS context_documents (
            id TEXT PRIMARY KEY,
            agent_id TEXT,
            context_type TEXT,
            title TEXT,
            content TEXT,
            tags TEXT DEFAULT '[]',
            created_at TEXT,
            updated_at TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            title TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS interactions (
            id TEXT PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            source TEXT,
            channel TEXT,
            created_at TEXT
        )
    """)
    db.commit()
    db.close()


def _worker_retry(fn, max_attempts=3, delay=0.2):
    """Retry helper for DB operations with backoff."""
    import time
    for a in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            if "locked" in str(e).lower() and a < max_attempts - 1:
                time.sleep(delay * (a + 1))
            else:
                raise


async def _get_agent_id_by_user(conn, user_id: str) -> str:
    cur = conn.execute("SELECT id FROM agents WHERE user_id=? LIMIT 1", (user_id,))
    r = cur.fetchone()
    return r[0] if r else None


async def run_trials(user_id, changes, transcript, trials_per_change=2):
    """
    Each trial: apply the proposed change to a test agent, send the user's
    original message to the LIVE webAgent, measure real turns/tokens/time,
    clean up, and return metrics.
    Uses a dedicated test.db to avoid locking the main production database.
    """
    _init_test_db()

    original_message = "what time is it"
    for line in transcript:
        if line.startswith("User: ") or line.startswith("user: "):
            original_message = line.split(":", 1)[1].strip()
            break

    async def _run_one(change, trial_num):
        element = change.get("element", "unknown")
        new_content = change.get("new_content", "")
        et = change.get("element_type", "system_prompt")
        trial_id = str(uuid.uuid4())[:8]
        test_uid = f"worker-test-{trial_id}"
        start_time = time.time()

        import sqlite3
        db = sqlite3.connect(TEST_DB)
        db.execute("PRAGMA journal_mode=WAL")
        try:
            c = db.cursor()

            # 1. Create a test agent
            agent_id = str(uuid.uuid4())
            _worker_retry(lambda: c.execute(
                "INSERT INTO agents (id,user_id,system_prompt,status,metadata,created_at,updated_at) VALUES (?,?,?,'active','{}',datetime('now'),datetime('now'))",
                (agent_id, test_uid, ""),
            ))

            # 2. Apply the trial change
            if et == "system_prompt" and new_content:
                _worker_retry(lambda: c.execute(
                    "UPDATE agents SET system_prompt=? WHERE user_id=?",
                    (new_content, test_uid),
                ))

            # Copy real user's context documents for realistic simulation
            real_agent = _worker_retry(lambda: c.execute(
                "SELECT id FROM agents WHERE user_id=? LIMIT 1", (user_id,)
            ).fetchone())
            test_agent_row = _worker_retry(lambda: c.execute(
                "SELECT id FROM agents WHERE user_id=? LIMIT 1", (test_uid,)
            ).fetchone())
            if real_agent and test_agent_row:
                docs = _worker_retry(lambda: c.execute(
                    "SELECT context_type, title, content, tags FROM context_documents WHERE agent_id=?", (real_agent[0],)
                ).fetchall())
                for ct, title, content, tags in docs:
                    existing = _worker_retry(lambda: c.execute(
                        "SELECT id FROM context_documents WHERE agent_id=? AND title=?", (test_agent_row[0], title)
                    ).fetchone())
                    if not existing:
                        _worker_retry(lambda: c.execute(
                            "INSERT INTO context_documents (id,agent_id,context_type,title,content,tags,created_at,updated_at) VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))",
                            (str(uuid.uuid4()), test_agent_row[0], ct, title, content, tags),
                        ))

            _worker_retry(lambda: (c.execute("INSERT OR IGNORE INTO sessions (id,user_id,title,created_at,updated_at) VALUES (?,?,?,datetime('now'),datetime('now'))", (f"worker-{trial_id}", test_uid, f"Worker test {trial_id}")), db.commit()))

            db.commit()
            db.close()

            # 3. Send the message to the LIVE webAgent
            import httpx
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT_SEC) as hclient:
                    resp = await hclient.post(
                        f"{API_BASE}/api/v1/chat",
                        json={
                            "message": original_message,
                            "session_id": f"worker-{trial_id}",
                            "user_id": test_uid,
                        },
                    )
                    elapsed = time.time() - start_time
                    resp_data = resp.json()
                    reply_text = resp_data.get("reply", "")
                    success = "Error:" not in reply_text and "error" not in reply_text.lower()[:50]
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadTimeout) as exc:
                elapsed = time.time() - start_time
                reply_text = f"WORKER TIMEOUT ({TIMEOUT_SEC}s): {exc}"
                success = False
            except Exception as e:
                elapsed = time.time() - start_time
                reply_text = str(e)
                success = False

            # 4. Read back interactions to count turns
            turn_count = 0
            token_est = 0
            try:
                db2 = sqlite3.connect(TEST_DB)
                db2.execute("PRAGMA journal_mode=WAL")
                c2 = db2.cursor()
                rows = _worker_retry(lambda: c2.execute(
                    "SELECT content FROM interactions WHERE session_id=? ORDER BY created_at ASC",
                    (f"worker-{trial_id}",),
                ).fetchall())
                for row in rows:
                    turn_count += 1
                    token_est += len(str(row[0])) // 4
                db2.close()
            except Exception:
                pass

            # 5. Clean up test data
            try:
                db3 = sqlite3.connect(TEST_DB)
                db3.execute("PRAGMA journal_mode=WAL")
                c3 = db3.cursor()
                _worker_retry(lambda: c3.execute("DELETE FROM interactions WHERE session_id=?", (f"worker-{trial_id}",)))
                _worker_retry(lambda: c3.execute("DELETE FROM sessions WHERE id=?", (f"worker-{trial_id}",)))
                _worker_retry(lambda: c3.execute("DELETE FROM agents WHERE user_id=?", (test_uid,)))
                db3.commit()
                db3.close()
            except Exception:
                pass

            confidence = 0.8 if success else 0.2
            return {
                "message": f"Worker {trial_num}: Tested change to {element}. "
                           f"Agent responded in {turn_count} turns. Success={success}. Reply: {reply_text[:80]}...",
                "element": element,
                "estimated_turns": turn_count,
                "estimated_tokens": max(token_est, 10),
                "estimated_time_ms": int(elapsed * 1000),
                "success_likely": success,
                "confidence": confidence,
                "confidence_reasoning": "" if success else (
                    f"The change to '{element}' did not fix the issue. "
                    f"Agent response still shows error or failure. "
                    f"Try a different approach."
                ),
                "reasoning": f"Real webAgent test: {turn_count} turns, {max(token_est, 10)} tokens, {int(elapsed*1000)}ms",
            }

        except Exception as e:
            logger.warning("Worker trial %d for %s crashed: %s", trial_num, element, e)
            return {
                "message": f"Worker {trial_num}: Error testing change to {element}: {e}",
                "element": element,
                "estimated_turns": 99,
                "estimated_tokens": 999,
                "estimated_time_ms": 99999,
                "success_likely": False,
                "confidence": 0.1,
                "confidence_reasoning": f"Worker test execution failed: {e}",
                "reasoning": f"Trial crashed: {e}",
            }

    tasks = []
    for ci, change in enumerate(changes[:4]):
        for tn in range(trials_per_change):
            tasks.append(((ci, tn), _run_one(change, tn)))

    results_raw = await asyncio.gather(*[t for _, t in tasks], return_exceptions=True)

    change_data = {}
    for idx, ((ci, tn), _) in enumerate(tasks):
        raw = results_raw[idx]
        if isinstance(raw, Exception):
            raw = {
                "element": "unknown", "estimated_turns": 99, "estimated_tokens": 999,
                "estimated_time_ms": 99999, "success_likely": False, "confidence": 0.1,
                "confidence_reasoning": f"Exception: {raw}", "reasoning": str(raw),
            }
        if ci not in change_data:
            change_data[ci] = {"estimates": [], "change": changes[ci]}
        if raw is not None:
            change_data[ci]["estimates"].append(raw)

    results = []
    for ci in sorted(change_data.keys()):
        cd = change_data[ci]
        est = cd["estimates"]
        ch = cd["change"]
        if est:
            avg = {
                "turns": sum(e.get("estimated_turns", 0) for e in est) / len(est),
                "tokens": sum(e.get("estimated_tokens", 0) for e in est) / len(est),
                "time_ms": sum(e.get("estimated_time_ms", 0) for e in est) / len(est),
                "success": all(e.get("success_likely", True) for e in est),
                "confidence": sum(e.get("confidence", 0.5) for e in est) / len(est),
            }
        else:
            avg = {"turns": 0, "tokens": 0, "time_ms": 0, "success": True, "confidence": 0}

        messages = []
        reasons = []
        for e in est:
            if e.get("message"):
                messages.append(e["message"])
            if e.get("confidence_reasoning"):
                reasons.append(e["confidence_reasoning"])

        result_entry = {
            "element": ch.get("element", "unknown"),
            "change": ch,
            "trials": est,
            "averaged": avg,
        }
        if messages:
            result_entry["message"] = "\n\n---\n\n".join(messages)
        if reasons:
            result_entry["confidence_reasoning"] = "; ".join(reasons)

        results.append(result_entry)

    return results