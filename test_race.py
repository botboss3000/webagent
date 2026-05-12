import asyncio
import time
from openai import AsyncOpenAI
import json

async def test_run():
    async def _stream_one(name, model):
        try:
            with open("race.log", "a") as f: f.write(f"[{time.time()}] {name} starting...\n")
            client = AsyncOpenAI(api_key="QYHUt3Qj68BKt24LB1haxamFD69l7K0W", base_url="https://api.deepinfra.com/v1/openai")
            stream = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "hi"}],
                stream=True
            )
            count = 0
            async for chunk in stream:
                count += 1
            with open("race.log", "a") as f: f.write(f"[{time.time()}] {name} DONE with {count} chunks\n")
        except asyncio.CancelledError:
            with open("race.log", "a") as f: f.write(f"[{time.time()}] {name} CANCELLED!\n")
        except Exception as e:
            with open("race.log", "a") as f: f.write(f"[{time.time()}] {name} ERROR: {e}\n")

    with open("race.log", "w") as f: f.write("")
    
    t1 = asyncio.create_task(_stream_one("Qwen", "Qwen/Qwen2.5-72B-Instruct"))
    t2 = asyncio.create_task(_stream_one("DeepSeek", "deepseek-ai/DeepSeek-V4-Flash"))
    
    await asyncio.gather(t1, t2)

asyncio.run(test_run())

with open("race.log", "r") as f:
    print(f.read())
