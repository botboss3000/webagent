"""
End-to-end test: per-worker temp database approach for optimizer trials.

Tests the complete optimizer workflow:
1. Clean start (delete test_*.db, cleanup local.db)
2. Basic chat + /optimize trigger
3. Wait for Planner kickstart
4. Verify no temp DBs yet
5. Approve worker trials
6. Verify temp DBs exist with correct schema
7. Verify local.db is clean (no leaked worker rows)
8. Check session metadata for test_db_paths
9. Handoff to Finalizer, verify it reads from temp DBs
"""

import json
import os
import re
import sqlite3
import sys
import time
import traceback
import uuid

import requests

# ── Config ──
BASE_URL = "http://127.0.0.1:8080"
API_CHAT = f"{BASE_URL}/api/v1/chat"
API_HEALTH = f"{BASE_URL}/health"
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DB_DIR = os.path.normpath(os.path.join(PROJECT_ROOT, "app", "db"))
LOCAL_DB = os.path.join(DB_DIR, "local.db")

USER_ID = "test_user"
SESSION_ID = "test-iso"

results = {"passed": 0, "failed": 0, "steps": []}


def report(step: str, status: str, detail: str = ""):
    tag = "PASS" if status == "PASS" else "FAIL"
    print(f"\n{'='*60}")
    print(f"  Step {step}: [{tag}]")
    if detail:
        for line in detail.strip().split("\n"):
            print(f"    {line}")
    print(f"{'='*60}")
    results["steps"].append({"step": step, "status": status, "detail": detail})
    if status == "PASS":
        results["passed"] += 1
    else:
        results["failed"] += 1


def log(msg):
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}")


# ── DB helpers ──

def db_conn():
    return sqlite3.connect(LOCAL_DB)


def find_temp_db_files():
    files = []
    if not os.path.exists(DB_DIR):
        return files
    for f in os.listdir(DB_DIR):
        if f.startswith("test_") and f.endswith(".db"):
            files.append(os.path.join(DB_DIR, f))
    return sorted(files)


def delete_temp_db_files():
    for suffix in (".db", ".db-wal", ".db-shm"):
        for f in os.listdir(DB_DIR):
            if f.startswith("test_") and f.endswith(suffix):
                path = os.path.join(DB_DIR, f)
                try:
                    os.remove(path)
                    log(f"  Deleted {path}")
                except Exception as e:
                    log(f"  Warning: could not delete {path}: {e}")


def cleanup_local_db():
    conn = db_conn()
    try:
        conn.execute("DELETE FROM interactions WHERE session_id=?", (SESSION_ID,))
        conn.execute("DELETE FROM interactions WHERE session_id LIKE 'optimizer-%'")
        conn.execute("DELETE FROM interactions WHERE session_id LIKE 'trial-%'")
        conn.execute("DELETE FROM interactions WHERE source='worker_trial'")
        conn.execute("DELETE FROM sessions WHERE id=?", (SESSION_ID,))
        conn.execute("DELETE FROM sessions WHERE id LIKE 'optimizer-%'")
        conn.execute("DELETE FROM sessions WHERE id LIKE 'trial-%'")
        conn.execute("DELETE FROM agents WHERE user_id=?", (USER_ID,))
        conn.execute("DELETE FROM agents WHERE user_id LIKE 'worker-test-%'")
        conn.execute("DELETE FROM agents WHERE user_id LIKE 'opt_%'")
        # Remove orphaned context docs
        conn.execute("DELETE FROM context_documents WHERE agent_id NOT IN (SELECT id FROM agents)")
        conn.commit()
        log("  Cleaned up local.db test data")
    except Exception as e:
        log(f"  Cleanup warning: {e}")
    finally:
        conn.close()


# ── Chat API helper (synchronous) ──

def chat_post(user_id: str, session_id: str, message: str, timeout: float = 120.0) -> dict:
    """POST to /api/v1/chat and return JSON response.
    Uses a fresh session with Connection: close to avoid keep-alive issues.
    """
    sess = requests.Session()
    sess.headers.update({"Connection": "close"})
    resp = sess.post(
        API_CHAT,
        json={"user_id": user_id, "session_id": session_id, "message": message},
        timeout=timeout,
    )
    sess.close()
    resp.raise_for_status()
    return resp.json()


def count_interactions(session_id: str) -> int:
    conn = db_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM interactions WHERE session_id=?", (session_id,)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def get_optimizer_session_id() -> str:
    conn = db_conn()
    try:
        row = conn.execute(
            "SELECT id FROM sessions WHERE user_id=? AND id LIKE 'optimizer-%' ORDER BY created_at DESC LIMIT 1",
            (USER_ID,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_session_metadata(session_id: str) -> dict:
    conn = db_conn()
    try:
        row = conn.execute(
            "SELECT metadata FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return {}
    finally:
        conn.close()


def get_session_agent(session_id: str) -> dict:
    conn = db_conn()
    try:
        row = conn.execute(
            "SELECT agent_id FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row and row[0]:
            agent = conn.execute(
                "SELECT id, template_id, user_id FROM agents WHERE id=?", (row[0],)
            ).fetchone()
            if agent:
                return {"id": agent[0], "template_id": agent[1], "user_id": agent[2]}
        return {}
    finally:
        conn.close()


def get_latest_interactions(session_id: str, limit: int = 5) -> list:
    conn = db_conn()
    try:
        rows = conn.execute(
            "SELECT role, content, source, created_at FROM interactions "
            "WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit)
        ).fetchall()
        return [{"role": r[0], "content": r[1], "source": r[2], "created_at": r[3]} for r in rows]
    finally:
        conn.close()


# ════════════════════════════════════════════════════════════════════════
# MAIN TEST
# ════════════════════════════════════════════════════════════════════════

def run_test():
    log("Starting per-worker temp DB test...")
    log(f"Project root: {PROJECT_ROOT}")
    log(f"Local DB: {LOCAL_DB}")
    log(f"DB dir: {DB_DIR}")

    # Quick health check
    try:
        h = requests.get(API_HEALTH, timeout=5)
        log(f"Server health: {h.json()}")
    except Exception as e:
        log(f"FATAL: Server not reachable: {e}")
        report("0", "FAIL", f"Server not reachable at {BASE_URL}: {e}")
        sys.exit(1)

    # ─────────────────────────────────────────────
    # STEP 1: Clean start
    # ─────────────────────────────────────────────
    log("\n--- STEP 1: Clean start ---")
    try:
        delete_temp_db_files()
        cleanup_local_db()

        remaining = find_temp_db_files()
        if remaining:
            report("1", "FAIL", f"Temp DB files still exist after cleanup: {remaining}")
        else:
            report("1", "PASS", "All test_*.db deleted and local.db cleaned")
    except Exception as e:
        report("1", "FAIL", f"Exception: {e}\n{traceback.format_exc()}")

    # ─────────────────────────────────────────────
    # STEP 2: Basic chat + /optimize trigger
    # ─────────────────────────────────────────────
    log("\n--- STEP 2: Basic chat + /optimize ---")
    opt_sid = None
    try:
        log("  Sending: 'what time is it?'")
        resp1 = chat_post(USER_ID, SESSION_ID, "what time is it?", timeout=60.0)
        reply1 = resp1.get("reply", "")
        log(f"  Reply: {reply1[:200]}")
        if not reply1:
            report("2a", "FAIL", "No reply from basic chat")
        else:
            report("2a", "PASS", f"Got reply: {reply1[:100]}...")

        # Send /optimize with long timeout (the kickstart fires a background task)
        log("  Sending: '/optimize'")
        resp2 = chat_post(USER_ID, SESSION_ID, "/optimize", timeout=120.0)
        reply2 = resp2.get("reply", "")
        log(f"  Reply: {reply2[:300]}")

        # Extract optimizer session ID
        opt_match = re.search(r'optimizer-[a-f0-9-]+', reply2)
        if opt_match:
            opt_sid = opt_match.group(0)
            log(f"  Optimizer session: {opt_sid}")
        else:
            opt_sid = get_optimizer_session_id()
            log(f"  From DB: {opt_sid}")

        if opt_sid:
            report("2b", "PASS", f"Optimizer session created: {opt_sid}")
        else:
            report("2b", "FAIL", "Could not find optimizer session ID")

    except requests.exceptions.Timeout:
        report("2b", "FAIL", "/optimize timed out after 120s")
        opt_sid = get_optimizer_session_id()
        if opt_sid:
            log(f"  But optimizer session exists in DB: {opt_sid}")
            report("2b", "PASS", f"Optimizer session created despite timeout: {opt_sid}")
    except Exception as e:
        report("2", "FAIL", f"Exception: {e}\n{traceback.format_exc()}")

    if not opt_sid:
        log("FATAL: No optimizer session, cannot continue")
        _print_summary()
        sys.exit(1)

    # ─────────────────────────────────────────────
    # STEP 3: Wait for Planner kickstart (30s)
    # ─────────────────────────────────────────────
    log(f"\n--- STEP 3: Wait for Planner kickstart (opt_sid={opt_sid}) ---")
    try:
        start = time.time()
        planner_ready = False
        while time.time() - start < 30:
            cnt = count_interactions(opt_sid)
            agent = get_session_agent(opt_sid)
            log(f"  Interactions: {cnt}, agent template: {agent.get('template_id', 'N/A')}")
            if cnt >= 2 and agent.get("template_id") == "opt_planner":
                planner_ready = True
                break
            time.sleep(2)

        if planner_ready:
            report("3", "PASS", f"Planner active: template_id=opt_planner, interactions={cnt}")
        else:
            cnt = count_interactions(opt_sid)
            agent = get_session_agent(opt_sid)
            report("3", "FAIL", f"No Planner after 30s. Interactions={cnt}, Agent={agent}")
    except Exception as e:
        report("3", "FAIL", f"Exception: {e}\n{traceback.format_exc()}")

    # ─────────────────────────────────────────────
    # STEP 4: Check no temp DB files before trials
    # ─────────────────────────────────────────────
    log("\n--- STEP 4: Check for temp DB files before trials ---")
    try:
        temp_files = find_temp_db_files()
        if temp_files:
            report("4", "FAIL", f"Temp DB files found before worker trials: {temp_files}")
        else:
            report("4", "PASS", "No test_*.db files (expected before worker trials)")
    except Exception as e:
        report("4", "FAIL", f"Exception: {e}\n{traceback.format_exc()}")

    # ─────────────────────────────────────────────
    # STEP 5: Approve worker trials
    # ─────────────────────────────────────────────
    log("\n--- STEP 5: Approve worker trials ---")
    try:
        log("  Sending: 'yes, run worker trials'")
        resp5 = chat_post(USER_ID, opt_sid, "yes, run worker trials", timeout=240.0)
        reply5 = resp5.get("reply", "")
        log(f"  Reply length: {len(reply5)}")
        log(f"  Reply (first 500): {reply5[:500]}")

        if reply5:
            if "Error" in reply5[:50]:
                report("5", "FAIL", f"Worker trials returned error: {reply5[:300]}")
            else:
                report("5", "PASS", f"Worker trials done. Reply length={len(reply5)}")
        else:
            report("5", "FAIL", "Empty reply from worker trials")
    except requests.exceptions.Timeout:
        report("5", "FAIL", "Timeout waiting for worker trials (240s)")
        log("  Latest interactions in optimizer session:")
        for ix in get_latest_interactions(opt_sid, 5):
            log(f"    {ix['role']}: {ix['content'][:200] if ix['content'] else '(empty)'}")
    except Exception as e:
        report("5", "FAIL", f"Exception: {e}\n{traceback.format_exc()}")

    # ─────────────────────────────────────────────
    # STEP 6: Verify temp DB files exist
    # ─────────────────────────────────────────────
    log("\n--- STEP 6: Verify temp DB files ---")
    try:
        temp_files = find_temp_db_files()
        log(f"  Found {len(temp_files)} temp DB file(s): {[os.path.basename(f) for f in temp_files]}")

        if not temp_files:
            report("6", "FAIL", "No test_*.db files created by worker trials")
        else:
            errors = []
            for tf in temp_files:
                bn = os.path.basename(tf)
                log(f"  Inspecting: {bn}")
                try:
                    conn = sqlite3.connect(tf)
                    c = conn.cursor()

                    # Check agents table
                    agents = c.execute("SELECT id, user_id, system_prompt FROM agents").fetchall()
                    log(f"    agents: {len(agents)} rows")
                    if len(agents) == 0:
                        errors.append(f"{bn}: agents empty")
                    else:
                        for a in agents:
                            if not a[2] or len(a[2]) < 50:
                                errors.append(f"{bn}: agent prompt too short ({len(a[2]) if a[2] else 0} chars)")

                    # Check context_documents (optimizer agents should NOT have context docs)
                    ctx_count = c.execute("SELECT COUNT(*) FROM context_documents").fetchone()[0]
                    log(f"    context_documents: {ctx_count} rows")
                    # Note: it's OK if context docs exist (they might be copied from source)
                    # We just verify the schema works

                    # Check interactions
                    interactions = c.execute(
                        "SELECT role, content FROM interactions ORDER BY created_at"
                    ).fetchall()
                    log(f"    interactions: {len(interactions)} rows")
                    for ir in interactions:
                        snip = (ir[1] or "")[:80]
                        log(f"      {ir[0]}: {snip}...")

                    if len(interactions) < 2:
                        errors.append(f"{bn}: expected >=2 interactions, got {len(interactions)}")
                    else:
                        if interactions[0][0] != "user":
                            errors.append(f"{bn}: first role should be 'user', got '{interactions[0][0]}'")
                        if interactions[1][0] != "assistant":
                            errors.append(f"{bn}: second role should be 'assistant', got '{interactions[1][0]}'")
                        asst_content = interactions[1][1] or ""
                        if asst_content.startswith("Error:"):
                            errors.append(f"{bn}: assistant response starts with 'Error:'")

                    conn.close()
                except sqlite3.Error as e:
                    errors.append(f"{bn}: sqlite error: {e}")

            if errors:
                report("6", "FAIL", "; ".join(errors))
            else:
                report("6", "PASS", f"{len(temp_files)} temp DB(s) with valid schema and data")
    except Exception as e:
        report("6", "FAIL", f"Exception: {e}\n{traceback.format_exc()}")

    # ─────────────────────────────────────────────
    # STEP 7: Verify local.db is clean
    # ─────────────────────────────────────────────
    log("\n--- STEP 7: Verify local.db is clean ---")
    try:
        conn = db_conn()
        errors = []

        worker_agents = conn.execute(
            "SELECT id, user_id FROM agents WHERE user_id LIKE 'worker-test-%'"
        ).fetchall()
        if worker_agents:
            errors.append(f"{len(worker_agents)} worker agents leaked: {[(r[0][:8], r[1]) for r in worker_agents]}")

        trial_sessions = conn.execute(
            "SELECT id FROM sessions WHERE id LIKE 'trial-%'"
        ).fetchall()
        if trial_sessions:
            errors.append(f"{len(trial_sessions)} trial sessions leaked: {[r[0] for r in trial_sessions]}")

        trial_interactions = conn.execute(
            "SELECT COUNT(*) FROM interactions WHERE session_id LIKE 'trial-%'"
        ).fetchone()[0]
        if trial_interactions > 0:
            errors.append(f"{trial_interactions} trial interactions leaked")

        conn.close()
        if errors:
            report("7", "FAIL", "; ".join(errors))
        else:
            report("7", "PASS", "No worker/trial data leaked in local.db")
    except Exception as e:
        report("7", "FAIL", f"Exception: {e}\n{traceback.format_exc()}")

    # ─────────────────────────────────────────────
    # STEP 8: Check session metadata for test_db_paths
    # ─────────────────────────────────────────────
    log("\n--- STEP 8: Check session metadata ---")
    try:
        metadata = get_session_metadata(opt_sid)
        test_db_paths = metadata.get("test_db_paths", [])
        opt_role = metadata.get("opt_role", "unknown")
        log(f"  opt_role: {opt_role}")
        log(f"  metadata keys: {list(metadata.keys())}")
        log(f"  test_db_paths: {test_db_paths}")

        if test_db_paths:
            for p in test_db_paths:
                abs_p = os.path.normpath(os.path.join(PROJECT_ROOT, p))
                log(f"  Path: {p} -> exists={os.path.exists(abs_p)}")
            report("8", "PASS", f"test_db_paths found: {test_db_paths}")
        else:
            # Maybe handoff hasn't happened yet - check if status is still 'planner'
            if opt_role == "finalizer":
                report("8", "FAIL", "Handoff occurred but no test_db_paths")
            else:
                report("8", "INFO", f"opt_role={opt_role}, test_db_paths not yet expected")
    except Exception as e:
        report("8", "FAIL", f"Exception: {e}\n{traceback.format_exc()}")

    # ─────────────────────────────────────────────
    # STEP 9: Continue to Finalizer
    # ─────────────────────────────────────────────
    log("\n--- STEP 9: Handoff to Finalizer ---")
    try:
        log("  Sending: 'looks good, hand off to finalizer'")
        resp9 = chat_post(USER_ID, opt_sid, "looks good, hand off to finalizer", timeout=240.0)
        reply9 = resp9.get("reply", "")
        log(f"  Reply length: {len(reply9)}")
        log(f"  Reply (first 500): {reply9[:500]}")

        # Check metadata after handoff
        metadata = get_session_metadata(opt_sid)
        test_db_paths = metadata.get("test_db_paths", [])
        opt_role = metadata.get("opt_role", "unknown")
        log(f"  After handoff: opt_role={opt_role}, test_db_paths={test_db_paths}")

        # Check if finalizer response references trial data
        trial_keywords = ["trial", "worker", "test_", "24-hour", "time format", "evaluation", "transcript"]
        has_trial_ref = any(kw in reply9.lower() for kw in trial_keywords)

        if opt_role == "finalizer" and test_db_paths:
            report("9a", "PASS", f"Handoff complete: opt_role={opt_role}, test_db_paths present")
        elif opt_role == "finalizer":
            report("9a", "PASS", f"Handoff complete: opt_role={opt_role}")
        else:
            report("9a", "INFO", f"opt_role={opt_role} after handoff message")

        if reply9 and has_trial_ref:
            report("9b", "PASS", "Finalizer response references trial/worker data from temp DBs")
        elif reply9:
            report("9b", "INFO", "Finalizer responded (no explicit trial ref detected)")
        else:
            report("9b", "FAIL", "Empty finalizer response")

    except requests.exceptions.Timeout:
        report("9", "FAIL", "Timeout waiting for Finalizer (240s)")
        log("  Latest interactions:")
        for ix in get_latest_interactions(opt_sid, 5):
            log(f"    {ix['role']}: {ix['content'][:200]}")
    except Exception as e:
        report("9", "FAIL", f"Exception: {e}\n{traceback.format_exc()}")

    # ── Final Summary ──
    _print_summary()
    if results["failed"] > 0:
        sys.exit(1)


def _print_summary():
    print("\n" + "=" * 60)
    print(f"  TEST SUMMARY: {results['passed']} passed, {results['failed']} failed")
    print("=" * 60)
    for s in results["steps"]:
        print(f"    Step {s['step']}: [{s['status']}]")
        if s.get("detail"):
            for line in s["detail"].split("\n")[:3]:
                print(f"      {line}")
            if len(s["detail"].split("\n")) > 3:
                print(f"      ...")


if __name__ == "__main__":
    run_test()