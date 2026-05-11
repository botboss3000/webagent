import sqlite3
import uuid
from datetime import datetime

DB_PATH = "app/db/local.db"
USER_ID = "test-user-1"
OPT_SID = "optimizer-conv-test"
ORIG_SID = "conv-test-1"

def insert_msg(sid, role, content, source):
    db = sqlite3.connect(DB_PATH)
    try:
        c = db.cursor()
        c.execute(
            "INSERT INTO interactions (id,session_id,role,content,source,channel,created_at) VALUES (?,?,?,?,?,'optimizer',datetime('now'))",
            (str(uuid.uuid4()), sid, role, content, source)
        )
        db.commit()
    finally:
        db.close()

# Ensure optimizer session exists
db = sqlite3.connect(DB_PATH)
db.execute("INSERT OR IGNORE INTO sessions (id,user_id,title,created_at,updated_at) VALUES (?,?,?,datetime('now'),datetime('now'))", 
           (OPT_SID, USER_ID, f"Optimizer — {ORIG_SID}"))
db.commit()
db.close()

# Log the conversation so far
insert_msg(OPT_SID, "user", "I want the webAgent to respond with 'Hi Human' when I say 'hi'. What approaches do you recommend?", "optimizer:human")
insert_msg(OPT_SID, "assistant", """## Planner Analysis

I've analyzed the current system and found 3 approaches to make the webAgent respond 'Hi Human' while preserving all existing functionality.

### Current State
- System prompt: "Hi! How can I help?"
- Agent Identity doc: Describes agent as "Bot Boss, an AI-powered assistant"
- Current response to "hi": Long verbose greeting with emojis and capabilities list

### Approach 1: Modify system_prompt (RECOMMENDED)
Change: Append a greeting rule to the system_prompt
Content to add: "IMPORTANT GREETING RULE: When the user greets you with just 'hi', 'hello', or similar simple greetings, respond with 'Hi Human' as your opening. Keep the rest of your response unchanged after the greeting."
Risk: Low — purely additive, no functionality removed, immediate effect, no restart needed.

### Approach 2: Update Agent Identity Context Document
Change: Add a 'Greeting Behavior' section to the Agent Identity doc
Risk: Low-Medium — clean separation of concerns, but depends on context document loading order.

### Approach 3: Modify system_prompt.md file
Change: Add 'Greeting Directive' section to the file-based prompt source
Risk: Medium — requires server restart, depends on composition logic.

### My Recommendation
Approach 1 is best — it's immediate, purely additive, and easiest to verify. Approach 2 is a good secondary option for better organization.
""", "optimizer:planner")

print("Conversation logged to optimizer session:", OPT_SID)
