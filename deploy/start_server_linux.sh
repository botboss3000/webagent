#!/usr/bin/env bash
# ============================================================================
# webAgent — native Linux launch + keep-alive loop.
#
# Launches run.py on a plain Linux box and restarts it if it ever stops.
# Detached with nohup so it survives the terminal closing. This is the Linux
# counterpart of start_server_termux.sh (which does the same inside a Termux
# proot-distro). It is used ONLY on boxes without systemd (or without root) —
# when systemd is available, deploy/termux-setup.sh installs a proper
# webagent.service instead and this script is not used.
#
# Called by deploy/termux-setup.sh on first install, and by the @reboot crontab
# entry that script adds (so the server also comes back after a reboot).
#
# Usage: start_server_linux.sh [REPO_DIR]      (defaults to $HOME/webagent)
# ============================================================================
REPO_DIR="${1:-$HOME/webagent}"
cd "$REPO_DIR" || exit 1

# nohup-detached so it outlives the terminal; the inner loop relaunches run.py
# (run.py itself guarantees a single server on :8080, so a stray duplicate loop
# can't stack two real servers).
nohup bash -c '
cd "'"$REPO_DIR"'"
source .venv/bin/activate
while true; do
  python run.py >> server_log.txt 2>&1 || true
  echo "Server stopped — restarting in 5s…" >> server_log.txt
  sleep 5
done
' >/dev/null 2>&1 &
echo "Launcher PID: $!"
echo $! > /tmp/webagent_launcher.pid
