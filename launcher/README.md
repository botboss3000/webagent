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
- **Theme** — color preset picker (phosphor / amber / cyan / sunset /
  vaporwave / neon tide / tricolor / rainbow / fire / ice / lime pulse / …)
- **Animation** — plasma / flowfield / rings / static, all colored by
  the active palette

Configuration lives at `%APPDATA%\webagent\launcher.json`. The .exe can
sit anywhere — first run prompts for your webagent project folder.

## Develop

```powershell
cd launcher
uv sync
uv run python -m webagent_launcher
```

Edit theme/animation live with `T`, `C` to cycle palettes, `Space` to
cycle animation styles.

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
| `T` | Theme & animation settings |
| `C` | Cycle to next color preset |
| `Space` | Cycle animation style |
| `Q` / `Ctrl+C` | Quit |
