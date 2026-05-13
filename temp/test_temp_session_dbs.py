"""
Quick test: optimizer / finalizer sessions use their own temp DB files.

Tests core isolation:
  Step 1. Clean
  Step 2. Basic chat + /optimize
  Step 3. Check optimizer temp DB exists, local.db has NO interactions
  Step 4. Wait for kickstart, verify only temp DB has interactions
  Step 5-8: Handoff to finalizer, verify finalizer temp DB isolation

Usage: python -u temp/test_temp_session_dbs.py
"""

import glob, json, os, re, sqlite3, sys, time, traceback

API = "http://127.0.0.1:8080"
DB_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app", "db"))
LOCAL_DB = os.path.join(DB_DIR, "local.db")
TEST_USER = "test_user"
TEST_SESSION = "test-iso"

import urllib.request
import urllib.error

pass_count = fail_count = 0
step_results = []

def log(step, name, passed, detail=""):
    global pass_count, fail_count
    if passed: pass_count += 1
    else: fail_count += 1
    s = f"[Step {step}] {name}: {'PASS' if passed else 'FAIL'}"
    if detail: s += f" | {detail}"
    print(s.encode('ascii', errors='replace').decode('ascii'))
    step_results.append(s.encode('ascii', errors='replace').decode('ascii'))

def post(path, body, timeout=30):
    url = f"{API}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.read().decode()[:300]}
    except Exception as e:
        return {"error": str(e)}

def dbq(db, sql, params=()):
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        c = conn.execute(sql, params)
        r = c.fetchall()
        conn.commit()
        return r
    finally:
        conn.close()

def find_dbs(prefix):
    fs = glob.glob(os.path.join(DB_DIR, f"{prefix}_*.db"))
    fs.sort(key=os.path.getmtime)
    return fs

def clean_all():
    for p in ("optimizer_*.db*", "finalizer_*.db*", "test_*.db*"):
        for f in glob.glob(os.path.join(DB_DIR, p)):
            try: os.remove(f)
            except: pass
    conn = sqlite3.connect(LOCAL_DB)
    try:
        conn.execute("DELETE FROM interactions WHERE session_id LIKE 'optimizer-%' OR session_id LIKE 'finalizer-%'")
        conn.execute("DELETE FROM sessions WHERE id LIKE 'optimizer-%' OR id LIKE 'finalizer-%'")
        conn.execute("DELETE FROM agents WHERE user_id LIKE 'opt_%'")
        conn.execute("DELETE FROM interactions WHERE session_id=?", (TEST_SESSION,))
        conn.execute("DELETE FROM sessions WHERE id=?", (TEST_SESSION,))
        conn.commit()
    finally:
        conn.close()

def safe_print(s):
    print(s.encode('ascii', errors='replace').decode('ascii'))

# ═══════════════════════
safe_print("="*70)
safe_print("TEST: Optimizer / Finalizer Temp DB Isolation")
safe_print(f"Started: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
safe_print("="*70)

# Step 1: Clean
safe_print("\n--- Step 1: Clean ---")
clean_all()
opt = find_dbs("optimizer")
fin = find_dbs("finalizer")
tst = find_dbs("test")
os_ = dbq(LOCAL_DB, "SELECT COUNT(*) FROM sessions WHERE id LIKE 'optimizer-%'")[0][0]
fs_ = dbq(LOCAL_DB, "SELECT COUNT(*) FROM sessions WHERE id LIKE 'finalizer-%'")[0][0]
oa_ = dbq(LOCAL_DB, "SELECT COUNT(*) FROM agents WHERE user_id LIKE 'opt_%'")[0][0]
oi_ = dbq(LOCAL_DB, "SELECT COUNT(*) FROM interactions WHERE session_id LIKE 'optimizer-%'")[0][0]
fi_ = dbq(LOCAL_DB, "SELECT COUNT(*) FROM interactions WHERE session_id LIKE 'finalizer-%'")[0][0]
ok = len(opt)==0 and len(fin)==0 and len(tst)==0 and os_==0 and fs_==0 and oa_==0 and oi_==0 and fi_==0
log(1, "Clean", ok, f"opt_files={len(opt)} fin_files={len(fin)} test_files={len(tst)} "
    f"opt_sess={os_} fin_sess={fs_} opt_agents={oa_} opt_int={oi_} fin_int={fi_}")
if not ok:
    sys.exit(1)

# Step 2: Basic chat + /optimize
safe_print("\n--- Step 2: Chat + /optimize ---")
r1 = post("/api/v1/chat", {"user_id":TEST_USER,"session_id":TEST_SESSION,"message":"what time is it in Detroit?"})
log(2.1, "Chat reply", bool(r1.get("reply")), f"reply={str(r1.get('reply',''))[:80]}")
time.sleep(1)

r2 = post("/api/v1/chat", {"user_id":TEST_USER,"session_id":TEST_SESSION,"message":"/optimize"}, timeout=30)
safe_print(f"  /optimize reply: {str(r2.get('reply',''))[:200]}")
log(2.2, "/optimize accepted", bool(r2.get("reply")))
time.sleep(2)

# Extract optimizer session ID
opt_sid = None
m = re.search(r'optimizer-([a-f0-9]+)', r2.get("reply",""))
if m: opt_sid = f"optimizer-{m.group(1)}"
if not opt_sid:
    rows = dbq(LOCAL_DB, "SELECT id FROM sessions WHERE id LIKE 'optimizer-%' ORDER BY created_at DESC LIMIT 1")
    if rows: opt_sid = rows[0][0]
log(2.3, "Optimizer session ID found", bool(opt_sid), f"opt_sid={opt_sid}")
if not opt_sid: sys.exit(1)
safe_print(f"  OPT SESSION: {opt_sid}")

# Step 3: Check optimizer temp DB
safe_print("\n--- Step 3: Check optimizer temp DB ---")
time.sleep(5)
opt_dbs = find_dbs("optimizer")
log(3.1, "Optimizer temp DB file exists", len(opt_dbs)>0,
    f"files={[os.path.basename(p) for p in opt_dbs]}")
if not opt_dbs: sys.exit(1)
opt_db = opt_dbs[0]
safe_print(f"  Temp DB: {opt_db}")

# 3a: session row in temp DB
sr = dbq(opt_db, "SELECT id FROM sessions")
log(3.2, "Temp DB has session row", len(sr)>0, f"sessions={[r[0] for r in sr]}")

# 3b: planner agent in temp DB
ar = dbq(opt_db, "SELECT id, user_id, template_id FROM agents LIMIT 10")
hp = any("opt_planner" in str(r[2] or "") for r in ar)
log(3.3, "Temp DB has planner agent", hp, f"agents={[(r[1],r[2]) for r in ar]}")

# 3c: participants
pr = dbq(opt_db, "SELECT participants FROM sessions WHERE id=?", (opt_sid,))
has_part = False
if pr and pr[0][0]:
    try:
        parts = json.loads(pr[0][0])
        has_part = any(p.get("role")=="agent" for p in parts if isinstance(p,dict))
        log(3.4, "Participants include agent", has_part, f"participants={parts}")
    except: pass
if not pr or not pr[0][0]:
    log(3.4, "Participants not set", False, "no participants data")

# 3d: local.db has session with temp_db_path
lm = dbq(LOCAL_DB, "SELECT id, metadata FROM sessions WHERE id=?", (opt_sid,))
sid_in_local = len(lm)>0
tp_in_meta = False
if lm and lm[0][1]:
    meta = json.loads(lm[0][1])
    tp_in_meta = bool(meta.get("temp_db_path"))
log(3.5, "Local.db has session with temp_db_path in metadata",
    sid_in_local and tp_in_meta,
    f"session_in_local={sid_in_local} temp_db_in_meta={tp_in_meta}")

# 3e: local.db has ZERO interactions for optimizer session
oi_local = dbq(LOCAL_DB, "SELECT COUNT(*) FROM interactions WHERE session_id=?", (opt_sid,))[0][0]
if oi_local > 0:
    dr = dbq(LOCAL_DB, "SELECT role, content[:120], source, from_id FROM interactions WHERE session_id=? ORDER BY created_at",
             (opt_sid,))
    safe_print(f"  DEBUG: {oi_local} optimizer interactions in local.db:")
    for r in dr: safe_print(f"    role={r[0]} source={r[2]} from={r[3]} content={str(r[1])[:80]}")
log(3.6, "Local.db has ZERO optimizer interactions", oi_local==0,
    f"interactions in local.db={oi_local}")

# 3f: local.db has ZERO opt_ agents
oa_local = dbq(LOCAL_DB, "SELECT COUNT(*) FROM agents WHERE user_id LIKE 'opt_%'")[0][0]
log(3.7, "Local.db has ZERO opt_ agents", oa_local==0, f"opt_agents={oa_local}")

# Step 4: Wait for kickstart
safe_print("\n--- Step 4: Wait for kickstart (35s) ---")
for a in range(35):
    time.sleep(1)
    ic = dbq(opt_db, "SELECT COUNT(*) FROM interactions WHERE session_id=?", (opt_sid,))[0][0]
    if ic >= 2:
        safe_print(f"  Interactions after {a+1}s: {ic}")
        break
    if a % 10 == 9: safe_print(f"  ...{a+1}s, {ic} interactions")

ic = dbq(opt_db, "SELECT COUNT(*) FROM interactions WHERE session_id=?", (opt_sid,))[0][0]
log(4.1, "Temp DB has interactions", ic>=2, f"interactions in temp DB={ic}")

oi_local2 = dbq(LOCAL_DB, "SELECT COUNT(*) FROM interactions WHERE session_id=?", (opt_sid,))[0][0]
if oi_local2 > 0:
    dr = dbq(LOCAL_DB, "SELECT role, content[:120], source, from_id FROM interactions WHERE session_id=? ORDER BY created_at",
             (opt_sid,))
    safe_print(f"  DEBUG: {oi_local2} optimizer interactions in local.db:")
    for r in dr: safe_print(f"    role={r[0]} source={r[2]} from={r[3]} content={str(r[1])[:80]}")
log(4.2, "Local.db still ZERO optimizer interactions", oi_local2==0,
    f"interactions in local.db={oi_local2}")

# Check from_ids in temp DB
fi_ids = dbq(opt_db, "SELECT DISTINCT from_id FROM interactions WHERE session_id=? AND from_id IS NOT NULL", (opt_sid,))
safe_print(f"  from_ids in temp DB: {[r[0] for r in fi_ids]}")
uuid_fi = [r[0] for r in fi_ids if r[0] and r[0] != TEST_USER and r[0] != 'system']
log(4.3, "Temp DB from_ids include planner agent UUID", len(uuid_fi)>0,
    f"from_ids={[r[0] for r in fi_ids]}")

# ═══════════════════════
# Now try Steps 5-8 (worker trials + finalizer)
# ═══════════════════════

safe_print("\n--- Step 5: Approve worker trials ---")
r3 = post("/api/v1/chat", {
    "user_id": TEST_USER,
    "session_id": opt_sid,
    "message": "yes, run worker trials"
}, timeout=180)
safe_print(f"  Reply: {str(r3.get('reply',''))[:200]}")
log(5.1, "Worker trials approval sent", "reply" in r3,
    f"reply={str(r3.get('reply',''))[:120]}")

# Wait for test DBs
for a in range(30):
    tst = find_dbs("test")
    if len(tst) > 0:
        safe_print(f"  Test DBs after {a+1}s: {[os.path.basename(p) for p in tst]}")
        break
    time.sleep(2)
    if a % 10 == 9: safe_print(f"  ...{a+1}s")

tst = find_dbs("test")
log(5.2, "Worker test temp DBs exist", len(tst)>0, f"test DBs={[os.path.basename(p) for p in tst]}")

# Step 6: Handoff to finalizer
safe_print("\n--- Step 6: Handoff to Finalizer ---")
r4 = post("/api/v1/chat", {
    "user_id": TEST_USER,
    "session_id": opt_sid,
    "message": "looks good, hand off to finalizer"
}, timeout=180)
safe_print(f"  Reply: {str(r4.get('reply',''))[:400]}")
log(6.1, "Handoff sent", "reply" in r4,
    f"reply={str(r4.get('reply',''))[:200]}")

# Extract finalizer session ID
fin_sid = None
m = re.search(r'finalizer-([a-f0-9]+)', r4.get("reply",""))
if m: fin_sid = f"finalizer-{m.group(1)}"
if not fin_sid:
    try:
        d = json.loads(r4.get("reply","{}"))
        if isinstance(d, dict) and "finalizer_session_id" in d:
            fin_sid = d["finalizer_session_id"]
    except: pass
if not fin_sid:
    rows = dbq(LOCAL_DB, "SELECT id FROM sessions WHERE id LIKE 'finalizer-%' ORDER BY created_at DESC LIMIT 1")
    if rows: fin_sid = rows[0][0]
log(6.2, "Finalizer session ID found", bool(fin_sid), f"finalizer_session_id={fin_sid}")

# Step 7: Check finalizer temp DB
safe_print("\n--- Step 7: Check finalizer temp DB ---")
time.sleep(5)
fin_dbs = find_dbs("finalizer")
log(7.1, "Finalizer temp DB exists", len(fin_dbs)>0,
    f"files={[os.path.basename(p) for p in fin_dbs]}")

if fin_dbs:
    fin_db = fin_dbs[0]
    safe_safe_print(f"  Temp DB: {fin_db}")
    if fin_sid:
        fsr = dbq(fin_db, "SELECT id FROM sessions WHERE id=?", (fin_sid,))
        log(7.2, "Finalizer temp DB has session row", len(fsr)>0, f"session={fin_sid}")
    far = dbq(fin_db, "SELECT id, user_id, template_id FROM agents LIMIT 10")
    hfa = any("opt_finalizer" in str(r[2] or "") for r in far)
    log(7.3, "Temp DB has opt_finalizer agent", hfa, f"agents={[(r[1],r[2]) for r in far]}")

    # Wait for finalizer kickstart
    target = fin_sid
    if not target:
        sall = dbq(fin_db, "SELECT id FROM sessions")
        if sall: target = sall[0][0]
    if target:
        for a in range(20):
            fic = dbq(fin_db, "SELECT COUNT(*) FROM interactions WHERE session_id=?", (target,))[0][0]
            if fic >= 1:
                safe_print(f"  Finalizer interactions after {a+1}s: {fic}")
                break
            time.sleep(2)
            if a % 10 == 9: safe_print(f"  ...{a+1}s, {fic} ints")
        fic = dbq(fin_db, "SELECT COUNT(*) FROM interactions WHERE session_id=?", (target,))[0][0]
        log(7.4, "Finalizer temp DB has interactions", fic>=1, f"interactions={fic}")

        # from_ids in finalizer temp DB
        ffids = dbq(fin_db, "SELECT DISTINCT from_id FROM interactions WHERE session_id=? AND from_id IS NOT NULL", (target,))
        safe_print(f"  Finalizer from_ids: {[r[0] for r in ffids]}")
        uuid_ff = [r[0] for r in ffids if r[0] != TEST_USER and r[0] != 'system']
        log(7.5, "Finalizer from_ids include opt_finalizer agent UUID", len(uuid_ff)>0,
            f"from_ids={[r[0] for r in ffids]}")

        # local.db has ZERO finalizer interactions
        if fin_sid:
            fi_local = dbq(LOCAL_DB, "SELECT COUNT(*) FROM interactions WHERE session_id=?", (fin_sid,))[0][0]
            log(7.6, "Local.db ZERO finalizer interactions", fi_local==0,
                f"finalizer interactions in local.db={fi_local}")

# Step 8: Verify local.db is clean
safe_print("\n--- Step 8: Final local.db check ---")
opt_sess = dbq(LOCAL_DB, "SELECT id, metadata FROM sessions WHERE id LIKE 'optimizer-%'")
fin_sess = dbq(LOCAL_DB, "SELECT id, metadata FROM sessions WHERE id LIKE 'finalizer-%'")
wt = 0
wo = 0
for rows in (opt_sess, fin_sess):
    for r in rows:
        meta_raw = r[1]
        if meta_raw:
            try:
                meta = json.loads(meta_raw)
                if meta.get("temp_db_path"): wt += 1; continue
            except: pass
        wo += 1
all_opt_sess = dbq(LOCAL_DB, "SELECT COUNT(*) FROM sessions WHERE id LIKE 'optimizer-%'")[0][0]
log(8.1, "Sessions have metadata.temp_db_path", wo==0,
    f"with={wt} without={wo} total_opt_sess={all_opt_sess}")

t_oi = dbq(LOCAL_DB, "SELECT COUNT(*) FROM interactions WHERE session_id LIKE 'optimizer-%'")[0][0]
log(8.2, "Local.db ZERO optimizer interactions", t_oi==0, f"opt_interactions={t_oi}")

t_fi = dbq(LOCAL_DB, "SELECT COUNT(*) FROM interactions WHERE session_id LIKE 'finalizer-%'")[0][0]
log(8.3, "Local.db ZERO finalizer interactions", t_fi==0, f"fin_interactions={t_fi}")

t_oa = dbq(LOCAL_DB, "SELECT COUNT(*) FROM agents WHERE user_id LIKE 'opt_%'")[0][0]
log(8.4, "Local.db ZERO opt_ agents", t_oa==0, f"opt_agents={t_oa}")

t_cd = dbq(LOCAL_DB, "SELECT COUNT(*) FROM context_documents cd JOIN agents a ON cd.agent_id=a.id WHERE a.user_id LIKE 'opt_%'")[0][0]
log(8.5, "Local.db ZERO context docs for opt_ agents", t_cd==0, f"context_docs={t_cd}")

# Summary
safe_print("\n"+"="*70)
safe_print(f"RESULTS: {pass_count} PASS, {fail_count} FAIL, {pass_count+fail_count} TOTAL")
safe_print("="*70)
for s in step_results:
    safe_print(s)

# Cleanup
safe_print("\n--- Cleanup ---")
clean_all()
safe_print("Done")
sys.exit(0 if fail_count==0 else 1)