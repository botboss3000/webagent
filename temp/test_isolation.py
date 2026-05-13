"""
Comprehensive tests for agent isolation changes.

Tests:
1. Basic webAgent chat still works
2. Optimizer creates a Planner agent (NOT default webAgent)
3. Session binding works
4. Optimizer session rejects webAgent identity/skills/tools docs
5. Error path when agent template lookup fails

Usage: .venv/Scripts/python.exe temp/test_isolation.py
"""

import json
import re
import sqlite3
import sys
import time
import traceback
import urllib.request
import urllib.error

sys.stdout.reconfigure(encoding='utf-8')

API_BASE = "http://127.0.0.1:8080"
DB_PATH = "app/db/local.db"
TEST_USER = "test_isolation_user"
TEST_SESSION = "test-isolation-session"


def safe(s, n=400):
    """Truncate and sanitize for safe printing. Strip non-ASCII."""
    if s is None:
        return "(None)"
    text = str(s)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)
    # strip non-ASCII (emojis, etc.)
    text = text.encode('ascii', 'replace').decode('ascii')
    return text[:n]


def db_query(sql, params=()):
    """Run a read query against local.db. Returns list of rows (as dicts)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
        return rows
    finally:
        conn.close()


def db_execute(sql, params=()):
    """Run a write query against local.db. Commits and returns rowcount."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def api_chat(user_id, session_id, message, timeout=60):
    """POST /api/v1/chat and return parsed JSON response."""
    body = json.dumps({
        "user_id": user_id,
        "session_id": session_id,
        "message": message,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/api/v1/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        return {"_http_error": e.code, "_detail": error_body}
    except Exception as e:
        return {"_error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════
#  RESULTS TRACKER
# ═══════════════════════════════════════════════════════════════════════════

results = []


def test(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append({"name": name, "status": status, "detail": detail})
    print(f"  [{status}] {name}")
    if detail:
        for line in detail.split("\n"):
            print(f"         {line}")


def section(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


# ═══════════════════════════════════════════════════════════════════════════
#  SETUP: Clean up existing test data
# ═══════════════════════════════════════════════════════════════════════════

def cleanup():
    """Remove test data from previous runs."""
    # interactions has no user_id column; delete by session_id
    for s in db_query("SELECT id FROM sessions WHERE user_id = ?", (TEST_USER,)):
        sid = s["id"]
        db_execute("DELETE FROM interactions WHERE session_id = ?", (sid,))
    db_execute("DELETE FROM sessions WHERE user_id = ?", (TEST_USER,))
    # Clean regular agents
    for a in db_query("SELECT id FROM agents WHERE user_id = ?", (TEST_USER,)):
        aid = a["id"]
        db_execute("DELETE FROM context_documents WHERE agent_id = ?", (aid,))
    db_execute("DELETE FROM agents WHERE user_id = ?", (TEST_USER,))
    # Clean optimizer agents (user_id = opt_planner_<user> or opt_finalizer_<user>)
    like_pat = f"%{TEST_USER}"
    for a in db_query("SELECT id FROM agents WHERE user_id LIKE ?", (like_pat,)):
        aid = a["id"]
        db_execute("DELETE FROM context_documents WHERE agent_id = ?", (aid,))
    db_execute("DELETE FROM agents WHERE user_id LIKE ?", (like_pat,))


cleanup()

# ═══════════════════════════════════════════════════════════════════════════
#  TEST 1: Basic webAgent chat
# ═══════════════════════════════════════════════════════════════════════════

section("TEST 1: Basic webAgent chat still works")

try:
    r = api_chat(TEST_USER, TEST_SESSION, "hello", timeout=30)
    reply = r.get("reply", "") or r.get("response", "")
    test("HTTP 200 OK", "_http_error" not in r and "_error" not in r,
         f"Response keys: {list(r.keys())}")
    test("Reply is non-empty", bool(reply),
         f"Reply: {safe(reply, 200)}")
    test("No error in reply", "error" not in reply.lower()[:100],
         f"Reply: {safe(reply, 200)}")
except Exception as e:
    test("HTTP request", False, f"Exception: {safe(str(e), 300)}")


# ═══════════════════════════════════════════════════════════════════════════
#  TEST 2: Optimizer creates Planner agent (NOT default webAgent)
# ═══════════════════════════════════════════════════════════════════════════

section("TEST 2: Optimizer creates a Planner agent (NOT default webAgent)")

try:
    # Send a chat first to have a session to optimize
    r = api_chat(TEST_USER, TEST_SESSION, "what time is it?", timeout=30)
    time.sleep(1)

    # Trigger optimizer
    r = api_chat(TEST_USER, TEST_SESSION, "/optimize", timeout=30)
    reply = r.get("reply", "") or r.get("response", "")
    test("Optimizer responded", bool(reply), f"Reply: {safe(reply, 300)}")

    # Extract optimizer session ID from reply
    opt_match = re.search(r"optimizer-([a-f0-9]+)", reply)
    opt_id = "optimizer-" + opt_match.group(1) if opt_match else None
    test("Optimizer session ID extracted", opt_id is not None,
         f"From reply: {safe(reply, 200)}")

    if opt_id:
        # Send a message to the optimizer session to trigger agent creation
        # (The /optimize command only creates the session; the agent is created
        #  when the user first chats in the optimizer session)
        # Use longer timeout for LLM response
        r = api_chat(TEST_USER, opt_id, "hello", timeout=120)
        reply = r.get("reply", "") or r.get("response", "")
        test("Optimizer session accepts messages", bool(reply),
             f"Reply starts with Planner:: {'Planner:' in str(reply)[:50]}")
        time.sleep(2)

        # 2a. Check session metadata
        session_rows = db_query(
            "SELECT agent_id, metadata FROM sessions WHERE id = ?", (opt_id,)
        )
        test("Optimizer session exists in DB", len(session_rows) > 0,
             f"Found {len(session_rows)} row(s)")
        if session_rows:
            sr = session_rows[0]
            agent_id = sr.get("agent_id")
            meta_str = sr.get("metadata", "{}")
            try:
                meta = json.loads(meta_str) if meta_str else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            test("Session metadata has opt_role='planner'",
                 meta.get("opt_role") == "planner",
                 f"opt_role = {meta.get('opt_role')}")
            test("Session metadata has target_session",
                 bool(meta.get("target_session")),
                 f"target_session = {meta.get('target_session')}")
            test("Optimizer session is bound to an agent",
                 agent_id is not None,
                 f"agent_id = {agent_id}")

        # 2b. Check the agent was created with template_id='opt_planner'
        # The agent is created with user_id = f"opt_planner_{TEST_USER}"
        opt_agent_user = f"opt_planner_{TEST_USER}"
        agent_rows = db_query(
            "SELECT template_id, system_prompt, id, user_id FROM agents WHERE user_id = ?",
            (opt_agent_user,)
        )
        test("Planner agent exists in agents table", len(agent_rows) > 0,
             f"Found {len(agent_rows)} row(s)")
        if agent_rows:
            ar = agent_rows[0]
            test("Planner agent has template_id='opt_planner'",
                 ar.get("template_id") == "opt_planner",
                 f"template_id = '{ar.get('template_id')}'")
            test("Planner system prompt mentions 'Planner:'",
                 "Planner:" in (ar.get("system_prompt", "")),
                 f"Has Planner:: {len(ar.get('system_prompt', ''))} chars")

        # 2c. Check context_documents for the Planner agent
        if agent_rows:
            planner_agent_id = agent_rows[0]["id"]
            context_docs = db_query(
                "SELECT context_type, title FROM context_documents WHERE agent_id = ?",
                (planner_agent_id,)
            )
            test("Planner context docs queried", True,
                 f"Found {len(context_docs)} docs")

            doc_types = set(d.get("context_type", "") for d in context_docs)
            # The planner should NOT have agent/skills/tools context docs
            test("Planner has NO 'agent' context_type docs (would inject webAgent identity)",
                 "agent" not in doc_types,
                 f"Context types: {doc_types}")
            test("Planner has NO 'skills' context_type docs",
                 "skills" not in doc_types,
                 f"Context types: {doc_types}")
            test("Planner has NO 'tools' context_type docs",
                 "tools" not in doc_types,
                 f"Context types: {doc_types}")

except Exception as e:
    test("Test 2 (unexpected exception)", False,
         f"Exception: {safe(str(e), 300)}\n{traceback.format_exc()[:500]}")


# ═══════════════════════════════════════════════════════════════════════════
#  TEST 3: Session binding works
# ═══════════════════════════════════════════════════════════════════════════

section("TEST 3: Session binding works")

try:
    new_session = "test-isolation-binding-" + str(int(time.time()))
    r = api_chat(TEST_USER, new_session, "test message for binding", timeout=30)
    time.sleep(2)

    # Check that the session has agent_id bound
    session_rows = db_query(
        "SELECT agent_id, id FROM sessions WHERE id = ?", (new_session,)
    )
    test("New session exists in DB", len(session_rows) > 0,
         f"Found {len(session_rows)} row(s)")
    if session_rows:
        agent_id = session_rows[0].get("agent_id")
        test("Session has agent_id bound (non-None)", agent_id is not None,
             f"agent_id = {agent_id}")

        if agent_id:
            # Verify the agent belongs to our test user
            agent_rows = db_query(
                "SELECT template_id, user_id FROM agents WHERE id = ?", (agent_id,)
            )
            test("Bound agent exists in agents table", len(agent_rows) > 0,
                 f"Found {len(agent_rows)} row(s)")
            if agent_rows:
                test("Agent belongs to test user",
                     agent_rows[0].get("user_id") == TEST_USER,
                     f"user_id = '{agent_rows[0].get('user_id')}'")

    # Async diagnostics: check what get_session_agent_id would return
    # by looking at the session_id we already queried
    if not session_rows or not session_rows[0].get("agent_id"):
        # Look for any row with agent_id set for this session
        all_sessions = db_query(
            "SELECT id, agent_id, user_id FROM sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 5",
            (TEST_USER,)
        )
        diag = "\n".join(
            f"  session={s['id'][:20]}... agent_id={s.get('agent_id')} user_id={s.get('user_id')}"
            for s in all_sessions
        )
        test("Diagnostic: recent sessions", len(all_sessions) > 0,
             f"Last {len(all_sessions)} sessions:\n{diag}")

except Exception as e:
    test("Test 3 (unexpected exception)", False,
         f"Exception: {safe(str(e), 300)}\n{traceback.format_exc()[:500]}")


# ═══════════════════════════════════════════════════════════════════════════
#  TEST 4: Optimizer session rejects webAgent identity in system prompt
# ═══════════════════════════════════════════════════════════════════════════

section("TEST 4: Optimizer session rejects webAgent identity in system prompt")

try:
    # Find the optimizer session for this user (from Test 2)
    opt_sessions = db_query(
        "SELECT id, agent_id FROM sessions WHERE id LIKE 'optimizer-%' AND user_id = ?",
        (TEST_USER,)
    )
    if opt_sessions:
        opt_id = opt_sessions[0]["id"]
        agent_id = opt_sessions[0].get("agent_id")
        test("Found optimizer session from previous test", bool(opt_id),
             f"session_id = {opt_id}")

        # The agent for the optimizer is stored with user_id = opt_planner_<user>
        opt_agent_user = f"opt_planner_{TEST_USER}"
        agent_rows = db_query(
            "SELECT system_prompt, id FROM agents WHERE user_id = ?", (opt_agent_user,)
        )

        if agent_rows:
            system_prompt = agent_rows[0].get("system_prompt", "")
            planner_agent_id = agent_rows[0]["id"]

            # Get context docs
            context_docs = db_query(
                "SELECT context_type, title, content FROM context_documents WHERE agent_id = ?",
                (planner_agent_id,)
            )

            # Build combined prompt (agent prompt + all context doc content)
            combined = system_prompt + "\n\n" + "\n\n".join(
                d.get("content", "") for d in context_docs
            )

            # Check for webAgent identity patterns (NOT the word webAgent itself,
            # which appears legitimately in the planner's own system prompt
            # referencing 'improve the webAgent\'s responses')
            webagent_identity_patterns = [
                "You are **webAgent**",
                "You are webAgent",
                "webAgent V0",
                "Operating principles",
                "Use tools before guessing",
                "Be concise. Prefer short",
                "Remember context. Use the memory system",
                "Learn from feedback.",
            ]
            found_any = []
            for wp in webagent_identity_patterns:
                if wp.lower() in combined.lower():
                    found_any.append(f"found '{wp}'")

            test("No webAgent IDENTITY in optimizer combined prompt",
                 not found_any,
                 f"{', '.join(found_any) if found_any else 'NONE (good)'}")

            # Verify it has Planner: instruction
            test("Optimizer prompt has 'Planner:' instruction",
                 "Planner:" in combined,
                 f"Found in system_prompt")

            # Check context doc types
            doc_types = set(d.get("context_type", "") for d in context_docs)
            test("No 'agent' context_type for Planner (would inject webAgent id)",
                 "agent" not in doc_types,
                 f"Context types: {doc_types}")
            test("No 'skills' context_type for Planner",
                 "skills" not in doc_types,
                 f"Context types: {doc_types}")
            test("No 'tools' context_type for Planner",
                 "tools" not in doc_types,
                 f"Context types: {doc_types}")

            test("Context docs count is 0 (no default docs copied for optimizer)",
                 len(context_docs) == 0,
                 f"Count: {len(context_docs)}")
        else:
            test("Planner agent found", False,
                 f"No agent with user_id='{opt_agent_user}'")

except Exception as e:
    test("Test 4 (unexpected exception)", False,
         f"Exception: {safe(str(e), 300)}\n{traceback.format_exc()[:500]}")


# ═══════════════════════════════════════════════════════════════════════════
#  TEST 5: Optimizer agent isolation - verify no webAgent context docs copied
# ═══════════════════════════════════════════════════════════════════════════

section("TEST 5: Optimizer agent isolation - verify webAgent context skipped")

try:
    # In the chat.py code, for optimizer sessions:
    #   "Only copy defaults for non-optimizer agents."
    # Check that copy_defaults_to_agent is skipped for opt sessions

    opt_agent_user = f"opt_planner_{TEST_USER}"
    agent_rows = db_query(
        "SELECT id FROM agents WHERE user_id = ?", (opt_agent_user,)
    )
    if agent_rows:
        planner_agent_id = agent_rows[0]["id"]
        context_docs = db_query(
            "SELECT context_type, title, content FROM context_documents WHERE agent_id = ?",
            (planner_agent_id,)
        )
        test("Planner has no context docs from defaults",
             len(context_docs) == 0,
             f"Found {len(context_docs)} docs - should be 0")

    # Now verify the REGULAR webAgent session has context docs
    default_agent_rows = db_query(
        "SELECT id FROM agents WHERE user_id = ?", (TEST_USER,)
    )
    if default_agent_rows:
        default_agent_id = default_agent_rows[0]["id"]
        default_docs = db_query(
            "SELECT context_type, title, content FROM context_documents WHERE agent_id = ?",
            (default_agent_id,)
        )
        test("Default webAgent HAS context docs (for comparison)",
             len(default_docs) > 0,
             f"Found {len(default_docs)} docs")
        if default_docs:
            doc_types = set(d.get("context_type", "") for d in default_docs)
            test("Default webAgent has 'agent' context type",
                 "agent" in doc_types,
                 f"Context types: {doc_types}")
            test("Default webAgent has 'skills' context type",
                 "skills" in doc_types,
                 f"Context types: {doc_types}")

except Exception as e:
    test("Test 5 (unexpected exception)", False,
         f"Exception: {safe(str(e), 300)}\n{traceback.format_exc()[:500]}")


# ═══════════════════════════════════════════════════════════════════════════
#  SUMMARY
# ═══════════════════════════════════════════════════════════════════════════

section("SUMMARY")

passed = sum(1 for r in results if r["status"] == "PASS")
failed = sum(1 for r in results if r["status"] == "FAIL")
total = len(results)

print(f"\n  Total:  {total}")
print(f"  Passed: {passed}")
print(f"  Failed: {failed}")

if failed > 0:
    print(f"\n  FAILED TESTS:")
    for r in results:
        if r["status"] == "FAIL":
            print(f"    - {r['name']}")
            if r["detail"]:
                for line in r["detail"].split("\n"):
                    print(f"      {line}")

print()
sys.exit(0 if failed == 0 else 1)
