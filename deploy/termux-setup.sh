#!/usr/bin/env bash
# ============================================================================
# webAgent — Linux / Termux installer & launcher.
#
# Run this on the target machine. It is what the Deploy panel's one-line command
# hands off to after cloning the repo to $HOME/webagent. It works in TWO places
# and detects which one automatically:
#
#   * Termux (Android phone / tablet) — webAgent's full Python stack (compiled
#     wheels, optional SQLCipher) is unreliable on bare Termux, so we install it
#     inside a real Ubuntu userland via proot-distro (no root). This Ubuntu
#     sandbox is the Termux-specific "customization"; everything else is shared.
#   * A plain Linux computer / server — the dependencies build natively, so we
#     install straight onto the system (system package manager + a venv), with
#     no proot wrapper.
#
# Idempotent: re-running updates the code, reuses the existing distro / venv, and
# relaunches. Browser automation (Playwright) is intentionally omitted on both —
# see req_no_playwright.txt — to keep this lightweight install reliable.
# ============================================================================
set -e

REPO_DIR="$HOME/webagent"
PORT=8080

echo "============================================"
echo " webAgent — Linux / Termux setup"
echo "============================================"

# The one-line command clones the repo first; make sure it's really there. ----
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "ERROR: webAgent code not found at $REPO_DIR." >&2
  echo "       Clone it first, then re-run this script." >&2
  exit 1
fi
echo "Updating webAgent code…"
git -C "$REPO_DIR" pull --ff-only || echo "  (could not fast-forward — keeping the current code)"

# Detect Termux (the customization) vs a plain Linux box. ---------------------
if [ -n "$TERMUX_VERSION" ] || [ -d /data/data/com.termux ] || command -v termux-setup-storage >/dev/null 2>&1; then
  IS_TERMUX=1
else
  IS_TERMUX=0
fi

if [ "$IS_TERMUX" = 1 ]; then
  # ── Termux path: build + run inside an Ubuntu proot-distro sandbox ──
  DISTRO="ubuntu"
  ROOTFS="$PREFIX/var/lib/proot-distro/installed-rootfs/$DISTRO"

  echo "[Termux 1/4] Installing Termux packages (git, proot-distro)…"
  pkg update -y
  pkg install -y git proot-distro

  echo "[Termux 2/4] Keeping the device awake (wake-lock)…"
  termux-wake-lock || echo "  (wake-lock unavailable — the server may pause when the screen is off)"

  if [ -d "$ROOTFS" ]; then
    echo "[Termux 3/4] Ubuntu environment already installed."
  else
    echo "[Termux 3/4] Installing the Ubuntu environment (first time only — a few minutes)…"
    proot-distro install "$DISTRO"
  fi

  echo "[Termux 4/4] Installing Python dependencies inside Ubuntu (first run is slow)…"
  proot-distro login "$DISTRO" --bind "$REPO_DIR:/root/webagent" -- bash -c '
set -e
cd /root/webagent
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip git build-essential libffi-dev
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --upgrade pip wheel
# Playwright-free dependency set — browser automation is omitted on phones.
.venv/bin/pip install -r req_no_playwright.txt
echo "  dependencies ready."
'

  echo "Starting the server in the background…"
  bash "$REPO_DIR/start_server_termux.sh"
else
  # ── Plain Linux path: install natively, no proot ──
  SUDO=
  [ "$(id -u 2>/dev/null)" = 0 ] || SUDO=sudo

  echo "[Linux 1/3] Installing system packages (python3, venv, pip, git, build tools)…"
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update
    $SUDO apt-get install -y python3 python3-venv python3-pip git build-essential libffi-dev
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y python3 python3-pip python3-virtualenv git gcc make libffi-devel
  elif command -v pacman >/dev/null 2>&1; then
    $SUDO pacman -Sy --noconfirm python python-pip git base-devel libffi
  else
    echo "  (couldn't find apt/dnf/pacman — install python3, pip and git yourself, then re-run)" >&2
  fi

  echo "[Linux 2/3] Building the Python virtual environment…"
  cd "$REPO_DIR"
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install --upgrade pip wheel
  # Playwright-free dependency set — same lightweight install as the phone.
  .venv/bin/pip install -r req_no_playwright.txt

  echo "[Linux 3/3] Starting the server in the background…"
  # Keep-alive loop: launch run.py and restart it if it ever stops. Detached with
  # nohup so it survives the terminal closing.
  nohup bash -c '
cd "'"$REPO_DIR"'"
source .venv/bin/activate
while true; do
  python run.py >> server_log.txt 2>&1 || true
  echo "Server stopped — restarting in 5s…" >> server_log.txt
  sleep 5
done
' >/dev/null 2>&1 &
  echo "  launcher PID: $!"
fi

# Tell the user where to reach it. --------------------------------------------
IP="$( (ip -4 addr 2>/dev/null || ifconfig 2>/dev/null) | grep -Eo '([0-9]{1,3}\.){3}[0-9]{1,3}' | grep -v '^127\.' | head -n1)"
echo ""
echo "============================================"
echo " webAgent is starting in the background."
echo "   On this machine:     http://localhost:$PORT"
if [ -n "$IP" ]; then
  echo "   From another device: http://$IP:$PORT   (same network)"
fi
echo " It keeps running and restarts itself if it stops."
if [ "$IS_TERMUX" = 1 ]; then
  echo " To stop it:  proot-distro login ubuntu -- pkill -f run.py"
else
  echo " To stop it:  pkill -f run.py"
fi
echo "============================================"
