#!/usr/bin/env bash
# ============================================================================
# WebAgent - macOS installer & launcher.
#
# Run on the target Mac. This is what the Deploy panel's one-line command hands
# off to after cloning the repo to $HOME/webagent. It installs WebAgent natively
# (the dependencies build fine on macOS) and keeps it running in the background
# via launchd. It is the macOS sibling of deploy/termux-setup.sh (Linux/Termux).
#
# ORDER MATTERS (same as the Linux/Termux script): we install the lightweight
# Server Manager (the `webagent` command) FIRST, on its own minimal deps, and
# only then build the heavier server. A failure in the fragile server build then
# never leaves you empty-handed - you always have a working `webagent` to
# inspect / retry / diagnose. The server build runs non-fatally, mirrored to
# ~/webagent-install.log.
#
# launchd gives us all three behaviours at once: RunAtLoad (start now + at login)
# and KeepAlive (restart if it stops). Idempotent: re-running updates the code,
# reuses the venv, and reloads the agent. Browser automation (Playwright) is
# intentionally omitted - see req_no_playwright.txt.
# ============================================================================
set -e

# Where the repo actually lives. Derive it from THIS script's own location
# (deploy/macos-setup.sh → its parent is the repo root) so a CUSTOM install folder
# chosen in the Deploy panel works, not just the default. Fall back to the default.
REPO_DIR="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)"
[ -d "$REPO_DIR/.git" ] || REPO_DIR="$HOME/webagent"
PORT=8080
INSTALL_LOG="$HOME/webagent-install.log"
PLIST="$HOME/Library/LaunchAgents/com.webagent.server.plist"
LABEL="com.webagent.server"

echo "============================================"
echo " WebAgent - macOS setup"
echo "============================================"

# The one-line command clones the repo first; make sure it's really there. ----
if [ ! -d "$REPO_DIR/.git" ]; then
  echo "ERROR: WebAgent code not found at $REPO_DIR." >&2
  echo "       Clone it first, then re-run this script." >&2
  exit 1
fi
echo "Updating WebAgent code..."
git -C "$REPO_DIR" pull --ff-only || echo "  (could not fast-forward - keeping the current code)"

# Optional pre-set admin password (from the Deploy panel). The one-line install
# command passes it as WA_ADMIN_PW; we write it into .env, which the app loads at
# boot (app/main.py load_dotenv). .env is gitignored, so the git pull above never
# disturbs it. When it's absent nothing is written — the first visitor to the app's
# address sets the password on the setup page (open until an admin exists, so it
# works from another device on the same network too).
if [ -n "$WA_ADMIN_PW" ]; then
  ENV_FILE="$REPO_DIR/.env"
  touch "$ENV_FILE"
  # Drop any earlier line so re-running replaces, not stacks.
  grep -vE '^BOOTSTRAP_ADMIN_PASSWORD=' "$ENV_FILE" > "$ENV_FILE.tmp" 2>/dev/null || : > "$ENV_FILE.tmp"
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  printf 'BOOTSTRAP_ADMIN_PASSWORD=%s\n' "$WA_ADMIN_PW" >> "$ENV_FILE"
  echo "Admin account will be created on first boot from the password you set."
fi

# Make sure python3 is available (Command Line Tools or Homebrew provide it). ---
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is needed."
  if command -v brew >/dev/null 2>&1; then
    echo "Installing python via Homebrew..."
    brew install python || true
  else
    echo "Opening the Command Line Tools installer (a dialog may appear)..."
    xcode-select --install 2>/dev/null || true
    echo "Finish that install, then paste the command again." >&2
    exit 1
  fi
fi

# ── STEP 1: the Server Manager (the `webagent` command) - FIRST ──────────────
# A dedicated venv + a `webagent` launcher on PATH, so even if the heavier server
# build below fails you are ALWAYS left with a working `webagent`. Non-fatal.
# WEBAGENT_TUI_NO_SUPERVISE keeps it in observe/restart-only mode (no 2nd server).
echo "Installing the Server Manager (the 'webagent' command)..."
( set -e
  [ -d "$REPO_DIR/TUI/.venv" ] || python3 -m venv "$REPO_DIR/TUI/.venv"
  "$REPO_DIR/TUI/.venv/bin/pip" install --upgrade pip wheel >/dev/null
  "$REPO_DIR/TUI/.venv/bin/pip" install -e "$REPO_DIR/TUI"
  # /usr/local/bin is the usual spot; use sudo only if it isn't writable.
  WACMD=/usr/local/bin/webagent
  WRITE="tee"; [ -w /usr/local/bin ] || WRITE="sudo tee"
  $WRITE "$WACMD" >/dev/null <<WALAUNCH
#!/usr/bin/env bash
# WebAgent Server Manager (TUI) - installed FIRST by macos-setup.sh.
# launchd owns the running server; this just inspects / restarts it
# (WEBAGENT_TUI_NO_SUPERVISE = no second supervisor).
export WEBAGENT_PROJECT="$REPO_DIR"
export WEBAGENT_TUI_NO_SUPERVISE=1
exec "$REPO_DIR/TUI/.venv/bin/webagent" "\$@"
WALAUNCH
  chmod +x "$WACMD" 2>/dev/null || sudo chmod +x "$WACMD"
) && echo "  Server Manager installed - run 'webagent' to manage it." \
  || echo "  (Server Manager install skipped - the server build will still continue.)"

# ── STEP 2: build + start the full WebAgent server - NON-FATAL ───────────────
# Heavier and more fragile; run it WITHOUT aborting the whole script on failure
# so the manager above stays usable. All output is mirrored to $INSTALL_LOG and
# ${PIPESTATUS[0]} drives the closing banner.
HEAVY_OK=0
set +e
{
  set -e
  echo "[1/3] Building the Python virtual environment..."
  cd "$REPO_DIR"
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install --upgrade pip wheel
  # Playwright-free dependency set - same lightweight install as Linux/Termux.
  .venv/bin/pip install -r req_no_playwright.txt
  # Pre-compile bytecode so the first server boot is fast (no .pyc compile). Non-fatal.
  .venv/bin/python -m compileall -q app run.py || true

  echo "[2/3] Installing the launchd agent (background + restart + start at login)..."
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$REPO_DIR/.venv/bin/python</string>
    <string>$REPO_DIR/run.py</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$REPO_DIR/server_log.txt</string>
  <key>StandardErrorPath</key><string>$REPO_DIR/server_log.txt</string>
</dict>
</plist>
PLISTEOF

  echo "[3/3] Starting the server..."
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load -w "$PLIST"
} 2>&1 | tee "$INSTALL_LOG"
HEAVY_RC=${PIPESTATUS[0]}
set -e
[ "${HEAVY_RC:-1}" = 0 ] && HEAVY_OK=1

# Tell the user where to reach it (or how to recover). ------------------------
IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)"
echo ""
echo "============================================"
if [ "$HEAVY_OK" = 1 ]; then
  echo " WebAgent is starting in the background."
  echo "   On this Mac:         http://localhost:$PORT"
  if [ -n "$IP" ]; then
    echo "   From another device: http://$IP:$PORT   (same network)"
  fi
  echo " It keeps running, restarts itself if it stops, and starts when you log in."
  echo " Manage it any time with:  webagent   (the Server Manager TUI)"
  echo " To stop it:  launchctl unload $PLIST"
else
  echo " The Server Manager IS installed - run:   webagent"
  echo " ...but the WebAgent server build did NOT finish."
  echo ""
  echo "   - Retry any time by re-running this same install command."
  echo "   - Inspect / diagnose with the manager:   webagent"
  echo "   - The full install log is saved at:      $INSTALL_LOG"
fi
echo "============================================"
