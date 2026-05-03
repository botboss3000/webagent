#!/usr/bin/env python3
"""
Final test script for webAgent after database migration.
Run this after creating the Supabase tables.
"""
import os
import sys
import asyncio
import requests
from dotenv import load_dotenv

load_dotenv()

async def test_complete_setup():
    print("=" * 60)
    print("webAgent - Complete Setup Test")
    print("=" * 60)
    
    # Check environment
    api_key = os.environ.get("OPENROUTER_API_KEY")
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    
    if not api_key:
        print("❌ OPENROUTER_API_KEY not set")
        return False
    if not supabase_url or not supabase_key:
        print("❌ Supabase credentials not set")
        return False
    
    print("✅ Environment variables loaded")
    
    # Test Supabase connection and tables
    print("\n1. Testing Supabase tables...")
    try:
        from supabase import create_client
        client = create_client(supabase_url, supabase_key)
        
        # Check messages table
        try:
            result = client.table('messages').select('count', count='exact').limit(1).execute()
            print(f"   ✅ messages table: {result.count} rows")
        except Exception as e:
            print(f"   ❌ messages table error: {e}")
            print("   ➡️  Run the SQL migration first!")
            return False
        
        # Check context table (Web Portal schema)
        try:
            result = client.table("context").select("count", count="exact").limit(1).execute()
            print(f"   ✅ context table: {result.count} rows")
        except Exception as e:
            print(f"   ❌ context table error: {e}")
            print("   ➡️  Apply Web Portal/supabase/migrations/ first!")
            return False
        
    except Exception as e:
        print(f"❌ Supabase connection failed: {e}")
        return False
    
    # Start server if not running
    print("\n2. Testing API server...")
    try:
        response = requests.get("http://localhost:8000/health", timeout=5)
        if response.status_code == 200:
            print("   ✅ Server is running")
            server_started = False
        else:
            print("   ℹ️  Starting server...")
            # We would start server here, but for now just note
            print("   ➡️  Run: uvicorn app.main:app --reload --host 0.0.0.0 --port 8000")
            server_started = True
    except requests.exceptions.ConnectionError:
        print("   ℹ️  Server not running")
        print("   ➡️  Start server with: uvicorn app.main:app --reload --host 0.0.0.0 --port 8000")
        server_started = True
    
    # Test endpoints (if server is running)
    print("\n3. Testing API endpoints...")
    try:
        # Test seed endpoint
        response = requests.post("http://localhost:8000/api/v1/seed-docs?user_id=test_user_auto", timeout=10)
        if response.status_code == 200:
            print("   ✅ Seed documents endpoint works")
            data = response.json()
            print(f"   ➡️  Inserted {len(data.get('document_ids', []))} documents")
        else:
            print(f"   ❌ Seed endpoint failed: {response.status_code}")
            print(f"   Response: {response.text[:200]}")
    except requests.exceptions.ConnectionError:
        print("   ⚠️  Server not running, skipping endpoint tests")
    
    # Final instructions
    print("\n" + "=" * 60)
    print("✅ Setup almost complete!")
    print("\nNext steps:")
    print("1. Start the server:")
    print("   cd ~/projects/webagent")
    print("   .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000")
    print("\n2. In another terminal, seed documents:")
    print("   curl -X POST \"http://localhost:8000/api/v1/seed-docs?user_id=test_user\"")
    print("\n3. Test chat:")
    print("   curl -X POST \"http://localhost:8000/api/v1/chat\" \\")
    print("     -H \"Content-Type: application/json\" \\")
    print("     -d '{\"user_id\":\"test_user\",\"session_id\":\"test\",\"message\":\"Hello!\"}'")
    print("\n4. Open test_interface.html in your browser!")
    print("\n5. Connect your web app to: http://localhost:8000/api/v1/chat")
    
    return True

if __name__ == "__main__":
    success = asyncio.run(test_complete_setup())
    sys.exit(0 if success else 1)