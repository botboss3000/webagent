@echo off
title Kill webAgent
cd /d "%~dp0"

REM ── Admin detection ─────────────────────────────────────────────────
net session >nul 2>&1
if %errorlevel% equ 0 (
    set "_IS_ADMIN=1"
) else (
    set "_IS_ADMIN=0"
)

if "%_IS_ADMIN%"=="0" (
    echo ============================================================
    echo  NOT running as administrator.
    echo.
    echo  webAgent processes spawned by sandboxed shells
    echo  (Claude Code, Cowork, etc.) cannot be killed without
    echo  elevation -- taskkill returns "Access is denied" silently
    echo  and the port stays bound.
    echo ============================================================
    choice /C YN /M "Re-launch this script as administrator"
    if errorlevel 2 goto :run_kills
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
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
echo  KILLING ALL UNIQUE PIDs ON PORT 8080
echo ============================================================
setlocal enabledelayedexpansion
set "killed= "
set /a kill_count=0
set /a fail_count=0

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8080"') do (
    set "pid=%%a"
    if not "!pid!"=="0" (
        if not "!pid!"=="" (
            echo !killed! | findstr /C:" !pid! " >nul 2>&1
            if errorlevel 1 (
                set "killed=!killed!!pid! "
                echo Killing PID !pid!...
                taskkill /F /PID !pid!
                if errorlevel 1 (
                    echo     ^>^>^> FAILED to kill PID !pid!
                    set /a fail_count+=1
                ) else (
                    set /a kill_count+=1
                )
            )
        )
    )
)

echo.
echo ============================================================
echo  KILLING PYTHON PROCESSES BY NAME
echo ============================================================
tasklist /FI "IMAGENAME eq python.exe" 2>nul | findstr /I "python.exe" >nul
if not errorlevel 1 (
    taskkill /F /IM python.exe
    if errorlevel 1 (
        echo     ^>^>^> taskkill /IM python.exe FAILED
        set /a fail_count+=1
    )
) else (
    echo No python.exe processes running.
)

tasklist /FI "IMAGENAME eq python3.exe" 2>nul | findstr /I "python3.exe" >nul
if not errorlevel 1 (
    taskkill /F /IM python3.exe
    if errorlevel 1 (
        echo     ^>^>^> taskkill /IM python3.exe FAILED
        set /a fail_count+=1
    )
)

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
echo ============================================================
echo  SUMMARY
echo ============================================================
echo Killed:        !kill_count!
echo Kill failures: !fail_count!

if !fail_count! gtr 0 (
    echo.
    if "%_IS_ADMIN%"=="0" (
        echo Some kills failed. Re-run this script as administrator
        echo ^(right-click kill_webagent.bat -^> "Run as administrator"^).
    ) else (
        echo Some kills failed even with admin rights. Possible causes:
        echo   - Process is inside a Windows Job Object you cannot break
        echo   - Try Sysinternals Process Explorer -^> Kill Process Tree
        echo   - As a last resort, reboot the machine.
    )
)

endlocal

echo.
pause
