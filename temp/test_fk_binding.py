"""
Comprehensive test: agent FK binding fix for webAgent optimizer sessions.

Tests:
1. Normal sessions get a webAgent (default, no template_id)
2. Optimizer sessions get bound to opt_planner agent via sessions.agent_id FK
3. FK binding is stable and fetchable by agent_id
4. No cross-contamination between normal and optimizer sessions
5. Optimizer agent has correct system prompt (Optimizer Planner, NOT webAgent)
6. Optimizer agent has NO context_documents (zero rows)
"""

import json
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime

import httpx

BASE_URL = "http://127.0.0.1:8080"
DB_PATH = "C:/Users/Alex R/Projects/webAgent/app/db/local.db"

passed = 0
failed = 0
errors = []


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def safe_print(text):
    """Print with unicode fallback for Windows cp1252."""
    try:
        print(text)
    except UnicodeEncodeError:
        cleaned = text.encode('ascii', errors='replace').decode('ascii')
        print(cleaned)


def report(step_name, status, details=""):
    global passed, failed
    icon = "PASS" if status == "PASS" else "FAIL"
    if status == "PASS":
        passed += 1
    else:
        failed += 1
    safe_print(f"  [{icon}] Step {step_name}")
    if details:
        for line in details.strip().split("\n"):
            safe_print(f"         {line}")
    safe_print("")


def fail_step(step, msg):
    report(step, "FAIL", msg)
    errors.append((step, msg))


def pass_step(step, msg=""):
    report(step, "PASS", msg)


async def chat(user_id, session_id, message, max_retries=5):
    for attempt in range(max_retries):
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                f"{BASE_URL}/api/v1/chat",
                json={"user_id": user_id, "session_id": session_id, "message": message},
            )
            data = resp.json()
            if resp.status_code != 500 or "database is locked" not in str(data):
                return {"status": resp.status_code, "data": data}
            safe_print(f"  DB locked, retrying chat in 2s (attempt {attempt+1}/{max_retries})...")
            time.sleep(2)
    return {"status": resp.status_code, "data": data}


def db_query(sql, params=()):
    conn = db_conn()
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def db_query_one(sql, params=()):
    rows = db_query(sql, params)
    return rows[0] if rows else None


def extract_opt_sid(reply):
    m = re.search(r"`(optimizer-[a-f0-9]+)`", reply)
    return m.group(1) if m else None


async def main():
    global passed, failed, errors
    safe_print("=" * 70)
    safe_print("  webAgent FK Binding Test Suite")
    safe_print("  Started: " + datetime.now().isoformat())
    safe_print("  DB: " + DB_PATH)
    safe_print("  Server: " + BASE_URL)
    safe_print("=" * 70)
    safe_print("")

    # Clean up any leftover test data from prior runs
    safe_print("[Setup] Cleaning prior test data...")
    conn = db_conn()
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("DELETE FROM interactions WHERE session_id IN (SELECT id FROM sessions WHERE user_id='test_user')")
    conn.execute("DELETE FROM context_documents WHERE agent_id IN (SELECT id FROM agents WHERE user_id IN ('test_user', 'opt_planner_test_user'))")
    conn.execute("DELETE FROM agents WHERE user_id IN ('test_user', 'opt_planner_test_user')")
    conn.execute("DELETE FROM sessions WHERE user_id='test_user'")
    conn.execute("DELETE FROM interactions WHERE session_id LIKE 'optimizer-%'")
    conn.execute("DELETE FROM sessions WHERE id LIKE 'optimizer-%'")
    conn.commit()
    conn.close()
    safe_print("[Setup] Done.\n")

    # ============================================================================
    # STEP 1: Basic chat
    # ============================================================================
    safe_print("-" * 70)
    safe_print("STEP 1: Basic chat (create session, bound to default webAgent)")
    safe_print("-" * 70)

    resp = await chat("test_user", "test-fk-1", "hello")
    if resp["status"] != 200:
        fail_step("1", "Expected 200, got " + str(resp["status"]) + ". Body: " + str(resp["data"]))
    elif not resp["data"].get("reply"):
        fail_step("1", "Reply is empty. Response: " + str(resp["data"]))
    else:
        reply_preview = resp["data"]["reply"][:80]
        pass_step("1", "Status=" + str(resp["status"]) + ", reply=" + reply_preview)

    # DB: sessions.agent_id is SET (bound to default agent)
    s = db_query_one("SELECT id, agent_id FROM sessions WHERE id='test-fk-1'")
    if not s:
        fail_step("1a", "Session test-fk-1 not found in DB")
    elif not s["agent_id"]:
        fail_step("1a", "sessions.agent_id IS NULL -- session NOT bound to an agent")
    else:
        pass_step("1a", "session.agent_id = " + str(s["agent_id"]) + " (bound)")

    # DB: agents row for test_user has NO template_id (default webAgent)
    a = db_query_one("SELECT id, user_id, template_id, substr(system_prompt,1,60) as prompt FROM agents WHERE user_id='test_user'")
    if not a:
        fail_step("1b", "No agents row for test_user")
    elif a["template_id"] is not None:
        fail_step("1b", "Agent has template_id=" + str(a["template_id"]) + ", expected None (default)")
    else:
        pass_step("1b", "Agent " + str(a["id"]) + ": no template_id, prompt='" + a["prompt"] + "...'")

    # ============================================================================
    # STEP 2: Trigger optimizer
    # ============================================================================
    safe_print("")
    safe_print("-" * 70)
    safe_print("STEP 2: Trigger optimizer (/optimize command)")
    safe_print("-" * 70)

    resp2 = await chat("test_user", "test-fk-1", "/optimize")
    if resp2["status"] != 200:
        fail_step("2", "Expected 200, got " + str(resp2["status"]) + ". Body: " + str(resp2["data"]))
    else:
        pass_step("2", "Status=" + str(resp2["status"]))

    # Extract optimizer session_id from reply
    reply_text = resp2["data"].get("reply", "")
    opt_sid = extract_opt_sid(reply_text)
    if not opt_sid:
        fail_step("2a", "Could not extract optimizer session id from reply: " + str(reply_text[:200]))
        opt_sid = "optimizer-UNKNOWN"  # placeholder so later steps fail gracefully
    else:
        pass_step("2a", "Optimizer session: " + str(opt_sid))

    # DB: optimizer session EXISTS
    opt_s = db_query_one("SELECT id, agent_id, metadata FROM sessions WHERE id=?", (opt_sid or "__NONE__",))
    if not opt_s:
        fail_step("2b", "Optimizer session " + str(opt_sid) + " not found in sessions table")
    else:
        meta_info = str(opt_s.get("metadata"))
        pass_step("2b", "Session exists. agent_id=" + str(opt_s.get("agent_id")) + ", metadata=" + meta_info)

    # DB: optimizer session.agent_id is None initially (before kickstart)
    if opt_s and opt_s["agent_id"] is None:
        pass_step("2c", "agent_id is None (before kickstart) -- correct")
    elif opt_s and opt_s["agent_id"] is not None:
        fail_step("2c", "agent_id=" + str(opt_s["agent_id"]) + " -- already set before kickstart (unexpected)")
    else:
        fail_step("2c", "Cannot check agent_id -- session not found")

    # ============================================================================
    # STEP 3: Wait for kickstart (30 seconds)
    # ============================================================================
    safe_print("")
    safe_print("-" * 70)
    safe_print("STEP 3: Wait for kickstart (30 seconds)")
    safe_print("-" * 70)

    safe_print("  Waiting 30 seconds for planner kickstart to complete...")
    bound_after = None
    for i in range(30):
        time.sleep(1)
        opt_s_now = db_query_one("SELECT agent_id FROM sessions WHERE id=?", (opt_sid,))
        if opt_s_now and opt_s_now["agent_id"] is not None:
            bound_after = i + 1
            safe_print("  Agent bound after ~" + str(i+1) + "s")
            break
    if bound_after is None:
        safe_print("  Timed out waiting for agent binding")
    safe_print("")

    # DB: optimizer session now has agent_id set
    opt_s_final = db_query_one("SELECT id, agent_id, metadata FROM sessions WHERE id=?", (opt_sid,))
    if not opt_s_final:
        fail_step("3", "Session " + opt_sid + " disappeared!")
    elif not opt_s_final["agent_id"]:
        fail_step("3", "agent_id still None after 30s. Kickstart may have failed.")
    else:
        pass_step("3", "Session " + opt_sid + ": agent_id=" + opt_s_final["agent_id"])

    # DB: agent_id points to agent with user_id LIKE 'opt_planner_%'
    planner_agent = db_query_one(
        "SELECT id, user_id, template_id, status, substr(system_prompt,1,80) as prompt_prefix FROM agents WHERE id=?",
        (opt_s_final["agent_id"],)
    )
    if not planner_agent:
        fail_step("3a", "No agent found with id=" + str(opt_s_final["agent_id"]))
    elif not planner_agent["user_id"].startswith("opt_planner_"):
        fail_step("3a", "Agent user_id='" + planner_agent["user_id"] + "', expected to start with 'opt_planner_'")
    else:
        pass_step("3a", "Agent user_id='" + planner_agent["user_id"] + "' (correct prefix)")

    # DB: that agent has template_id='opt_planner'
    if planner_agent and planner_agent.get("template_id") != "opt_planner":
        fail_step("3b", "Agent template_id='" + str(planner_agent.get("template_id")) + "', expected 'opt_planner'")
    elif planner_agent:
        pass_step("3b", "template_id='opt_planner' (correct)")

    # DB: that agent has system_prompt starting with 'Optimizer Planner'
    if planner_agent:
        prompt = planner_agent.get("prompt_prefix", "")
        if "Optimizer Planner" in prompt:
            pass_step("3c", "System prompt starts with correct Optimizer Planner directive")
        else:
            fail_step("3c", "System prompt: '" + prompt + "', expected to contain 'Optimizer Planner'")

    # ============================================================================
    # STEP 4: Check system prompt in optimizer messages
    # ============================================================================
    safe_print("")
    safe_print("-" * 70)
    safe_print("STEP 4: Check system prompt in optimizer messages")
    safe_print("-" * 70)

    # Poll up to 15s for an assistant interaction with 'input' column
    assistant_input = None
    for attempt in range(15):
        opt_msgs = db_query(
            "SELECT id, session_id, role, input, content, created_at FROM interactions WHERE session_id=? ORDER BY created_at ASC LIMIT 20",
            (opt_sid,)
        )
        if opt_msgs:
            safe_print("  Found " + str(len(opt_msgs)) + " interactions (attempt " + str(attempt+1) + ")")
            for m in opt_msgs:
                if m["role"] == "assistant" and m["input"]:
                    try:
                        parsed = json.loads(m["input"])
                        assistant_input = parsed
                        break
                    except (json.JSONDecodeError, TypeError):
                        continue
            if assistant_input:
                break
        time.sleep(1)

    if not assistant_input:
        fail_step("4a", "No assistant interaction with parseable 'input' found after 15s")
        for m in opt_msgs or []:
            safe_print("         role=" + str(m["role"]) + ", input_len=" + str(len(m.get("input") or "")) + ", content_len=" + str(len(m.get("content") or "")))
    else:
        pass_step("4", "Found assistant interaction with 'input' column")

        # input is a JSON array of messages (not a dict with 'messages' key)
        msgs = assistant_input
        if isinstance(msgs, str):
            msgs = [{"role": "system", "content": msgs}]

        system_msgs = [m for m in msgs if isinstance(m, dict) and m.get("role") == "system"]
        if not system_msgs:
            fail_step("4b", "No system message found in input list. msg count=" + str(len(msgs) if msgs else 0))
        else:
            sys_content = system_msgs[0].get("content", "")
            pass_step("4b", "System message found (" + str(len(sys_content)) + " chars)")

            # Verify '**webAgent**' is NOT in system prompt
            if "**webAgent**" in sys_content:
                fail_step("4c", "System prompt contains '**webAgent**' -- WRONG agent prompt!")
                safe_print("         First 300 chars: " + sys_content[:300])
            else:
                pass_step("4c", "'**webAgent**' NOT in system prompt (correct)")

            # Verify '**Optimizer Planner**' IS in system prompt
            if "**Optimizer Planner**" not in sys_content:
                fail_step("4d", "System prompt does NOT contain '**Optimizer Planner**' -- WRONG!")
                safe_print("         First 300 chars: " + sys_content[:300])
            else:
                pass_step("4d", "'**Optimizer Planner**' IS in system prompt (correct)")

            # Verify 'BOOTSTRAP TOOLS' section exists
            if "BOOTSTRAP TOOLS" not in sys_content:
                fail_step("4e", "System prompt missing 'BOOTSTRAP TOOLS' section")
                safe_print("         First 300 chars: " + sys_content[:300])
            else:
                pass_step("4e", "'BOOTSTRAP TOOLS' section exists (correct)")

    # ============================================================================
    # STEP 5: Check NO webAgent context docs for planner agent
    # ============================================================================
    safe_print("")
    safe_print("-" * 70)
    safe_print("STEP 5: Check NO webAgent context docs for planner agent")
    safe_print("-" * 70)

    if planner_agent:
        ctx_docs = db_query(
            "SELECT id, context_type, title FROM context_documents WHERE agent_id=?",
            (planner_agent["id"],)
        )
        if len(ctx_docs) > 0:
            doc_list = [(d["context_type"], d["title"]) for d in ctx_docs]
            fail_step("5", "Planner agent has " + str(len(ctx_docs)) + " context_documents (expected 0). Docs: " + str(doc_list))
        else:
            pass_step("5", "Planner agent has 0 context_documents (correct)")

    # ============================================================================
    # STEP 6: Check session FK binding is stable
    # ============================================================================
    safe_print("")
    safe_print("-" * 70)
    safe_print("STEP 6: Check session FK binding is stable")
    safe_print("-" * 70)

    s_fk = db_query_one("SELECT agent_id FROM sessions WHERE id=?", (opt_sid,))
    if not s_fk:
        fail_step("6", "Session " + opt_sid + " not found")
    elif s_fk["agent_id"] != planner_agent["id"]:
        fail_step("6", "FK mismatch: session.agent_id=" + str(s_fk["agent_id"]) + ", planner.id=" + str(planner_agent["id"]))
    else:
        pass_step("6", "FK stable: session.agent_id=" + s_fk["agent_id"] + " == agent.id=" + planner_agent["id"])

    # fetch_agent_by_id_with_context returns the planner agent with planner prompt
    agent_by_id = db_query_one(
        "SELECT id, user_id, template_id, substr(system_prompt,1,80) as prompt, status FROM agents WHERE id=?",
        (s_fk["agent_id"],)
    )
    if not agent_by_id:
        fail_step("6a", "Agent not found by id=" + str(s_fk["agent_id"]))
    elif agent_by_id["template_id"] != "opt_planner":
        fail_step("6a", "Agent template_id='" + str(agent_by_id["template_id"]) + "', expected 'opt_planner'")
    else:
        pass_step("6a", "Agent by ID returns correct template_id='opt_planner'")

    # ============================================================================
    # STEP 7: Verify webAgent still works in normal session
    # ============================================================================
    safe_print("")
    safe_print("-" * 70)
    safe_print("STEP 7: Verify webAgent still works in normal session (test-fk-2)")
    safe_print("-" * 70)

    resp7 = await chat("test_user", "test-fk-2", "what time is it?")
    if resp7["status"] != 200:
        fail_step("7", "Expected 200, got " + str(resp7["status"]) + ". Body: " + str(resp7["data"]))
    elif not resp7["data"].get("reply"):
        fail_step("7", "Reply empty. Response: " + str(resp7["data"]))
    else:
        reply7_preview = resp7["data"]["reply"][:80]
        pass_step("7", "Status=" + str(resp7["status"]) + ", reply=" + reply7_preview)

    # DB: sessions.agent_id is set (different agent from optimizer)
    s7 = db_query_one("SELECT agent_id FROM sessions WHERE id='test-fk-2'")
    if not s7:
        fail_step("7a", "Session test-fk-2 not found")
    elif not s7["agent_id"]:
        fail_step("7a", "session.agent_id IS NULL for normal session")
    else:
        pass_step("7a", "session.agent_id = " + s7["agent_id"] + " (bound)")

    # DB: the agent has NO template_id (default webAgent)
    a7 = db_query_one("SELECT id, template_id FROM agents WHERE id=?", (s7["agent_id"],))
    if not a7:
        fail_step("7b", "Agent " + str(s7["agent_id"]) + " not found")
    elif a7["template_id"] is not None:
        fail_step("7b", "Agent has template_id='" + str(a7["template_id"]) + "', expected None (default)")
    else:
        pass_step("7b", "Agent has no template_id (default webAgent -- correct)")

    # ============================================================================
    # STEP 8: Verify cross-session isolation
    # ============================================================================
    safe_print("")
    safe_print("-" * 70)
    safe_print("STEP 8: Cross-session isolation check")
    safe_print("-" * 70)

    normal_aid = s7["agent_id"]
    opt_aid = s_fk["agent_id"]

    if normal_aid == opt_aid:
        fail_step("8", "NORMAL session agent=" + normal_aid + " == OPTIMIZER agent=" + opt_aid + " -- SAME AGENT (isolation FAIL)")
    else:
        pass_step("8", "Normal agent=" + normal_aid + " != Optimizer agent=" + opt_aid + " -- completely different (isolation OK)")

    # ============================================================================
    # Results
    # ============================================================================
    safe_print("")
    safe_print("=" * 70)
    safe_print("  RESULTS: " + str(passed) + " passed, " + str(failed) + " failed, " + str(len(errors)) + " errors")
    safe_print("=" * 70)

    if errors:
        safe_print("\n  FAILURES:")
        for step, msg in errors:
            safe_print("    Step " + step + ": " + msg)
        sys.exit(1)
    else:
        safe_print("\n  All tests passed!")
        sys.exit(0)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
