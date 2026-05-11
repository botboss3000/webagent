import sqlite3
import json

db = sqlite3.connect("app/db/local.db")
cur = db.cursor()

# Get context docs
cur.execute("SELECT id, title, content FROM context_documents LIMIT 5;")
docs = cur.fetchall()

print("Context Docs:")
for doc in docs:
    print(f"- {doc[1]} ({doc[0]})")
    print(f"Content excerpt: {doc[2][:100]}...\n")

# Get context templates that are seeded
cur.execute("SELECT id, title, content FROM context_templates WHERE context_type = 'optimizer' LIMIT 5;")
templates = cur.fetchall()

print("\nOptimizer Templates:")
for t in templates:
    print(f"- {t[1]} ({t[0]})")
    print(f"Content excerpt: {t[2][:100]}...\n")

db.close()
