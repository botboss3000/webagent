@echo off
title webAgent
setlocal enabledelayedexpansion

echo =========================================
echo   webAgent - Starting Agent + Web Portal
echo =========================================
echo.

:: ── Paths ──
set "AGENT_DIR=%~dp0"
set "WEB_PORTAL_DIR=%~dp0..\Web Portal"

:: Remove trailing backslash from AGENT_DIR for clean cd
if "%AGENT_DIR:~-1%"=="\" set "AGENT_DIR=%AGENT_DIR:~0,-1%"

:: Resolve Web Portal path to absolute
for %%i in ("%WEB_PORTAL_DIR%") do set "WEB_PORTAL_DIR=%%~fi"

:: Always start from agent directory
cd /d "%AGENT_DIR%"

:: Ports
if not defined WEBAGENT_PORT set WEBAGENT_PORT=8765
if not defined WEB_PORTAL_PORT set WEB_PORTAL_PORT=3000

echo   Agent API  : http://localhost:%WEBAGENT_PORT%
echo   Web Portal : http://localhost:%WEB_PORTAL_PORT%
echo   Agent dir  : %AGENT_DIR%
echo   Portal dir : %WEB_PORTAL_DIR%
echo.
echo   Web Portal .env.local should have:
echo     PYTHON_AGENT_INTERNAL_URL=http://127.0.0.1:%WEBAGENT_PORT%
echo.

:: ═══════════════════════════════════════════════════════════
::  STEP 1 - Kill stale processes
:: ═══════════════════════════════════════════════════════════

echo [STEP 1/6] Cleaning up stale processes...

:: Kill stale Python agent on agent port
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%WEBAGENT_PORT% " ^| findstr LISTEN') do (
    echo   Killing stale process PID %%a on port %WEBAGENT_PORT%...
    taskkill /F /PID %%a >nul 2>nul
)

:: Kill stale Next.js dev server on web portal port
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%WEB_PORTAL_PORT% " ^| findstr LISTEN') do (
    echo   Killing stale process PID %%a on port %WEB_PORTAL_PORT%...
    taskkill /F /PID %%a >nul 2>nul
)

ping -n 3 127.0.0.1 >nul
echo   Done.
echo.

:: ═══════════════════════════════════════════════════════════
::  STEP 2 - Check prerequisites
:: ═══════════════════════════════════════════════════════════

echo [STEP 2/6] Checking prerequisites...

where python
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python not found. Install Python 3.10+ from python.org
    pause
    exit /b 1
)
echo   [OK] Python

echo.
echo   Checking Node.js...
where node
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Node.js not found. Install Node.js 18+ from nodejs.org
    pause
    exit /b 1
)
echo   [OK] Node.js

echo.
echo   Checking npm...
where npm
if %ERRORLEVEL% NEQ 0 (
    echo [WARNING] npm not found in PATH - Web Portal may not start.
    echo   You can install Node.js from https://nodejs.org
    echo   or skip the Web Portal and use the terminal directly.
    echo.
    echo   Continuing without Web Portal...
    set "WEB_PORTAL_SKIP=1"
) else (
    echo   [OK] npm
)
echo.

:: ═══════════════════════════════════════════════════════════
::  STEP 3 - Set up Python virtual environment
:: ═══════════════════════════════════════════════════════════

echo [STEP 3/6] Setting up Python virtual environment...

if not exist ".venv\Scripts\python.exe" (
    echo   Creating virtual environment...
    python -m venv .venv
    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] Failed to create venv
        pause
        exit /b 1
    )
) else if not exist ".venv\pyvenv.cfg" (
    echo   Recreating broken virtual environment...
    rmdir /s /q .venv
    python -m venv .venv
    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] Failed to create venv
        pause
        exit /b 1
    )
)

echo   Installing Python dependencies...
echo.
echo   --- pip upgrade ---
.venv\Scripts\python.exe -m pip install --upgrade pip
if %ERRORLEVEL% NEQ 0 (
    echo   [ERROR] pip upgrade failed. See output above.
    pause
    exit /b 1
)
echo.
echo   --- requirements.txt ---
.venv\Scripts\python.exe -m pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo   [ERROR] requirements install failed. See output above.
    pause
    exit /b 1
)
echo.
echo   --- pywinpty ---
.venv\Scripts\python.exe -m pip install pywinpty
if %ERRORLEVEL% NEQ 0 (
    echo   [WARN] pywinpty install had issues (terminal may not work)
)
echo.
echo   Done.
echo.

:: ═══════════════════════════════════════════════════════════
::  STEP 4 - Check .env for Python agent
:: ═══════════════════════════════════════════════════════════

echo [STEP 4/6] Checking Python agent .env file...

if not exist ".env" (
    echo [WARNING] No .env file found in %AGENT_DIR%!
    echo.
    echo   Copy .env.example to .env and fill in:
    echo     SUPABASE_URL
    echo     SUPABASE_SERVICE_ROLE_KEY
    echo     OPENROUTER_API_KEY
    echo.
    copy .env.example .env >nul
    echo   A blank .env was created from .env.example.
    echo   Edit it with your credentials, then run this again.
    echo.
    pause
    exit /b 1
)
echo   [OK] .env found
echo.

:: ═══════════════════════════════════════════════════════════
::  STEP 5 - Check / install npm dependencies
:: ═══════════════════════════════════════════════════════════

echo [STEP 5/6] Checking Web Portal...

if defined WEB_PORTAL_SKIP goto :step5_skip

if exist "%WEB_PORTAL_DIR%\node_modules" (
    echo   [OK] node_modules found, skipping npm install.
    goto :step5_done
)

echo   Installing npm dependencies (first run only, may take a minute)...
pushd "%WEB_PORTAL_DIR%"
npm install
set "NPM_EXIT=%ERRORLEVEL%"
popd

if %NPM_EXIT% NEQ 0 (
    echo [ERROR] npm install failed. Check package.json in Web Portal folder.
    pause
    exit /b 1
)

echo   npm install complete.
:step5_done
echo.
goto :step6

:step5_skip
echo   [SKIP] npm not available - skipping Web Portal setup.
echo.

:step6

:: ═══════════════════════════════════════════════════════════
::  STEP 6 - Start both servers
:: ═══════════════════════════════════════════════════════════

echo [STEP 6/6] Starting servers...
echo.

:: Start Web Portal (if not skipped)
if defined WEB_PORTAL_SKIP goto :step6_skip_portal

echo   Starting Web Portal (http://localhost:%WEB_PORTAL_PORT%)...
start "webAgent - Web Portal" /D "%WEB_PORTAL_DIR%" cmd /c "title webAgent - Web Portal && echo. && echo  Web Portal dev server starting on http://localhost:%WEB_PORTAL_PORT% && echo  Close this window to stop the Web Portal. && echo. && npm run dev"

:: Wait a few seconds for Web Portal to start binding
ping -n 5 127.0.0.1 >nul

echo   Opening web portal in browser...
start "" http://localhost:%WEB_PORTAL_PORT%

goto :step6_continue

:step6_skip_portal
echo   [SKIP] Skipping Web Portal start (npm not found).

:step6_continue

:: Open agent terminal in browser
echo   Opening agent terminal in browser...
start "" http://localhost:%WEBAGENT_PORT%/ui/

echo.
echo =========================================
if defined WEB_PORTAL_SKIP (
    echo   Agent API  : http://localhost:%WEBAGENT_PORT% (terminal only)
) else (
    echo   Agent API  : http://localhost:%WEBAGENT_PORT%
    echo   Web Portal : http://localhost:%WEB_PORTAL_PORT%
)
echo.
echo   Ctrl+C in THIS window to stop the Agent.
if not defined WEB_PORTAL_SKIP (
    echo   Close the "webAgent - Web Portal" window to stop the frontend.
)
echo =========================================
echo.

:: Launch the Python agent (blocking)
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port %WEBAGENT_PORT%

:: If we get here, agent stopped
echo.
echo Agent server stopped.
echo The Web Portal window may still be open - close it manually.
pause
