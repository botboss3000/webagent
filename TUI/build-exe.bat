@echo off
title webAgent Server Manager - Build .exe
cd /d "%~dp0"
setlocal enabledelayedexpansion

:: One-click: build the portable webagent.exe (the Server Manager TUI) from
:: source. Output: webagent.exe in THIS folder (TUI\).
:: A RUNNING webagent.exe locks the output file, so close any open one first.

:: -- Ensure uv is installed (Astral's Python + venv manager) --
where uv >nul 2>&1
if !ERRORLEVEL! neq 0 (
    echo [build] uv not found. Installing via PowerShell...
    powershell -ExecutionPolicy ByPass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex"
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    where uv >nul 2>&1
    if !ERRORLEVEL! neq 0 (
        echo [ERROR] uv installed but not found on PATH. Open a new terminal and re-run.
        pause
        exit /b 1
    )
)

:: -- Install the build extra (PyInstaller) --
echo [build] syncing build dependencies ^(uv sync --extra build^)...
uv sync --extra build
if !ERRORLEVEL! neq 0 (
    echo [ERROR] uv sync --extra build failed. See the output above.
    pause
    exit /b 1
)

:: -- Build the single-file exe --
echo [build] building webagent.exe ^(this can take a few minutes^)...
uv run python scripts/build_exe.py
set "EXITCODE=!ERRORLEVEL!"

echo.
if "!EXITCODE!"=="0" (
    echo [build] Done. The Server Manager exe is here:
    echo         "%~dp0webagent.exe"
) else (
    echo [build] Build FAILED with code !EXITCODE!. See the messages above.
    echo [build] Tip: if it says the exe is in use, close any running webagent window and retry.
)
echo.
pause
endlocal
