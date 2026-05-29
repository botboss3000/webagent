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
  drives. The end user needs **nothing** pre-installed — just the `.exe`.
  Progress streams live in an install log. The first-run screen also still lets
  you point at a webagent folder you already have. (The Chromium download is
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
- **Browser** — opens the saved URL (default `/index.html`)
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

Configuration lives at `%APPDATA%\webagent\launcher.json`; tools the launcher
downloads itself (like `uv`) are cached under `%APPDATA%\webagent\tools`. The
.exe can sit anywhere — on first run choose **Download & Install** to set
everything up from scratch, or point it at a webagent folder you already have.

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

```powershell
cd launcher
uv sync --extra build
uv run python scripts/build_exe.py
# → webagent.exe   (single portable file at the PROJECT ROOT)
```

The build script regenerates the Lucide-bot icon, runs PyInstaller in
onefile mode, moves the result to **`webagent.exe` at the project root**
(the repo's top folder, parent of `launcher/`), and removes the
`launcher/build` and `launcher/dist` scratch folders so the only artifact
left is the .exe. Drop it anywhere — first launch offers **Download & Install**
(fetch the repo + uv + Python + all dependencies) or lets you point at a
webagent folder you already have.

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
| `B` | Open browser |
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
| `Ctrl+C` / `Ctrl+V` / `Ctrl+Z` | Copy / paste / undo (editor) |
| `Enter` | Send · `Ctrl+J` newline |
| `Up` / `Down` | Recall previous / next sent message (single-line input) |
| `Esc` | Stop a running turn, then exit to home |
