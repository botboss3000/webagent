"""The shared install recipe — one bootstrap script for every fresh-VM target.

Google VM, AWS and a plain Linux box are all just "a fresh Ubuntu machine," so
they install webAgent the SAME way; they differ only in how the machine is
created and how this script is handed to it (cloud metadata "startup-script" for
GCE/AWS; over SSH for a plain box). Docker uses the repo's Dockerfile instead and
does not call this.

The script (targets Ubuntu 24.04 LTS, which ships Python 3.12):
  1. installs Python, git and Caddy (official apt repo),
  2. clones the chosen repo + branch into /opt/webagent,
  3. builds a venv and installs requirements,
  4. writes a minimal .env (LLM is configured in-app afterwards),
  5. installs a systemd unit running uvicorn on 127.0.0.1:<port>,
  6. fronts it with Caddy — automatic HTTPS for a domain, or plain :80 on the
     server's IP when no domain is given,
  7. enables + starts both services.

Its full output is logged on the box at /var/log/webagent-bootstrap.log.

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

# ── Caddy reverse proxy (auto-HTTPS for a domain, else plain :80) ──
if [ -n "$DOMAIN" ]; then
cat > /etc/caddy/Caddyfile <<CADDYEOF
$DOMAIN {
    reverse_proxy localhost:$PORT
}
CADDYEOF
else
cat > /etc/caddy/Caddyfile <<CADDYEOF
:80 {
    reverse_proxy localhost:$PORT
}
CADDYEOF
fi

systemctl daemon-reload
systemctl enable webagent
systemctl restart webagent
systemctl restart caddy
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
