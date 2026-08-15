import sqlite3, json
conn = sqlite3.connect(r"C:\Users\Alex R\Projects\webagent-dev\data\db\local.db")
conn.row_factory = sqlite3.Row
rows = conn.execute("SELECT agent_id, config FROM agent_connections WHERE connection_type='agent_orchestration'").fetchall()
for row in rows:
    cfg = json.loads(row['config'] or "{}")
    print("AGENT:", row['agent_id'])
    print(json.dumps(cfg, indent=2))
conn.close()
