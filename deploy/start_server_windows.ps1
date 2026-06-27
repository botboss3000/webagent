# ============================================================================
# webAgent - Windows launch + keep-alive loop.
#
# Launches run.py on a Windows PC and restarts it if it ever stops. This is the
# Windows counterpart of deploy/start_server_linux.sh / start_server_termux.sh.
# It is the action the "webAgent" Scheduled Task runs (registered by
# deploy/windows-setup.ps1) - the task runs it hidden at logon, so the server
# runs in the background and comes back after a reboot.
#
# Usage: start_server_windows.ps1 [REPO_DIR]   (defaults to this script's repo)
# run.py itself guarantees a single server on :8080 (it takes over a live one),
# so a stray duplicate loop can never stack two real servers.
# ============================================================================
$ErrorActionPreference = 'Continue'

$RepoDir = if ($args.Count -ge 1 -and $args[0]) { $args[0] } else { Split-Path -Parent $PSScriptRoot }
Set-Location $RepoDir

# uv lives in %USERPROFILE%\.local\bin; make sure the (possibly fresh) logon
# session can find it, then prefer the project's uv-managed environment.
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
$log = Join-Path $RepoDir 'server_log.txt'

while ($true) {
  try {
    & uv run python run.py *>> $log
  } catch {
    "Server error: $($_.Exception.Message)" | Out-File -Append -FilePath $log -Encoding utf8
  }
  "Server stopped - restarting in 5s..." | Out-File -Append -FilePath $log -Encoding utf8
  Start-Sleep -Seconds 5
}
