---
type: concept
tags: [webagent, admin, tools, filesystem, security, privileged-access]
---

# webAgent Administrator Tools

The webAgent has an optional `app/admin/` module that grants privileged filesystem read/write/edit/delete, shell command execution, and server restart capabilities to the AI agent. These are **debug/development tools, not normal user-facing features**.

## Source management stack (optional)

- **`app/admin/source.py`** — FastAPI router at `/admin/source/` with REST endpoints for filesystem ops
- **`app/admin/source_tools.py`** — Agent tool wrappers that inject `read_source`, `write_source`, `edit_source`, `delete_source`, `run_command`, `restart_server` into the agent's tool list
- **`app/admin/guardrails.py`** — Optional security deny-list (`.env`, SSH keys, dangerous commands)

## Agent tools injected

| Tool | Requires confirmation |
|------|-----------------------|
| `read_source` | ❌ No (read-only) |
| `write_source` | ✅ Yes |
| `edit_source` | ✅ Yes |
| `delete_source` | ✅ Yes |
| `run_command` | ✅ Yes |
| `restart_server` | ✅ Yes |

## Guardrails

By default, guardrails block: `.env`, `.env.*`, `.bash_history`, `.zsh_history`, `.gitconfig`, `.ssh/*`, `rm -rf /`, `rm -rf ~`, fork bombs. Deleting `guardrails.py` removes all restrictions.

## Disabling

Delete `app/admin/source.py` + `app/admin/source_tools.py` (or the whole `app/admin/` directory). The import in `loader.py` and `main.py` is guarded by `try/except ImportError` — no code changes needed.

## Other admin endpoints (non-filesystem)

- `review.py` — `/admin/tools` — list/get/deprecate DB tools
- `settings.py` — `/admin/settings/provider` — AI provider config, model list
- `db_mode.py` — `/admin/db/mode` — cloud/local DB switch
- `communications.py` — `/admin/communications/plugins` — plugin enable/disable

---

**2026-05-07:** Documented admin tools architecture in README.md and app/admin/README.md per user request. README now has "Administrator Tools" section with disable instructions. Created `app/admin/README.md` with full tool descriptions, injection mechanism, and production lockdown notes.
