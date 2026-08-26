@echo off
title WebAgent Commit Preview
cd /d "%~dp0"
chcp 65001 >nul

where python >nul 2>&1
if errorlevel 1 ( set "PY=py -3" ) else ( set "PY=python" )

%PY% preview_commit.py %*
if errorlevel 1 (
    echo.
    echo [preview] Exited with an error — see message above.
    pause
)
