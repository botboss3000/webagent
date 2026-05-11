import sqlite3

db = sqlite3.connect("app/db/local.db")
cur = db.cursor()

cur.execute("SELECT id, status, started_at, completed_at, summary, errors FROM optimizer_runs ORDER BY started_at DESC LIMIT 1;")
run = cur.fetchone()

if run:
    print(f"Run ID: {run[0]}")
    print(f"Status: {run[1]}")
    print(f"Started: {run[2]}")
    print(f"Completed: {run[3]}")
    print(f"Summary: {run[4]}")
    print(f"Errors: {run[5]}")

    print("\nInteractions in optimizer session:")
    cur.execute("SELECT role, content FROM interactions WHERE session_id = 'optimizer-manual-test' ORDER BY created_at ASC;")
    for row in cur.fetchall():
        print(f"[{row[0].upper()}] {row[1]}")
else:
    print("No optimizer runs found.")

db.close()
