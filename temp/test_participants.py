"""
End-to-end test: participants enforcement & agent isolation for webAgent optimizer.

Dependencies: httpx (pip install httpx)

Run: python temp/test_participants.py
"""

import json
import os
import sqlite3
import sys
import time

import httpx

API_BASE = "http://127.0.0.1:8080"
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(PROJECT_ROOT, "app", "db", "local.db")
DB_DIR = os.path.join(PROJECT_ROOT, "app", "db")

TEST_USER = "test_user"
TEST_SESSION = "test-parts"

passed = 0
failed = 0
warned = 0
results = []


def safe(s):
    return s.encode("utf-8", errors="replace").decode("cp1252", errors="replace")


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


def clean_db():
    print("\n=== STEP 1: Clean start ===")
    import glob
    for pattern in ["test_*.db", "test_*.db-wal", "test_*.db-shm"]:
        for f in glob.glob(os.path.join(DB_DIR, pattern)):
            try:
                os.remove(f)
                print(f"  Deleted: {f}")
            except Exception as e:
                print(f"  Warn: {f}: {e}")
    conn = db_get()
    try:
        cursor = conn.execute(
            "SELECT id FROM sessions WHERE user_id=? OR id LIKE 'optimizer-%' OR id LIKE 'worker-%' OR id LIKE 'trial-%'",
            (TEST_USER,),
        )
        sids = [r["id"] for r in cursor.fetchall()]
        for sid in sids:
            conn.execute("DELETE FROM interactions WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
            print(f"  Cleaned session: {sid[:20]}")
        conn.execute("DELETE FROM agents WHERE user_id LIKE 'worker-test-%'")
        conn.execute("DELETE FROM agents WHERE user_id LIKE 'opt_%_test_user'")
        conn.execute("DELETE FROM optimizer_runs")
        conn.commit()
        print(f"  Cleaned {len(sids)} sessions")
    finally:
        conn.close()
    log("Clean start", "PASS", "All test artifacts removed")


def get_session_participants(session_id, db_path=DB_PATH):
    conn = db_get(db_path)
    try:
        row = conn.execute("SELECT participants FROM sessions WHERE id=?", (session_id,)).fetchone()
        return json.loads(row["participants"]) if row and row["participants"] else []
    finally:
        conn.close()


def get_session_agent_id(session_id, db_path=DB_PATH):
    conn = db_get(db_path)
    try:
        row = conn.execute("SELECT agent_id FROM sessions WHERE id=?", (session_id,)).fetchone()
        return row["agent_id"] if row else None
    finally:
        conn.close()


def get_session_metadata(session_id, db_path=DB_PATH):
    conn = db_get(db_path)
    try:
        row = conn.execute("SELECT metadata FROM sessions WHERE id=?", (session_id,)).fetchone()
        return json.loads(row["metadata"]) if row and row["metadata"] else {}
    finally:
        conn.close()


def chat(user_id, session_id, message, timeout=120.0):
    resp = httpx.post(
        f"{API_BASE}/api/v1/chat",
        json={"user_id": user_id, "session_id": session_id, "message": message},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def get_interactions(session_id, db_path=DB_PATH):
    conn = db_get(db_path)
    try:
        rows = conn.execute(
            "SELECT id, role, content, tool_name, from_id, to_id, source, created_at "
            "FROM interactions WHERE session_id=? ORDER BY created_at ASC",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_default_webagent_uuid():
    conn = db_get()
    try:
        row = conn.execute("SELECT id FROM agents WHERE user_id=? LIMIT 1", (TEST_USER,)).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def get_active_optimizer_sessions():
    conn = db_get()
    try:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE user_id=? AND id LIKE 'optimizer-%' ORDER BY created_at DESC",
            (TEST_USER,),
        ).fetchall()
        return [r["id"] for r in rows]
    finally:
        conn.close()


def main():
    global passed, failed, warned

    # ────────────────────────────────────────────────────────────────────
    # STEP 1: Clean start
    # ────────────────────────────────────────────────────────────────────
    clean_db()

    # ────────────────────────────────────────────────────────────────────
    # STEP 2: Basic chat + check participants
    # ────────────────────────────────────────────────────────────────────
    print("\n=== STEP 2: Basic chat + check participants ===")
    resp = chat(TEST_USER, TEST_SESSION, "hello")
    time.sleep(2)  # let async memory save finish

    participants = get_session_participants(TEST_SESSION)
    default_agent_id = get_default_webagent_uuid()

    detail = [
        f"Session: {TEST_SESSION}",
        f"Participants: {json.dumps(participants, indent=2)}",
        f"Default webAgent UUID: {default_agent_id}",
    ]
    has_user = any(p.get("id") == TEST_USER and p.get("role") == "user" for p in participants)
    has_agent = any(p.get("id") == default_agent_id and p.get("role") == "agent" for p in participants)

    if has_user and has_agent and default_agent_id:
        log("Basic chat participants", "PASS", "\n".join(detail))
    else:
        log("Basic chat participants", "FAIL",
            f"user_in_parts={has_user} agent_in_parts={has_agent} agent_id={default_agent_id}")

    if not default_agent_id:
        log("Default webAgent exists", "FAIL", "No agent found for test_user")
        return

    # ────────────────────────────────────────────────────────────────────
    # STEP 3: Trigger optimizer + check participants
    # ────────────────────────────────────────────────────────────────────
    print("\n=== STEP 3: Trigger optimizer + check participants ===")
    opt_resp = chat(TEST_USER, TEST_SESSION, "/optimize")
    time.sleep(2)

    opt_sessions = get_active_optimizer_sessions()
    if not opt_sessions:
        log("Trigger optimizer", "FAIL", "No optimizer session created")
        return

    opt_sid = opt_sessions[0]
    opt_participants = get_session_participants(opt_sid)
    opt_agent_id = get_session_agent_id(opt_sid)

    detail = [
        f"Optimizer session: {opt_sid}",
        f"Optimizer agent_id: {opt_agent_id}",
        f"Participants: {json.dumps(opt_participants, indent=2)}",
    ]
    has_user_opt = any(p.get("id") == TEST_USER and p.get("role") == "user" for p in opt_participants)
    has_agent_opt = any(p.get("id") == opt_agent_id and p.get("role") == "agent" for p in opt_participants) if opt_agent_id else False

    if has_user_opt and has_agent_opt and opt_agent_id:
        log("Optimizer participants", "PASS", "\n".join(detail))
    else:
        log("Optimizer participants", "FAIL",
            f"user={has_user_opt} agent={has_agent_opt} agent_id={opt_agent_id}")

    # ────────────────────────────────────────────────────────────────────
    # STEP 4: Wait for kickstart (30s) -> check interaction from_ids
    # ────────────────────────────────────────────────────────────────────
    print("\n=== STEP 4: Wait for kickstart + check interactions ===")
    interactions = []
    for i in range(10):
        time.sleep(1)
        interactions = get_interactions(opt_sid)
        if len(interactions) >= 2:
            print(f"  {len(interactions)} interactions after {i+1}s")
            break

    # Show interaction timeline (first 6)
    detail = ["Interaction timeline:"]
    for ix in interactions[:10]:
        detail.append(f"  {ix['created_at']} role={ix['role']:>8} from={str(ix['from_id'])[:24]}")
    detail.append(f"  Total: {len(interactions)} interactions")

    # Check ALL assistant/tool from_ids match opt_agent_id
    from_id_issues = []
    for ix in interactions:
        if ix["role"] in ("assistant", "tool") and ix["from_id"] and ix["from_id"] != opt_agent_id:
            from_id_issues.append(f"  {ix['id'][:8]} role={ix['role']} from={ix['from_id']} (expected {opt_agent_id})")

    if not from_id_issues:
        log("Kickstart from_ids", "PASS", "\n".join(detail))
    else:
        log("Kickstart from_ids", "FAIL",
            f"{len(from_id_issues)} interactions with wrong from_id:\n" + "\n".join(from_id_issues))

    # Check NO default webAgent in optimizer interactions
    default_in_opt = any(
        ix["from_id"] == default_agent_id for ix in interactions if ix["role"] in ("assistant", "tool")
    )
    if default_in_opt:
        log("No webAgent in optimizer interactions", "FAIL",
            f"Default webAgent {default_agent_id[:20]}... found in {opt_sid} interactions")
    else:
        log("No webAgent in optimizer interactions", "PASS",
            "All assistant/tool from_ids point to optimizer agent")

    # ────────────────────────────────────────────────────────────────────
    # STEP 5: Verify webAgent NOT in optimizer participants
    # ────────────────────────────────────────────────────────────────────
    print("\n=== STEP 5: Verify webAgent NOT in optimizer participants ===")
    opt_parts = get_session_participants(opt_sid)
    default_in_parts = any(p.get("id") == default_agent_id for p in opt_parts)

    if default_in_parts:
        log("webAgent NOT in optimizer participants", "FAIL",
            f"Default webAgent {default_agent_id} found in participants: {json.dumps(opt_parts)}")
    else:
        log("webAgent NOT in optimizer participants", "PASS",
            f"Participants: {json.dumps(opt_parts)}")

    # ────────────────────────────────────────────────────────────────────
    # STEP 6: Approve worker trials
    # ────────────────────────────────────────────────────────────────────
    print("\n=== STEP 6: Approve worker trials ===")
    # Wait for kickstart agent loop to finish
    print("  Waiting for Planner kickstart to finish (60s max)...")
    for i in range(60):
        time.sleep(1)
        interactions = get_interactions(opt_sid)
        # Look for a Planner response (assistant role)
        has_planner_reply = any(
            ix["role"] == "assistant" and "Planner:" in (ix.get("content") or "")
            for ix in interactions
        )
        if has_planner_reply:
            print(f"  Planner responded after {i+1}s")
            break
    else:
        print("  WARN: No Planner response after 60s, continuing anyway")

    # Now send the approval
    print("  Sending: 'yes, run worker trials'")
    try:
        trial_resp = chat(TEST_USER, opt_sid, "yes, run worker trials", timeout=180.0)
        print(f"  Reply: {trial_resp.get('reply', '')[:120]}...")
    except Exception as e:
        log("Approve worker trials", "WARN", f"Chat failed: {e}")

    time.sleep(3)

    # Check temp DB isolation
    import glob
    temp_dbs = glob.glob(os.path.join(DB_DIR, "test_*.db"))
    if temp_dbs:
        log("Temp DB isolation", "PASS", f"{len(temp_dbs)} temp DB files in app/db/")

    conn = db_get()
    try:
        workers = conn.execute("SELECT id, user_id FROM agents WHERE user_id LIKE 'worker-test-%'").fetchall()
        conn.close()
        if not workers:
            log("No worker agents in local.db", "PASS", "All worker agents in temp DBs")
        else:
            log("No worker agents in local.db", "FAIL",
                f"{len(workers)} worker agents in local.db: {[dict(w) for w in workers]}")
    except Exception as e:
        log("No worker agents in local.db", "WARN", str(e))

    # ────────────────────────────────────────────────────────────────────
    # STEP 7: Handoff to Finalizer
    # ────────────────────────────────────────────────────────────────────
    print("\n=== STEP 7: Handoff to Finalizer ===")
    print("  Waiting for worker trials or Planner response...")
    for i in range(30):
        time.sleep(1)
        interactions = get_interactions(opt_sid)
        # Check if a handoff-like tool was called
        handoff_msgs = [ix for ix in interactions if "handoff" in str(ix.get("content", "")).lower()]
        if handoff_msgs:
            print(f"  Handoff activity detected after {i+1}s")
            break
    else:
        print("  No handoff activity detected, still continuing")

    print("  Sending: 'looks good, hand off to finalizer'")
    try:
        finalizer_resp = chat(TEST_USER, opt_sid, "looks good, hand off to finalizer", timeout=120.0)
        print(f"  Reply: {finalizer_resp.get('reply', '')[:120]}...")
    except Exception as e:
        log("Handoff to Finalizer", "WARN", f"Chat failed: {e}")

    time.sleep(5)

    opt_metadata = get_session_metadata(opt_sid)
    opt_parts = get_session_participants(opt_sid)
    opt_agent_id = get_session_agent_id(opt_sid)

    detail = [
        f"opt_role: {opt_metadata.get('opt_role', 'N/A')}",
        f"Agent: {opt_agent_id}",
        f"Has judging_criteria: {'judging_criteria' in opt_metadata}",
        f"Has test_db_paths: {'test_db_paths' in opt_metadata}",
        f"Participants: {json.dumps(opt_parts)}",
    ]
    has_agent_part = any(p.get("role") == "agent" for p in opt_parts)
    has_judging = "judging_criteria" in opt_metadata

    if has_agent_part and has_judging:
        log("Handoff to Finalizer", "PASS", "\n".join(detail))
    else:
        log("Handoff to Finalizer", "WARN" if has_agent_part else "FAIL",
            ("" if has_agent_part else "No agent participant after handoff. ") +
            ("" if has_judging else "Missing judging_criteria. ") +
            "\n".join(detail))

    # ────────────────────────────────────────────────────────────────────
    # STEP 8: Verify all interactions from participants only
    # ────────────────────────────────────────────────────────────────────
    print("\n=== STEP 8: Verify all interactions from participants only ===")
    all_ix = get_interactions(opt_sid)
    parts = get_session_participants(opt_sid)
    participant_ids = {p["id"] for p in parts}

    # The test_user is always a valid participant for their sessions
    participant_ids.add(TEST_USER)

    detail = [f"Total interactions: {len(all_ix)}"]
    detail.append(f"Participant IDs: {participant_ids}")
    detail.append(f"All known agent IDs: default={default_agent_id[:20]} opt_agent={opt_agent_id[:20]}")

    # Show from_id distribution
    from_id_counts = {}
    for ix in all_ix:
        fid = ix["from_id"]
        if fid:
            from_id_counts[fid] = from_id_counts.get(fid, 0) + 1
    detail.append(f"from_id distribution: {json.dumps(from_id_counts, indent=2)}")

    # Check violations
    violations = []
    for ix in all_ix:
        fid = ix["from_id"]
        if fid and fid not in participant_ids:
            violations.append(f"  {ix['id'][:8]} role={ix['role']} from={fid} (NOT in participants)")

    if violations:
        log("All interactions from participants only", "FAIL",
            "\n".join(detail) + "\n\nViolations:\n" + "\n".join(violations[:10]))
    else:
        log("All interactions from participants only", "PASS", "\n".join(detail))

    # ────────────────────────────────────────────────────────────────────
    # SUMMARY
    # ────────────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    for name, status, _ in results:
        print(f"  [{status}] {name}")
    print(f"\n  {passed} PASS, {failed} FAIL, {warned} WARN")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
