@echo off
title Kill webAgent
cd /d "%~dp0"

REM ── Pause at the very start so you can see output ──
echo ============================================================
echo  Kill webAgent — starting up
echo  If this window closes immediately, there's a script crash.
echo ============================================================
echo.
pause
echo.

REM ── Admin detection ─────────────────────────────────────────
net session >nul 2>&1
set "_IS_ADMIN=%errorlevel%"

if not "%_IS_ADMIN%"=="0" (
    echo ============================================================
    echo  NOT running as administrator.
    echo.
    echo  taskkill returns "Access is denied" for sandboxed processes.
    echo ============================================================
    choice /C YN /M "Re-launch as administrator"
    if not errorlevel 2 (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
        exit /b
    )
    echo.
)

:run_kills

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
echo  KILLING PORT 8080 PROCESSES
echo ============================================================
setlocal enabledelayedexpansion
set "killed= "
set /a kill_count=0
set /a fail_count=0

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8080"') do (
    set "pid=%%a"
    if not "!pid!"=="0" if not "!pid!"=="" (
        echo !killed! | findstr /C:" !pid! " >nul 2>&1
        if errorlevel 1 (
            set "killed=!killed!!pid! "
            echo Killing PID !pid!...
            taskkill /F /PID !pid! >nul 2>&1
            if errorlevel 1 (
                echo     ^>^>^> FAILED to kill PID !pid!
                set /a fail_count+=1
            ) else (
                set /a kill_count+=1
            )
        )
    )
)
endlocal

echo.
echo ============================================================
echo  KILLING PYTHON PROCESSES BY NAME
echo ============================================================
tasklist /FI "IMAGENAME eq python.exe" 2>nul | findstr /I "python.exe" >nul
if not errorlevel 1 (
    echo Killing python.exe...
    taskkill /F /IM python.exe >nul 2>&1
) else (
    echo No python.exe found.
)
tasklist /FI "IMAGENAME eq python3.exe" 2>nul | findstr /I "python3.exe" >nul
if not errorlevel 1 (
    echo Killing python3.exe...
    taskkill /F /IM python3.exe >nul 2>&1
)
echo.

ping -n 2 127.0.0.1 >nul

echo ============================================================
echo  PORT 8080 CHECK
echo ============================================================
netstat -ano | findstr ":8080" || echo Port 8080 is clear.
echo.

echo ============================================================
echo  REMAINING PYTHON PROCESSES
echo ============================================================
tasklist | findstr /I "python" || echo None.
echo.

echo ============================================================
echo  DONE
echo ============================================================
echo.
REM Counts were lost after endlocal above, but output is shown.

REM ── Final pause: this window will NOT close until you press a key ──
echo Press any key to close this window...
pause >nul
