@echo off
REM ─────────────────────────────────────────────────────────────────────────
REM Run the webAgent Server Manager (TUI) FROM SOURCE — no .exe build needed.
REM Double-click this file, or run it from a terminal. It always reflects the
REM latest code in this folder. Uses (and, on first run, creates) a local .venv.
REM ─────────────────────────────────────────────────────────────────────────
setlocal
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [run] First run: creating a virtual environment and installing deps...
    REM Needs Python 3.11 or 3.12 (the TUI pins ^>=3.11,^<3.13).
    py -3.12 -m venv .venv 2>nul || py -3.11 -m venv .venv 2>nul || python -m venv .venv
    if not exist "%VENV_PY%" (
        echo [run] Could not create .venv. Install Python 3.11 or 3.12 and retry.
        pause
        exit /b 1
    )
    "%VENV_PY%" -m pip install --upgrade pip
    "%VENV_PY%" -m pip install -e .
)

"%VENV_PY%" -m webagent_tui
if errorlevel 1 pause
