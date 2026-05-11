import uvicorn
from fastapi import FastAPI, Query
from typing import List

app = FastAPI()

@app.delete("/reset")
async def reset_database(
    db: str = Query("local.db"),
    exclude: List[str] = Query(default=["context_templates", "agent_templates"])
):
    return {"db": db, "exclude": exclude}

if __name__ == "__main__":
    import threading
    import time
    import requests
    
    def run():
        uvicorn.run(app, host="127.0.0.1", port=8005)
        
    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(1)
    
    try:
        # Test request
        print("Testing with no exclude:")
        r1 = requests.delete("http://127.0.0.1:8005/reset?db=local.db")
        print(r1.json())
        
        print("\nTesting with one exclude:")
        r2 = requests.delete("http://127.0.0.1:8005/reset?db=local.db&exclude=test_table")
        print(r2.json())
        
        print("\nTesting with multiple exclude:")
        r3 = requests.delete("http://127.0.0.1:8005/reset?db=local.db&exclude=test1&exclude=test2")
        print(r3.json())
        
        print("\nTesting with empty exclude array:")
        r4 = requests.delete("http://127.0.0.1:8005/reset?db=local.db&exclude=")
        print(r4.json())
    except Exception as e:
        print(f"Error: {e}")

