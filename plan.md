## Completed: optimizer.json config fix

Read `optimizer.json` in project root.

Changes made:
- `mode`: `"live"` → `"scheduled"` — optimizer will no longer auto-trigger on every chat message
- `user_feedback`: `"always"` → `"always"` (already set, kept as-is)
- Added/verified `"schedule": {"interval": "per-interaction", "min_interactions": 0}`

Config written back as valid JSON to `optimizer.json`.
