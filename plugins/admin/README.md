# `plugins/admin/` — Privileged filesystem & source-control tools

**WARNING: The files here grant the agent broad filesystem read/write/delete and shell-command execution. These are debug/development tools, NOT normal user-facing features.**

This folder holds the **implementation** behind the privileged developer abilities. It is the shared library that three drop-in abilities build on — each ability (in `plugins/abilities/`) declares *which* tools it gates; the handlers live here.

| Ability (`plugins/abilities/`) | Gets (gated in `app/tools/loader.py`) | Built from |
|---|---|---|
| **Codebase Admin** (`codebase_admin`) | full filesystem + shell + git + `search_comments` | `inject_source_tools` |
| **Git Control** (`git_control`) | `git_tool` / `resolve_conflict` only | `inject_git_tools` |
| **UI Admin** (`ui_admin`) | file edits confined to `ui/` (`.css`/`.html`) | `inject_ui_tools` (wraps the source tools behind `ui_guardrails`) |

> **Tied to the ability — no ability, no tools.** The loader only imports and injects these handlers when the matching ability is enabled (admin-level **and** per-agent). Disable or delete the ability and the agent never receives the tools. The HTTP router below is the one surface mounted independently of the ability.

## What's here

| File | Purpose |
|------|---------|
| **`source_tools.py`** | Agent tool wrappers. `inject_source_tools` builds the full suite (`read_source`, `write_source`, `edit_source`, `patch_source`, `delete_source`, `search_source`, **`search_comments`**, `read_directory`, `run_command`, `run_python`, `restart_server`, `git_tool`, `resolve_conflict`, `commit_and_push`). `inject_git_tools` exposes only the git subset. |
| **`source.py`** | FastAPI router (`/admin/source/`) — REST endpoints for reading, writing, deleting files and running shell commands. Backs up overwritten files to `.source-backups/`. Mounted in `app/main.py` behind an `ImportError` guard. |
| **`guardrails.py`** | Optional deny-list. Blocks `.env`, `.env.*`, shell-history, `.gitconfig`, `.ssh/*`, and dangerous commands (`rm -rf /`, fork bombs). **Delete this file to remove all path/command restrictions.** |
| **`ui_tools.py`** | `inject_ui_tools` — re-exposes only the file tools (no shell / python / git / restart), each wrapped with a `ui_guardrails` pre-check. Backs the **UI Admin** ability. |
| **`ui_guardrails.py`** | UI-only boundary: confines paths to `ui/`, restricts writes to `.css`/`.html`, marks sensitive pages read-only. |

## How injection works

These three abilities are **self-contained drop-ins**. Each ability file in `plugins/abilities/` (`codebase_admin.py`, `git_control.py`, `ui_admin.py`) exposes a `build_tools(...)` hook that the core loader discovers and calls generically for every enabled ability — there is **no** per-ability `if/elif` in `app/tools/loader.py` anymore. `build_tools` returns `{tool_name: handler}`, and the loader then reads each module's `TOOL_SCHEMAS` / `DESTRUCTIVE` (populated *during* the call) to register the tools.

The handlers themselves still live here in the shared admin library via the `inject_*` functions, which mutate a tools dict with `ToolInfo` objects (the historical shape). The bridge between the two is **`adapter.extract_injected`**:

- Each ability's `build_tools` calls `extract_injected(inject_source_tools | inject_git_tools | inject_ui_tools, user_id)`.
- `extract_injected` runs the injector against a *throwaway* dict, then pulls `handler` and `parameters` off every `ToolInfo` and folds `destructive` / `requires_confirmation` into a destructive set. It returns the `(handlers, schemas, destructive)` triple the loader contract wants — so the schemas and guardrail flags come straight from the `ToolInfo` objects and **never drift**.

Per-ability specifics:

| Ability | `build_tools` does |
|---|---|
| `codebase_admin` | `extract_injected(inject_source_tools, user_id)` for the full suite, **plus** a `db_query` handler lazily wrapped from `app.tools.core_tools.db_query` (merged into handlers + schemas; `db_query` is not destructive). |
| `git_control` | `extract_injected(inject_git_tools, user_id)` — the git subset only. |
| `ui_admin` | `extract_injected(inject_ui_tools, user_id)`, but returns `{}` when `enabled_providers` contains `codebase_admin` (the unrestricted superset wins). It accepts `enabled_providers=None` and guards defensively, since the generic loader call does not yet pass `enabled_providers` into `build_tools`. |

Imports stay **lazy** (inside `build_tools`) so scanning the `FEATURE` descriptor is cheap. If these files don't exist, the agent simply gets no privileged tools — no errors, no side effects. `app/main.py` mounts the `/admin/source` router the same guarded way.

## Disabling / editions

**To remove all privileged filesystem and shell access, delete this directory:**

```bash
rm -rf plugins/admin/
```

No code edits, no config changes — the loader's `try/except ImportError` and the main-app router guard make it vanish cleanly. A production edition that excludes the Codebase Admin / Git Control / UI Admin abilities can drop this folder.

## `create_tool` lockout (still required)

Deleting this folder is **not enough on its own**: the agent's built-in `create_tool` could otherwise re-create `read_source` / `run_command` by writing raw Python. Two protections in **core** prevent that and must stay:

| Layer | File | What it does |
|-------|------|-------------|
| Code scanner | `app/tools/registry.py` | Blocks dangerous imports/patterns (`os`, `subprocess`, `open(`, `exec(`, …) before tool code is saved. |
| Restricted namespace | `app/tools/loader.py` | `_compile_tool` strips `open`/`exec`/`eval`/`compile`/`__import__` from builtins before executing tool code. |

## Production note

In production or user-facing deployments, **delete this directory** (and keep the `create_tool` lockout). Normal users should never have filesystem read/write/command-execution tools.
