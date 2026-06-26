# webagent TUI — Server Manager

A **standalone, server-independent Server Manager agent** for webAgent. It is its own
project — separate from the web app and from `launcher/` — packaged as a single
relocatable executable.

Its agent talks **directly to the LLM API** (never through the webAgent server),
so it can inspect, edit, and source-control a webAgent checkout **even when the
server is down**.

> **mk2 — event-driven build.** This copy (in `TUI/`) upgrades the agent
> loop from strictly synchronous to **event-driven**: the agent can delegate work to
> background **subagents**, accept **steering** while it's mid-turn, **auto-continue**
> when delegated results arrive, and react to **watchdog alerts** as events. When run
> from source it keeps its data **in this project folder** (see **External database**),
> so the install is self-contained. See **Subagents & the event-driven loop** below.

> **mk3 — the app front door (two brains).** Once a checkout is **linked**, an
> ordinary chat turn is no longer handled by the TUI's own clone-agent — it is
> driven by the **linked checkout's REAL agent loop**, run headlessly in the
> checkout's **own venv**, against the app's **own database**, as the **admin**
> user and the new **`server-manager`** agent template — but with **no web
> server**. The terminal becomes *a different front door to the same app*, the way
> the browser is, except it doesn't need the server switched on. The TUI's built-in
> brain stays the **onboarding / recovery / watchdog brain** and the **automatic
> fallback** when the checkout can't load. See **Two brains** below.

### Two brains (mk3)

| | **Built-in manager brain** | **App-brain front door** |
|---|---|---|
| **Code** | the TUI's own loop (`agent.py`), talks straight to the LLM | the linked checkout's real `stream_agent_events` loop |
| **Runs where** | inside the TUI process | a headless subprocess in the checkout's `.venv` (`engine_driver.py`) |
| **Database** | the TUI's own `webagent.db` | the **app's own DB** (shared with the browser) |
| **Identity** | the Server Manager persona (`tui-data/prompt.md`) | admin user + `data/agents/server-manager.json` template |
| **Handles** | onboarding, install/link, watchdog & synthetic turns, fallback | ordinary user turns in managed mode |
| **Needs the server?** | no | no — that's the whole point |

The two are bridged by [`tui_app/app_engine.py`](tui_app/app_engine.py) (parent
side: launch the venv driver, ready/fatal handshake, serialize turns, translate
the app's loop events into the TUI's event contract) and
[`tui_app/engine_driver.py`](tui_app/engine_driver.py) (runs under the checkout's
interpreter: boots provider config + DB, materializes the agent, streams the real
loop over a line-delimited JSON stdio protocol). If the checkout can't load
(missing venv, broken code), the bridge raises `EngineUnavailable` and the turn
falls back to the built-in brain so you can still diagnose and repair it.

A managed turn is recorded **only** in the app's own DB (user text, assistant
bubbles and tool steps), never mirrored into `webagent.db`. The TUI reads that
session's visible transcript straight back from the app DB via
[`tui_app/app_db.py`](tui_app/app_db.py) — a read-only reader (sqlite `mode=ro`,
**no app import**) — so the terminal and the browser show the exact same
conversation, tool steps and all. Built-in-brain sessions stay in `webagent.db`.

### Modes

The manager runs even with **no checkout linked** — it's an onboarding guide
first, a codebase agent second:

- **Onboarding** (no repo linked) — orient, explain webAgent, **install a fresh
  copy**, or **link an existing checkout**. Runs on the **app key**, on the
  built-in brain.
- **Managed** (a webAgent checkout linked) — ordinary turns run on the **app-brain
  front door** (the checkout's real agent, shared app DB); the built-in brain keeps
  the management/recovery ability sets below and the linked repo's AI key.

Every turn the built-in brain is handed a **live situation snapshot** — host OS,
Python, git, whether a repo is linked, server health, and the AI key in use — so
its guidance stays grounded instead of guessed.

Ability sets:

- **Web Search** — `web_search` (any mode, no API key) — search DuckDuckGo for
errors, docs, solutions, and current information.
- **Browser (Playwright)** — `browser_navigate`, `browser_snapshot`, `browser_screenshot`,
  `browser_click`, `browser_type`, `browser_evaluate`, `browser_close` — a shared headless
  Chromium browser you can navigate, inspect, interact with, and screenshot. Use to test
  `localhost:8080`, verify UI changes, scrape data, or fill forms. Launch-on-first-use,
  lives until `browser_close`.
- **Onboarding / Install** — `check_install_readiness`, `clone_repo`,
  `setup_environment`, `seed_config`, `verify_install` (clone
  `github.com/botboss3000/webagent` → build the venv + deps + browser → seed
  config & AI key → verify), plus `link_project` to adopt an existing copy.
- **Server (local)** — `server_status`, `server_start`, `server_stop`,
  `server_restart`, `server_logs`. Runs `run.py` from the checkout's venv as a
  detached process on port 8080; PID + log live in the manager's data dir.
- **Reset** — `reset_app` wipes the linked install back to a clean state (the
  in-app `reset_webagent.bat`): always the **userbase** (the **active database
  backend** + `visuals/users/`), and opt-in the app's **secrets**, **local
  logins**, **`.env`**, and **agent template JSONs**. **Backend-aware** via
  `app/db_connection.json`: if **Postgres** (`postgres`/`neon`/`gcp_cloud_sql`)
  is active, it connects through the checkout's venv and **drops + recreates the
  schema** (irreversible — **not** backed up — and the stray SQLite files are left
  alone); if **SQLite** is active, it removes `local.db` + its sidecars (backed up).
  Stops the server first and **backs up** the files it removes to
  `temp/reset-backup-<timestamp>/` unless told not to. The DB, the default
  `admin/admin` user, and the agents regenerate on next start.
- **Drive & observe the app (as a user)** — log into the **running** server over its
  HTTP + **WebSocket** API (default `admin`/`admin`) and hold a **live, shared
  conversation** with the app's own agent — the same stream the browser sees. Browse
  agents/sessions (`app_list_agents`, `app_list_sessions`), set a target and bridge the
  Manager to it (`webapp_send`, governed by the two mutes — see
  [Driving the running app](#driving-the-running-app-the-two-mutes)), or fire a one-shot
  `app_chat`. Same API the web UI uses (no direct DB access), so it can do exactly what a
  logged-in admin can. Send is write-gated (the app's agent takes real actions).
- **Edit app config** — read/write `app-settings.json` (`app_get_settings` /
  `app_set_settings`) and the app's **LLM auth key / provider** stored in the DB
  (`app_get_auth_keys` / `app_set_auth_keys`), via the App Config panel or the agent.
- **Updates** — `check_updates` (compare the checkout to the public repo).
- **Diagnose** — `read_diagnostics` reads the app's **server-side** flight-recorder
  (warnings / errors with tracebacks, agent-loop problems, tool errors) straight
  from the checkout's `logs.db`, so it works even when the server is **down**.
  `read_recordings` reads the app's **browser-side** flight-recorder (the render
  recorder: HTML snapshots, DOM-mutation deltas, lag / long-task metrics, JS
  errors, console warnings, failed/slow network calls) straight from
  `recordings.db` — the agent can both **turn the recorder on/off**
  (`app_set_settings({"render_recording_enabled": true})`) and **collect the
  captured logs itself**, so it can verify a UI/render change end-to-end.
- **Monitoring / alarms** — `monitor_status`, `server_resources`, `list_alarms`,
  `add_alarm`, `remove_alarm`, `set_monitor_config`, `notify_test`. Configure the
  autonomous **watchdog** by talking to the agent (see
  [Monitoring & alarms](#monitoring--alarms-autonomous-watchdog)).
- **Playbook (self-healing)** — `playbook_list`, `playbook_show`,
  `playbook_record_remedy`, `playbook_set_remedy`, `playbook_forget`. The learned
  issue knowledge base that remembers what fixes what and gets smarter over time
  (see [The Playbook](#the-playbook-self-healing-issue-knowledge-base)).
- **Codebase Admin** — `read_source`, `write_source`, `edit_source`,
  `patch_source`, `delete_source`, `search_source`, `read_directory`,
  `run_command`, `run_python`.
- **Source Control** — `git_tool` (status/diff/commit/push/pull/…),
  `resolve_conflict`. Never force-pushes.
- **Self-update** — `self_status`, `self_update`, `self_restart`. The manager can
  update **its own code** — on request or when it notices it's behind upstream.
  See [Updating itself](#updating-itself).

## External database

The server manager keeps its **own** SQLite store (conversation history, a full
audit trail of every mutating action, a small **`settings`** key/value table — e.g.
the linked **repo directory** the user/agent designated — and the **Playbook**
knowledge base — `playbook_issues` / `playbook_remedies` / `playbook_incidents`) —
**separate from the web app's `app/db/local.db`**, so resetting the web app never
wipes the manager's memory of what fixes what.

When you **run from source**, it lives **right here in the project folder** (this
`TUI` directory), so the whole install is self-contained — `webagent.db`,
`config.json`, the watchdog's `monitor.json` / `alarms.json`, and per-subagent
session transcripts all sit next to the code, and these runtime files are
`.gitignore`d:

```
TUI/
├── tui_app/            ← the package (source)
├── webagent.db          ← sessions + audit trail   (gitignored)
├── config.json          ← settings                 (gitignored)
├── monitor.json         ← watchdog config          (gitignored)
├── alarms.json          ← watchdog alarms          (gitignored)
├── guardian.json        ← keep-alive on/off state  (gitignored)
├── guardian.pid         ← guardian PID (singleton)  (gitignored)
├── tui.pid              ← TUI PID + heartbeat       (gitignored)
├── tui.clean_exit       ← deliberate-quit marker    (gitignored)
├── guardian.log         ← guardian activity log     (gitignored)
└── attachments/         ← pasted images            (gitignored)
```

Each delegated **subagent runs in its own session** inside `webagent.db`, so you
can open it from the session list (`/resume`) and watch its progress.

A session's **id is its creation timestamp** (`YYYYMMDD-HHMMSS-…`), so the list
sorts and reads chronologically. Its **name** is generated after each turn — by
default a short LLM summary of the last ~10 messages, falling back to the latest
user message when no summary is available (e.g. no API key yet). The naming
behaviour is adjustable in **Admin ▸ App Config ▸ Session naming**
(Summary (AI) · Latest message · Off).

A packaged **`.exe`** has no stable source folder, so it falls back to the per-user
app-data dir instead (`%APPDATA%\webagent` on Windows,
`~/Library/Application Support/webagent` on macOS, `$XDG_DATA_HOME/webagent` or
`~/.local/share/webagent` on Linux).

Override the location with `WEBAGENT_DATA_DIR=/path/to/dir` (or point just the DB
elsewhere with `WEBAGENT_DB=/path/to.db`).

## Monitoring & alarms (autonomous watchdog)

Alongside the chat, a background **watchdog** ([`tui_app/watchdog.py`](tui_app/watchdog.py))
runs on a fixed interval (managed mode, when enabled). Each tick it:

1. probes the server's health (`/health`) + PID-alive,
2. reads the app's **new** diagnostics since it last looked,
3. samples **host + process resources** (CPU, memory, disk, server-process RSS) and
   checks the **port** (8080),
4. evaluates everything against the user's **alarm rules** and thresholds,
5. reacts within the configured **autonomy** level — notifies the user and, when
   allowed, recovers the server (auto-restart with backoff + a **crash-loop guard**).

**Liveness & process checks:**

- **Health + PID** — `/health` probe plus whether the manager's tracked process is
  still alive.
- **Crash-loop detection** — if the server keeps dying right after start, it stops
  auto-restarting (capped at `max_restarts_per_hour`) and **escalates** instead of
  flapping.
- **Auto-restart with backoff** — recovers a server that *was* up (never fights the
  initial autostart), waiting `restart_backoff_seconds` and respecting the cap.
- **Port / zombie detection** — distinguishes a clean server, an **untracked**
  instance the manager didn't start, and a **zombie** holding port 8080 without
  serving `/health` (which blocks a clean restart — the orphaned-LISTENER case
  `run.py` fights). Built dependency-free in
  [`tui_app/sysmetrics.py`](tui_app/sysmetrics.py).

**Resource health** (`sysmetrics.py`, no `psutil`): host **disk** (`shutil`),
**memory** (Windows `GlobalMemoryStatusEx` / Linux `/proc/meminfo`), **CPU** (a
delta sample between ticks; load-average fallback), and the **server process's
memory** (Windows ctypes / Linux `/proc` / macOS `ps`). Thresholds in `monitor.json`
(`disk_percent_threshold`, `mem_percent_threshold`, `cpu_percent_threshold`; a
percent of `0` disables that check) alert when crossed, deduped hourly. Ask the
agent for `server_resources` to see them on demand; metrics an OS can't supply show
as `n/a`.

**Autonomy levels** (in `monitor.json`):

| Level | What the watchdog may do unattended |
|-------|-------------------------------------|
| `notify` | Watch and alert only — never restart or change anything. |
| `auto_restart` *(default)* | Restart/recover the server automatically; **code changes wait for the user**. |
| `self_heal` | Reserved for agent-driven code fixes; recovery behaves like `auto_restart` (fixes happen in conversation, with the user's eyes). |

**Notifications** go to the channels in `monitor.json` (`channels`). v1 ships the
**`desktop`** channel — a native OS toast (Windows tray balloon, macOS
`osascript`, Linux `notify-send`) via [`tui_app/notify.py`](tui_app/notify.py),
which always **also** echoes the alert into the chat transcript. The notifier is
channel-based so Telegram / email / an in-webapp banner can be added later.

In the transcript each alert renders as a **colour-coded, collapsible notification
box** (`_notify_box` in [`tui_app/app.py`](tui_app/app.py)): a header showing the
**timestamp** + a 1–3 word summary on the left and a clickable **`[Show]`/`[Hide]`**
toggle on the right. It's **collapsed by default** — only the header line shows;
click **`[Show]`** to reveal the full detail below (and **`[Hide]`** to re-collapse). It's
**orange** while it's a **warning** and **green** once it's **resolved** — and an
incoming recovery (e.g. "Server recovered", a healthy auto-restart) also flips the
matching still-orange warning boxes (same coarse subject: server / disk / memory /
cpu) to green, so a downed-then-recovered server reads as orange → green. Colours
are theme variables (`$warning` / `$success`), legible in light and dark.

**The "watch for this error" flow:** tell the agent about an error → it finds the
matching diagnostics → on your OK it writes an **alarm rule** (`add_alarm`): a
`contains` / `level` / `category` match, an `action` (`notify` | `auto_restart`),
a `loudness` (`every` | `once` | `digest`), and channels. "Stop watching that" →
`remove_alarm`. Retune the whole watchdog with `set_monitor_config`; inspect it
with `monitor_status`.

The live files are per-machine and **gitignored**: `monitor.json` (config, seeded
from the shipped defaults) and `alarms.json` (the watch list), both in the data
dir. The watchdog re-reads them every tick, so edits apply with no restart.

## Keep-alive guardian (survives the TUI)

The watchdog above is **in-process** — it dies the instant the TUI dies, which is
exactly when the server it launched is most likely to fall over. The **keep-alive
guardian** ([`tui_app/guardian.py`](tui_app/guardian.py)) is the answer: a
**separate, detached process** the TUI spawns on open that **outlives** it and
keeps **both** running:

- **the web server** — if `/health` stops answering and a checkout is linked, it
  relaunches `run.py` from the checkout's venv (clearing a zombie port first, with
  a crash-loop cap);
- **one server only** — once the server is healthy, it kills any **other** webAgent
  `run.py` tree on the machine besides the one actually serving port 8080. This
  stops a second launcher (e.g. a stray `webAgent.bat`) from leaving a duplicate
  server running its own background loops against the same database — which is what
  let a single test fan-out balloon into 120+ runaway spawns. The guardian **adopts
  whichever process is serving** (it seeds from the live listener, so it can never
  kill the live server) and reaps the rest. *Corollary: pick one launch path — let
  the guardian own the server; don't also run `webAgent.bat` alongside it.*
- **the TUI itself** — if the TUI process vanishes **without** a clean-quit marker
  (i.e. it crashed or its window was closed), it relaunches the TUI in a fresh
  console window.

It's **on by default** and designed to be invisible:

- **Spawned on open, singleton.** The TUI starts it on launch; reopening the TUI
  never stacks a second one (a cheap pre-check plus an atomic `O_EXCL` claim on
  `guardian.pid` make a duplicate spawn a harmless immediate no-op).
- **Truly detached.** It runs windowless in its own process group, so it survives
  the TUI crashing/closing — that's the whole point.
- **Clean quit vs crash.** Only a deliberate quit (**Ctrl+Q**, or the self-update
  restart) writes `tui.clean_exit`; the guardian then leaves the TUI closed.
  Anything else (crash, window-X) has no marker → the TUI is revived.
- **Restart cycles the guardian.** A **Ctrl+Q** quit leaves the guardian running
  (so it keeps the server alive while you're away). A deliberate **restart** (the
  self-update flow) instead retires the old guardian on the way out, so the
  relaunched TUI spawns a **fresh** one — this is how a self-update's new guardian
  code actually takes over instead of an old long-lived process lingering.
- **Off switch.** **Admin ▸ `[Keep-alive: ON/OFF]`** flips it. **OFF** terminates
  the guardian but **leaves the running server and window untouched** — it only
  stops auto-reviving them. The state lives in `guardian.json` and **persists
  across restarts** (a future open won't re-spawn it until turned back ON).

Standard-library only and import-light (no Textual / httpx / tool registry), so
the daemon stays tiny. The runtime files (`guardian.pid`, `guardian.json`,
`tui.pid`, `tui.clean_exit`, `guardian.log`) live in the data dir and are
**gitignored**. The guardian-launched server writes the **same** `server.pid` /
`server.log` the manager already tracks, so Server status/logs keep working.

## The Playbook (self-healing issue knowledge base)

The watchdog doesn't just react — it **learns**. The Playbook
([`tui_app/playbook.py`](tui_app/playbook.py) pure logic +
[`tui_app/pb_coordinator.py`](tui_app/pb_coordinator.py) persistence) turns every
detected problem into a closed feedback loop:

1. **Detect → Fingerprint.** Each condition (server down, zombie port, crash-loop,
   error spike, resource pressure) and each error diagnostic is reduced to a stable
   **issue key** — diagnostics are normalised (paths/numbers/hex/UUIDs stripped) so
   near-identical errors cluster into one issue.
2. **Pick & apply a remedy.** The issue's remedies are ranked by **confidence**
   (Laplace-smoothed success rate). The best one runs — if the policy allows.
3. **Verify.** Over a short window the watchdog re-checks whether the condition
   actually cleared (health back up, error didn't recur, metric under threshold).
4. **Learn.** Cleared → the remedy is credited (`helped`); didn't → `didn't-help`
   and, under `self_heal`, the agent is asked to dig in. Confidence re-ranks.
5. **Program a trigger onto itself.** After a diagnostic issue recurs
   `program_trigger_after` times, the system auto-writes a standing **alarm rule**
   for it, so it's loudly + explicitly detected from then on.

The knowledge lives in the manager's own `webagent.db` (`playbook_issues` /
`playbook_remedies` / `playbook_incidents`) and **survives restarts** — it
accumulates over time.

**Remediation mode** (in `monitor.json`, set via `set_monitor_config` or the
Playbook screen) governs how autonomous it is — it composes with the autonomy
level (a remedy runs only if both allow it):

| `remediation_mode` | Behaviour |
|--------------------|-----------|
| `document` | Records issues + ranks remedies, but never acts on its own. |
| `safe_auto` *(default)* | Auto-runs only the built-in safe remedies (restart, clear-port, escalate). |
| `autonomous` | Also auto-runs **approved** learned shell-command remedies once they prove themselves. |

**Remedy catalog:** built-in safe actions (`restart_server`, `clear_port`,
`escalate_to_agent`, `notify_only`) plus agent-authored **`command`** (a shell
command — starts *suggested*, must be approved before it auto-runs) and **`note`**
(a written instruction, never auto-run).

**Agent tools:** `playbook_list`, `playbook_show`, `playbook_record_remedy`,
`playbook_set_remedy` (approve / disable / prioritise), `playbook_forget`. The
agent consults the Playbook before guessing and records a remedy after it diagnoses
a recurring problem — that's how triggers get programmed onto the system over time.

**Playbook screen:** the **Playbook** header pill (managed mode) opens a side panel
listing every learned issue with its best remedy + confidence; click an issue to
see its remedies (helped/didn't stats), recent incidents, and approve/disable/forget
controls, plus the remediation-mode selector.

## Subagents & the event-driven loop (mk2)

The agent loop ([`tui_app/agent.py`](tui_app/agent.py)) is **event-driven and
re-entrant**. A turn can be triggered by any of three events:

| Event | Source | What the agent does |
|-------|--------|---------------------|
| **User message** | you type | a full turn — think, call tools, respond |
| **Subagent completed** | a background worker finishes | the result is folded in as a synthetic message; the agent reacts |
| **Watchdog alert** | the monitor (under `self_heal`) | a crash-loop / error-spike becomes a turn — diagnose + remediate |

**Idle vs busy.** The agent is either idle (ready) or busy (mid-turn). While busy it keeps
accumulating subagent results in a buffer; at the top of every loop iteration it drains the
buffer and any **steering** message, so background work and mid-turn steers fold into the
same conversation. If a result lands while the model is answering, the loop **auto-continues**
instead of ending, so nothing is stranded.

**Delegation tools** (the agent's interface to subagents):

| Tool | Behaviour |
|------|-----------|
| `delegate_async(task, tools, mode, group_id)` | launch a subagent and keep working; returns a task id. `mode="notify"` delivers its result on its own; `mode="gather"` + a shared `group_id` delivers a whole group **once all members finish** |
| `delegate_background(task, tools)` | fire-and-forget; result is stored but never auto-triggers a turn — pull it with `check_task` |
| `check_task(task_id)` | running / completed / error + summary |

**Caller-scoped tools.** A subagent only ever gets the exact tool names passed in `tools`
(no inherited default) and cannot spawn its own subagents — so a fan-out worker can be made
read-only or granted write/run access deliberately, per call.

The mechanics live in [`tui_app/subagents.py`](tui_app/subagents.py) (a Textual-free,
unit-tested registry + result buffer + delivery logic) and
[`tui_app/tools/delegate.py`](tui_app/tools/delegate.py) (the three tools). The
Textual app ([`tui_app/app.py`](tui_app/app.py)) hosts the workers: `_spawn_subagent`
creates the sub-session and launches a worker; `_run_subagent` runs it and reports back; steering
is queued on submit while busy; `_after_turn_settle` handles auto-continue. Tests live in
[`tests/`](tests/) (`test_subagents.py`, `test_delegate.py`, `test_event_loop.py`).

## Testing

Two complementary styles, both as plain `asyncio.run(...)` scripts (no
pytest-asyncio dependency), run with the venv Python:

| Style | What it drives | Files |
|-------|----------------|-------|
| **Logic / agent loop** | `ServerManagerAgent` directly with a `FakeLLM` — no UI, no network. Verifies the event-driven loop, subagents, and delegate tools. | `test_event_loop.py`, `test_subagents.py`, `test_delegate.py`, `test_playbook.py` |
| **UI / Pilot** | The whole `ServerManagerApp` **headlessly** via Textual's `App.run_test()` → a `Pilot`. Boots into an off-screen buffer (no terminal opens), then types, presses keys, clicks widgets, inspects the tree, and takes **text** screenshots to verify appearance. | [`pilot_harness.py`](tests/pilot_harness.py) (reusable boot/snapshot/LLM helpers), [`test_tui_pilot.py`](tests/test_tui_pilot.py) (pure-UI smoke test), [`test_tui_pilot_repo_dir.py`](tests/test_tui_pilot_repo_dir.py) (Admin ▸ Repo directory field save/clear → DB), [`test_tui_pilot_admin_panels.py`](tests/test_tui_pilot_admin_panels.py) (every side panel builds without crashing — guards Admin ▸ Reset) |
| **UI / Pilot + LLM** | A full chat turn driven **through the UI** with a scripted `FakeLLM` — prompt submit → agent loop → assistant bubble → token HUD → Stop pill — offline and deterministic. Use `use_fake_llm` + `drive_turn` from the harness. | [`test_tui_pilot_llm.py`](tests/test_tui_pilot_llm.py) |

Run a Pilot test:

```
cd tests
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ../.venv/Scripts/python.exe test_tui_pilot.py
```

The harness handles the three boot quirks for you — `_do_autostart = False` (no
managed server launch), `cfg.bridge_enabled = False` (silence the bridge thread),
and UTF-8 stdout (the box-drawing chrome crashes a cp1252 console). `snapshot(app)`
returns the off-screen buffer as plain text (capture it inside the `run_test` block,
print/assert **after**). Header/category buttons are id-less — select them with
`hdr_button(app, "<_btn_action>")` and pass the widget to `pilot.click`.

To test a **chat turn through the UI** without a live provider, install a scripted
LLM and drive a submit: `use_fake_llm(app, [assistant("hi")])` then
`await drive_turn(app, pilot, "say hi")`. The fake also handles tool-call turns
(`call("tool", {args})` — the agent runs the tool for real and loops to the next
scripted reply). `drive_turn` presses Enter as a real key event (so the submit
message resolves) and waits on `app._busy` (not `wait_for_complete`, which would hang
on the never-ending watchdog worker). Leave the fake out only for a genuine
live-provider smoke test — that needs an API key + network and a longer timeout.

## Configuration files (human-readable)

The agent's **prose and settings** live in files, not Python, so its behaviour can
be read and changed without editing code or rebuilding the `.exe`. See
[`tui-data/README.md`](tui-data/README.md):

| File | Holds |
|------|-------|
| `tui-data/prompt.md` | The system prompt (loaded at startup; restart to apply). |
| `tui-data/tools.json` | Each tool's description + `enabled` + category (the code only binds names → handlers + schemas). |
| `tui-data/monitor.defaults.json` | Shipped watchdog defaults; seeds the live `monitor.json`. |

Loaded by [`tui_app/resources.py`](tui_app/resources.py) with a
**user-override → packaged-default → built-in-fallback** order, and bundled into
the frozen build by `scripts/build_exe.py`.

## Run from source

**One-click (Windows):** double-click **`run.bat`** in this folder — it creates
the `.venv` + installs deps on first run, then launches the TUI from source (so
it always reflects the latest code, no `.exe` rebuild needed).

**By hand** (needs Python 3.11 or 3.12):

```bash
cd webagent
pip install -e .
WEBAGENT_PROJECT=/path/to/webagent python -m webagent
```

Already have the `.venv` from a previous run? Just launch with it directly —
`.venv\Scripts\python.exe -m webagent` (Windows) or
`.venv/bin/python -m webagent` (macOS/Linux) — no reinstall needed.

**Install as a global `webagent` command (uv).** The package exposes two console
scripts — `tui-app` and `webagent` (both launch the TUI). To put `webagent` on
your `PATH` straight from the repo in one line, no manual clone needed:

```bash
uv tool install "git+https://github.com/botboss3000/webagent-dev.git#subdirectory=TUI"
```

This installs a **frozen snapshot** of whatever is on GitHub. For a local working
copy whose command tracks your live edits, clone the repo and install editable
against the `TUI` folder instead: `uv tool install --editable ./webagent-dev/TUI`.
Either way, launch afterwards with `webagent`.

- **Target project** — the webAgent checkout to operate on. Auto-detected from
  the working directory if it contains `run.py` + `app/`; otherwise set
  `WEBAGENT_PROJECT`.
- **LLM provider** — resolved (highest priority first) from an explicit
  `WEBAGENT_*` override → the **linked repo's `provider.json`** (the same
  credential store the webAgent server itself uses; `admin` profile
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
cd ~ && pkg install -y git && git clone --depth 1 https://github.com/botboss3000/webagent ~/webagent && bash ~/webagent/TUI/install-termux.sh
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
a **`webagent`** command onto `$PREFIX/bin` and a `~/.shortcuts/webagent.sh`
**Termux:Widget** shortcut (install the Termux:Widget add-on from F-Droid for a
tappable home-screen button). Launch afterwards with `webagent`. The script is
idempotent — safe to re-run to update.

On Android the agent runs in onboarding / managed mode but **headless-browser
features are off** (no Chromium on Android), so it's a codebase / source-control /
diagnostics manager there. Give it a key inline (`LLM_API_KEY=… webagent`) or
in the checkout's `.env`; the installer prints exactly what to set if none is found.

### Guided onboarding (tap to start)

When no checkout is linked, the onboarding screen shows a tappable **`Click here to
get started`** button under the host/model details. Tapping it enables writes for
the session and hands the agent a kickoff message that runs the whole guided install
(readiness → clone → environment → seed → verify → link), explains the Android
browser skip, fixes issues as they arise, and on Termux finishes by writing the
home-screen shortcut (the `setup_launch_shortcut` tool) with instructions to add the
Termux:Widget. It's deliberately tap-driven, because a terminal app **cannot raise
the Android soft keyboard** — that's controlled by Termux, not the program (raise it
from the **Termux left-edge drawer ▸ KEYBOARD** toggle, or **Volume-Up + K**, if you
need to type; the footer's **⌨ Keyboard** shortcut focuses the input and, on Termux,
flashes this reminder).

The agent's onboarding context is a **live guide fetched from the public repo** at
runtime — [`onboarding-guide.md`](onboarding-guide.md). Edit that file (and push to
`main`) to improve onboarding for every installed manager with **no TUI change or
reinstall**. The last fetched copy is cached in the data dir for offline use, falling
back to the agent's built-in instructions if neither network nor cache is available.

### Uninstall (Android/Termux)

Remove the four things the installer created:

```bash
rm -f "$PREFIX/bin/webagent" "$HOME/.shortcuts/webagent.sh"   # launcher + home-screen shortcut
pip uninstall -y webagent 2>/dev/null                      # the package (if pip-installed)
rm -rf ~/webagent                                              # the cloned repo + its venv
rm -rf ~/.local/share/webagent                             # the manager's data (history, config, cached guide)
```

The data folder honours `XDG_DATA_HOME` / `WEBAGENT_DB` overrides; `textual`,
`httpx`, and `websockets` are left in place since other tools may use them. The agent can also do this
for you on request — the uninstall steps live in its onboarding guide too.

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
cd webagent
pip install -e ".[build]"
python scripts/build_exe.py      # → ./webagent  (or webagent.exe)
```

The build **stamps the source commit + timestamp** into the bundle (a generated,
gitignored `tui_app/_build.py`, removed from the tree right after) so a
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
| **frozen `.exe`** | fetches fresh source, rebuilds the exe with PyInstaller, **backs up the current exe** (`webagent.<timestamp>.bak.exe`), and stages the new one beside it | a detached helper waits for exit, swaps the new exe in, and relaunches |

- **Backups.** Source backups go to `…/webagent/self-backups/source-<timestamp>/`
  in the data dir; the exe backup is written next to the running exe (timestamped),
  falling back to the data dir if that folder isn't writable.
- **Safety.** Gated behind *Allow writes* like any mutation, fully audited, and
  fast-forward-only (never force). A frozen rebuild needs **git + Python 3.11/3.12**
  on the host; `self_update` reports exactly what's missing and changes nothing if
  it can't proceed.
- **Restart ends the session.** `self_restart` closes the manager so the swap /
  reload can finish, then relaunches it in a new window. A source `git pull` updates
  the **whole repository**, so a managed checkout living in the same repo is updated
  too. Work (clone + build venv) is cached under `…/webagent/self-update/`.

## Header toolbar + side panels (clickable)

The header is a row of **clickable pill buttons** (rounded, padded — easy to tap on a
phone; no title/model text). The pills are **flat** — they share the main background and
are **stacked with no gaps**, distinguished by a bright outline rather than a filled box.
An open category (and the current mode) is highlighted by **colouring its outline + text**,
not by inverting its fill. Clicking a category opens a **thin panel docked to the right**
holding that category's controls. The **chat column stays visible to its left**, and the
header/footer span the full width above and below — opening a menu never hides the
conversation. The panel appears only while a category is open. **Every panel view has
an expand/collapse toggle at the top** (`[‹› wide]` / `[›‹ narrow]`) that widens the
panel for the busier views (Connect, App Config) and is remembered across sessions.

Directly below the category toolbar is a **second header row — the session tab bar**.
It always shows three controls on the left, then one pill per **open session**:

| Tab-row item | Action |
|--------------|--------|
| **✕** (far left, always visible) | **Closes the window** (quits the manager). It no longer closes the side panel — panels toggle shut by clicking their own category again. |
| **+** | **Starts a new session** in a fresh tab and switches to it (the transcript clears to an empty conversation). |
| **SESSIONS** | Opens the **ALL SESSIONS** panel — the list of every stored session. Clicking a session there **opens it as a new tab** (or just switches to it if already open). |
| **session tabs** | One pill per open session, the active one highlighted. **Click a tab** to switch the transcript + agent context to that session. Tabs are named by the same auto-summary the Sessions list uses, updating after each turn. |

So a session is either **started fresh** (the **+** button or `/new`) or **resumed** from the
SESSIONS list — either way it becomes a tab you can click between. Switching is instant: the
agent re-reads each session's history by id, so its context follows the active tab.

| Header item | Action |
|-------------|--------|
| **mode** (far left) — a **one-word** write-gate (`read` / `write` / `auto`) | **Click to cycle** read → write → auto (colour signals the mode). Maps to the App panel's **Ask / Plan / Auto** pill (plan↔read, ask↔write). |
| **Admin** | a **Repo directory** field (paste a folder path → `[Save]` / `[Clear]`) at the top, then opens `[Connect]` · `[App Config]` · `[Model Settings]` · `[Commands]` · `[Update]` · `[Install]` · `[Reset]` · `[Uninstall]` · `[Diagnostics]` · `[Logs]` · `[Keep-alive: ON/OFF]` |
| **Git** (managed mode only) | source control: a **GitHub token** field with `[Save]` / `[Clear]` (used to authenticate network ops; stored in the TUI's own config, never written into the repo's `.git/config`), then `[Fetch]` · `[Pull]` · `[Push]`. Each button hands the agent a plain-language request so it runs the matching `git_tool` op under the usual op-safety rules (force-push blocked); Pull/Push arm writes first since the click is the consent. |
| **Playbook** (managed mode only) | the self-healing **issue knowledge base**: a **remediation-mode** selector (`[Document]` / `[Safe-auto]` / `[Autonomous]`), then the list of learned issues (occurrences, status, best remedy + confidence). Click an issue to drill in: its remedies with helped/didn't stats, recent incidents, and `[Approve]` / `[Disable]` / `[Forget]` controls. See [The Playbook](#the-playbook-self-healing-issue-knowledge-base). |
| **App** | the **AI provider** block — **Provider** as a grid of **pill buttons** (OpenRouter / OpenAI / DeepSeek / Groq / Together / Mistral / xAI / Custom); clicking one highlights it and fills the **Base URL** + **Model** to match (Custom leaves them as typed). Then a plain-text **AI key** field, `[Save]` / `[Clear]`; plus the write-gate `[Read-only]` / `[Write]` / `[Autonomous]` (current one highlighted) and `[Open Browser]` (opens `http://localhost:8080/index.html`). **Keys are shown in clear text** (not masked) so you can verify what you pasted. |
| **server status** (right after **App**, managed mode) | the live `live` / `stopped` pill (spins `…starting` while booting). **Click it** to open the **Server** view (`[Start]` · `[Restart]` · `[Kill]`). |

**Closing a panel:** click the **same category again** to toggle it shut. (Esc no longer
closes the panel — it now stops the running agent turn.) The open category is highlighted in
the header.

**Admin panel actions:**

| Button | Action |
|--------|--------|
| **Repo directory** (field, top of the panel) | Paste the folder a webAgent checkout lives in (or should live in), e.g. `C:\webagent`, and `[Save]`. The path is stored as an entry in the manager's own SQLite store (`settings` table, key `repo_dir`) and the agent is handed a message — *"the repo directory X has been saved"* — telling it to **link** an existing checkout there, or **install** one if the folder is empty. The field is pasteable and pre-fills from the saved entry; it's also written whenever the agent links a checkout itself, so it always reflects the directory in play. `[Clear]` forgets it. (If no AI key is set yet the path is still saved, with a nudge to set a provider first.) |
| `[Connect]` | Open the **Connect** view — browse the admin's agents, pick one, then pick (or start) a session to set the **target**, and flip the two mute toggles (see [Driving the running app](#driving-the-running-app-the-two-mutes)) |
| `[App Config]` | Open the **App Config** view — edit `app-settings.json` (access mode, presentation mode, **render recorder** on/off — the browser flight-recorder that captures HTML snapshots / lag / JS errors), saved over the admin API; plus **Session naming** (TUI-local) — how the Sessions list names each conversation: **Summary (AI)** (LLM summary of the last ~10 messages, falling back to the latest user message), **Latest message**, or **Off** |
| `[Model Settings]` | Open the **Model Settings** view — the app's **LLM provider + auth key** (provider quick-pick pills that fill base URL + model, then provider / base URL / model / API key), saved over the admin API. (Moved here out of App Config.) |
| `[Commands]` | Print a user reference to the transcript — on-screen controls, keyboard shortcuts, plain-language things to ask the agent, and the terminal commands for install / launch / proot-Python / uninstall (tailored to Termux vs desktop) |
| `[Update]` | Update the manager/repo — backs up, pulls (source) or rebuilds the exe (frozen), and restarts |
| `[Install]` | Run the guided install (onboarding mode); in managed mode it points you at `[Update]` instead |
| `[Reset]` | Reset the install to a clean state — stops the server and wipes the userbase (active database backend + generated pages), app secrets, and local logins (keeps `.env` + agent templates so the app reboots clean). **Backend-aware:** an active **Postgres** DB has its schema dropped & recreated (irreversible — not backed up); an active **SQLite** DB and the other files are **backed up** to `temp/reset-backup-<timestamp>/` first. For a deeper/lighter wipe, ask the agent to run `reset_app` with the flags you want |
| `[Uninstall]` | Remove webAgent from the device (Termux) — lists exactly what's deleted (launcher, shortcut, repo, data, package), then removes it and closes |
| `[Diagnostics]` | Show the app's recorded warnings/errors — reads the local DB, so it works even when the server is down |
| `[Logs]` | Show the captured server log in the transcript |
| `[Keep-alive: ON/OFF]` | Toggle the **external keep-alive guardian** (see [Keep-alive guardian](#keep-alive-guardian-survives-the-tui)). ON keeps the server **and** this window alive through crashes; OFF stops supervising but leaves both running. Persists across restarts |

Install, Update, Reset and Uninstall each open a **confirmation right inside the
sidebar** (the panel switches from its buttons to an info + `[…]` / `[Cancel]` view —
no pop-up modal); `[Cancel]` returns to the buttons. Uninstall is irreversible.
Reset of a **SQLite** install is reversible via its backup; Reset of a **Postgres**
install drops the schema and is **not** reversible.

### Driving the running app (the two mutes)

The TUI can hold a **live, shared conversation** with the running app's own agent — the
same stream the browser sees, so anything you do is saved server-side and continuable in
the web UI. Open **Admin ▸ Connect**, pick an **agent** and a **session** (or start a new
one) to set the **target**, then use the two toggles:

| State | Effect |
|-------|--------|
| **Mute WebAgent** *(default)* | No app link — the **Manager** (the TUI's own agent) talks to you. Prep it and set up the target here. |
| **Unmute WebAgent** | The Manager is **bridged** to the target session: it's told the agent/session is ready and can send/receive/respond to the app agent on your behalf (`webapp_send`); replies — and anything you type in the browser — stream into the transcript. |
| **Mute Manager** | The Manager goes silent and **your input goes straight to the app agent** — using the web app normally, from the TUI. |

The connection is built on the app's own HTTP + WebSocket API (login as `admin/admin` by
default), so the TUI can only do what a logged-in admin can. The agent also has matching
tools (`app_login`, `app_list_agents`, `app_list_sessions`, `webapp_send`, `webapp_status`,
`app_chat`, and the config tools `app_get/set_settings`, `app_get/set_auth_keys`).

### Open-time process manager

On open the manager **lists every running webAgent server process** (PIDs, whether each
holds port 8080, and which one it tracks) and flags **stale/zombie** instances — a process
squatting on 8080 without serving `/health`, or a leftover `run.py` from a crashed launch.
If any are found it opens a sidebar confirmation listing them; **with your permission** it
terminates them. The healthy/serving instance (and a healthy server's uvicorn-reload
supervisor) is never touched.

### Setting the AI key / provider (App panel)

Pick your **Provider** (it sets the matching **Base URL** and a default **Model**),
paste your **AI key**, and press **`[Save]`** (or Enter in the key field). **`[Clear]`**
forgets it. A key saved here is an **explicit override that wins over the linked repo's
`provider.json` / `.env`** (only a `WEBAGENT_*` env var outranks it), so you can fix
a bad or mismatched key straight from the manager. Everything persists to the manager's
config.

> The usual cause of a **"missing authentication header" / 401** is a key paired with
> the *wrong base URL* (e.g. an OpenAI key sent to the OpenRouter endpoint). The
> Provider dropdown keeps the URL and the key aligned. The client is **OpenAI-compatible**
> (`/chat/completions`, `Authorization: Bearer`), so use a provider that exposes that —
> Anthropic's native API is **not** OpenAI-compatible; reach Claude via OpenRouter instead.

> **Transient provider hiccups are retried.** OpenAI-compatible providers (OpenRouter
> especially) sometimes return a rate-limit / upstream-failure body — including an
> **HTTP 200 that carries an error or no `choices`** instead of a completion. The client
> retries these a few times with exponential backoff, so a single blip no longer fails the
> turn. Only a persistent failure surfaces, and now with a readable cause (the provider's
> message + raw body) rather than the old cryptic `bad response shape: 'choices'`.

The server **auto-starts** when you open the manager in managed mode (if it isn't
already running), so there's no separate Launch control. The server status item is
polled every few seconds (live = green, stopped = red, checking = amber).

**Small screens:** the panel is narrow (capped at 60% width) so the chat stays visible
even on a phone, and its labels stack vertically and wrap. Because the header collapsed
to a few short words (Admin · Git · App), it fits a narrow terminal.

### Look & feel (vendored from the launcher)

- **Plain-text header** — a simple two-line title (**WEBAGENT** / **Server Manager**)
  at the top of the chat column, coloured from the active theme. (The old animated
  ASCII logo banner was removed.)
- **Theme** — cycle the 23 shared themes with **Ctrl+T** (there is no longer a Theme
  header button). The choice applies **live** and persists.
- **Activity spinner** — a small `-/|\` spinner above the input spins whenever the
  agent is busy (thinking or running a tool), so it's clear the app isn't frozen;
  it's blank at rest.
- **Stop / Continue** — small bracketed **text** (`[Stop]` `[Continue]`) on the right of
  the action bar (above the input): **Stop** cancels the running turn (enabled only while
  busy; **Esc** does the same from the keyboard); **Continue** asks the agent to pick up
  where it left off (enabled only when idle).
- **Messages** — your messages render in a bordered **bubble** that matches the input pill
  (the main background with a bright outline); the agent's replies render as **Markdown**
  (lists, emphasis). Every **fenced code block** in a reply becomes its own framed
  **code card** with a one-click **`[Copy]`** button (copies the raw code to the OS
  clipboard, flashes `[Copied]`) and a `~N tok` size estimate. No user/agent emoji prefixes.
- **Expandable tool calls** — a turn's tool calls collapse to a single **"N tool calls"**
  row; expand it to list each call. The collapsed call row shows the tool name, an
  **ok/error** mark, the call's **wall time** (e.g. `717ms` / `1.2s`), and an output
  **`~N tok`** estimate. Expand a call to see its **arguments** and **result** as
  framed **code cards**, each with its own **`[Copy]`** button. Results that are
  **file diffs** (from `edit_source` / `patch_source`) are **colour-coded** — additions
  in the success colour, removals in the error colour, hunk headers in the accent —
  and error results show their first line in red. (All theme variables, so it reads in
  light AND dark.)
- **Transcript scrolling** — the chat pane is **anchored to the bottom**: it auto-follows
  new output while you're at the bottom, but the moment you scroll up (wheel or scrollbar)
  it **stays put** so you can read history without being yanked down. Sending a message
  (or **Continue**) snaps back to the bottom and re-arms the follow.
- **Session HUD** — a tight line: tokens in/out this session and a compact context
  reading **`ctx N%`** (green → amber → red) when the model's window is known.

## Keyboard

| Key | Action |
|-----|--------|
| `Enter` | Send. A **bare** Enter submits in every terminal spelling (`Ctrl+M` / `Ctrl+J` / numpad Enter all count), so it's reliable regardless of keyboard protocol |
| `Shift+Enter` / `Ctrl+Enter` / `Alt+Enter` | Insert a newline in the input without sending — *when your terminal reports the modifier distinctly* (some terminals collapse all Enters into one keystroke; on those, only plain Enter exists and these send too) |
| `↑` / `↓` | **Recall previous messages** — when the input pill is **empty**, `↑` loads your last message; keep pressing `↑` / `↓` to scroll older / newer through this session's history (shell-style). Any other key ends recall. With text already in the pill, the arrows move the cursor as usual |
| `Esc` | **Stop the running agent turn** (its sole job now). When idle it does nothing; the side menu opens from the header pills instead. (A confirm dialog still cancels on Esc.) |
| `Ctrl+Q` | Quit the manager |
| `Ctrl+A` | Select all text in the input field |
| `Ctrl+C` / `Ctrl+V` / `Ctrl+X` | Copy / paste / cut — wired to the **real OS clipboard** (Ctrl+V reads it, Ctrl+C/X write it), so pasting a key/token from a browser or password manager works. (Textual's defaults only use an internal buffer + OSC 52, which don't reach the OS clipboard on Windows/many terminals.) **Ctrl+V also pastes an _image_** on the clipboard as an attachment — see [Image attachments](#image-attachments). |
| `Ctrl+T` | Cycle theme (23 shared with the launcher; not shown in the footer) |

The **footer** is minimal: a left `Esc stop` hint and a right-aligned **⌨ Keyboard**
shortcut. Tapping **⌨ Keyboard** focuses the input — the standard way to raise the soft
keyboard on desktop and most platforms.

> **Android/Termux note.** A terminal program **cannot force the Android soft keyboard
> up** — only the OS/Termux can. If tapping **⌨ Keyboard** doesn't raise it, open it
> from the **Termux left-edge drawer ▸ KEYBOARD** toggle (or Vol-Up + K). The manager
> flashes this reminder on Termux.

**Copying transcript text.** Code blocks in replies and a tool call's arguments/result
each carry a **`[Copy]`** button that writes straight to the OS clipboard — the easiest
way to grab code or output. The footer's `Ctrl+C` copies the input field. To copy any
other text from the **transcript**, hold **Shift** while dragging to use your terminal's
own native selection/copy (works anywhere on screen).

### Image attachments

The chat sends images to the model as a **multimodal message** (the OpenAI-compatible
`image_url` part), so you can paste a screenshot and ask about it. **This needs a
vision-capable model** (e.g. a GPT-4o / Claude-via-OpenRouter vision model); a
text-only model will reject the image.

- **Add an image two ways:** press **Ctrl+V** with an image on the clipboard
  (screenshot tools, browsers — Windows reads the clipboard's PNG/bitmap natively, with
  Pillow used automatically if it happens to be installed; macOS via `osascript`; Linux
  via `wl-paste`/`xclip`), **or drag an image file onto the terminal** — most terminals
  deliver a drop as a pasted file path, which the input recognises and attaches.
- **Multiple per message:** each attached image becomes a small **chip** above the input
  showing its filename with a **`[x]`** to remove it. Send with text, or on its own.
- **Where they live:** images are copied into the manager's data dir under
  `attachments/<session>/` and the conversation history stores only the **path + type**
  (re-read and base64-encoded at send time), so the SQLite store stays small and the
  attachment still rides along on later turns when the history is replayed. Terminals
  can't render the picture inline — the transcript shows `attached: <names>`.

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
  app at `/termux` — installs python+git, the TUI, a `webagent` launcher + a
  Termux:Widget home-screen shortcut); **guided onboarding** — a tap-to-start button
  that runs the whole install, driven by a **live onboarding guide fetched from the
  repo** (`onboarding-guide.md`), plus the `setup_launch_shortcut` Termux:Widget tool.
- **Planned next** — **keyless web search** + page reading, a secure **credential
  prompt** (ask-and-seed a key when a context has none), general-coding in any
  folder, live **progress streaming** for long installs (incl. the self-update
  rebuild), and opt-in autonomous self-repair. See
  `temp/webagent-onboarding-design.md` for the full design.
