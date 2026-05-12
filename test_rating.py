import asyncio
from app.admin.settings import update_multi_provider_rating

async def main():
    await update_multi_provider_rating("admin_default", "deepinfra", "Qwen/Qwen2.5-72B-Instruct", 1)

asyncio.run(main())
