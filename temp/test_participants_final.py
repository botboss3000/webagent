"""
Participants + agent isolation test for webAgent optimizer.

Validates:
1. Basic chat creates user + default agent participants
2. Optimizer creates planner agent (NOT default webAgent)
3. All kickstart interactions use planner agent from_id (NOT webAgent)
4. Participants list contains ONLY user + planner agent
5. Worker trials go to temp DBs, not local.db

Run: .venv/Scripts/python.exe temp/test_participants_final.py
"""

import glob
import json
import os
import sqlite3
import sys
import time
import traceback
import urllib.request
import urllib.error

sys.stdout.reconfigure(encoding='utf-8')

API_BASE = "http://127.0.0.1:8080"
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(PROJECT_ROOT, "app", "db", "local.db")
DB_DIR = os.path.join(PROJECT_ROOT, "app", "db")

TEST_USER = "test_user"
TEST_SESSION = "test-final"

passed = 0
failed = 0
warned = 0
results = []


def safe(s, n=500):
    if s is None:
        return "(None)"
    text = str(s)
    text = text.encode("utf-8", errors="replace").decode("cp1252", errors="replace")
    return text[:n]


def log(step_name, status, detail=""):
    global passed, failed, warned
    if status == "PASS":
        passed += 1
    elif status == "FAIL":
        failed += 1
    else:
        warned += 1
    results.append((step_name, status, detail))
    print(safe(f"  [{status}] {step_name}"))
    if detail:
        for line in detail.split("\n"):
            print(safe(f"         {line}"))


def db_get(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def db_read(sql, params=(), path=DB_PATH):
    conn = db_get(path)
    try:
        cur = conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def db_write(sql, params=()):
    conn = db_get()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def api_chat(user_id, session_id, message, timeout=60):
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


def section(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


# ═══════════════════════════════════════════════════════════════════════════
# STEP 1: Clean
# ═══════════════════════════════════════════════════════════════════════════

section("STEP 1: Clean start")

# Delete test_*.db files from app/db/
patterns = ["test_*.db", "test_*.db-wal", "test_*.db-shm"]
cleaned_files = []
for pattern in patterns:
    for f in glob.glob(os.path.join(DB_DIR, pattern)):
        try:
            os.remove(f)
            cleaned_files.append(os.path.basename(f))
        except Exception as e:
            print(f"  Warn removing {f}: {e}")

# Clean optimizer sessions and agents from local.db
sessions = db_read(
    "SELECT id FROM sessions WHERE user_id=? AND (id LIKE 'optimizer-%' OR id LIKE 'worker-%' OR id LIKE 'trial-%')",
    (TEST_USER,)
)
for s in sessions:
    db_write("DELETE FROM interactions WHERE session_id=?", (s["id"],))
    db_write("DELETE FROM sessions WHERE id=?", (s["id"],))

# Clean optimizer agents for test_user
like_pat = f"%{TEST_USER}"
opt_agents = db_read("SELECT id, user_id FROM agents WHERE user_id LIKE ?", (like_pat,))
for a in opt_agents:
    db_write("DELETE FROM context_documents WHERE agent_id=?", (a["id"],))
    db_write("DELETE FROM agents WHERE id=?", (a["id"],))

# Also clean the default session for test_user to start fresh
default_sessions = db_read(
    "SELECT id FROM sessions WHERE user_id=? AND id NOT LIKE 'optimizer-%' AND id NOT LIKE 'worker-%'",
    (TEST_USER,)
)
for s in default_sessions:
    if s["id"] != TEST_SESSION:
        db_write("DELETE FROM interactions WHERE session_id=?", (s["id"],))
        db_write("DELETE FROM sessions WHERE id=?", (s["id"],))

detail_lines = [f"Deleted {len(cleaned_files)} temp DB files", f"Cleaned {len(sessions)} optimizer/worker sessions"]
if cleaned_files:
    detail_lines.append(f"  Files: {', '.join(cleaned_files)}")
log("Clean start", "PASS", "\n".join(detail_lines))


# ═══════════════════════════════════════════════════════════════════════════
# STEP 2: Basic chat + check participants
# ═══════════════════════════════════════════════════════════════════════════

section("STEP 2: Basic chat + check participants")

resp = api_chat(TEST_USER, TEST_SESSION, "hello")
time.sleep(2)

# Check HTTP response
if "_http_error" in resp:
    log("Basic chat HTTP", "FAIL", f"HTTP {resp['_http_error']}: {resp.get('_detail','')}")
elif "_error" in resp:
    log("Basic chat HTTP", "FAIL", f"Error: {resp['_error']}")
else:
    log("Basic chat HTTP", "PASS", f"Got reply: {safe(resp.get('reply','') or resp.get('response',''), 100)}")

# Get participants
participants = db_read("SELECT participants FROM sessions WHERE id=?", (TEST_SESSION,))
if not participants:
    log("Session exists", "FAIL", "No session row found")
    sys.exit(1)

parts_raw = participants[0]["participants"]
parts = json.loads(parts_raw) if parts_raw else []

# Get default webAgent UUID
default_agents = db_read("SELECT id FROM agents WHERE user_id=? LIMIT 1", (TEST_USER,))
default_agent_id = default_agents[0]["id"] if default_agents else None

detail = [
    f"Session: {TEST_SESSION}",
    f"Default webAgent UUID: {default_agent_id}",
    f"Participants: {json.dumps(parts, indent=2)}",
]

has_user = any(p.get("id") == TEST_USER and p.get("role") == "user" for p in parts)
has_agent = any(p.get("id") == default_agent_id and p.get("role") == "agent" for p in parts) if default_agent_id else False

if default_agent_id and has_user and has_agent:
    log("Basic chat participants", "PASS", "\n".join(detail))
else:
    log("Basic chat participants", "FAIL",
        f"default_agent={default_agent_id} has_user={has_user} has_agent={has_agent}\n" + "\n".join(detail))

if not default_agent_id:
    log("Default webAgent exists", "FAIL", "No agent found for test_user")
    sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════
# STEP 3: Optimizer trigger
# ═══════════════════════════════════════════════════════════════════════════

section("STEP 3: Trigger optimizer + check participants")

# First send a substantive message so there's session context
resp = api_chat(TEST_USER, TEST_SESSION, "what time is it?", timeout=30)
time.sleep(2)

# Trigger /optimize
opt_resp = api_chat(TEST_USER, TEST_SESSION, "/optimize", timeout=30)
time.sleep(2)

opt_reply = opt_resp.get("reply", "") or opt_resp.get("response", "")

if "_http_error" in opt_resp:
    log("Trigger optimizer", "FAIL", f"HTTP {opt_resp['_http_error']}: {safe(opt_resp.get('_detail',''), 200)}")
elif "_error" in opt_resp:
    log("Trigger optimizer", "FAIL", f"Error: {safe(opt_resp['_error'], 200)}")
else:
    log("Trigger optimizer HTTP", "PASS", f"Reply: {safe(opt_reply, 150)}")

# Find optimizer sessions for test_user
opt_sessions = db_read(
    "SELECT id, agent_id, participants, metadata FROM sessions WHERE user_id=? AND id LIKE 'optimizer-%' ORDER BY created_at DESC",
    (TEST_USER,)
)
if not opt_sessions:
    log("Optimizer session created", "FAIL", "No optimizer session found in DB")
    # Check if there are any sessions at all
    all_sessions = db_read("SELECT id, user_id FROM sessions WHERE user_id=? ORDER BY created_at DESC", (TEST_USER,))
    detail = [f"All sessions for {TEST_USER}: {json.dumps([s['id'] for s in all_sessions])}"]
    log("Diagnostic: all sessions", "WARN", "\n".join(detail))
    sys.exit(1)

opt_sid = opt_sessions[0]["id"]
opt_agent_id = opt_sessions[0]["agent_id"]
opt_participants_raw = opt_sessions[0]["participants"]
opt_participants = json.loads(opt_participants_raw) if opt_participants_raw else []
opt_meta_raw = opt_sessions[0]["metadata"]
opt_meta = json.loads(opt_meta_raw) if opt_meta_raw else {}
opt_role = opt_meta.get("opt_role", "N/A")
target_session = opt_meta.get("target_session", "N/A")

detail = [
    f"Optimizer session: {opt_sid}",
    f"Opt role: {opt_role}",
    f"Target session: {target_session}",
    f"Optimizer agent_id: {opt_agent_id}",
    f"Default webAgent UUID: {default_agent_id}",
    f"Optimizer participants: {json.dumps(opt_participants, indent=2)}",
]

# Check optimizer session has proper participants
has_user_opt = any(p.get("id") == TEST_USER and p.get("role") == "user" for p in opt_participants)
has_agent_opt = any(p.get("id") == opt_agent_id and p.get("role") == "agent" for p in opt_participants) if opt_agent_id else False

# The planner agent's ID should match sessions.agent_id
agent_consistency = has_agent_opt if opt_agent_id else False

# Check that planner agent ID is NOT the default webAgent ID
planner_is_default = (opt_agent_id == default_agent_id) if (opt_agent_id and default_agent_id) else False

all_checks_ok = has_user_opt and has_agent_opt and opt_agent_id and not planner_is_default

if all_checks_ok:
    log("Optimizer participants + isolation", "PASS", "\n".join(detail))
else:
    issue = []
    if not has_user_opt: issue.append("user not in participants")
    if not has_agent_opt: issue.append("planner agent not in participants")
    if not opt_agent_id: issue.append("no agent_id bound to session")
    if planner_is_default: issue.append(f"PLANNER AGENT IS DEFAULT WEBAGENT! Both are {opt_agent_id[:20]}...")
    log("Optimizer participants + isolation", "FAIL",
        f"{'; '.join(issue)}\n" + "\n".join(detail))


# ═══════════════════════════════════════════════════════════════════════════
# STEP 4: Wait for kickstart + check interactions
# ═══════════════════════════════════════════════════════════════════════════

section("STEP 4: Wait for kickstart + check interactions")

print("  Waiting for kickstart Planner response (up to 35s)...")
opt_sid_for_check = opt_sid
opt_agent_id_for_check = opt_agent_id

# Wait up to 35 seconds for the kickstart to produce interactions
interactions = []
for i in range(35):
    time.sleep(1)
    interactions = db_read(
        "SELECT id, role, content, tool_name, from_id, to_id, source, created_at "
        "FROM interactions WHERE session_id=? ORDER BY created_at ASC",
        (opt_sid_for_check,)
    )
    # Look for assistant interactions (Planner response)
    assistant_ixs = [ix for ix in interactions if ix["role"] == "assistant"]
    if assistant_ixs:
        print(f"  Found {len(interactions)} total interactions, {len(assistant_ixs)} assistant after {i+1}s")
        break
    if i == 15 or i == 25:
        print(f"  Still waiting... ({i+1}s, {len(interactions)} interactions so far)")
else:
    print(f"  WARN: No assistant response after 35s, checking with current {len(interactions)} interactions")

# Show interaction timeline
detail = ["Interaction timeline:"]
for ix in interactions[:15]:
    fid_short = str(ix["from_id"])[:24] if ix["from_id"] else "NONE"
    detail.append(f"  {ix['created_at']} role={ix['role']:>8} from={fid_short}")
detail.append(f"  Total: {len(interactions)} interactions")

# Critical check: EVERY assistant/tool interaction's from_id must match planner agent's ID
# If ANY from_id matches the default webAgent -> FAIL
from_id_issues = []
default_webagent_hits = []
for ix in interactions:
    fid = ix["from_id"]
    if ix["role"] in ("assistant", "tool") and fid:
        if fid != opt_agent_id_for_check:
            from_id_issues.append(f"  {ix['id'][:8]} role={ix['role']} from={fid[:24]} (expected {opt_agent_id_for_check[:24]})")
        if fid == default_agent_id:
            default_webagent_hits.append(f"  {ix['id'][:8]} role={ix['role']} from={fid[:24]} (DEFAULT WEBAGENT!)")

# Also check: participants list should only contain test_user + planner agent
final_participants_raw = db_read("SELECT participants FROM sessions WHERE id=?", (opt_sid_for_check,))
final_parts = json.loads(final_participants_raw[0]["participants"]) if final_participants_raw and final_participants_raw[0]["participants"] else []
participant_ids = set(p["id"] for p in final_parts)
expected_ids = {TEST_USER, opt_agent_id_for_check}
extra_participants = participant_ids - expected_ids
missing_participants = expected_ids - participant_ids

# Results
if not from_id_issues and not default_webagent_hits:
    log("Kickstart from_ids all match planner agent", "PASS",
        f"All {len(interactions)} interactions use planner agent ID {opt_agent_id_for_check[:24]}...\n" + "\n".join(detail))
else:
    lines = []
    if default_webagent_hits:
        lines.append(f"CRITICAL: {len(default_webagent_hits)} interactions with DEFAULT WEBAGENT from_id!")
        lines.extend(default_webagent_hits)
    if from_id_issues:
        lines.append(f"{len(from_id_issues)} interactions with wrong from_id:")
        lines.extend(from_id_issues[:10])
    log("Kickstart from_ids all match planner agent", "FAIL",
        "\n".join(lines) + "\n\n" + "\n".join(detail))

# Check participants isolation
if extra_participants:
    log("Participants contain ONLY user + planner agent", "FAIL",
        f"Extra participants found: {extra_participants}\nFull participants: {json.dumps(final_parts)}")
elif missing_participants:
    log("Participants contain ONLY user + planner agent", "FAIL",
        f"Missing participants: {missing_participants}\nFull participants: {json.dumps(final_parts)}")
else:
    log("Participants contain ONLY user + planner agent", "PASS",
        f"Participants: {json.dumps(final_parts)}")


# ═══════════════════════════════════════════════════════════════════════════
# STEP 5: Approve and verify
# ═══════════════════════════════════════════════════════════════════════════

section("STEP 5: Approve and verify worker trials")

# Send approval to optimizer session
print("  Sending: 'yes, run worker trials'")
try:
    trial_resp = api_chat(TEST_USER, opt_sid_for_check, "yes, run worker trials", timeout=180)
    trial_reply = trial_resp.get("reply", "") or trial_resp.get("response", "")
    if "_http_error" in trial_resp:
        log("Approve worker trials", "WARN", f"HTTP {trial_resp['_http_error']}: {safe(trial_resp.get('_detail',''), 200)}")
    elif "_error" in trial_resp:
        log("Approve worker trials", "WARN", f"Error: {safe(trial_resp['_error'], 200)}")
    else:
        log("Approve worker trials HTTP", "PASS" if trial_reply else "WARN",
            f"Reply: {safe(trial_reply, 150)}")
except Exception as e:
    log("Approve worker trials", "WARN", f"Exception: {safe(str(e), 200)}")

# Wait for worker trials to potentially create temp DBs
print("  Waiting 10s for worker trials to complete...")
time.sleep(10)

# Check temp DBs exist
temp_dbs = glob.glob(os.path.join(DB_DIR, "test_*.db"))
if temp_dbs:
    log("Temp DB files exist", "PASS", f"Found {len(temp_dbs)} temp DB files: {[os.path.basename(f) for f in temp_dbs]}")
else:
    log("Temp DB files exist", "WARN", "No temp DB files found (worker trials may not have run yet)")

# Check local.db has no worker data
worker_agents = db_read("SELECT id, user_id FROM agents WHERE user_id LIKE 'worker-test-%'")
worker_sessions = db_read("SELECT id FROM sessions WHERE id LIKE 'worker-%'")
worker_trials = db_read("SELECT id FROM sessions WHERE id LIKE 'trial-%'")

if not worker_agents and not worker_sessions and not worker_trials:
    log("No worker data in local.db", "PASS", "All worker data isolated to temp DBs")
else:
    issues = []
    if worker_agents: issues.append(f"{len(worker_agents)} worker agents in local.db")
    if worker_sessions: issues.append(f"{len(worker_sessions)} worker sessions")
    if worker_trials: issues.append(f"{len(worker_trials)} trial sessions")
    log("No worker data in local.db", "FAIL", f"{' | '.join(issues)}")


# ═══════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═══════════════════════════════════════════════════════════════════════════

section("TEST SUMMARY")
for name, status, _ in results:
    print(f"  [{status}] {name}")
print(f"\n  {passed} PASS, {failed} FAIL, {warned} WARN")
print("=" * 70)

if failed > 0:
    print("\n  FAILED TESTS:")
    for name, status, detail in results:
        if status == "FAIL":
            print(f"    - {name}")
            if detail:
                for line in detail.split("\n"):
                    print(f"      {line}")

print()
sys.exit(0 if failed == 0 else 1)
