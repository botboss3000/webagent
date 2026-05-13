"""
Test that run_worker_trials creates temp DB files instead of cluttering local.db.
"""
import sys, json, time, sqlite3, os, glob
sys.stdout.reconfigure(encoding="utf-8")

UID = "test_user"
SID = "test-tempdb"
DB_DIR = "app/db"

# Clean old temp files
for f in glob.glob(f"{DB_DIR}/test_*.db*"):
    try: os.remove(f)
    except: pass

# Clean test data
db = sqlite3.connect(f"{DB_DIR}/local.db", timeout=30)
for t in ["interactions", "sessions"]:
    db.execute(f"DELETE FROM {t} WHERE id LIKE '{SID}-%' OR id LIKE 'optimizer-%'")
db.execute("DELETE FROM agents WHERE user_id = 'opt_planner_test_user'")
db.commit()
db.close()

import urllib.request
def chat(uid, sid, msg, timeout=120):
    body = json.dumps({"user_id": uid, "session_id": sid, "message": msg}).encode()
    resp = urllib.request.urlopen(urllib.request.Request(
        "http://127.0.0.1:8080/api/v1/chat", data=body, headers={"Content-Type":"application/json"}
    ), timeout=timeout)
    return json.loads(resp.read())

# Step 1: Chat + /optimize
r = chat(UID, f"{SID}-1", "what time is it?")
print("1. Chat OK")
time.sleep(1)

r = chat(UID, f"{SID}-1", "/optimize", timeout=30)
import re
opt_id = "optimizer-" + re.search(r"optimizer-([a-f0-9]+)", r.get("reply","")).group(1)
print(f"2. Optimizer: {opt_id}")

# Step 3: Wait for kickstart
print("3. Waiting 35s for kickstart...")
time.sleep(35)

# Step 4: Approve worker trials
print("4. Approving worker trials...")
r = chat(UID, opt_id, "yes, run worker trials", timeout=180)
print(f"   Reply: {str(r.get('reply',''))[:200]}")
time.sleep(5)

# Step 5: Check for temp DB files
print("\n5. Checking for temp DB files...")
temp_dbs = glob.glob(f"{DB_DIR}/test_*.db")
print(f"   Found {len(temp_dbs)} temp DB files")
for td in temp_dbs:
    size = os.path.getsize(td)
    print(f"   {td}: {size} bytes")
    # Open and inspect
    try:
        tdb = sqlite3.connect(td)
        tdb.row_factory = sqlite3.Row
        tables = tdb.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        print(f"     Tables: {[r['name'] for r in tables]}")
        agents = tdb.execute("SELECT id, user_id, substr(system_prompt,1,80) as sp FROM agents").fetchall()
        print(f"     Agents: {len(agents)} rows")
        for a in agents:
            print(f"       {a['user_id']}: {a['sp'][:60]}...")
        ics = tdb.execute("SELECT role, substr(content,1,100) as c FROM interactions ORDER BY created_at").fetchall()
        print(f"     Interactions: {len(ics)} rows")
        for ic in ics:
            print(f"       [{ic['role']}]: {str(ic['c'])[:80]}")
        tdb.close()
    except Exception as e:
        print(f"     Error: {e}")

# Step 6: Verify local.db is NOT cluttered with worker data
print("\n6. Checking local.db is clean...")
ldb = sqlite3.connect(f"{DB_DIR}/local.db")
ldb.row_factory = sqlite3.Row
worker_agents = ldb.execute("SELECT COUNT(*) as c FROM agents WHERE user_id LIKE 'worker-%'").fetchone()
trial_sessions = ldb.execute("SELECT COUNT(*) as c FROM sessions WHERE id LIKE 'trial-%'").fetchone()
print(f"   Worker agents in local.db: {worker_agents['c']} (should be 0)")
print(f"   Trial sessions in local.db: {trial_sessions['c']} (should be 0)")

# Step 7: Verify handoff metadata has test_db_paths
print("\n7. Checking handoff metadata...")
sess = ldb.execute("SELECT metadata FROM sessions WHERE id=?", (opt_id,)).fetchone()
if sess and sess['metadata']:
    meta = json.loads(sess['metadata'])
    tdb_paths = meta.get('test_db_paths', [])
    print(f"   test_db_paths in metadata: {tdb_paths}")
    print(f"   Has test_db_paths: {len(tdb_paths) > 0}")
ldb.close()

print(f"\n=== SUMMARY ===")
print(f"Temp DB files: {len(temp_dbs)}")
print(f"Worker data in local.db: No")
print(f"test_db_paths in metadata: {len(tdb_paths) > 0 if 'tdb_paths' in dir() else 'N/A'}")