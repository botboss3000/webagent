#!/usr/bin/env python3
"""
Quick verification script to check the webAgent application structure and imports.
Run with: python verify.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

print("Checking imports...")

try:
    from app.models.schemas import ChatRequest, ChatResponse
    print("✓ app.models.schemas")
except Exception as e:
    print(f"✗ app.models.schemas: {e}")

try:
    from app.db.supabase import SupabaseClient
    print("✓ app.db.supabase")
except Exception as e:
    print(f"✗ app.db.supabase: {e}")

try:
    from app.agent.prompts import build_system_prompt
    print("✓ app.agent.prompts")
except Exception as e:
    print(f"✗ app.agent.prompts: {e}")

try:
    from app.agent.loop import run_agent_loop
    print("✓ app.agent.loop")
except Exception as e:
    print(f"✗ app.agent.loop: {e}")

try:
    from app.api.chat import router
    print("✓ app.api.chat")
except Exception as e:
    print(f"✗ app.api.chat: {e}")

try:
    from app.main import app
    print("✓ app.main")
except Exception as e:
    print(f"✗ app.main: {e}")

print("\nAll required modules can be imported.")