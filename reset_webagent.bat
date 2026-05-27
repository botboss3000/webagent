@echo off
title Reset webAgent
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo ============================================================
echo  webAgent Reset
echo ============================================================
echo This wipes user data from your local webAgent install.
echo The database, default admin user (admin/admin), and agent
echo templates are recreated automatically on next start.
echo.

REM ============================================================
REM  Step 0 / Stop any running webAgent (port 8080 + python)
REM ============================================================
echo ------------------------------------------------------------
echo  Step 0 / Stopping any running webAgent (port 8080 + python)
echo ------------------------------------------------------------
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8080"') do (
    if not "%%a"=="0" if not "%%a"=="" (
        echo   Killing PID %%a on port 8080...
        taskkill /F /PID %%a >nul 2>&1
    )
)
taskkill /F /IM python.exe >nul 2>&1
taskkill /F /IM python3.exe >nul 2>&1
ping -n 2 127.0.0.1 >nul
echo   Done.
echo.

REM ============================================================
REM  Step 1 / Backup mode
REM ============================================================
set "BACKUP_MODE=1"
set "BACKUP_ANS="
set /p BACKUP_ANS=Back up files to temp\reset-backup-^<timestamp^>\ before deleting? [Y/n]:
if /i "!BACKUP_ANS!"=="n"  set "BACKUP_MODE=0"
if /i "!BACKUP_ANS!"=="no" set "BACKUP_MODE=0"

set "BACKUP_DIR="
if "!BACKUP_MODE!"=="1" goto :setupBackup
echo   Hard delete selected (no backup).
goto :afterBackupSetup
:setupBackup
for /f %%a in ('powershell -NoLogo -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "TS=%%a"
set "BACKUP_DIR=temp\reset-backup-!TS!"
echo   Backups will go to: !BACKUP_DIR!\
:afterBackupSetup
echo.

REM ============================================================
REM  Step 2 / Userbase (mandatory)
REM ============================================================
echo Userbase (database + visual pages) WILL be wiped.
echo.

REM ============================================================
REM  Step 3 / Optional: app secrets
REM ============================================================
set "DO_SECRETS=0"
set "ANS="
set /p ANS=Clear app secrets (LLM API keys, OAuth tokens, integration creds, scheduler config)? [y/N]:
if /i "!ANS!"=="y"   set "DO_SECRETS=1"
if /i "!ANS!"=="yes" set "DO_SECRETS=1"

REM ============================================================
REM  Step 4 / Optional: user logins
REM ============================================================
set "DO_USERS=0"
set "ANS="
set /p ANS=Clear local user accounts (passwords, remember-me tokens)? [y/N]:
if /i "!ANS!"=="y"   set "DO_USERS=1"
if /i "!ANS!"=="yes" set "DO_USERS=1"

REM ============================================================
REM  Step 5 / Optional: .env
REM ============================================================
set "DO_ENV=0"
set "ANS="
set /p ANS=Delete .env file? (contains Supabase/OAuth creds - most installs need this) [y/N]:
if /i "!ANS!"=="y"   set "DO_ENV=1"
if /i "!ANS!"=="yes" set "DO_ENV=1"

REM ============================================================
REM  Step 6 / Optional: agent template JSONs
REM ============================================================
set "DO_AGENTS=0"
set "ANS="
set /p ANS=Delete agent template JSON files in app\context\agents\? (no fallback - next start = zero agents) [y/N]:
if /i "!ANS!"=="y"   set "DO_AGENTS=1"
if /i "!ANS!"=="yes" set "DO_AGENTS=1"

REM ============================================================
REM  Step 7 / Summary + confirm
REM ============================================================
echo.
echo ============================================================
echo  Summary - paths that will be processed
echo ============================================================
set "ACTION_TAG=[DELETE]"
if "!BACKUP_MODE!"=="1" set "ACTION_TAG=[BACKUP]"

echo Userbase (always):
call :showFile "app\db\local.db"
call :showFile "app\db\local.db-journal"
call :showFile "app\db\local.db-wal"
call :showFile "app\db\local.db-shm"
call :showFile "app\db\local.db.preprompt-bak"
call :showFile "local.db"
call :showDir  "visuals\users"

if not "!DO_SECRETS!"=="1" goto :sumAfterSecrets
echo App secrets:
call :showFile "provider.json"
call :showFile "app-settings.json"
call :showFile "scheduler_config.json"
call :showFile "app\db_mode.json"
call :showFile "app\pages_mode.json"
call :showFile "app\db_connection.json"
call :showFile "app\secrets_mode.json"
:sumAfterSecrets

if not "!DO_USERS!"=="1" goto :sumAfterUsers
echo User logins:
call :showFile "app\auth\users.json"
call :showFile "app\auth\users.json.bak"
:sumAfterUsers

if not "!DO_ENV!"=="1" goto :sumAfterEnv
echo Env file:
call :showFile ".env"
:sumAfterEnv

if not "!DO_AGENTS!"=="1" goto :sumAfterAgents
echo Agent templates:
for %%F in (app\context\agents\*.json) do call :showFile "%%F"
:sumAfterAgents

echo.
set "PROCEED="
set /p PROCEED=Proceed? [y/N]:
if /i "!PROCEED!"=="y"   goto :doExecute
if /i "!PROCEED!"=="yes" goto :doExecute
echo Aborted. No changes made.
echo.
pause
exit /b 0

:doExecute
echo.
echo ------------------------------------------------------------
echo  Executing...
echo ------------------------------------------------------------
if "!BACKUP_MODE!"=="1" if not exist "!BACKUP_DIR!" mkdir "!BACKUP_DIR!" >nul 2>&1

set /a SUCCESS=0
set /a FAILED=0
set /a SKIPPED=0

echo Userbase:
call :processFile "app\db\local.db"
call :processFile "app\db\local.db-journal"
call :processFile "app\db\local.db-wal"
call :processFile "app\db\local.db-shm"
call :processFile "app\db\local.db.preprompt-bak"
call :processFile "local.db"
call :processDir  "visuals\users"

if not "!DO_SECRETS!"=="1" goto :execAfterSecrets
echo App secrets:
call :processFile "provider.json"
call :processFile "app-settings.json"
call :processFile "scheduler_config.json"
call :processFile "app\db_mode.json"
call :processFile "app\pages_mode.json"
call :processFile "app\db_connection.json"
call :processFile "app\secrets_mode.json"
:execAfterSecrets

if not "!DO_USERS!"=="1" goto :execAfterUsers
echo User logins:
call :processFile "app\auth\users.json"
call :processFile "app\auth\users.json.bak"
:execAfterUsers

if not "!DO_ENV!"=="1" goto :execAfterEnv
echo Env file:
call :processFile ".env"
:execAfterEnv

if not "!DO_AGENTS!"=="1" goto :execAfterAgents
echo Agent templates:
for %%F in (app\context\agents\*.json) do call :processFile "%%F"
:execAfterAgents

echo.
echo ============================================================
echo  Reset complete.   OK=!SUCCESS!   FAIL=!FAILED!   SKIPPED=!SKIPPED!
echo ============================================================
if "!BACKUP_MODE!"=="1" echo Backup at: !BACKUP_DIR!\
echo Start the app with webAgent.bat - the database, default admin
echo user (admin/admin), and agent templates will be recreated
echo automatically (provided agent JSONs were not deleted).
echo.
pause
exit /b 0


REM ============================================================
REM  Helpers
REM ============================================================

:showFile
if exist "%~1" (
    echo   !ACTION_TAG! %~1
) else (
    echo   [skip-missing] %~1
)
goto :eof

:showDir
if exist "%~1\" (
    echo   !ACTION_TAG! %~1\
) else (
    echo   [skip-missing] %~1\
)
goto :eof

:processFile
set "src=%~1"
if not exist "!src!" (
    set /a SKIPPED+=1
    echo   skip   : !src! ^(not present^)
    goto :eof
)
if "!BACKUP_MODE!"=="1" goto :processFileBackup
del /F /Q "!src!" >nul 2>&1
if exist "!src!" (
    set /a FAILED+=1
    echo   FAIL   delete: !src!
) else (
    set /a SUCCESS+=1
    echo   OK     delete: !src!
)
goto :eof
:processFileBackup
set "dst=!BACKUP_DIR!\!src!"
for %%P in ("!dst!") do set "dstdir=%%~dpP"
if not exist "!dstdir!" mkdir "!dstdir!" >nul 2>&1
move /Y "!src!" "!dst!" >nul 2>&1
if errorlevel 1 (
    set /a FAILED+=1
    echo   FAIL   backup: !src!
) else (
    set /a SUCCESS+=1
    echo   OK     backup: !src!
)
goto :eof

:processDir
set "src=%~1"
if not exist "!src!\" (
    set /a SKIPPED+=1
    echo   skip   : !src!\ ^(not present^)
    goto :eof
)
if "!BACKUP_MODE!"=="1" goto :processDirBackup
rmdir /S /Q "!src!" >nul 2>&1
if exist "!src!\" (
    set /a FAILED+=1
    echo   FAIL   delete: !src!\
) else (
    set /a SUCCESS+=1
    echo   OK     delete: !src!\
)
goto :eof
:processDirBackup
set "dst=!BACKUP_DIR!\!src!"
for %%P in ("!dst!") do set "dstdir=%%~dpP"
if not exist "!dstdir!" mkdir "!dstdir!" >nul 2>&1
move /Y "!src!" "!dst!" >nul 2>&1
if errorlevel 1 (
    set /a FAILED+=1
    echo   FAIL   backup: !src!\
) else (
    set /a SUCCESS+=1
    echo   OK     backup: !src!\
)
goto :eof
