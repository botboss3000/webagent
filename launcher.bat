@echo off
title webAgent Launcher
cd /d "%~dp0launcher"
setlocal enabledelayedexpansion

:: One-click: run the webAgent LAUNCHER (the TUI control surface) from source.
:: No .exe build needed — this uses the live launcher code via uv.
:: (webAgent.bat at the repo root runs the SERVER; this runs the launcher.)

:: ── Ensure uv is installed (Astral's Python + venv manager) ──
where uv >nul 2>&1
if !ERRORLEVEL! neq 0 (
    echo [launcher] uv not found. Installing via PowerShell...
    powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    where uv >nul 2>&1
    if !ERRORLEVEL! neq 0 (
        echo [ERROR] uv installed but not found on PATH. Open a new terminal and re-run.
        pause
        exit /b 1
    )
)

:: ── Run the launcher TUI from source (uv auto-syncs deps on first run) ──
echo [launcher] starting webAgent launcher...
uv run python -m webagent_launcher
set "EXITCODE=!ERRORLEVEL!"

:: Keep the window open if it crashed, so the error is readable.
if not "!EXITCODE!"=="0" (
    echo.
    echo [launcher] exited with code !EXITCODE!.
    pause
)
endlocal
