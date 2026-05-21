@echo off
title Kill webAgent
cd /d "%~dp0"

echo ============================================================
echo  PROCESSES ON PORT 8080 (before kill)
echo ============================================================
netstat -ano | findstr ":8080"
echo.

echo ============================================================
echo  PYTHON PROCESSES (before kill)
echo ============================================================
tasklist | findstr /I "python"
echo.

echo ============================================================
echo  KILLING ALL UNIQUE PIDs ON PORT 8080
echo ============================================================
setlocal enabledelayedexpansion
set killed=

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8080"') do (
    set pid=%%a
    if not "!pid!"=="0" (
        if not "!pid!"=="" (
            echo !killed! | findstr /C:" !pid! " >nul 2>&1
            if errorlevel 1 (
                set killed=!killed! !pid!
                echo Killing PID !pid!...
                taskkill /F /PID !pid!
            )
        )
    )
)
endlocal

echo.
echo ============================================================
echo  KILLING PYTHON PROCESSES BY NAME
echo ============================================================
taskkill /F /IM python.exe
taskkill /F /IM python3.exe

echo.
ping -n 2 127.0.0.1 >nul

echo ============================================================
echo  PORT 8080 CHECK (should be empty)
echo ============================================================
netstat -ano | findstr ":8080" || echo Port 8080 is clear.

echo.
echo ============================================================
echo  REMAINING PYTHON PROCESSES (should be empty)
echo ============================================================
tasklist | findstr /I "python" || echo None.

echo.
pause
