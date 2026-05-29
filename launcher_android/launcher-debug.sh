#!/bin/bash
DISTRO="ubuntu"
WEBAGENT_DIR="/root/webagent"
PROJECT_DIR_HOST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "DEBUG: About to enter proot..."
echo "Host: $PROJECT_DIR_HOST"

proot-distro login "${DISTRO}" --bind "${PROJECT_DIR_HOST}:${WEBAGENT_DIR}" -- bash -c "
    set -x
    echo 'DEBUG: Inside proot!'
    cd '${WEBAGENT_DIR}'
    echo 'DEBUG: Current dir: \$PWD'
    ls -F
    exit 0
"
echo "DEBUG: Proot exited."
