@echo off
title WebAgent Preview Stop
cd /d "%~dp0"
chcp 65001 >nul

where python >nul 2>&1
if errorlevel 1 ( set "PY=py -3" ) else ( set "PY=python" )

%PY% preview_commit.py --stop
echo.
pause
