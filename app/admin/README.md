# Administrator Tools — Privileged Access

**WARNING: The files in this directory grant the webAgent broad filesystem read/write/delete capabilities and shell command execution. These are debug/development tools, NOT normal user-facing features.**

**Removable by design.** This whole directory is optional. Boot no longer hard-depends on it — `app/main.py` imports the startup provider-config applier from `app/provider_boot.py` (not from here), and every admin router is imported + registered behind an `ImportError` guard. Delete `app/admin/` and the app still starts cleanly; the admin tools and `/admin/*` endpoints simply disappear.

## What's here

| File | Purpose |
|------|---------|
| **`source.py`** | FastAPI router (`/admin/source/`) — REST endpoints for reading, writing, deleting any file on the system, and executing shell commands. Backs up overwritten files to `.source-backups/`. Validates Python/JSON syntax before writing. |
| **`source_tools.py`** | Agent tool wrappers — injects `read_source`, `write_source`, `edit_source`, `delete_source`, `run_command`, and `restart_server` into the agent's tool list. Only `read_source` is confirmation-free; all mutating tools require explicit user approval. |
| **`guardrails.py`** | Optional security deny-list. Blocks access to `.env`, `.env.*`, `.bash_history`, `.zsh_history`, `.gitconfig`, `.ssh/*`. Also blocks dangerous commands (`rm -rf /`, `rm -rf ~`, fork bombs). **Delete this file to remove all restrictions.** |
| **`review.py`** | Admin endpoints for listing, viewing, and deprecating tools in the DB (`/admin/tools`). |
| **`settings.py`** | Provider configuration (switch AI provider, set API key/model), metadata logging toggle, provider preset list, model fetching. |
| **`db_mode.py`** | Toggle between Cloud (Supabase) and Local (SQLite) database backends (`/admin/db/`). |
| **`communications.py`** | Enable/disable communication plugins (Telegram, WhatsApp) and set webhook URLs (`/admin/communications/`). |

## Agent tools injected

| Tool | Action | Requires confirmation |
|------|--------|-----------------------|
| **`read_source`** | Read any file on the system | ❌ No |
| **`write_source`** | Create or overwrite files (auto-backup) | ❌ No |
| **`edit_source`** | Replace exact text (exact match required) | ❌ No |
| **`patch_source`** | Fuzzy find-and-replace (handles whitespace diffs) | ❌ No |
| **`delete_source`** | Delete files or directories | ❌ No if user commands. ✅ Yes if own initiative |
| **`search_source`** | grep-style regex search (ripgrep or Python fallback) | ❌ No |
| **`read_directory`** | List files with size, configurable depth | ❌ No |
| **`run_python`** | Execute Python code in subprocess | ❌ No |
| **`browser_test`** | Fetch web page, verify content (HTTP-level) | ❌ No |
| **`git_tool`** | Structured git ops (status/log/diff/commit/push) | ❌ No for read-only. ✅ for mutating |
| **`run_command`** | Execute shell commands | ❌ No for read-only. ✅ for mutating |
| **`restart_server`** | Kill and restart the webAgent process | ✅ Yes |

## How injection works

`app/tools/loader.py` tries to import `inject_source_tools` from `app.admin.source_tools`:

```python
try:
    from app.admin.source_tools import inject_source_tools
    inject_source_tools(tools, user_id)
except ImportError:
    pass  # source editing disabled
```

If the file doesn't exist, the agent simply gets no filesystem tools — no errors, no side effects.

Similarly, `app/main.py` conditionally mounts the `/admin/source` router:

```python
try:
    from app.admin.source import router as admin_source_router
    _HAS_SOURCE_TOOLS = True
except ImportError:
    _HAS_SOURCE_TOOLS = False
```

## Disabling

**To disable all privileged filesystem and shell access, delete this directory:**

```bash
# Remove everything
rm -rf app/admin/

# Or just the source management files (preserves other admin endpoints):
rm app/admin/source.py app/admin/source_tools.py
```

No code edits, no config changes. The server does not need a restart for most operations (the agent picks up tool availability on the next turn), though removing `source.py` does need a server restart to unmount the `/admin/source` REST endpoints.

## create_tool lockout

Deleting the `admin/` folder is **not enough** on its own. The agent has a built-in `create_tool` tool that writes arbitrary Python code into the tools DB, which is then compiled and executed in-process. Without further protection, the agent could recreate `read_source`/`write_source`/`run_command` by simply writing `open(path).read()` or `subprocess.run(cmd, shell=True)` as a new tool.

Two protections prevent `create_tool` from being used to circumvent the admin tool lockdown:

| Layer | File | What it does |
|-------|------|-------------|
| 1 — Code scanner | `app/tools/registry.py` | Before writing tool code to DB, scans for dangerous imports (`os`, `subprocess`, `shutil`, `sqlite3`, `pathlib`, `builtins`, `importlib`, `aiofiles`, `io`, `tempfile`, zip/tar/gzip modules) and dangerous patterns (`open(`, `exec(`, `eval(`, `compile(`, `__import__(`, `os.`, `subprocess.`, `shutil.`). Returns a clear `"status": "blocked"` error if unsafe. |
| 2 — Restricted namespace | `app/tools/loader.py` | `_compile_tool` strips `open`, `exec`, `eval`, `compile`, `__import__` from `__builtins__` before `exec()`-ing tool code. Defense-in-depth — even if scanner is bypassed, imports and file operations fail at runtime. |

**Allowed**: Tools that import `httpx`, `aiohttp`, `json`, `re`, `datetime`, `collections`, `uuid`, `typing`, `math`, `asyncio` — anything for external API calls, data transformation, or async coordination.

**Blocked**: Any filesystem, shell, DB, or code-execution capability.

## Production note

In production or user-facing deployments, **delete this directory**. Normal users should never have access to filesystem read/write/command execution tools, and `create_tool` (in the standard codebase) is locked to external tools only.
