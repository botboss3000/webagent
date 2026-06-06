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

`app/tools/loader.py` imports the injector for whichever ability is enabled, e.g.:

```python
if "codebase_admin" in enabled_providers:
    try:
        from plugins.admin.source_tools import inject_source_tools
        inject_source_tools(tools, user_id)
    except ImportError:
        pass  # plugins/admin/source_tools.py not present — source editing disabled
```

If the files don't exist, the agent simply gets no privileged tools — no errors, no side effects. `app/main.py` mounts the `/admin/source` router the same guarded way.

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
