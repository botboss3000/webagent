#!/usr/bin/env python3
"""
Test OpenRouter API key and model directly.
This script tests if your OpenRouter configuration works without the full app.
"""
import os
from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables
load_dotenv()

api_key = os.environ.get("OPENROUTER_API_KEY")
model = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v3.2")

if not api_key:
    print("❌ ERROR: OPENROUTER_API_KEY not found in .env or environment")
    print("Make sure you have a .env file with your OpenRouter key")
    exit(1)

print(f"Testing OpenRouter API key...")
print(f"Model: {model}")
print("=" * 50)

try:
    # Create a simple client instance
    client = OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
    )

    print("✅ OpenAI client instance created successfully")

    # Test with a simple message
    print("\nSending test message to OpenRouter...")
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "Hello! Reply with just 'OK' if you can hear me."}],
        temperature=0.0,
        max_tokens=100,
    )

    print(f"✅ Response received!")
    print(f"Content: {response.choices[0].message.content}")
    print(f"Model used: {response.model}")

    # Check for rate limit info
    if hasattr(response, 'headers'):
        headers = response.headers
        print(f"\nRate limit info from headers:")
        for key in ['x-ratelimit-limit-requests', 'x-ratelimit-remaining-requests']:
            if key in headers:
                print(f"  {key}: {headers[key]}")

except Exception as e:
    print(f"❌ Error testing OpenRouter: {e}")
    print("\nTroubleshooting tips:")
    print("1. Check your OpenRouter API key at https://openrouter.ai/keys")
    print("2. Verify the model name is correct")
    print("3. Check your internet connection")
    print("4. Make sure you have credits on OpenRouter")
    exit(1)

print("\n" + "=" * 50)
print("✅ OpenRouter configuration is working correctly!")
print("\nYour webAgent agent is ready to use with:")
print(f"• Model: {model}")
print(f"• API key: {api_key[:20]}...")
print("\nYou can now start the full application with:")
print("  uvicorn app.main:app --reload --host 0.0.0.0 --port 8000")
