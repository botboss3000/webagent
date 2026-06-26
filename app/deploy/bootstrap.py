"""The shared install recipe — one bootstrap script for every fresh-VM target.

Google VM, AWS and a plain Linux box are all just "a fresh Ubuntu machine," so
they install webAgent the SAME way; they differ only in how the machine is
created and how this script is handed to it (cloud metadata "startup-script" for
GCE/AWS; over SSH for a plain box). Docker uses the repo's Dockerfile instead and
does not call this.

The script (targets Ubuntu 24.04 LTS, which ships Python 3.12):
  1. installs Python, git and Caddy (official apt repo),
  2. IMMEDIATELY fronts the box with Caddy pointed at the app + a friendly
     "installing…" holding page, so the misleading Caddy welcome page never
     shows. The holding page auto-refreshes and turns into the app the moment
     the backend answers; if the install fails it flips to a clear error page.
  3. clones the chosen repo + branch into /opt/webagent,
  4. builds a venv and installs requirements,
  5. writes a minimal .env (LLM is configured in-app afterwards),
  6. installs a systemd unit running uvicorn on 127.0.0.1:<port>,
  7. enables + starts the app service (Caddy is already configured from step 2).

Why Caddy is configured FIRST (before the slow dependency install): the apt
package starts Caddy on its default "Congratulations, configure me" welcome
page the instant it installs. Left until the end — after a multi-minute pip
install — that welcome page is what a visitor sees the whole time, and *forever*
if any later step fails. Configuring Caddy up front replaces it with an honest
status page that needs no working backend.

Its full output is logged on the box at /var/log/webagent-bootstrap.log, and a
failure also rewrites the holding page so the problem is visible in the browser.

We substitute ``__TOKEN__`` placeholders (not ``str.format``) because the script
is full of shell ``$VAR`` and ``{}`` that would collide with format fields.
"""

from __future__ import annotations

_SCRIPT = r"""#!/bin/bash
set -e
exec > /var/log/webagent-bootstrap.log 2>&1
echo "webAgent bootstrap starting at $(date)"
export DEBIAN_FRONTEND=noninteractive

REPO_URL="__REPO_URL__"
BRANCH="__BRANCH__"
DOMAIN="__DOMAIN__"
PORT="__PORT__"
APP_DIR="/opt/webagent"
APP_USER="webagent"

apt-get update
apt-get install -y python3 python3-venv python3-pip git curl \
    debian-keyring debian-archive-keyring apt-transport-https

# ── Caddy (official apt repo) ──
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list
apt-get update
apt-get install -y caddy

# ── Caddy FIRST: an honest holding page now, the app the moment it is up ──
# The apt package starts Caddy on its default "Congratulations, configure me"
# welcome page immediately. We replace that right away with a reverse proxy to
# the (not-yet-running) app PLUS a handle_errors fallback that serves a friendly
# "installing…" page whenever the backend isn't answering. So a visitor sees an
# honest status page during the whole install — never the misleading welcome
# page — and it turns into the real app automatically once the backend answers.
STATUS_DIR="/var/www/webagent-status"
mkdir -p "$STATUS_DIR"
cat > "$STATUS_DIR/index.html" <<'STATUSEOF'
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>webAgent — installing…</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         background:#0d1117; color:#e6edf3; }
  .card { max-width:34rem; padding:2.5rem 2rem; text-align:center; }
  .dot { width:12px; height:12px; border-radius:50%; background:#3fb950; display:inline-block;
         margin-right:.5rem; vertical-align:middle; animation:pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:.35} 50%{opacity:1} }
  h1 { font-size:1.4rem; margin:0 0 1rem; font-weight:650; }
  p  { line-height:1.6; margin:.6rem 0; color:#c9d1d9; }
  .muted { color:#8b949e; font-size:.85rem; margin-top:1.6rem; }
  code { background:#161b22; padding:.15rem .4rem; border-radius:4px; font-size:.85em; }
</style></head>
<body><div class="card">
  <h1><span class="dot"></span>Installing webAgent…</h1>
  <p>Your server is up and setting itself up. This usually takes <b>3&ndash;8 minutes</b> on a small VM.</p>
  <p>This page refreshes itself &mdash; when the app is ready it appears here automatically. Nothing to do.</p>
  <p class="muted">Still on this page after ~15 minutes? The install hit a snag. Connect to the VM and read
  <code>/var/log/webagent-bootstrap.log</code> to see what went wrong.</p>
</div></body></html>
STATUSEOF

if [ -n "$DOMAIN" ]; then
cat > /etc/caddy/Caddyfile <<CADDYEOF
$DOMAIN {
    reverse_proxy localhost:$PORT
    handle_errors {
        root * $STATUS_DIR
        rewrite * /index.html
        file_server
    }
}
CADDYEOF
else
cat > /etc/caddy/Caddyfile <<CADDYEOF
:80 {
    reverse_proxy localhost:$PORT
    handle_errors {
        root * $STATUS_DIR
        rewrite * /index.html
        file_server
    }
}
CADDYEOF
fi
systemctl reload caddy 2>/dev/null || systemctl restart caddy

# From here on, any failure flips the holding page to a clear error message (and
# stops), so a broken install shows up in the browser instead of a stuck spinner.
on_error() {
  rc=$?
  echo "BOOTSTRAP FAILED (exit $rc) near line ${1:-?}"
  cat > "$STATUS_DIR/index.html" <<'FAILEOF'
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>webAgent — install problem</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         background:#0d1117; color:#e6edf3; }
  .card { max-width:34rem; padding:2.5rem 2rem; text-align:center; }
  h1 { font-size:1.4rem; margin:0 0 1rem; font-weight:650; color:#f85149; }
  p  { line-height:1.6; margin:.6rem 0; color:#c9d1d9; }
  code { background:#161b22; padding:.15rem .4rem; border-radius:4px; font-size:.85em; }
</style></head>
<body><div class="card">
  <h1>The webAgent install didn't finish</h1>
  <p>The server was created, but setting up the app ran into a problem.</p>
  <p>Connect to the VM and read <code>/var/log/webagent-bootstrap.log</code> for the exact error,
  or tear this server down from <b>App Settings &rarr; Deploy</b> and try again.</p>
</div></body></html>
FAILEOF
  systemctl reload caddy 2>/dev/null || true
  exit $rc
}
trap 'on_error $LINENO' ERR

# ── App user + code ──
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
rm -rf "$APP_DIR"
git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$APP_DIR"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

# ── Python venv + dependencies ──
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip wheel
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# ── .env (minimal; LLM keys are set in-app) ──
cat > "$APP_DIR/.env" <<ENVEOF
PORT=$PORT
ENVEOF
chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"

# ── systemd service ──
cat > /etc/systemd/system/webagent.service <<UNITEOF
[Unit]
Description=webAgent FastAPI server
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT --ws wsproto --proxy-headers --timeout-keep-alive 300
Restart=always
RestartSec=2
StandardOutput=journal
StandardError=journal
SyslogIdentifier=webagent

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable webagent
systemctl restart webagent
# Caddy was already configured up top (holding page → app). A reload re-asserts
# the config; the status page now auto-yields to the live backend.
systemctl reload caddy 2>/dev/null || systemctl restart caddy

# ── Server Manager TUI (the `webagent` command, installed alongside) ──
# Put the standalone Server Manager TUI on the box so an admin who SSHes in can
# run `webagent` to inspect / restart / diagnose the install from a terminal. The
# web server itself keeps running under the systemd unit above — this is just the
# manager front door, so WEBAGENT_TUI_NO_SUPERVISE=1 stops it from auto-starting
# or keep-aliving a SECOND, competing server. Wrapped so a hiccup here never
# blocks the deploy: the server is what matters and it is already up.
if [ -d "$APP_DIR/TUI" ]; then
  ( set -e
    sudo -u "$APP_USER" python3 -m venv "$APP_DIR/TUI/.venv"
    sudo -u "$APP_USER" "$APP_DIR/TUI/.venv/bin/pip" install --upgrade pip wheel
    sudo -u "$APP_USER" "$APP_DIR/TUI/.venv/bin/pip" install -e "$APP_DIR/TUI"
    cat > /usr/local/bin/webagent <<WALAUNCH
#!/bin/bash
# webAgent Server Manager (TUI) — installed alongside the server by the in-app
# Deploy bootstrap. The systemd unit owns the running server; this manager just
# inspects / restarts it on request (no auto-start, no keep-alive guardian).
export WEBAGENT_PROJECT="$APP_DIR"
export WEBAGENT_TUI_NO_SUPERVISE=1
exec "$APP_DIR/TUI/.venv/bin/webagent" "\$@"
WALAUNCH
    chmod +x /usr/local/bin/webagent
    echo "Server Manager installed — run 'webagent' on the box to manage it."
  ) || echo "Server Manager (webagent command) install skipped; the web server is unaffected."
fi

echo "webAgent bootstrap complete at $(date)"
"""


def build_install_script(
    *, repo_url: str, branch: str = "main", domain: str = "", port: int = 8080
) -> str:
    """Render the install script for one deploy with the chosen settings."""
    return (
        _SCRIPT
        .replace("__REPO_URL__", (repo_url or "").strip())
        .replace("__BRANCH__", (branch or "main").strip() or "main")
        .replace("__DOMAIN__", (domain or "").strip())
        .replace("__PORT__", str(port or 8080))
    )


# A sensible default repo to deploy when the admin leaves the field blank: this
# project's public GitHub home. Overridable per-deploy in the panel.
DEFAULT_REPO_URL = "https://github.com/botboss3000/webagent.git"
DEFAULT_BRANCH = "main"
