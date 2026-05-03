# Plan: Windows App + Standalone Executable for webAgent

## Goal

Make the webAgent Python agent + terminal webapp runnable in two modes:

1. **Native Windows app** — run via `python app.main:app` from a Windows Python install (no WSL needed)
2. **Standalone executable** — bundle everything into a single `.exe` the user can double-click

---

## Current state

The project lives at `C:\Users\Alex R\Projects\webAgent\python_agent\` and is served by:

```
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

Key facts:

- **`app/api/terminal.py`** already has cross-platform PTY support using `pywinpty` on Windows and `pty`/`fcntl`/`termios` on Unix. No other files import Unix-only modules.
- **`app/main.py`** currently does a top-level `from app.api.terminal import router as terminal_router` — on Windows this will try to `import pywinpty` and must succeed or the app won't start.
- **Static HTML** (`app/terminal/index.html`) is read from disk at runtime via `open()` — must be bundled for .exe.
- **Dependencies** (requirements.txt / pyproject.toml): `fastapi`, `uvicorn[standard]`, `openai`, `supabase`, etc. — all pure Python except `pywinpty` (native DLL) needed on Windows.
- **Configuration**: `.env` file with `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `OPENROUTER_API_KEY`.
- **Network required**: Supabase and OpenRouter APIs are always called over the internet.

---

## Approach

Two deliverables, can be done independently:

### Deliverable A: Native Windows runner (no WSL)

Make `python -m uvicorn app.main:app` work from a regular Windows Command Prompt / PowerShell.

#### Changes needed

1. **Add `pywinpty` to requirements.txt** — already supported in code, just needs `pip install pywinpty` or listed in deps.

2. **Soft-import terminal router in main.py** — wrap the top-level import so it doesn't crash if `pywinpty` isn't installed yet (show a helpful error instead):

   ```python
   try:
       from app.api.terminal import router as terminal_router
   except ImportError as e:
       terminal_router = None
       logger.warning(f"Terminal unavailable: {e}")
   ```

   And guard the `app.include_router(terminal_router)` call.

3. **Shell detection** — in `app/api/terminal.py`'s `spawn()`, the Windows branch already searches for `pwsh.exe`, `powershell.exe`, `cmd.exe`. This should work as-is. Verify on actual Windows.

4. **Test on Windows** — run from `C:\Users\Alex R\Projects\webAgent\python_agent\` using:

   ```
   .venv\Scripts\pip install pywinpty
   .venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8001
   ```

#### Files likely to change

| File | Change |
|------|--------|
| `requirements.txt` | Add `pywinpty` |
| `app/main.py` | Try/except import of terminal router |
| `README.md` | Add Windows setup instructions |

#### Risks & tradeoffs

- `pywinpty` requires a native build toolchain on Windows (MSVC build tools) or a pre-built wheel. On Python 3.12+, wheels may not be available → may need conda or a manual build.
- The terminal PTY on Windows will reflect the actual Windows shell experience (PowerShell/cmd) — different bash behavior users may expect if they're used to WSL.

---

### Deliverable B: Standalone executable

Bundle the whole app into a single `.exe` the user can download and run without installing Python.

#### Recommended tool: **PyInstaller**

PyInstaller is the most mature and widely used option. Nuitka is faster at runtime but harder to configure and slower to build. Start with PyInstaller.

#### What PyInstaller does

- Scans your imports, bundles the Python interpreter + all required modules + DLLs into one folder (or one file with `--onefile`).
- Launches a web server on localhost.
- Optionally auto-opens the browser on startup.

#### Steps

1. **Add entry point script** (`run.py`) at the project root:

   ```python
   # run.py — entry point for PyInstaller
   import uvicorn
   import webbrowser
   import threading

   def open_browser():
       webbrowser.open("http://localhost:8001/terminal")

   if __name__ == "__main__":
       threading.Timer(1.5, open_browser).start()
       uvicorn.run("app.main:app", host="0.0.0.0", port=8001)
   ```

2. **Configure PyInstaller** — create `webagent.spec`:

   - Include the `app/` package
   - Include `app/terminal/index.html` as a data file (or embed as a string)
   - Include `.env.example` (not the real .env — credentials must be configured by the user)
   - Hook `pywinpty` native DLLs
   - `--onefile` mode for a single .exe (slower startup) or `--onedir` (folder, faster startup)

3. **Handle static files** — currently `main.py` reads `app/terminal/index.html` and `test_interface.html` from disk with `open()`. PyInstaller bundles these as data files. The paths must be resolved at runtime using `sys._MEIPASS` when frozen. Add a helper:

   ```python
   import sys, os

   def resource_path(relative_path):
       """Get absolute path to resource, works for dev and for PyInstaller."""
       try:
           base_path = sys._MEIPASS
       except AttributeError:
           base_path = os.path.dirname(__file__)
       return os.path.join(base_path, relative_path)
   ```

4. **Build the executable**:

   ```bash
   pip install pyinstaller
   pyinstaller webagent.spec
   ```

   Output: `dist/webAgent.exe` (or `dist/webAgent/` folder)

5. **Credentials at runtime** — the user needs an `.env` file next to the .exe, or a settings dialog on first launch. Simplest approach: read `.env` from the same directory as the .exe.

#### Files likely to change / create

| File | Change |
|------|--------|
| `run.py` | **New** — entry point for PyInstaller build |
| `webagent.spec` | **New** — PyInstaller build spec |
| `app/main.py` | Add `resource_path()` helper for static files |
| `app/terminal/index.html` | No change needed |
| `.github/workflows/build.yml` | **New** — optional CI build for releases |

#### Build & distribution

- **Build platform**: must be built ON Windows to produce a Windows .exe. Cross-compilation from Linux is possible but unreliable with native DLLs.
- **Output**: ~40–80 MB .exe (Python interpreter + deps + native DLLs).
- **Distribution options**:
  - GitHub Releases (upload the .exe)
  - Simple zip file with .exe + instruction to create .env

#### Risks & tradeoffs

- **Virus false positives** — PyInstaller `.exe` files are frequently flagged by Windows Defender. Need to either sign the binary or submit to Microsoft for whitelisting.
- **Size** — 40–80 MB for a web server feels heavy for the user.
- **Updates** — user must re-download the .exe. No auto-update built in.
- **API keys still required** — not truly standalone if user needs to configure Supabase + OpenRouter keys anyway.
- **`supabase` library** uses HTTPX under the hood — native SSL handling must be bundled correctly (usually works with PyInstaller's default hooks, but test).

---

## Open questions

1. **Is the terminal PTY essential on Windows?** If the user mostly uses the chat panel (not the shell), we could stub out the terminal on Windows entirely and avoid `pywinpty` dependency. But `terminal.py` already supports it — just needs `pip install pywinpty`.

2. **Should the executable auto-open the browser?** Yes for consumer UX, but some users may want headless.

3. **How should credentials be managed?** Options:
   - `.env` file next to .exe (simplest)
   - Built-in settings page / first-run wizard (better UX, more work)
   - Command-line args: `webAgent.exe --supabase-url=... --api-key=...` (power-user friendly)

4. **Do we need a tray icon?** For a polished Windows app, hiding the console window and showing a system tray icon is standard. Requires `pystray` or `win32api` — adds complexity.

---

## Recommendation

**Phase 1 — Native Windows runner** (low effort, ~1 hour):

- Add `pywinpty` to deps
- Soft-import the terminal router
- Test on actual Windows

**Phase 2 — Standalone executable** (medium effort, ~4–6 hours):

- Create `run.py` entry point
- Create `webagent.spec` with PyInstaller
- Add `resource_path()` helper for static files
- Build and test on Windows
- Handle `.env` config at runtime

**Phase 3 — Polish** (optional, ~2–4 hours):

- Auto-open browser on startup
- System tray icon
- Sign the binary / handle antivirus false positives
- GitHub Actions CI build workflow
