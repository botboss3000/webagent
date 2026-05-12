import json

# Simulate faulty LLM output: JSON followed by trailing ``` and extra text
bad_output = '''{
  "message": "Test analysis",
  "analysis": "test",
  "changes": [{"element": "system_prompt", "element_type": "system_prompt", "change_type": "trim", "old_excerpt": "old", "new_content": "new", "expected_impact": {"turns_pct": 0, "tokens_pct": -80}, "risk": "low", "reasoning": "test"}]
}
```

Here is my additional analysis that should not be in the JSON.'''

# Apply the fix
text = bad_output.strip()
if text.startswith("```"):
    parts = text.split("```")
    text = parts[1].replace("json", "", 1).strip() if len(parts) > 1 else text
if not text.startswith('{'):
    brace = text.find('{')
    if brace >= 0:
        text = text[brace:]
last_brace = text.rfind('}')
if last_brace >= 0:
    text = text[:last_brace+1]

result = json.loads(text)
print("SUCCESS - parsed correctly")
print("Changes:", len(result.get("changes", [])))
print("Message:", result.get("message"))
