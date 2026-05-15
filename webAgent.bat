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
