@echo off
title webAgent
cd /d "%~dp0"
setlocal enabledelayedexpansion

:: ── 1. Detect Python on PATH ──
set "PYTHON_CMD="
where python >nul 2>&1
if !ERRORLEVEL! equ 0 (
    set "PYTHON_CMD=python"
) else (
    where py >nul 2>&1
    if !ERRORLEVEL! equ 0 set "PYTHON_CMD=py -3"
)
if "!PYTHON_CMD!"=="" (
    echo [ERROR] Python not found on PATH.
    echo   Install Python 3.11 or 3.12 from https://www.python.org/downloads/
    echo   Tick "Add Python to PATH" during install, then re-run this script.
    pause
    exit /b 1
)

:: ── 2. Enforce tested Python version range (3.11.x or 3.12.x) ──
for /f "tokens=2" %%v in ('!PYTHON_CMD! --version 2^>^&1') do set "PY_VER=%%v"
for /f "tokens=1,2 delims=." %%a in ("!PY_VER!") do (
    set "PY_MAJOR=%%a"
    set "PY_MINOR=%%b"
)
set "PY_OK=0"
if "!PY_MAJOR!"=="3" (
    if !PY_MINOR! geq 11 if !PY_MINOR! lss 13 set "PY_OK=1"
)
if !PY_OK! equ 0 (
    echo [ERROR] Python !PY_VER! is not supported.
    echo   webAgent is tested on Python 3.11 and 3.12.
    echo   Newer versions may drop stdlib modules or lack wheels for pinned deps.
    echo   Download a supported version from https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [webAgent] Python !PY_VER! detected.

:: ── 3. Create venv if missing ──
if not exist ".venv\Scripts\python.exe" (
    echo [webAgent] Creating virtual environment in .venv ...
    !PYTHON_CMD! -m venv .venv
    if !ERRORLEVEL! neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

:: ── 4. Install/update requirements if drifted (hash of requirements.txt) ──
.venv\Scripts\python.exe -c "import hashlib,os,sys; p='requirements.txt'; h=hashlib.md5(open(p,'rb').read()).hexdigest(); s=open('.venv/.req-stamp').read().strip() if os.path.exists('.venv/.req-stamp') else ''; sys.exit(0 if h==s else 1)" 2>nul
if !ERRORLEVEL! neq 0 (
    echo [webAgent] Installing/updating dependencies...
    .venv\Scripts\python.exe -m pip install --upgrade pip
    if !ERRORLEVEL! neq 0 (
        echo [ERROR] Failed to upgrade pip.
        pause
        exit /b 1
    )
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if !ERRORLEVEL! neq 0 (
        echo [ERROR] Failed to install dependencies from requirements.txt.
        pause
        exit /b 1
    )
    .venv\Scripts\python.exe -c "import hashlib; open('.venv/.req-stamp','w').write(hashlib.md5(open('requirements.txt','rb').read()).hexdigest())"
    echo [webAgent] Dependencies installed.
) else (
    echo [webAgent] Dependencies up to date.
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
.venv\Scripts\python.exe run.py

:: Loop on exit
echo.
echo [webAgent] Server stopped. Restarting...
ping -n 3 127.0.0.1 >nul
goto :restart
