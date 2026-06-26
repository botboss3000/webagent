# Self-Improvement Reference — Manager Architecture & Conventions

## Where code lives (relative to `tui_app/`)

| File/Dir | Purpose |
|---|---|
| `__main__.py` | Entry point — `python -m tui_app` → `app.py:run()` |
| `app.py` | Textual TUI (chat screen, panels, user settings, server start/stop UI) |
| `agent.py` | `ServerManagerAgent` (built-in brain) — builds system prompt + history, calls LLM, dispatches tools. Handles onboarding, watchdog/synthetic turns, and fallback |
| `app_engine.py` | `AppEngine` (mk3 app front door) — launches the checkout's headless agent loop in its venv, handshake, serializes turns, translates loop events → `AgentEvent`; raises `EngineUnavailable` to trigger fallback |
| `engine_driver.py` | Runs under the **checkout's** interpreter (not the TUI's): boots the app's provider config + DB, materializes the `server-manager` agent for admin, streams the real `stream_agent_events` loop over JSON stdio |
| `resources.py` | Loads `prompt.md`, `tools.json`, `monitor.defaults.json` from manager dir |
| `config.py` | `TuiConfig`, `ProviderConfig`, data dir resolution, provider resolution |
| `db.py` | External SQLite store (chat history, audit trail) — separate from web app's local.db |
| `llm.py` | `LLMClient` — OpenAI-compatible chat completions via httpx (no openai SDK) |
| `safety.py` | Path resolution scoped to project, secret scanning, git checkpoints |
| `selfinfo.py` | `SelfInfo`, `UpdateStatus` — version/build introspection, remote check |
| `env_probe.py` | `MachineFacts` (static host info), `server_health()` (probe /health) |
| `onboarding.py` | Fetches live `onboarding-guide.md` from the public repo |
| `appclient.py` | `WebAppClient` — HTTP + WS client for driving the running web app |
| `monstate.py` | Persisted monitor config + alarm rules (per-user data dir) |
| `sysmetrics.py` | CPU/memory/disk/port probes — no psutil dependency (stdlib + ctypes) |
| `watchdog.py` | Background monitoring loop — health, diagnostics, alarms, auto-restart |
| `notify.py` | Desktop notifications (Win/Mac/Linux) — never raises |
| `procscan.py` | Processes scanning utility |
| `clip.py` | Clipboard read/write |
| `glyphs.py` | Emoji + ASCII glyph constants |
| `themes.py` | Theme system (23 themes) |
| `model_windows.py` | LLM context window sizes |
| `tui-data/prompt.md` | **THIS file** — loaded at startup; restart to apply changes |
| `tui-data/tools.json` | Human-readable tool descriptions + enable flags overlaying Python specs |
| `tui-data/monitor.defaults.json` | Shipped watchdog defaults |
| `self-improvement.md` | **This file** — back-end details for self-improvement work |

### Tool files (`tools/`)

| File | Tools it provides |
|---|---|
| `fs.py` | `read_source`, `write_source`, `edit_source`, `patch_source`, `delete_source`, `search_source`, `read_directory` |
| `shell.py` | `run_command`, `run_python` |
| `git.py` | `git_tool`, `resolve_conflict` |
| `install.py` | `check_install_readiness`, `clone_repo`, `setup_environment`, `seed_config`, `verify_install` |
| `server.py` | `server_status`, `server_start`, `server_stop`, `server_restart`, `server_logs` |
| `appctl.py` | `app_login`, `app_list_agents`, `app_chat` |
| `webapp.py` | `webapp_send`, `webapp_status`, `app_list_sessions`, `app_get_settings`, `app_set_settings`, `app_get_auth_keys`, `app_set_auth_keys` |
| `diagnostics.py` | `read_diagnostics` (server flight-recorder → `logs.db`) |
| `recordings.py` | `read_recordings` (browser render flight-recorder → `recordings.db`) |
| `monitor.py` | `monitor_status`, `server_resources`, `list_alarms`, `add_alarm`, `remove_alarm`, `set_monitor_config`, `notify_test` |
| `selfupdate.py` | `self_status`, `self_update`, `self_restart` |
| `update.py` | `check_updates` |
| `reset.py` | `reset_app` |
| `web_search.py` | `web_search` |
| `browser.py` | `browser_navigate`, `browser_snapshot`, `browser_screenshot`, `browser_click`, `browser_type`, `browser_evaluate`, `browser_close` |
| `manage.py` | `link_project`, `setup_launch_shortcut` |
| `registry.py` | `ToolRegistry` — assembles all specs, overlays tools.json, dispatches calls |
| `base.py` | `ToolContext`, `ToolSpec`, `WRITES_DISABLED_MSG` |

---

## How the agent loop works (`agent.py`)

1. `run_turn(session_id, user_text, on_event, situation)` is called
2. Messages are built: `system_prompt + [optional onboarding guide] + [current situation block] + history`
3. Tool schemas from `registry.schemas(has_project=...)` (onboarding mode hides project-requiring tools)
4. `llm.complete(messages, tools=tools)` — calls the LLM API
5. If the LLM returns `tool_calls`, they're **batched by conflict group**:
   - Same-file mutations (`edit_source`/`write_source`/`patch_source`/`delete_source`/`resolve_conflict`) on the same path → sequential
   - `run_command`/`run_python` each in their own group
   - Everything else → concurrent
6. Each tool result is persisted to `Store` and dispatched to the UI
7. Loop until the LLM answers with text or `max_turns` is hit

### System prompt loading
- `resources.py`: user override (`data_dir()/prompt.md`) → packaged default (`tui-data/prompt.md`) → built-in fallback string
- Loaded ONCE at startup by `agent.py:SYSTEM_PROMPT = load_prompt()`
- **Changes need a restart** — Python doesn't hot-reload module-level variables

---

## Tool architecture

### Adding a new tool
1. **Write the handler** in the appropriate `tools/*.py` file (or a new one). Handlers are async functions taking `(ctx: ToolContext, **kwargs)` returning `str`.
2. **Register it** in `registry.py:_base_specs()` as a `ToolSpec(name, description, params_schema, handler, mutating=..., needs_project=...)`
3. **Optionally** add a description/enable flag in `tui-data/tools.json`

### ToolSpec fields
```python
ToolSpec(
    name="my_tool",                    # must match the key in tools.json
    description="Does X and Y.",       # overridden by tools.json if present
    parameters={"type": "object", ...}, # JSON Schema for the LLM
    handler=my_handler,                # async (ctx, **kwargs) -> str
    mutating=False,                    # gates behind "Allow writes"
    needs_project=True,                # hidden in onboarding mode (no checkout linked)
)
```

### ToolContext fields
```python
ToolContext(
    project_root=Optional[Path],  # None in onboarding mode
    writes_enabled=bool,          # True if "Allow writes" or Autonomous is on
    autonomous=bool,              # True in Autonomous mode
    log=Callable,                 # stream a status line to the UI
    audit=Callable,               # (tool, args, ok, detail) → audit trail
    session_id=str,
    set_project=Optional[Callable],  # link a checkout (from app)
    app_provider=Optional[ProviderConfig],  # seeded into fresh installs
    request_exit=Optional[Callable],  # close the manager (self-update restart)
    webapp_client=Optional[WebAppClient],
    webapp_session_id=str,
    webapp_agent_id=str,
    webapp_agent_name=str,
    git_token=str,
)
```

### Mutating tool gating
Always check at the top:
```python
if not ctx.writes_enabled:
    return WRITES_DISABLED_MSG
```

---

## Config & data flow

### Data dir (`config.py:data_dir()`)
- **Windows**: `%APPDATA%\webagent`
- **macOS**: `~/Library/Application Support/webagent`
- **Linux**: `$XDG_DATA_HOME/webagent` or `~/.local/share/webagent`

### Config locations
| What | Where |
|---|---|
| TUI config (UI state) | `data_dir() / config.json` |
| TUI DB (chat history, audit) | `data_dir() / webagent.db` |
| Server PID tracking | `data_dir() / server.pid` |
| Server log capture | `data_dir() / server.log` |
| Watchdog config | `data_dir() / monitor.json` |
| Alarm rules | `data_dir() / alarms.json` |
| Onboarding guide cache | `data_dir() / onboarding-guide.md` |

### Provider resolution order (`config.py:resolve_provider`)
1. `WEBAGENT_API_KEY` / `WEBAGENT_BASE_URL` / `WEBAGENT_MODEL` env vars
2. UI override (saved via App panel with `provider_override=True`)
3. Linked repo's `provider.json` (the complete, coherent triple)
4. `LLM_*` / `OPENROUTER_*` env vars or `.env`
5. Saved TuiConfig (the app-level key, used during onboarding)
6. Built-in defaults (OpenRouter / deepseek-chat)

---

## Server lifecycle (`tools/server.py`)

- Start: spawns `run.py` from checkout's `.venv` as a **detached** process, writes PID to `data_dir()/server.pid`
- Stop: `taskkill /F /T` (Windows) or `SIGTERM` (POSIX)
- Log: captured to `data_dir()/server.log`
- Health: probes `http://127.0.0.1:8080/health`
- Cross-platform: no psutil dep; uses `os.kill`/`tasklist`/`taskkill`

---

## Driving the running web app (`appclient.py`, `tools/appctl.py`, `tools/webapp.py`)

- `WebAppClient` logs in as admin/admin via `POST /api/v1/auth/login`
- Holds a WebSocket subscription to `/api/v1/agent/ws` for live streaming
- Sends messages via `POST /api/v1/chat/send` or `POST /api/v1/chat` (sync)
- Admin API: `/api/v1/agents`, `/api/v1/db/sessions`, `/admin/settings/app`, `/admin/settings/provider`
- **Two modes**: standalone client (created when no ToolContext is available) or shared app-owned client

---

## Watchdog (`watchdog.py`, `monstate.py`)

- Background asyncio loop owned by `app.py`
- Each tick: probe health → check port → check resources → read new diagnostics → evaluate alarms
- Alarm rules: match by `contains` text, `level`, `category` — action `notify` or `auto_restart`
- Loudness: `every` (alert each time), `once` (one alert per signature), `digest` (batched summary)
- Autonomy levels: `notify` (alert only), `auto_restart` (may restart server), `self_heal` (may fix code)
- Crash-loop guard: tracks restarts per hour, pauses and escalates if rate exceeded
- Config lives in `data_dir()/monitor.json`, re-read every tick — changes apply live

---

## Constraints & gotchas

1. **Python 3.11–3.12 only** — never install/run webAgent on 3.13+. Never loosen pins.
2. **No psutil** — sysmetrics uses stdlib + ctypes + /proc. Keep it dependency-free.
3. **No openai SDK** — llm.py uses raw httpx. Keeps frozen exe size small.
4. **Never import webAgent app internals** — the manager talks to it over HTTP only.
5. **Never commit** `.env`, `local.db`, `provider.json`, `db_connection.json`, or other per-machine files. No force-push.
6. **Sensitive files list** (`safety.py:SENSITIVE_FILES`): `.env`, `app/db/local.db`, `app/auth/users.json`, `provider.json`, `app/db_mode.json`, `scheduler_config.json`.
7. **Backups are automatic** — `write_source`, `edit_source`, `patch_source`, `delete_source`, and git operations all auto-backup.
8. **File tools resolve paths against project_root** — relative paths are joined to the root; absolute paths are allowed but flagged.
9. **TUI DB is separate** from web app's `app/db/local.db` — the manager's chat history/audit survive web app resets.
10. **onboarding mode** (no checkout linked) shows only tools with `needs_project=False`.
11. **Tool descriptions in tools.json** overlay Python code — edit there to change how the model sees a tool without touching code.
12. **Prompt changes need restart** — `SYSTEM_PROMPT` is a module-level constant loaded once.
13. **The `Current situation` block** is appended to the system prompt each turn by the app — describes the host, mode, server status, available actions. The agent reads it but doesn't control its content.
14. **Self-update in source mode** does `git pull --ff-only` — so it pulls the WHOLE repo, not just the manager. A managed webAgent checkout sharing the same repo gets updated too.
15. **Self-update in frozen mode** fetches fresh source into data dir, builds new exe, stages it beside the current one — swap happens on restart via a detached helper script.

---

## Common patterns

### Audit trail
All mutating tools should call:
```python
ctx.audit("tool_name", {"param": value}, ok_status, detail_str)
```

### Logging to UI
```python
ctx.log(f"status message here")
```

### Path resolution (for codebase tools)
```python
from ..safety import resolve_path, is_within
resolved = resolve_path(raw_path, ctx.project_root)
if not is_within(resolved, ctx.project_root):
    return "Error: path escapes the project root."
```

### Git checkpoint before autonomous edits
```python
from ..safety import git_checkpoint
label = f"edit {path} — {description}"
checkpoint = git_checkpoint(ctx.project_root, label)
```