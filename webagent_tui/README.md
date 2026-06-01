# webagent TUI — Server Manager

A **standalone, server-independent Server Manager agent** for webAgent. It is its own
project — separate from the web app and from `launcher/` — packaged as a single
relocatable executable.

Its agent talks **directly to the LLM API** (never through the webAgent server),
so it can inspect, edit, and source-control a webAgent checkout **even when the
server is down**.

### Modes

The manager runs even with **no checkout linked** — it's an onboarding guide
first, a codebase agent second:

- **Onboarding** (no repo linked) — orient, explain webAgent, **install a fresh
  copy**, or **link an existing checkout**. Runs on the **app key**.
- **Managed** (a webAgent checkout linked) — the full ability sets below, plus
  the linked repo's AI key takes over automatically (live re-pick on link).

Every turn the agent is handed a **live situation snapshot** — host OS, Python,
git, whether a repo is linked, server health, and the AI key in use — so its
guidance stays grounded instead of guessed.

Ability sets:

- **Onboarding / Install** — `check_install_readiness`, `clone_repo`,
  `setup_environment`, `seed_config`, `verify_install` (clone
  `github.com/botboss3000/webagent` → build the venv + deps + browser → seed
  config & AI key → verify), plus `link_project` to adopt an existing copy.
- **Server (local)** — `server_status`, `server_start`, `server_stop`,
  `server_restart`, `server_logs`. Runs `run.py` from the checkout's venv as a
  detached process on port 8080; PID + log live in the manager's data dir.
- **Updates** — `check_updates` (compare the checkout to the public repo).
- **Diagnose** — `read_diagnostics` reads the app's flight-recorder (warnings /
  errors with tracebacks, agent-loop problems, tool errors) straight from the
  checkout's local DB, so it works even when the server is **down**.
- **Codebase Admin** — `read_source`, `write_source`, `edit_source`,
  `patch_source`, `delete_source`, `search_source`, `read_directory`,
  `run_command`, `run_python`.
- **Source Control** — `git_tool` (status/diff/commit/push/pull/…),
  `resolve_conflict`. Never force-pushes.
- **Self-update** — `self_status`, `self_update`, `self_restart`. The manager can
  update **its own code** — on request or when it notices it's behind upstream.
  See [Updating itself](#updating-itself).

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

**One-click (Windows):** double-click **`run.bat`** in this folder — it creates
the `.venv` + installs deps on first run, then launches the TUI from source (so
it always reflects the latest code, no `.exe` rebuild needed).

**By hand** (needs Python 3.11 or 3.12):

```bash
cd webagent_tui
pip install -e .
WEBAGENT_TUI_PROJECT=/path/to/webagent python -m webagent_tui
```

Already have the `.venv` from a previous run? Just launch with it directly —
`.venv\Scripts\python.exe -m webagent_tui` (Windows) or
`.venv/bin/python -m webagent_tui` (macOS/Linux) — no reinstall needed.

- **Target project** — the webAgent checkout to operate on. Auto-detected from
  the working directory if it contains `run.py` + `app/`; otherwise set
  `WEBAGENT_TUI_PROJECT`.
- **LLM provider** — resolved (highest priority first) from an explicit
  `WEBAGENT_TUI_*` override → the **linked repo's `provider.json`** (the same
  credential store the webAgent server itself uses; `admin_default` profile
  preferred) → generic/legacy `LLM_*` / `OPENROUTER_*` env / the project's `.env`
  (the **app key**, used during onboarding) → saved config → built-in defaults.
  OpenAI compatible. Reading `provider.json` as one coherent (api_key, base_url,
  model) triple — above the generic `LLM_*` key — means a **linked repo's key
  wins** over the app key (onboarding uses the app key; a linked checkout uses
  its own), and avoids pairing one provider's key with another's base URL (a 401
  cause). `provider.json` is gitignored, so a fresh checkout with none falls back
  to env / `.env`.

## Install on Android (Termux)

Install the manager straight into Termux — no proot, no full server — with a
single paste:

```bash
pkg install -y git && git clone --depth 1 https://github.com/botboss3000/webagent ~/webagent && bash ~/webagent/webagent_tui/install-termux.sh
```

…or via the short URL the webAgent server hosts (it serves `/termux` → this same
script, LF-normalised):

```bash
pkg install -y curl && curl -fsSL https://webagent.live/termux | bash
```

`install-termux.sh` installs Termux's `python` + `git`, finds the checkout it's
running inside (or clones one into `~/webagent`), then installs the TUI — a clean
`pip install -e .` on Python 3.11/3.12, or a **deps-only "run from source"
fallback** on newer Python so the `<3.13` pin never blocks the install. It writes
a **`webagent-tui`** command onto `$PREFIX/bin` and a `~/.shortcuts/webagent-tui.sh`
**Termux:Widget** shortcut (install the Termux:Widget add-on from F-Droid for a
tappable home-screen button). Launch afterwards with `webagent-tui`. The script is
idempotent — safe to re-run to update.

On Android the agent runs in onboarding / managed mode but **headless-browser
features are off** (no Chromium on Android), so it's a codebase / source-control /
diagnostics manager there. Give it a key inline (`LLM_API_KEY=… webagent-tui`) or
in the checkout's `.env`; the installer prints exactly what to set if none is found.

## Safety model

- Mutating tools are **gated**: off by default. Press **Ctrl+W** to *Allow
  writes* for the session, or **Ctrl+A** for **Autonomous mode** (opt-in: the
  agent runs mutating tools without per-call gating).
- **Force-push is hard-blocked**; secrets are scanned before commit; per-machine
  runtime files (`.env`, `local.db`, …) are flagged and not auto-committed.
- A **self-update always backs up first** (timestamped) and never force-updates —
  the previous version is always recoverable (see [Updating itself](#updating-itself)).
- Every mutating action is recorded in the `actions` audit table.

## Build the single executable

```bash
cd webagent_tui
pip install -e ".[build]"
python scripts/build_exe.py      # → ./webagent-tui  (or webagent-tui.exe)
```

The build **stamps the source commit + timestamp** into the bundle (a generated,
gitignored `webagent_tui/_build.py`, removed from the tree right after) so a
frozen exe can tell whether it's behind upstream — the input to self-update.

## Updating itself

The manager can update **its own code**, triggered two ways: **on request**
("update yourself") or **when it finds itself behind** — a background check at
startup flags a newer version in the transcript, and the per-turn snapshot tells
the agent the same. What "update" means depends on how it's running (it knows
which via `self_status`):

| Running as… | `self_update` does | Applied by `self_restart` |
|-------------|--------------------|----------------------------|
| **source** (`run.bat` / `python -m`) | backs up the manager's source (timestamped), then `git pull --ff-only` the repo it lives in | relaunches so Python loads the new code |
| **frozen `.exe`** | fetches fresh source, rebuilds the exe with PyInstaller, **backs up the current exe** (`webagent-tui.<timestamp>.bak.exe`), and stages the new one beside it | a detached helper waits for exit, swaps the new exe in, and relaunches |

- **Backups.** Source backups go to `…/webagent-tui/self-backups/source-<timestamp>/`
  in the data dir; the exe backup is written next to the running exe (timestamped),
  falling back to the data dir if that folder isn't writable.
- **Safety.** Gated behind *Allow writes* like any mutation, fully audited, and
  fast-forward-only (never force). A frozen rebuild needs **git + Python 3.11/3.12**
  on the host; `self_update` reports exactly what's missing and changes nothing if
  it can't proceed.
- **Restart ends the session.** `self_restart` closes the manager so the swap /
  reload can finish, then relaunches it in a new window. A source `git pull` updates
  the **whole repository**, so a managed checkout living in the same repo is updated
  too. Work (clone + build venv) is cached under `…/webagent-tui/self-update/`.

## Header toolbar (clickable)

The header is a row of **clickable controls** (no title/model text):

| Control | Action |
|---------|--------|
| `[Read]` / `[Write]` / `[Auto]` | Write-gate button — shows the current mode; click to cycle Read → Write → Auto. |
| `[Theme]` | Open the theme & animation picker (theme · animation style · palette · speed · intensity · FPS · banner on/off) |
| `[Browser]` | Open the web UI (`http://localhost:8080/index.html`) in your browser |
| `[Start]` ↔ `[Restart]` `[Stop]` | Server control, state-aware: `[Start]` when stopped; `[Restart]` + `[Stop]` when running |
| `[Logs]` | Show the captured server log in the transcript |
| `[Diagnostics]` | Show the app's recorded warnings/errors — reads the local DB, so it works even when the server is down |
| server dot | live (green) / stopped (red) / checking — polled every few seconds |

The server **auto-starts** when you open the manager in managed mode (if it isn't
already running), so there's no separate Launch control.

### Look & feel (vendored from the launcher)

- **Animated logo banner** — plasma / flow-field / rings / noise (or a static
  "off") behind the "webagent" ASCII logo, above the transcript. Coloured from
  the active theme (or a chosen palette). Stops animating when off or when the
  window loses focus (≈0% CPU).
- **`[Theme]` picker** — a modal to set the theme, animation style, palette
  (match-theme or a preset), speed, intensity, FPS, and the banner on/off. Every
  choice applies **live** and persists.
- **Walker** — a tiny ascii guy above the input reacts to the agent loop: walks
  while thinking, works during a tool, cheers on a reply, trips on an error.
- **Session HUD** — tokens in/out this session and a context-window gauge
  (green → amber → red) when the model's window is known.

## Keyboard

| Key | Action |
|-----|--------|
| `Enter` | Send |
| `Esc` | Exit |
| `Ctrl+A` | Select all text in the input field |
| `Ctrl+C` / `Ctrl+V` / `Ctrl+X` | Copy / paste / cut (input field) |
| `Ctrl+T` | Cycle theme (23 shared with the launcher; not shown in the footer) |

**Copying transcript text.** The footer's `Ctrl+C` copies the input field. To copy
text from the **transcript**, hold **Shift** while dragging to use your terminal's
own native selection/copy (works anywhere on screen).

The UI shares the launcher's **23 themes** and emoji/ASCII **glyph** set (the
relevant assets are vendored alongside this package, so the `.exe` stays
self-contained). The active theme persists to config. Force emoji on/off with
`WEBAGENT_EMOJI=1` / `=0`.

## Status

- **Done** — the serverless agent with Codebase Admin + Source Control;
  onboarding mode (runs with no repo linked), the per-turn situation snapshot,
  context-aware AI-key resolution with live re-pick; **linking** an existing
  checkout; the **desktop install flow** (readiness → clone → environment → seed →
  verify); **local server lifecycle** control (start/stop/restart/status/logs);
  **update checks**; **self-update** (the manager pulls/rebuilds and relaunches
  its own code, backing up first — source pull or frozen-exe rebuild + swap); and
  the **Android/Termux one-line install** (`install-termux.sh`, also served by the
  app at `/termux` — installs python+git, the TUI, a `webagent-tui` launcher + a
  Termux:Widget home-screen shortcut).
- **Planned next** — **keyless web search** + page reading, a secure **credential
  prompt** (ask-and-seed a key when a context has none), general-coding in any
  folder, live **progress streaming** for long installs (incl. the self-update
  rebuild), and opt-in autonomous self-repair. See
  `temp/webagent-tui-onboarding-design.md` for the full design.
