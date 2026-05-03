#!/usr/bin/env python3
"""
webAgent - Setup Diagnostics & Verification
Run this script to check your setup step-by-step.
"""

import os
import sys
import subprocess
import json
import requests
from dotenv import load_dotenv

load_dotenv()

def print_step(text, status=None):
    if status == "success":
        print(f"✅ {text}")
    elif status == "error":
        print(f"❌ {text}")
    elif status == "warning":
        print(f"⚠️  {text}")
    else:
        print(f"🔍 {text}")

def check_env_vars():
    """Check required environment variables."""
    required = [
        ("OPENROUTER_API_KEY", "OpenRouter API key"),
        ("OPENROUTER_MODEL", "OpenRouter model (default: deepseek/deepseek-v3.2)"),
        ("SUPABASE_URL", "Supabase project URL"),
        ("SUPABASE_SERVICE_ROLE_KEY", "Supabase service role key"),
    ]
    
    all_ok = True
    for var, desc in required:
        value = os.getenv(var)
        if value:
            masked = value[:8] + "..." + value[-4:] if len(value) > 12 else "***"
            print_step(f"{desc}: {masked}", "success")
        else:
            print_step(f"{desc}: MISSING", "error")
            all_ok = False
    
    return all_ok

def check_python_version():
    """Check Python version."""
    version = sys.version_info
    if version.major == 3 and version.minor >= 10:
        print_step(f"Python {version.major}.{version.minor}.{version.micro}", "success")
        return True
    else:
        print_step(f"Python {version.major}.{version.minor}.{version.micro} (needs 3.10+)", "warning")
        return True  # Continue anyway

def check_imports():
    """Check if required packages are importable."""
    packages = [
        "fastapi",
        "supabase",
        "pydantic",
        "httpx",
    ]
    
    all_ok = True
    for pkg in packages:
        try:
            __import__(pkg)
            print_step(f"Package {pkg}", "success")
        except ImportError as e:
            print_step(f"Package {pkg}: {e}", "error")
            all_ok = False
    
    return all_ok

def test_openrouter():
    """Test OpenRouter API connection."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    model = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v3.2")
    
    if not api_key:
        print_step("OpenRouter API key not set", "error")
        return False
    
    try:
        import httpx
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "model": model,
            "messages": [{"role": "user", "content": "Respond with just 'OK'"}],
            "max_tokens": 10
        }
        response = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=data,
            timeout=30.0
        )
        
        if response.status_code == 200:
            result = response.json()
            reply = result["choices"][0]["message"]["content"].strip()
            print_step(f"OpenRouter API: Connected ({model}) - Response: '{reply}'", "success")
            return True
        else:
            print_step(f"OpenRouter API: HTTP {response.status_code} - {response.text[:100]}", "error")
            return False
    except Exception as e:
        print_step(f"OpenRouter API: Error - {e}", "error")
        return False

def test_supabase_connection():
    """Test Supabase connection."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    
    if not url or not key:
        print_step("Supabase credentials missing", "error")
        return False
    
    try:
        from supabase import create_client
        client = create_client(url, key)
        
        # Try to fetch OpenAPI spec
        import httpx
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        resp = httpx.get(f"{url}/rest/v1/", headers=headers)
        
        if resp.status_code == 200:
            print_step(f"Supabase: Connected to {url}", "success")
            
            required = [
                ("messages", "messages"),
                ("context", "context"),
                ("sessions", "sessions"),
            ]
            all_tables = True
            for table_name, label in required:
                try:
                    client.table(table_name).select("count", count="exact").limit(1).execute()
                    print_step(f"Supabase: {label} table exists", "success")
                except Exception as e:
                    if "PGRST205" in str(e) or "Could not find the table" in str(e):
                        print_step(
                            f"Supabase: {label} table MISSING — apply Web Portal/supabase/migrations",
                            "warning",
                        )
                        all_tables = False
                    else:
                        print_step(f"Supabase: Error checking {label} - {e}", "warning")
                        all_tables = False

            try:
                client.table("memories").select("count", count="exact").limit(1).execute()
                print_step("Supabase: memories table exists", "success")
            except Exception as e:
                if "PGRST205" in str(e) or "Could not find the table" in str(e):
                    print_step(
                        "Supabase: memories table MISSING (optional if migration not applied)",
                        "warning",
                    )
                else:
                    print_step(f"Supabase: Error checking memories - {e}", "warning")

            return all_tables
        else:
            print_step(f"Supabase: HTTP {resp.status_code} - {resp.text[:100]}", "error")
            return False
    except Exception as e:
        print_step(f"Supabase: Connection error - {e}", "error")
        return False

def test_fastapi_server():
    """Test if FastAPI server is running."""
    try:
        resp = requests.get("http://localhost:8000/health", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "healthy":
                print_step("FastAPI server: Running on http://localhost:8000", "success")
                return True
            else:
                print_step(f"FastAPI server: Unexpected response {data}", "warning")
                return False
        else:
            print_step(f"FastAPI server: HTTP {resp.status_code}", "warning")
            return False
    except requests.exceptions.ConnectionError:
        print_step("FastAPI server: Not running (connection refused)", "warning")
        return False
    except Exception as e:
        print_step(f"FastAPI server: Error - {e}", "warning")
        return False

def main():
    print("=" * 60)
    print("webAgent - Setup Diagnostics")
    print("=" * 60)
    
    checks = []
    
    print("\n1. Environment Variables:")
    checks.append(check_env_vars())
    
    print("\n2. Python Version:")
    checks.append(check_python_version())
    
    print("\n3. Package Imports:")
    checks.append(check_imports())
    
    print("\n4. OpenRouter API:")
    checks.append(test_openrouter())
    
    print("\n5. Supabase Connection:")
    checks.append(test_supabase_connection())
    
    print("\n6. FastAPI Server:")
    checks.append(test_fastapi_server())
    
    print("\n" + "=" * 60)
    
    # Summary
    print("SUMMARY:")
    
    if all(checks):
        print("✅ All checks passed! webAgent is ready.")
        print("\nNEXT STEP: Start the server with:")
        print("  cd ~/projects/webagent && ./start_webagent.sh")
        print("\nThen open test_interface.html in your browser.")
    else:
        print("⚠️  Some checks failed.")
        
        # Check if it's just missing database tables
        from supabase import create_client
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        
        if url and key:
            try:
                client = create_client(url, key)
                client.table("messages").select("count", count="exact").limit(1).execute()
            except Exception as e:
                if "PGRST205" in str(e) or "Could not find the table" in str(e):
                    print("\n🔧 PRIMARY ISSUE: Database tables missing.")
                    print("\nSOLUTION: Apply the combined migration SQL:")
                    print("  Web Portal/supabase/migrations/20260130120000_webagent_complete_schema.sql")
                    print("Use Supabase CLI (supabase db push) or paste SQL in the Supabase SQL Editor.")
                    print("\nDo not use python_agent/migration_final.sql for the shared project schema.")
                    print("\nAfter migration, run this check again.")
        
        print("\nSee MIGRATION_INSTRUCTIONS.md for detailed steps.")
    
    print("=" * 60)

if __name__ == "__main__":
    main()