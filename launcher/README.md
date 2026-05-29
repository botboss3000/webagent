# webagent launcher

Polished TUI that controls the webagent server. Bundles into a single
`webagent.exe` you can drop anywhere — Desktop, Start Menu, USB stick.

## What it does

- **Install (first run)** — set everything up from nothing on a clean machine:
  download the public webagent repo (via `git` if it's installed — which also
  enables clean **Update**s later — otherwise a ZIP snapshot), fetch the
  self-contained `uv` toolchain (which installs its own private Python),
  `uv sync` every dependency, and download the **Playwright Chromium** browser
  (~150 MB, into `%LOCALAPPDATA%\ms-playwright`) that the `browser_action` tool
  drives. The end user needs **nothing** pre-installed — just the `.exe`. The
  whole thing happens **inline in the launcher window** — the animated logo
  stays on screen the entire time, no pop-up modal. The install panel is a
  **destination field** (pre-filled with `C:\webagent`, editable — paste a path
  into it with `Ctrl+V`) with the **Download & Install** button on the same row,
  so it's never clipped. Until a
  project exists, the normal controls are hidden and the footer becomes
  installer tabs — **Install** (this panel), **Manual Install** (copy-paste
  steps to do it by hand), **Health Check** (a full environment report: git & uv
  versions, system Python, internet to GitHub & PyPI, plus the install's own
  state at the target — code, `.venv` + Python version, whether the
  dependencies and Chromium are present, the local DB, writable destination,
  free disk; re-runs each time you open the tab) — alongside **Theme** and
  **Quit**.
  Progress streams live in the same panel; once the install starts it **cannot
  be cancelled**, and the panel clears itself the moment it finishes (the
  launcher restores the normal buttons and drops into normal operation).
  Pointing the destination at a folder that's already a webagent checkout simply
  adopts and re-syncs it instead of downloading. (The Chromium download is
  best-effort: if it fails the server still runs and you can retry via Update —
  only the browser tool needs it.)
- **Launch** — starts `run.py` in the configured project folder, polls
  `http://localhost:8080` for readiness, optionally opens the browser
- **Restart** — graceful CTRL-BREAK then re-launch
- **Stop** — graceful shutdown, falls back to kill, cleans port 8080
- **Auto-restart (watchdog)** — if the server exits **unexpectedly** (crash /
  disconnect, i.e. anything that wasn't a Stop / Restart / reset you asked
  for), the launcher relaunches it on its own. Rapid crashes back off
  exponentially (1s → 2s → 4s …, capped at 30s) and the watchdog gives up
  after 5 rapid crashes so a genuinely broken build doesn't spin forever — fix
  the error in the log and press Launch to re-arm it. A run that stays up ≥30s
  is treated as healthy and forgives earlier crashes. On by default; toggle via
  `auto_restart_server` in `launcher.json`.
- **Health check (hung-server watchdog)** — a crash isn't the only way to lose
  the server: it can stay *alive but wedged* (blocked event loop) and answer
  nothing. While the server is running the launcher polls `http://localhost:8080`
  every ~10s; **3 consecutive misses** (~30s unresponsive) is treated as hung and
  the server is brought down and relaunched. It shares the same backoff and
  5-strike give-up guard as the crash watchdog, and several misses are required
  so a momentary blip under load can't cause a needless restart. On by default;
  toggle via `health_check_restart` in `launcher.json`.
- **Clear DB** — backs up and removes `app/db/local.db*` + `visuals/users/`
- **Reset Python** — wipes `.venv` and re-runs `uv sync` (if `uv` isn't on PATH
  it falls back to the launcher's own downloaded copy)
- **Full Reset** — DB clear + Python reset
- **Update** (`U`) — pull the latest code (`git pull` for a git checkout, else a
  fresh ZIP over the top), re-run `uv sync`, and refresh the Playwright Chromium
  browser (fast when it's already present). Your local data (DB, users,
  generated pages) is kept — it isn't in the public repo. The server is stopped
  during the update and restarted afterwards.
- **Browser** — opens the saved URL (default `/index.html`) in your normal browser
- **App Window** (`W`) — open the webagent app in a **chromeless Chromium
  window** (app mode — no tabs or address bar, like a native desktop app)
  instead of a browser tab. It's launched with a remote-debugging port, so the
  agent's `browser_action` tool **attaches to and drives the very window you're
  watching** (when the agent's **Browser Control** ability is on). Close the
  window and the agent silently reverts to its invisible headless browser. Uses
  the Playwright Chromium the installer downloaded (falls back to a system
  Chrome, then Edge). Local / Windows only — it needs a real screen, so it
  doesn't apply to the remote server. One window at a time; pressing `W` again
  while it's open is a no-op. The port can be overridden with
  `WEBAGENT_BROWSER_CDP_PORT` (default `9222`) — the launcher passes it through
  to the server so the two always agree.
- **Location** — re-point this exe at a different folder. The status bar shows
  the current **target**; clicking Location opens the installer panel pre-filled
  with it, with a live status line and an adaptive button — **Use this folder**
  when the path is already a webAgent install (adopt + ready), or **Download &
  Install** when it's new. **Cancel** leaves the current target untouched.
  Committing a new target stops any server bound to the old one and switches. A
  **Current Folder** button (shown only once the exe has a target) drops that
  saved target back into the box and re-saves it as the location in one click —
  no re-typing, no re-download; if the saved folder has gone missing it installs
  there instead. Re-confirming the folder that's already active leaves a healthy
  server running rather than bouncing it.
- **Chat** (`A`) — full keyboard-driven chat against the local server: live
  token streaming, expandable tool-call blocks, agent/session pickers. The
  header carries a **server dot** that tracks the connection in real time —
  `live` (green), `reconnecting` (amber) while a watchdog relaunches, and
  `disconnected` (red) when the server is down — so a crash/auto-restart is
  visible right in the chat. A
  **static ascii banner** sits at the top of the transcript (agent · model ·
  session · date) and scrolls up with the conversation — no background
  animation in chat. A little ascii **guy walks on the input "pill"** while the
  loop works (strolls while streaming, works during a tool, cheers on the
  reply, trips on an error). Above the input a **session HUD** shows total
  tokens, running cost, and a **context-window gauge** (used / max, green →
  amber → red, with a "context full" nudge). `Ctrl+F` opens a checkbox
  **filter** to show/hide interaction types (memory, loop steps, tool calls).
- **Theme** — inline settings panel that opens **in the log area** so the
  animation above stays visible and updates **live** as you change it. Pick a
  color preset (phosphor / amber / cyan / sunset / vaporwave / neon tide /
  tricolor / rainbow / fire / ice / lime pulse / …), the animation style, and
  drag the **Speed / Intensity / FPS** sliders (click-drag, or ←/→ when focused).
- **Animation** — `Plasma` / `Flow Field` / `Rings` / `Noise` (scrolling TV
  static), all colored by the active palette — plus **`Off`**, a motionless
  solid ASCII graphic that stops the frame loop entirely (~0% CPU).
- **Auto-idle** — the animation pauses itself whenever the launcher loses focus
  or the chat screen covers it, and resumes when you return. Lower the FPS
  slider (or pick `Off`) if the idle animation is still too busy for you.

**Each `.exe` is a pointer to one install — stored per-exe (no shared file).**
A `webagent.exe` is a static controller that points at one install folder; it
can **install**, **update**, or **run** that folder. The target (and prefs) live
in the **Windows registry** under `HKEY_CURRENT_USER\Software\webagent\Instances`,
keyed by **the exe's own path** — so you can keep several copies of the exe in
different folders, each pointed at a **different repo**, with no shared
`launcher.json` to collide. (In dev / non-Windows, a plain
`%APPDATA%\webagent\launcher.json` is used instead.)

On first run an exe has no saved target, so it shows the installer with the box
defaulting to `C:\webagent` (a live line warns if the chosen folder is empty,
already a webAgent install, or already holds other files). After it's pointed
somewhere, that exe runs/updates that folder. To re-point or start fresh, delete
the install folder (its saved target becomes invalid → installer shows again) or
clear the exe's entry under that registry key. Tools the launcher downloads
itself (like `uv`) are cached under `%APPDATA%\webagent\tools` (shared — it's a
tool, not per-target state).

## Develop

```powershell
cd launcher
uv sync
uv run python -m webagent_launcher
```

Press `T` to toggle the inline theme/animation panel (it swaps into the log
area; `Esc` or `T` again closes it). `C` cycles palettes and `Space` cycles
animation styles (including `Off`) without opening the panel.

## Build the .exe

Requires **Node.js** (to run the pi-ai model-extraction script) and the
`@earendil-works/pi-ai` npm package (the global `pi` install provides it).

```powershell
cd launcher
uv sync --extra build
uv run python scripts/build_exe.py
# → webagent.exe   (single portable file at the PROJECT ROOT)
```

The build script runs three steps:
1. **Generate `model_windows.py`** — extracts context-window sizes for every
   known model from `@earendil-works/pi-ai`'s auto-generated model registry
   via `launcher/scripts/build_model_windows.mjs`. The TUI uses this to draw
   the context-window gauge (used / max, green → amber → red).
2. **Regenerate the Lucide-bot icon** via `generate_icon.py`.
3. **Run PyInstaller** in onefile mode, moves the result to **`webagent.exe`**
   at the project root (the repo's top folder, parent of `launcher/`), and
   removes `launcher/build` and `launcher/dist`.

Re-generate model windows manually (e.g. after pi-ai updates):

```powershell
node launcher/scripts/build_model_windows.mjs
```

This produces `launcher/webagent_launcher/model_windows.py` (~969 models).
It is checked in — only re-run when pi-ai publishes new model entries.
The script skips gracefully if `@earendil-works/pi-ai` isn't installed.

Drop the .exe anywhere — first launch offers **Download & Install**
(fetch the repo + uv + Python + all dependencies) or point it at an
existing webagent folder.

> **Standalone window + emoji:** the .exe opens in its own console window. On
> Windows 10 that legacy console can't draw colour emoji, so the chat uses
> clean ASCII glyphs (`>>`, `ok`, `*`) there. If you instead launch the exe
> from **Windows Terminal**, it auto-detects that and switches to emoji
> (🔧 ✅ 🟢 …). Force either way with `WEBAGENT_EMOJI=1` / `WEBAGENT_EMOJI=0`.

## Keyboard

**Home / control screen**

| Key | Action |
|-----|--------|
| `A` | Open chat |
| `L` | Launch server |
| `R` | Restart server |
| `S` | Stop server |
| `B` | Open browser (your default browser) |
| `W` | Open the webagent app in a chromeless, agent-controllable Chromium window |
| `D` | Clear DB (with confirm) |
| `P` | Reset Python (with confirm) |
| `F` | Full reset (with confirm) |
| `U` | Update — re-pull code + re-sync dependencies (with confirm) |
| `T` | Toggle inline theme & animation panel (in the log area) |
| `C` | Cycle to next color preset |
| `Space` | Cycle animation style (incl. `Off`) |
| `Ctrl+Q` | Jump to chat (toggles with home) |
| `Esc` | Close the theme panel if open, otherwise quit |
| `Q` | Quit |

**Chat screen** — all command keys are `Ctrl`-prefixed so they never collide
with typing. Each binds the shifted symbol *and* the plain key, so it fires
whether your terminal reports `Ctrl+!` or `Ctrl+1`.

| Key | Action |
|-----|--------|
| `Ctrl+~` / `` Ctrl+` `` | Home |
| `Ctrl+Q` | Go back to last screen (reliable fallback) |
| `Ctrl+!` / `Ctrl+1` | Agent picker |
| `Ctrl+@` / `Ctrl+2` | Session picker |
| `Ctrl+#` / `Ctrl+3` | New session |
| `Ctrl+$` / `Ctrl+4` | New agent |
| `Ctrl+F` | Filter interactions (show/hide memory, loop steps, tools) |
| `Ctrl+C` / `Ctrl+V` / `Ctrl+Z` | Copy / paste / undo (editor). `Ctrl+V` pastes from the **system clipboard** (works in every field, incl. the install box). |
| `Enter` | Send · `Ctrl+J` newline |
| `Up` / `Down` | Recall previous / next sent message (single-line input) |
| `Esc` | Stop a running turn, then exit to home |
