import requests
import json
import time
import os

token = os.environ.get("TOKEN", "")

headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {token}"
}

USER_ID = "test-user-1"
SESSION_ID = "test-opt-1"

# 1. Send "hi" to chat
print("Sending message...")
res = requests.post(
    "http://127.0.0.1:8000/api/v1/chat",
    json={"message": "hi", "session_id": SESSION_ID, "user_id": USER_ID},
    headers=headers
)
print("Chat status:", res.status_code)
print("Chat response:", res.text.encode("utf-8"))

time.sleep(2)

# 2. Run Optimizer with criteria
print("\nRunning optimizer...")
opt_res = requests.post(
    f"http://127.0.0.1:8000/admin/settings/optimizer/run?user_id=test-user-1&session_id=manual-test",
    json={"criteria": "agent identity MUST say 'hi Human'", "feedback": "Make sure agent identity document update makes me say 'hi Human'"},
    headers=headers
)
print("Opt status:", opt_res.status_code)
print("Opt response:", opt_res.text)

if opt_res.status_code == 200:
    data = opt_res.json()
    opt_session_id = data.get("optimizer_session_id")
    print(f"\nOptimizer Session ID: {opt_session_id}")
    
    # Wait for optimizer to process. It runs async.
    print("Waiting 30 seconds for optimizer to run...")
    time.sleep(30)
