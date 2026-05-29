#!/data/data/com.termux/files/usr/bin/bash
set -e

DISTRO="${DISTRO:-ubuntu}"
WEBAGENT_DIR="${WEBAGENT_DIR:-/root/webagent}"
BRIDGE_PROOT="${WEBAGENT_DIR}/launcher_android/.browser-bridge"
PROJECT_DIR_HOST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Initialize proot
if ! command -v proot-distro >/dev/null 2>&1; then pkg install -y proot-distro; fi

# Launch inside proot
proot-distro login "${DISTRO}" --bind "${PROJECT_DIR_HOST}:${WEBAGENT_DIR}" -- bash -c '
    set -x
    set -e
    
    cd "'"${WEBAGENT_DIR}"'"
    
    # 1. Update/Install system-level dependencies (NO venv)
    apt-get update -qq
    apt-get install -y python3 python3-pip
    
    # 2. Force install textual globally
    python3 -m pip install --break-system-packages textual
    
    # 3. Setup and Launch
    export WEBAGENT_PROJECT_DIR="'"${WEBAGENT_DIR}"'"
    export WEBAGENT_BROWSER_BRIDGE="'"${BRIDGE_PROOT}"'"
    cd launcher_android
    # Capture all output and errors to a log file inside the proot
    # We remove "exec" so the script doesn't just replace the process before redirecting
    python3 -m launcher_android > launcher_output.log 2>&1
'
