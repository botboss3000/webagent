import asyncio
from openai import AsyncOpenAI

async def test_call():
    client = AsyncOpenAI(api_key="123", base_url="https://api.deepinfra.com/v1/openai")
    print("Sending...")
    try:
        stream = await client.chat.completions.create(
            model="Qwen/Qwen2.5-72B-Instruct",
            messages=[{"role": "user", "content": "hi"}],
            stream=True
        )
        async for chunk in stream:
            pass
        print("Done!")
    except Exception as e:
        print("Error:", e)

asyncio.run(test_call())
