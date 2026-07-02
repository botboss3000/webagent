@echo off
title webAgent
cd /d "%~dp0"
setlocal enabledelayedexpansion

:: ── 1. Ensure uv is installed (Astral's Python + venv manager) ──
where uv >nul 2>&1
if !ERRORLEVEL! neq 0 (
    echo [webAgent] uv not found. Installing via PowerShell...
    powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    if !ERRORLEVEL! neq 0 (
        echo [ERROR] uv installation failed.
        echo   Install manually: https://docs.astral.sh/uv/getting-started/installation/
        echo   Then re-run this script.
        pause
        exit /b 1
    )
    :: uv installer adds %USERPROFILE%\.local\bin to PATH for new shells;
    :: this shell needs it added explicitly so the rest of the script works.
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    where uv >nul 2>&1
    if !ERRORLEVEL! neq 0 (
        echo [ERROR] uv installed but not found on PATH. Open a new terminal and re-run.
        pause
        exit /b 1
    )
)
echo [webAgent] uv is available.

:: ── 2. If existing .venv uses an unsupported Python, remove it so uv rebuilds clean ──
if exist ".venv\Scripts\python.exe" (
    for /f "tokens=2" %%v in ('.venv\Scripts\python.exe --version 2^>^&1') do set "VENV_VER=%%v"
    for /f "tokens=1,2 delims=." %%a in ("!VENV_VER!") do (
        set "VENV_MAJOR=%%a"
        set "VENV_MINOR=%%b"
    )
    set "VENV_OK=0"
    if "!VENV_MAJOR!"=="3" if !VENV_MINOR! geq 11 if !VENV_MINOR! lss 13 set "VENV_OK=1"
    if !VENV_OK! equ 0 (
        echo [webAgent] Existing .venv uses Python !VENV_VER! ^(unsupported^). Removing so uv can rebuild...
        rmdir /S /Q ".venv"
    )
)

:: ── 3. Sync: downloads Python 3.12 if needed, creates .venv, installs deps from pyproject.toml ──
:: --extra encryption installs the SQLCipher engine (sqlcipher3). It is MANDATORY
:: whenever a database is encrypted at rest (app/db_encryption.json) — without it
:: the encrypted files can't be opened and the server cannot boot. Keep this flag
:: so a plain `uv sync` never prunes the engine out from under an encrypted install.
echo [webAgent] Syncing dependencies via uv (may download Python on first run)...
uv sync --extra encryption
if !ERRORLEVEL! neq 0 (
    echo [ERROR] uv sync failed. Check the output above.
    pause
    exit /b 1
)

echo Starting webAgent agent...
echo.

:restart
:: Kill anything listening on port 8080
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8080 " ^| findstr LISTEN') do (
    if not "%%a"=="" (
        echo [webAgent] Killing stale process PID %%a on port 8080...
        taskkill /F /PID %%a 2>nul
    )
)
ping -n 3 127.0.0.1 >nul

:: Run the server
echo [webAgent] Server running. Press Ctrl+C to stop permanently.
echo [webAgent] Use the "Restart" button in the terminal page to restart.
echo.
uv run --extra encryption python run.py

:: Loop on exit
echo.
echo [webAgent] Server stopped. Restarting...
ping -n 3 127.0.0.1 >nul
goto :restart
