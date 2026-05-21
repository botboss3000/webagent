@echo off
title Kill webAgent
cd /d "%~dp0"

echo ============================================================
echo  PROCESSES ON PORT 8080
echo ============================================================
netstat -ano | findstr ":8080"
echo.

echo ============================================================
echo  PYTHON PROCESSES
echo ============================================================
tasklist | findstr /I "python"
echo.

echo ============================================================
echo  KILLING PORT 8080 PROCESSES
echo ============================================================
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8080 "') do (
    if not "%%a"=="" (
        echo Killing PID %%a (port 8080)...
        taskkill /F /PID %%a 2>nul
    )
)

echo.
echo ============================================================
echo  KILLING PYTHON PROCESSES
echo ============================================================
taskkill /F /IM python.exe 2>nul && echo Killed python.exe || echo No python.exe found.
taskkill /F /IM python3.exe 2>nul && echo Killed python3.exe || echo No python3.exe found.

echo.
echo ============================================================
echo  REMAINING PYTHON PROCESSES (should be empty)
echo ============================================================
tasklist | findstr /I "python" || echo None.

echo.
pause
