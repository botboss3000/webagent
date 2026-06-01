# webagent TUI — Server Manager

A **standalone, server-independent Server Manager agent** for webAgent. It is its own
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

The server manager keeps its **own** SQLite store (conversation history + a full audit
trail of every mutating action) in the per-user data dir — **separate from the
web app's `app/db/local.db`** — so resetting the web app never wipes the
server manager's memory:

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
- **LLM provider** — resolved (highest priority first) from `WEBAGENT_TUI_*` /
  `LLM_*` overrides → the **target project's `provider.json`** (the same
  credential store the webAgent server itself uses; `admin_default` profile
  preferred) → legacy `OPENROUTER_*` env / the project's `.env` → saved config →
  built-in defaults. OpenAI compatible. Reading `provider.json` as one coherent
  (api_key, base_url, model) triple means the server manager "just works" with
  whatever the web app is already configured to use, and avoids pairing a key
  from one provider with another provider's base URL (a 401 cause). It's
  gitignored, so a fresh checkout with none falls back to env / `.env`.

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
| `Ctrl+T` | Cycle theme (23 shared with the launcher) |
| `Ctrl+C` | Copy the highlighted transcript text to the clipboard |
| `Ctrl+Q` | Quit |

**Selecting / copying text.** The TUI captures the mouse, so a normal click-drag
is handled by the app: drag to highlight transcript text, then `Ctrl+C` to copy
it to your system clipboard. Alternatively, hold **Shift** while dragging to use
your terminal's own native selection/copy (works anywhere on screen).

The UI shares the launcher's **23 themes** and emoji/ASCII **glyph** set (the
relevant assets are vendored alongside this package, so the `.exe` stays
self-contained). The active theme persists to config. Force emoji on/off with
`WEBAGENT_EMOJI=1` / `=0`.

## Status

v1 = the serverless agent with Codebase Admin + Source Control. Planned next:
server lifecycle control (start/stop/restart + health), log/traceback
diagnosis, the conversational install/onboarding flow, and opt-in autonomous
self-repair (auto-handoff on repeated crashes).
