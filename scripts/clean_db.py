"""Force-clean the SQLite DB — checkpoint, switch to DELETE mode, clean test data."""
import sqlite3, os, time

DB_PATH = "data/db/local.db"

# Step 1: Try checkpoint to settle WAL
try:
    db = sqlite3.connect(DB_PATH, timeout=60)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    print("Checkpoint OK")
except Exception as e:
    print(f"Checkpoint error: {e}")

time.sleep(1)

# Step 2: Switch to DELETE mode to clear WAL files
try:
    db = sqlite3.connect(DB_PATH, timeout=60)
    db.execute("PRAGMA journal_mode=DELETE")
    db.close()
    print("Switched to DELETE mode")
except Exception as e:
    print(f"Mode switch error: {e}")

time.sleep(1)

# Step 3: Clean test data
try:
    db = sqlite3.connect(DB_PATH, timeout=60)
    for t in ["interactions", "sessions"]:
        db.execute("DELETE FROM " + t + " WHERE id LIKE 'test-%' OR id LIKE 'optimizer-%' OR id LIKE 'trial-%'")
    db.execute("DELETE FROM agents WHERE user_id LIKE 'test_%' OR user_id LIKE 'opt_%' OR user_id LIKE 'worker-%'")
    db.commit()
    print("Clean OK")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
except Exception as e:
    print(f"Clean error: {e}")
    # Last resort
    try:
        os.rename(DB_PATH + "-wal", DB_PATH + "-wal.bak")
        os.rename(DB_PATH + "-shm", DB_PATH + "-shm.bak")
        print("Renamed WAL files")
    except Exception as e2:
        print(f"Rename failed: {e2}")
