@echo off
REM Export agent_templates from DB back to JSON seed files.
REM Double-click or run from command line.
REM   export_agent_templates.bat             (write files)
REM   export_agent_templates.bat --dry-run   (preview only)

cd /d "%~dp0"
python scripts\export_agent_templates.py %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Export failed.
)
pause
