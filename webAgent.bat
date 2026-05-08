@echo off
title webAgent
cd /d "%~dp0"

:: Use existing venv or skip if missing
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found.
    echo   Run start_webAgent.bat first to set up dependencies.
    pause
    exit /b 1
)

echo Starting webAgent agent...
echo.

:: ── Kill any stale server on this port ──
:restart
for /f "tokens=5*" %%a in ('netstat -ano ^| findstr ":8080" ^| findstr LISTEN') do (
    if not "%%b"=="" (
        echo [webAgent] Killing stale process PID %%b on port 8080...
        taskkill /F /PID %%b >nul 2>nul
    )
)
ping -n 2 127.0.0.1 >nul

:: Run uvicorn inline
echo [webAgent] Server running. Press Ctrl+C to stop permanently.
echo [webAgent] Use the "Restart" button in the terminal page to restart.
echo.
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --ws wsproto

:: If uvicorn exits (e.g. via /api/v1/restart), restart the loop
echo.
echo [webAgent] Server stopped. Restarting...
ping -n 3 127.0.0.1 >nul
goto :restart
