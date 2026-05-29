#!/data/data/com.termux/files/usr/bin/bash
# webAgent Android launcher — Termux entry point.
#
# Run from Termux (NOT from inside the proot):
#     bash ~/webagent/launcher_android/start.sh
#
# What it does:
#   1. Ensures proot-distro + Ubuntu are installed (offers to install if not).
#   2. Ensures `textual` is installed inside the Ubuntu proot.
#   3. Tails a "browser bridge" file in the proot's /tmp — when the launcher
#      writes a URL there (user pressed "Browser"), this script forwards it
#      to termux-open-url so Android's browser actually opens.
#   4. Drops into proot-distro Ubuntu and runs `python -m launcher_android`.
#
# Env overrides:
#   DISTRO          — proot-distro name (default: ubuntu)
#   WEBAGENT_DIR    — webagent path inside the proot (default: ~/webagent)
#   BROWSER_BRIDGE  — bridge file path inside the proot (default: /tmp/webagent_open.url)

set -u

DISTRO="${DISTRO:-ubuntu}"
WEBAGENT_DIR="${WEBAGENT_DIR:-/root/webagent}"
BROWSER_BRIDGE="${BROWSER_BRIDGE:-/tmp/webagent_open.url}"

c_red='\033[1;31m'; c_grn='\033[1;32m'; c_ylw='\033[1;33m'; c_blu='\033[1;34m'; c_off='\033[0m'
say()  { printf "%b[launcher]%b %s\n" "$c_blu" "$c_off" "$*"; }
warn() { printf "%b[launcher]%b %s\n" "$c_ylw" "$c_off" "$*"; }
err()  { printf "%b[launcher]%b %s\n" "$c_red" "$c_off" "$*" >&2; }
ok()   { printf "%b[launcher]%b %s\n" "$c_grn" "$c_off" "$*"; }

# 1. Termux sanity
if [ ! -d /data/data/com.termux ]; then
    err "this script is for Termux on Android. exiting."
    exit 1
fi

# 2. proot-distro
if ! command -v proot-distro >/dev/null 2>&1; then
    warn "proot-distro not found. installing..."
    pkg update -y && pkg install -y proot-distro || { err "pkg install failed"; exit 1; }
fi

# 3. distro — check rootfs directory directly (more reliable than parsing `list`)
if [ ! -d "$PREFIX/var/lib/proot-distro/installed-rootfs/${DISTRO}" ]; then
    warn "distro '${DISTRO}' not installed. installing (this can take a few minutes)..."
    proot-distro install "${DISTRO}" || { err "distro install failed"; exit 1; }
fi

# 4. resolve script dir (host side — Termux home), so we know where to bind-mount
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR_HOST="$(dirname "$SCRIPT_DIR")"
say "host project dir: $PROJECT_DIR_HOST"
say "proot project dir: $WEBAGENT_DIR  (distro: $DISTRO)"

# 5. termux-api (for Browser button forwarding)
TERMUX_OPEN_URL_BIN="$(command -v termux-open-url || true)"
if [ -z "$TERMUX_OPEN_URL_BIN" ]; then
    warn "termux-open-url not installed — Browser button will only print the URL."
    warn "to enable: pkg install termux-api  (and install the 'Termux:API' addon app)"
fi

# 6. Browser-bridge tailer.
# proot-distro mounts the Termux fs at /data/data/com.termux. The proot's
# /tmp lives inside the rootfs at:
PROOT_ROOTFS="$PREFIX/var/lib/proot-distro/installed-rootfs/${DISTRO}"
PROOT_BRIDGE_HOST="${PROOT_ROOTFS}${BROWSER_BRIDGE}"
mkdir -p "$(dirname "$PROOT_BRIDGE_HOST")" 2>/dev/null || true
: > "$PROOT_BRIDGE_HOST" 2>/dev/null || true

if [ -n "$TERMUX_OPEN_URL_BIN" ] && [ -e "$PROOT_BRIDGE_HOST" ]; then
    say "starting browser bridge watcher → $PROOT_BRIDGE_HOST"
    (
        last_url=""
        while true; do
            if [ -s "$PROOT_BRIDGE_HOST" ]; then
                url="$(head -n 1 "$PROOT_BRIDGE_HOST" 2>/dev/null || true)"
                if [ -n "$url" ] && [ "$url" != "$last_url" ]; then
                    "$TERMUX_OPEN_URL_BIN" "$url" 2>/dev/null || true
                    last_url="$url"
                fi
            fi
            sleep 1
        done
    ) &
    BRIDGE_PID=$!
    trap 'kill "$BRIDGE_PID" 2>/dev/null || true' EXIT
fi

# 7. Ensure launcher deps in the proot, then launch.
ok "entering ${DISTRO} proot..."
proot-distro login "${DISTRO}" --bind "${PROJECT_DIR_HOST}:${WEBAGENT_DIR}" -- /bin/bash -lc "
    set -e
    cd '${WEBAGENT_DIR}'
    if ! command -v python3 >/dev/null 2>&1; then
        echo '[launcher] installing python3...'
        apt-get update && apt-get install -y python3 python3-pip python3-venv
    fi
    # Textual lives in a tiny launcher-only venv to stay isolated from the
    # webagent project venv (which doctor/fixes will manage separately).
    LAUNCHER_VENV='${WEBAGENT_DIR}/launcher_android/.venv'
    if [ ! -x \"\$LAUNCHER_VENV/bin/python\" ]; then
        echo '[launcher] creating launcher venv at \$LAUNCHER_VENV...'
        python3 -m venv \"\$LAUNCHER_VENV\"
        \"\$LAUNCHER_VENV/bin/pip\" install --quiet --upgrade pip
        \"\$LAUNCHER_VENV/bin/pip\" install --quiet textual
    fi
    export WEBAGENT_PROJECT_DIR='${WEBAGENT_DIR}'
    export WEBAGENT_BROWSER_BRIDGE='${BROWSER_BRIDGE}'
    cd '${WEBAGENT_DIR}/launcher_android'
    exec \"\$LAUNCHER_VENV/bin/python\" -m launcher_android
"
