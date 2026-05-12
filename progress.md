# Progress

## Fix: Optimizer Auto-Trigger

**File changed:** `app/agent/loop.py`

**Problem:** `_fire_optimizer()` was called unconditionally on every chat message (lines 921, 934, 938, 944). With optimizer config mode='live', this triggered the full optimizer pipeline on EVERY user message, causing cascading Worker test sessions and recursive optimizer runs.

**Fix:** Added a config guard inside `_fire_optimizer()` (defined at line 32) that loads `optimizer.json` and checks the mode setting:
- If mode is `'live'` → fires optimizer as normal
- If mode is anything else (e.g. `'manual'`, `'scheduled'`, or unset) AND session_id doesn't contain `'manual'` → skips with a debug log
- Also skips Worker test sessions (handled by existing `worker-` prefix guard in `runner.py`)

This prevents the optimizer from running on casual chat messages while still allowing manual triggers and live-mode operation when explicitly configured.
