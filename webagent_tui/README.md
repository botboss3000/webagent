# webagent TUI

A **standalone, server-independent operator agent** for webAgent. It is its own
project — separate from the web app and from `launcher/` — packaged as a single
relocatable executable.

Its agent talks **directly to the LLM API** (never through the webAgent server),
so it can inspect, edit, and source-control a webAgent checkout **even when the
server is down**. v1 gives the agent two ability sets:

- **Codebase Admin** — `read_source`, `write_source`, `edit_source`,
  `patch_source`, `delete_source`, `search_source`, `read_directory`,
  `run_command`, `run_python`.
- **Source Control** — `git_tool` (status/diff/commit/push/pull/…),
  `resolve_conflict`. Never force-pushes.

## External database

The operator keeps its **own** SQLite store (conversation history + a full audit
trail of every mutating action) in the per-user data dir — **separate from the
web app's `app/db/local.db`** — so resetting the web app never wipes the
operator's memory:

| OS | Location |
|----|----------|
| Windows | `%APPDATA%\webagent-tui\webagent_tui.db` |
| macOS | `~/Library/Application Support/webagent-tui/webagent_tui.db` |
| Linux | `$XDG_DATA_HOME/webagent-tui/webagent_tui.db` (or `~/.local/share/...`) |

Override with `WEBAGENT_TUI_DB=/path/to.db`.

## Run from source

```bash
cd webagent_tui
pip install -e .
WEBAGENT_TUI_PROJECT=/path/to/webagent python -m webagent_tui
```

- **Target project** — the webAgent checkout to operate on. Auto-detected from
  the working directory if it contains `run.py` + `app/`; otherwise set
  `WEBAGENT_TUI_PROJECT`.
- **LLM provider** — resolved from `WEBAGENT_TUI_*` env → `LLM_*` env →
  `OPENROUTER_*` env → the **target project's `.env`** → saved config. OpenAI
  compatible (OpenRouter by default).

## Safety model

- Mutating tools are **gated**: off by default. Press **Ctrl+W** to *Allow
  writes* for the session, or **Ctrl+A** for **Autonomous mode** (opt-in: the
  agent runs mutating tools without per-call gating).
- **Force-push is hard-blocked**; secrets are scanned before commit; per-machine
  runtime files (`.env`, `local.db`, …) are flagged and not auto-committed.
- Every mutating action is recorded in the `actions` audit table.

## Build the single executable

```bash
cd webagent_tui
pip install -e ".[build]"
python scripts/build_exe.py      # → ./webagent-tui  (or webagent-tui.exe)
```

## Keyboard

| Key | Action |
|-----|--------|
| `Enter` | Send |
| `Ctrl+W` | Toggle *Allow writes* (mutating tools) |
| `Ctrl+A` | Toggle *Autonomous* mode |
| `Ctrl+Q` | Quit |

## Status

v1 = the serverless agent with Codebase Admin + Source Control. Planned next:
server lifecycle control (start/stop/restart + health), log/traceback
diagnosis, the conversational install/onboarding flow, and opt-in autonomous
self-repair (auto-handoff on repeated crashes).
