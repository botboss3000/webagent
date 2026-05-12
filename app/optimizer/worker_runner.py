"""
Worker runner — spawned as a subprocess by run_worker_trials tool.
Has its OWN event loop, DB connection, and HTTP client.
No shared state with the parent optimizer process.
"""
import sys, json, os, uuid, time, subprocess

CHANGES = json.loads(sys.argv[1]) if len(sys.argv) > 1 else []
USER_ID = sys.argv[2] if len(sys.argv) > 2 else ""
ORIGINAL_MESSAGE = sys.argv[3] if len(sys.argv) > 3 else "hi"
TIMEOUT_SEC = int(sys.argv[4]) if len(sys.argv) > 4 else 45

TEST_DB = "app/db/tests.db"
MAIN_DB = "app/db/local.db"

def run_test(change):
    element = change.get("element", "unknown")
    new_content = change.get("new_content", "")
    element_type = change.get("element_type", "system_prompt")
    agent_id = str(uuid.uuid4())
    test_user_id = f"wkr-{agent_id[:8]}"
    
    import sqlite3 as sq
    
    # Open separate connections (each subprocess has its own)
    test_db = sq.connect(TEST_DB)
    test_db.execute("PRAGMA journal_mode=WAL")
    main_db = sq.connect(MAIN_DB)
    main_db.execute("PRAGMA journal_mode=WAL")
    
    try:
        # Read the real user's system prompt
        cur = main_db.execute("SELECT system_prompt FROM agents WHERE user_id=? LIMIT 1", (USER_ID,))
        row = cur.fetchone()
        base_prompt = row[0] if row else ""
        
        # Build trial prompt: base + proposed change
        if element_type == "system_prompt" and new_content:
            trial_prompt = base_prompt + "\n\n" + new_content if base_prompt else new_content
        elif element_type == "context_document" and new_content:
            trial_prompt = f"{base_prompt}\n\nIMPORTANT: {new_content}" if base_prompt else new_content
        else:
            trial_prompt = base_prompt or "You are a helpful assistant."
        
        # Create test agent in tests.db
        test_db.execute(
            "INSERT OR IGNORE INTO agents (id,user_id,system_prompt,status,metadata,created_at,updated_at) VALUES (?,?,?,'active','{}',datetime('now'),datetime('now'))",
            (agent_id, test_user_id, trial_prompt)
        )
        
        # Copy context documents from real user to test agent
        real_agent = main_db.execute("SELECT id FROM agents WHERE user_id=? LIMIT 1", (USER_ID,)).fetchone()
        if real_agent:
            docs = main_db.execute("SELECT context_type, title, content, tags FROM context_documents WHERE agent_id=?", (real_agent[0],)).fetchall()
            for ct, title, content, tags in docs:
                existing = test_db.execute("SELECT id FROM context_documents WHERE agent_id=? AND title=?", (agent_id, title)).fetchone()
                if not existing:
                    test_db.execute(
                        "INSERT INTO context_documents (id,agent_id,context_type,title,content,tags,created_at,updated_at) VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))",
                        (str(uuid.uuid4()), agent_id, ct, title, content, tags or '[]')
                    )
        
        # Sync test.db data back to main.db so the webAgent can find the test agent
        # The webAgent reads from local.db, so we need the test agent there too
        main_db.execute(
            "INSERT OR IGNORE INTO agents (id,user_id,system_prompt,status,metadata,created_at,updated_at) VALUES (?,?,?,'active','{}',datetime('now'),datetime('now'))",
            (agent_id, test_user_id, trial_prompt)
        )
        if real_agent:
            docs = main_db.execute("SELECT context_type, title, content, tags FROM context_documents WHERE agent_id=?", (real_agent[0],)).fetchall()
            for ct, title, content, tags in docs:
                existing = main_db.execute("SELECT id FROM context_documents WHERE agent_id=? AND title=?", (agent_id, title)).fetchone()
                if not existing:
                    main_db.execute(
                        "INSERT INTO context_documents (id,agent_id,context_type,title,content,tags,created_at,updated_at) VALUES (?,?,?,?,?,?,datetime('now'),datetime('now'))",
                        (str(uuid.uuid4()), agent_id, ct, title, content, tags or '[]')
                    )
        
        main_db.commit()
        test_db.commit()
        
        # Call the webAgent API via httpx
        import httpx
        start = time.time()
        
        try:
            resp = httpx.post(
                "http://127.0.0.1:8000/api/v1/chat",
                json={"message": ORIGINAL_MESSAGE, "session_id": f"trial-{agent_id[:12]}", "user_id": test_user_id},
                timeout=TIMEOUT_SEC,
            )
            elapsed = time.time() - start
            resp_data = resp.json()
            reply_text = resp_data.get("reply", "")
            success = "Error:" not in reply_text[:100]
        except Exception as e:
            elapsed = time.time() - start
            reply_text = f"WORKER ERROR: {e}"
            success = False
        
        # Count turns from interactions
        turn_count = 0
        token_est = 0
        try:
            rows = main_db.execute(
                "SELECT content FROM interactions WHERE session_id=? ORDER BY created_at ASC",
                (f"trial-{agent_id[:12]}",)
            ).fetchall()
            for row in rows:
                turn_count += 1
                token_est += len(str(row[0])) // 4
        except:
            pass
        
        # Read full trial transcript before cleanup
        trial_transcript = []
        try:
            rows = main_db.execute(
                "SELECT role, content FROM interactions WHERE session_id=? ORDER BY created_at ASC",
                (f"trial-{agent_id[:12]}",)
            ).fetchall()
            for row in rows:
                trial_transcript.append({"role": row[0], "content": (row[1] or "")[:300]})
        except:
            pass

        # Cleanup test agent and session from both DBs
        for db in [main_db, test_db]:
            try:
                db.execute("DELETE FROM agents WHERE id=?", (agent_id,))
                db.execute("DELETE FROM context_documents WHERE agent_id=?", (agent_id,))
                db.execute("DELETE FROM interactions WHERE session_id=?", (f"trial-{agent_id[:12]}",))
                db.execute("DELETE FROM sessions WHERE id=?", (f"trial-{agent_id[:12]}",))
                db.commit()
            except:
                pass

        confidence = 0.8 if success else 0.2
        return {
            "element": element,
            "message": f"Worker tested change to {element}. Success={success}. Reply: {reply_text[:100]}...",
            "estimated_turns": max(turn_count, 1),
            "estimated_tokens": max(token_est, 10),
            "estimated_time_ms": int(elapsed * 1000),
            "success_likely": success,
            "confidence": confidence,
            "trial_transcript": trial_transcript[:10],
            "reasoning": f"Live webAgent test: {turn_count} turns, {token_est} tokens, {int(elapsed*1000)}ms",
        }
    finally:
        test_db.close()
        main_db.close()

# Run tests sequentially (one per change)
all_results = []
for change in CHANGES:
    result = run_test(change)
    all_results.append(result)

print(json.dumps(all_results, indent=2))