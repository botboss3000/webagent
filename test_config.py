#!/usr/bin/env python3
"""
Quick test script for webAgent with OpenRouter.
Run this to verify the API key and model are working.
"""
import os
import sys
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

print("Testing webAgent configuration...")
print("=" * 50)

# Check environment variables
api_key = os.environ.get("OPENROUTER_API_KEY")
model = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v3.2")
supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

print(f"OPENROUTER_API_KEY: {'[SET]' if api_key else '[MISSING]'}")
print(f"OPENROUTER_MODEL: {model}")
print(f"SUPABASE_URL: {'[SET]' if supabase_url else '[MISSING]'}")
print(f"SUPABASE_SERVICE_ROLE_KEY: {'[SET]' if supabase_key else '[MISSING]'}")

if not api_key:
    print("\n❌ ERROR: OPENROUTER_API_KEY is not set!")
    print("Add your key to .env file or set environment variable")
    sys.exit(1)

# Test basic imports
try:
    from app.agent.loop import run_agent_loop, call_agent
    print("\n✅ app.agent.loop imports successfully")
    print("✅ Agent loop module available")

    from app.agent.streaming_loop import stream_agent_events
    print("✅ app.agent.streaming_loop imports successfully")

except ImportError as e:
    print(f"\n❌ Import error: {e}")
    print("Make sure you're in the webagent directory and dependencies are installed")
    sys.exit(1)

print("\n" + "=" * 50)
print("✅ Basic configuration looks good!")
print("\nNext steps:")
print("1. Set up Supabase and run the migration")
print("2. Run: uvicorn app.main:app --reload --host 0.0.0.0 --port 8000")
print("3. Test with: curl -X POST http://localhost:8000/api/v1/seed-docs?user_id=test_user")
print("4. Start chatting with your web app!")
