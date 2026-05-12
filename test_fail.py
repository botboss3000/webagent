import asyncio
from app.admin.settings import update_multi_provider_rating

async def main():
    await update_multi_provider_rating("test_user_shield_1", "deepinfra", "Qwen/Qwen2.5-72B-Instruct", 1)
    await update_multi_provider_rating("test_user_shield_1", "deepinfra", "deepseek-ai/DeepSeek-V4-Flash", -1)
    
asyncio.run(main())
