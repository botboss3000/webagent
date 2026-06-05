@echo off
:: webAgent watchdog — keeps the server (run.py on port 8080) alive,
:: restarting it whenever it dies or stops answering /health.
:: Leave this window open. Press Ctrl+C to stop the watchdog and server.
title webAgent watchdog
cd /d "%~dp0"

:: Prefer the checkout's own venv python; fall back to system python.
if exist ".venv\Scripts\python.exe" (
    set "PYEXE=.venv\Scripts\python.exe"
) else (
    set "PYEXE=python"
)

"%PYEXE%" watchdog.py
echo.
echo [watchdog] exited. Press any key to close.
pause >nul
