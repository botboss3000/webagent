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

- **Web Search** — `web_search` (any mode, no API key) — search DuckDuckGo for
errors, docs, solutions, and current information.
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
cd ~ && pkg install -y git && git clone --depth 1 https://github.com/botboss3000/webagent ~/webagent && bash ~/webagent/webagent_tui/install-termux.sh
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
pip uninstall -y webagent-tui 2>/dev/null                      # the package (if pip-installed)
rm -rf ~/webagent                                              # the cloned repo + its venv
rm -rf ~/.local/share/webagent-tui                             # the manager's data (history, config, cached guide)
```

The data folder honours `XDG_DATA_HOME` / `WEBAGENT_TUI_DB` overrides; `textual` and
`httpx` are left in place since other tools may use them. The agent can also do this
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

## Header toolbar + side panels (clickable)

The header is a row of **clickable pill buttons** (rounded, padded — easy to tap on a
phone; no title/model text). The pills are **flat** — they share the main background and
are **stacked with no gaps**, distinguished by a bright outline rather than a filled box.
An open category (and the current mode) is highlighted by **colouring its outline + text**,
not by inverting its fill. Clicking a category opens a **thin panel docked to the right**
holding that category's controls. The **chat column stays visible to its left**, and the
header/footer span the full width above and below — opening a menu never hides the
conversation. The panel appears only while a category is open.

| Header item | Action |
|-------------|--------|
| **mode** (far left) — a **one-word** write-gate (`read` / `write` / `auto`) | **Click to cycle** read → write → auto (colour signals the mode). Same gate as the App panel's Read/Write/Auto. |
| **Admin** | opens `[Commands]` (command reference) · `[Update]` · `[Install]` · `[Uninstall]` · `[Diagnostics]` · `[Logs]` |
| **Scene** | the theme & animation controls (theme · animation style · palette · speed · intensity · FPS · banner on/off) — each applies **live** |
| **App** | the **AI provider** block — a **Provider** dropdown (OpenRouter / OpenAI / DeepSeek / Groq / Together / Mistral / xAI / Custom) that fills the **Base URL** + **Model** to match, an **AI key** field, and `[Save]` / `[Clear]` pills; plus the write-gate `[Read-only]` / `[Write]` / `[Autonomous]` (current one highlighted) and `[Open Browser]` (opens `http://localhost:8080/index.html`) |
| **server status** (the last item — shows `live` / `stopped`, **not** the word "Server"; managed mode; spins a `-\|/` with `starting` while it loads) | `[Start]` · `[Restart]` · `[Kill]` |

**Closing a panel:** click anywhere **outside** it (e.g. in the chat), press **Esc**,
or click the same category again to toggle it shut. The open category is highlighted in
the header.

**Admin panel actions:**

| Button | Action |
|--------|--------|
| `[Commands]` | Print a user reference to the transcript — on-screen controls, keyboard shortcuts, plain-language things to ask the agent, and the terminal commands for install / launch / proot-Python / uninstall (tailored to Termux vs desktop) |
| `[Update]` | Update the manager/repo — opens an info + confirm screen, then backs up, pulls (source) or rebuilds the exe (frozen), and restarts |
| `[Install]` | Run the guided install (onboarding mode); in managed mode it points you at `[Update]` instead |
| `[Uninstall]` | Remove webAgent from the device (Termux) — opens an info + confirm screen listing exactly what's deleted (launcher, shortcut, repo, data, package), then removes it and closes |
| `[Diagnostics]` | Show the app's recorded warnings/errors — reads the local DB, so it works even when the server is down |
| `[Logs]` | Show the captured server log in the transcript |

Install, Update and Uninstall each open a **warning + Yes/No confirmation** first;
Uninstall is irreversible.

### Setting the AI key / provider (App panel)

Pick your **Provider** (it sets the matching **Base URL** and a default **Model**),
paste your **AI key**, and press **`[Save]`** (or Enter in the key field). **`[Clear]`**
forgets it. A key saved here is an **explicit override that wins over the linked repo's
`provider.json` / `.env`** (only a `WEBAGENT_TUI_*` env var outranks it), so you can fix
a bad or mismatched key straight from the manager. Everything persists to the manager's
config.

> The usual cause of a **"missing authentication header" / 401** is a key paired with
> the *wrong base URL* (e.g. an OpenAI key sent to the OpenRouter endpoint). The
> Provider dropdown keeps the URL and the key aligned. The client is **OpenAI-compatible**
> (`/chat/completions`, `Authorization: Bearer`), so use a provider that exposes that —
> Anthropic's native API is **not** OpenAI-compatible; reach Claude via OpenRouter instead.

The server **auto-starts** when you open the manager in managed mode (if it isn't
already running), so there's no separate Launch control. The server status item is
polled every few seconds (live = green, stopped = red, checking = amber).

**Small screens:** the panel is narrow (capped at 60% width) so the chat stays visible
even on a phone, and its labels stack vertically and wrap. Because the header collapsed
to a few short words (Admin · Scene · App · status), it fits a narrow terminal.

### Look & feel (vendored from the launcher)

- **Animated logo banner** — plasma / flow-field / rings / noise (or a static
  "off") behind the "webagent" ASCII logo. Shown on the welcome screen, it
  **collapses once the conversation starts** so the transcript uses the full height
  (it doesn't stay pinned on top). Coloured from the active theme (or a chosen
  palette); stops animating when off or when the window loses focus (≈0% CPU).
- **Scene panel** — the theme, animation style, palette (match-theme or a preset),
  speed, intensity, FPS, and the banner on/off, opened from the header's **Scene**
  button. Every choice applies **live** and persists.
- **Activity spinner** — a small `-/|\` spinner above the input spins whenever the
  agent is busy (thinking or running a tool), so it's clear the app isn't frozen;
  it's blank at rest.
- **Stop / Continue** — small bracketed **text** (`[Stop]` `[Continue]`) on the right of
  the action bar (above the input): **Stop** cancels the running turn (enabled only while
  busy); **Continue** asks the agent to pick up where it left off (enabled only when idle).
- **Messages** — your messages render in a bordered **bubble** that matches the input pill
  (the main background with a bright outline); the agent's replies render as **Markdown**
  (code fences, lists, emphasis). No user/agent emoji prefixes.
- **Expandable tool calls** — a turn's tool calls collapse to a single **"N tool calls"**
  row; expand it to list each call, and expand a call to see its **arguments and result,
  each in a code block** (nested collapsibles, like the launcher).
- **Session HUD** — a tight line: tokens in/out this session and a compact context
  reading **`ctx N%`** (green → amber → red) when the model's window is known.

## Keyboard

| Key | Action |
|-----|--------|
| `Enter` | Send |
| `Esc` | Open the side menu (the **App** panel) — or close it if one is already open |
| `Ctrl+Q` | Quit the manager |
| `Ctrl+A` | Select all text in the input field |
| `Ctrl+C` / `Ctrl+V` / `Ctrl+X` | Copy / paste / cut (input field) |
| `Ctrl+T` | Cycle theme (23 shared with the launcher; not shown in the footer) |

The **footer** is minimal: a left `Esc menu` hint and a right-aligned **⌨ Keyboard**
shortcut. Tapping **⌨ Keyboard** focuses the input — the standard way to raise the soft
keyboard on desktop and most platforms.

> **Android/Termux note.** A terminal program **cannot force the Android soft keyboard
> up** — only the OS/Termux can. If tapping **⌨ Keyboard** doesn't raise it, open it
> from the **Termux left-edge drawer ▸ KEYBOARD** toggle (or Vol-Up + K). The manager
> flashes this reminder on Termux.

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
  app at `/termux` — installs python+git, the TUI, a `webagent` launcher + a
  Termux:Widget home-screen shortcut); **guided onboarding** — a tap-to-start button
  that runs the whole install, driven by a **live onboarding guide fetched from the
  repo** (`onboarding-guide.md`), plus the `setup_launch_shortcut` Termux:Widget tool.
- **Planned next** — **keyless web search** + page reading, a secure **credential
  prompt** (ask-and-seed a key when a context has none), general-coding in any
  folder, live **progress streaming** for long installs (incl. the self-update
  rebuild), and opt-in autonomous self-repair. See
  `temp/webagent-tui-onboarding-design.md` for the full design.
