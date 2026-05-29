# webagent launcher

Polished TUI that controls the webagent server. Bundles into a single
`webagent.exe` you can drop anywhere — Desktop, Start Menu, USB stick.

## What it does

- **Launch** — starts `run.py` in the configured project folder, polls
  `http://localhost:8080` for readiness, optionally opens the browser
- **Restart** — graceful CTRL-BREAK then re-launch
- **Stop** — graceful shutdown, falls back to kill, cleans port 8080
- **Clear DB** — backs up and removes `app/db/local.db*` + `visuals/users/`
- **Reset Python** — wipes `.venv` and re-runs `uv sync`
- **Full Reset** — DB clear + Python reset
- **Browser** — opens the saved URL (default `/index.html`)
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

Configuration lives at `%APPDATA%\webagent\launcher.json`. The .exe can
sit anywhere — first run prompts for your webagent project folder.

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
# → launcher/webagent.exe   (single portable file in the launcher root)
```

The build script regenerates the Lucide-bot icon, runs PyInstaller in
onefile mode, moves the result to `launcher/webagent.exe`, and removes
the `build/` and `dist/` scratch folders so the only artifact left is
the .exe itself. Drop it anywhere — first launch will ask for the path
to the webagent project folder.

## Keyboard

| Key | Action |
|-----|--------|
| `L` | Launch server |
| `R` | Restart server |
| `S` | Stop server |
| `B` | Open browser |
| `D` | Clear DB (with confirm) |
| `P` | Reset Python (with confirm) |
| `F` | Full reset (with confirm) |
| `T` | Toggle inline theme & animation panel (in the log area) |
| `Esc` | Close the theme panel (when open) |
| `C` | Cycle to next color preset |
| `Space` | Cycle animation style (incl. `Off`) |
| `Q` / `Ctrl+C` | Quit |
