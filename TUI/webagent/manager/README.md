# `manager/` — the Server Manager's human-readable configuration

These files hold everything about the Server Manager that is **prose or
settings**, not logic — so the agent's behaviour can be read and changed without
editing Python or rebuilding the `.exe`. They are loaded at runtime by
[`../resources.py`](../resources.py) and bundled into the frozen build by
[`../../scripts/build_exe.py`](../../scripts/build_exe.py).

| File | What it is | Edited by |
|------|-----------|-----------|
| `prompt.md` | The agent's **system prompt** (its whole instruction sheet). | You, or the agent (`edit_source`). Loaded at startup → **restart to apply**. |
| `tools.json` | The **tool list**: each tool's human-readable `description`, whether it's `enabled`, and its `category`. The code binds names → handlers + parameter schemas; the prose lives here. | You, or the agent. Restart to apply. |
| `monitor.defaults.json` | Shipped **defaults** for the background watchdog (interval, autonomy, channels, thresholds). | Maintainers. Seeds the live copy on first run. |

## Resolution order (see `resources.py`)

1. **User override** — a copy in the per-user data dir (`%APPDATA%\webagent\`
   on Windows). Wins when present, so a user can edit behaviour without touching
   the install. (`prompt.md`, `tools.json`.)
2. **Packaged default** — the file here (also inside the bundled `.exe`).
3. **Built-in fallback** — a short hard-coded default, so a missing or corrupt
   file never stops the manager from running.

## Live (per-machine) runtime files — NOT here, NOT in git

The watchdog's **live** state lives in the data dir and is gitignored:

- `monitor.json` — the active watchdog config (seeded from `monitor.defaults.json`
  on first run; then edited by the `set_monitor_config` tool or the admin panel).
- `alarms.json` — the live alarm watch list (grown by the `add_alarm` tool when
  the user says "tell me every time X happens").

The watchdog ([`../watchdog.py`](../watchdog.py)) re-reads both **every tick**, so
changes apply live with no restart.

## Editing notes

- `tools.json`: a tool **missing** from the file keeps its Python default
  description and stays enabled. Set `"enabled": false` to hide one. Unknown names
  are ignored.
- After editing `prompt.md` or `tools.json`, the agent should tell the user to
  **restart the manager** (Python reloads nothing live).
- Prefer the `monitor_*` / `*_alarm` tools over hand-editing `monitor.json` /
  `alarms.json` — they validate input and the watchdog picks the change up live.
