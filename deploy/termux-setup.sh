#!/data/data/com.termux/files/usr/bin/bash
# ============================================================================
# webAgent — Termux installer / launcher (Android phones & tablets).
#
# Run this INSIDE the Termux app. It is what the Deploy panel's one-line command
# hands off to after cloning the repo to $HOME/webagent.
#
# Why an Ubuntu proot, not native Termux: webAgent's full Python stack (compiled
# wheels, optional SQLCipher, etc.) is unreliable on bare Termux, but installs
# cleanly inside a real Ubuntu userland. proot-distro gives us that userland with
# no root. The repo is cloned on the Termux side and bind-mounted into the distro
# so the code + venv live in one place ($HOME/webagent).
#
# It is idempotent: re-running updates the code, reuses the existing distro/venv,
# and relaunches. Browser automation (Playwright) is intentionally omitted on
# phones — see req_no_playwright.txt.
# ============================================================================
set -e

REPO_DIR="$HOME/webagent"
DISTRO="ubuntu"
PORT=8080
ROOTFS="$PREFIX/var/lib/proot-distro/installed-rootfs/$DISTRO"

echo "============================================"
echo " webAgent — Termux setup"
echo "============================================"

# 1. Termux packages -------------------------------------------------------
echo "[1/5] Installing Termux packages (git, proot-distro)…"
pkg update -y
pkg install -y git proot-distro

# 2. Wake-lock so Android doesn't freeze the background server -------------
echo "[2/5] Keeping the device awake (wake-lock)…"
termux-wake-lock || echo "  (wake-lock unavailable — the server may pause when the screen is off)"

# 3. Get / update the code (Termux side; bind-mounted into the distro) -----
if [ -d "$REPO_DIR/.git" ]; then
  echo "[3/5] Updating webAgent code…"
  git -C "$REPO_DIR" pull --ff-only || echo "  (could not fast-forward — keeping the current code)"
else
  echo "[3/5] ERROR: webAgent code not found at $REPO_DIR." >&2
  echo "       Clone it first, then re-run this script." >&2
  exit 1
fi

# 4. Ensure the Ubuntu environment exists ----------------------------------
if [ -d "$ROOTFS" ]; then
  echo "[4/5] Ubuntu environment already installed."
else
  echo "[4/5] Installing the Ubuntu environment (first time only — a few minutes)…"
  proot-distro install "$DISTRO"
fi

# 5. Build the venv + install deps INSIDE Ubuntu ---------------------------
echo "[5/5] Installing Python dependencies inside Ubuntu (first run is slow)…"
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

# Launch + keep alive (restarts the server if it ever stops) ---------------
echo "Starting the server in the background…"
bash "$REPO_DIR/start_server_termux.sh"

# Tell the user where to reach it ------------------------------------------
IP="$(ifconfig 2>/dev/null | grep -Eo '([0-9]{1,3}\.){3}[0-9]{1,3}' | grep -v '^127\.' | head -n1)"
echo ""
echo "============================================"
echo " webAgent is starting in the background."
echo "   On this phone:       http://localhost:$PORT"
if [ -n "$IP" ]; then
  echo "   From another device: http://$IP:$PORT   (same Wi-Fi)"
fi
echo " It keeps running and restarts itself if it stops."
echo " To stop it:  proot-distro login $DISTRO -- pkill -f run.py"
echo "============================================"
