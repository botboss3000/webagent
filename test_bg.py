import asyncio
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import uvicorn

app = FastAPI()

async def background_worker(name, delay):
    with open("bg_trace.log", "a") as f:
        f.write(f"{name}: started\n")
    
    try:
        for i in range(delay):
            await asyncio.sleep(1)
            with open("bg_trace.log", "a") as f:
                f.write(f"{name}: tick {i+1}\n")
        with open("bg_trace.log", "a") as f:
            f.write(f"{name}: completed successfully\n")
    except asyncio.CancelledError:
        with open("bg_trace.log", "a") as f:
            f.write(f"{name}: CANCELLED\n")
    except Exception as e:
        with open("bg_trace.log", "a") as f:
            f.write(f"{name}: ERROR {e}\n")

@app.get("/stream")
async def stream():
    async def event_generator():
        # start a 5 second background task
        asyncio.create_task(background_worker("slow_loser", 5))
        
        # fast response yields immediately and finishes
        yield "data: chunk 1\n\n"
        await asyncio.sleep(0.5)
        yield "data: [DONE]\n\n"
        
    return StreamingResponse(event_generator(), media_type="text/event-stream")

if __name__ == "__main__":
    import threading
    import time
    import requests
    
    def run_server():
        uvicorn.run(app, host="127.0.0.1", port=8001, log_level="error")
        
    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    time.sleep(1)
    
    print("Making request...")
    with open("bg_trace.log", "w") as f: f.write("")
    r = requests.get("http://127.0.0.1:8001/stream")
    print("Response:", r.text)
    
    print("Waiting 6s to see if background task finishes...")
    time.sleep(6)
    with open("bg_trace.log", "r") as f:
        print("Log output:")
        print(f.read())
