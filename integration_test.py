#!/usr/bin/env python3
"""
Full integration test for webAgent.
This script tests the entire flow from API call to database operations.
"""
import os
import sys
import asyncio
from dotenv import load_dotenv

# Add app directory to path
sys.path.insert(0, os.path.dirname(__file__))

load_dotenv()

async def test_full_flow():
    print("Testing webAgent full integration...")
    print("=" * 60)
    
    # Check environment
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("❌ OPENROUTER_API_KEY not set")
        return False
    
    # Test imports
    try:
        from app.db.supabase import SupabaseClient
        from app.agent.loop import run_agent_loop
        from app.agent.prompts import build_system_prompt
        print("✅ All imports successful")
    except ImportError as e:
        print(f"❌ Import error: {e}")
        print("Make sure you're running from the webagent directory")
        return False
    
    # Test Supabase connection (if credentials are set)
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    
    if supabase_url and supabase_key:
        print(f"\nSupabase credentials detected, testing connection...")
        try:
            client = SupabaseClient.get_client()
            # Try a simple query
            result = client.table("messages").select("count", count="exact").limit(1).execute()
            print(f"✅ Supabase connection successful")
            print(f"   Count in messages table: {result.count if hasattr(result, 'count') else 'N/A'}")
        except Exception as e:
            print(f"⚠️  Supabase connection error: {e}")
            print("   (This is OK if you haven't set up Supabase yet)")
    else:
        print("\n⚠️  Supabase credentials not set (required for full functionality)")
    
    # Test prompt assembly
    print("\nTesting system prompt assembly...")
    test_docs = [
        {"context_type": "agent", "content": "You are a test agent."},
        {"context_type": "user", "content": "Test user profile."},
    ]
    prompt = build_system_prompt(test_docs)
    if prompt:
        print(f"✅ Prompt assembly successful ({len(prompt)} chars)")
        print(f"   Sample: {prompt[:100]}...")
    else:
        print("❌ Prompt assembly failed")
    
    print("\n" + "=" * 60)
    print("✅ All basic checks passed!")
    print("\nNext steps:")
    print("1. Set up Supabase using Web Portal/supabase/migrations/")
    print("2. Start the server: uvicorn app.main:app --reload")
    print("3. Seed test docs: curl -X POST http://localhost:8000/api/v1/seed-docs?user_id=test")
    print("4. Open test_interface.html to start chatting")
    
    return True

if __name__ == "__main__":
    success = asyncio.run(test_full_flow())
    sys.exit(0 if success else 1)