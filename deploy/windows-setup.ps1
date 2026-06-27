# ============================================================================
# webAgent - Windows installer & launcher.
#
# Run on the target PC. This is what the Deploy panel's one-line PowerShell
# command hands off to after cloning the repo to %USERPROFILE%\webagent. It:
#
#   1. installs uv (Astral's Python toolchain) if missing,
#   2. uv-syncs the dependencies (downloads Python 3.12 on first run),
#   3. installs the lightweight Server Manager (the `webagent` command) FIRST,
#      on its own venv, so a failure in the heavier server build still leaves a
#      working manager to inspect / retry / diagnose (mirrors termux-setup.sh),
#   4. registers a Scheduled Task "webAgent" that runs the keep-alive launcher
#      (deploy/start_server_windows.ps1) at logon - so the server runs in the
#      background, restarts itself if it stops, and survives a reboot - and
#      starts it now.
#
# Idempotent: re-running updates the code, reuses the venv, and re-registers the
# task. Browser automation (Playwright) is intentionally omitted (uv sync of the
# project deps); the server install is the lightweight, reliable set.
# ============================================================================
$ErrorActionPreference = 'Stop'

$RepoDir = Split-Path -Parent $PSScriptRoot   # deploy\ -> repo root
$Port = 8080
$LocalBin = Join-Path $env:USERPROFILE '.local\bin'

Write-Host "============================================"
Write-Host " webAgent - Windows setup"
Write-Host "============================================"
Set-Location $RepoDir

# Update the code best-effort (the one-liner cloned it; a re-run refreshes it). --
try { & git -C $RepoDir pull --ff-only 2>$null } catch { Write-Host "  (could not update - keeping the current code)" }

# -- 1. Ensure uv -----------------------------------------------------------
function Test-Cmd($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

if (-not (Test-Cmd 'uv')) {
  Write-Host "Installing uv (Python toolchain)..."
  try { powershell -ExecutionPolicy Bypass -NoProfile -Command "irm https://astral.sh/uv/install.ps1 | iex" } catch {}
  # The installer adds %USERPROFILE%\.local\bin to PATH for NEW shells; add it to
  # THIS session so the rest of the script (and uv) is found right away.
  $env:Path = "$LocalBin;$env:Path"
}
$Uv = if (Test-Cmd 'uv') { 'uv' } else { Join-Path $LocalBin 'uv.exe' }
if (-not (Test-Path $Uv) -and -not (Test-Cmd 'uv')) {
  Write-Host "[ERROR] uv could not be installed. Install it from https://docs.astral.sh/uv/ then re-run."
  exit 1
}

# -- 2. Sync dependencies (downloads Python 3.12 if needed) -----------------
Write-Host "Installing dependencies (first run downloads Python - a few minutes)..."
& $Uv sync
if ($LASTEXITCODE -ne 0) { Write-Host "[ERROR] uv sync failed - see the output above."; exit 1 }

# -- 3. Server Manager (the `webagent` command) - FIRST, best-effort --------
# A dedicated venv + a webagent.cmd launcher on PATH. Non-fatal: a hiccup here
# must not stop the server setup below. WEBAGENT_TUI_NO_SUPERVISE keeps it in
# observe/restart-only mode so it never spins up a SECOND server.
Write-Host "Installing the Server Manager (the 'webagent' command)..."
try {
  $TuiDir = Join-Path $RepoDir 'TUI'
  $TuiPy = Join-Path $TuiDir '.venv\Scripts\python.exe'
  if (-not (Test-Path $TuiPy)) {
    & py -3.12 -m venv "$TuiDir\.venv" 2>$null
    if (-not (Test-Path $TuiPy)) { & py -3.11 -m venv "$TuiDir\.venv" 2>$null }
    if (-not (Test-Path $TuiPy)) { & python -m venv "$TuiDir\.venv" }
  }
  & $TuiPy -m pip install --upgrade pip wheel | Out-Null
  & $TuiPy -m pip install -e "$TuiDir" | Out-Null
  New-Item -ItemType Directory -Force -Path $LocalBin | Out-Null
  $launcher = @"
@echo off
set "WEBAGENT_PROJECT=$RepoDir"
set "WEBAGENT_TUI_NO_SUPERVISE=1"
"$TuiPy" -m tui_app %*
"@
  Set-Content -Path (Join-Path $LocalBin 'webagent.cmd') -Value $launcher -Encoding ascii
  # Make sure .local\bin is on the USER PATH so `webagent` works in new terminals.
  $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
  if ($userPath -notlike "*$LocalBin*") {
    [Environment]::SetEnvironmentVariable('Path', "$LocalBin;$userPath", 'User')
  }
  Write-Host "  Server Manager installed - open a new terminal and run 'webagent' to manage it."
} catch {
  Write-Host "  (Server Manager install skipped - the server setup will still continue.)"
}

# -- 4. Background service: a Scheduled Task at logon ------------------------
# Runs the keep-alive launcher hidden, restarts on failure, starts at logon (so
# it survives a reboot). No admin rights needed - it runs as the current user.
Write-Host "Setting up webAgent to run in the background and start when you log in..."
$TaskName = 'webAgent'
$LoopScript = Join-Path $RepoDir 'deploy\start_server_windows.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$LoopScript`" `"$RepoDir`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
try {
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description 'webAgent server (keep-alive)' -Force | Out-Null
  Start-ScheduledTask -TaskName $TaskName
  $svcOk = $true
} catch {
  Write-Host "  (could not register the Scheduled Task - starting it once in this window instead.)"
  Start-Process powershell -ArgumentList "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$LoopScript`" `"$RepoDir`""
  $svcOk = $false
}

# -- Tell the user where to reach it ----------------------------------------
$ip = $null
try {
  $ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
    Select-Object -First 1 -ExpandProperty IPAddress)
} catch {}

Write-Host ""
Write-Host "============================================"
Write-Host " webAgent is starting in the background."
Write-Host "   On this PC:           http://localhost:$Port"
if ($ip) { Write-Host "   From another device:  http://${ip}:$Port   (same network)" }
if ($svcOk) {
  Write-Host " It keeps running, restarts itself if it stops, and starts when you log in."
  Write-Host " To stop it:   Stop-ScheduledTask -TaskName webAgent"
} else {
  Write-Host " It is running in this session. Re-run this command to set up auto-start."
}
Write-Host " Manage it any time with:  webagent   (open a NEW terminal first)"
Write-Host "============================================"
