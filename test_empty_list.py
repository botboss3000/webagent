from typing import List
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

app = FastAPI()

@app.delete("/reset")
async def reset_database(
    db: str = Query("local.db"),
    exclude: List[str] = Query(default=["context_templates", "agent_templates"])
):
    exclude_set = set(e for e in exclude if e)
    return {"db": db, "exclude_set": list(exclude_set)}

client = TestClient(app)

print("No query params:", client.delete("/reset").json())
print("Specific exclude:", client.delete("/reset?exclude=interactions").json())
print("Empty exclude:", client.delete("/reset?exclude=").json())

