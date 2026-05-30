#!/data/data/com.termux/files/usr/bin/bash
# webAgent Termux shortcut — Android home-screen launcher entry point.
#
# Copy this into the Termux:Widget shortcuts dir so it shows up as a tappable
# widget, then run it once to make it executable:
#
#     cp ~/webagent/launcher_android/start_agent.sh ~/.shortcuts/start_agent.sh
#     chmod +x ~/.shortcuts/start_agent.sh
#
# On first run it prompts for an LLM API key (saved to .env), then logs into
# the proot-distro Ubuntu guest, makes sure `textual` is installed, and starts
# the launcher TUI. From the TUI press L to launch the webAgent server.
#
# PROJECT_ROOT is hard-coded to ~/webagent on purpose: this file is meant to be
# *copied* into ~/.shortcuts/, so it cannot resolve the repo path relative to
# its own location.

PROJECT_ROOT="$HOME/webagent"
ENV_FILE="$PROJECT_ROOT/.env"

# Keep the launcher package importable and clear stale bytecode.
touch "$PROJECT_ROOT/launcher_android/__init__.py"
find "$PROJECT_ROOT" -name "__pycache__" -exec rm -rf {} +
find "$PROJECT_ROOT" -name "*.pyc" -delete

# --- API key prompt (Termux side, before entering the proot) -----------------
API_KEY=""
if [ -f "$ENV_FILE" ]; then
    API_KEY=$(grep -E '^(LLM_API_KEY|OPENROUTER_API_KEY)=' "$ENV_FILE" | head -1 | cut -d'=' -f2 | tr -d "\"'" | xargs)
fi

if [ -z "$API_KEY" ] || [ "$API_KEY" = "your_key_here" ] || [ "$API_KEY" = "your_openrouter_api_key" ]; then
    clear
    echo "webAgent - Set Your API Key"
    echo "Get a free key at: openrouter.ai (or use your OpenAI key)"
    echo ""
    echo -n "Paste your API key: "
    read -r USER_KEY

    if [ -n "$USER_KEY" ]; then
        if grep -q '^LLM_API_KEY=' "$ENV_FILE" 2>/dev/null; then
            sed -i "s|^LLM_API_KEY=.*|LLM_API_KEY=$USER_KEY|" "$ENV_FILE"
        elif grep -q '^OPENROUTER_API_KEY=' "$ENV_FILE" 2>/dev/null; then
            sed -i "s|^OPENROUTER_API_KEY=.*|OPENROUTER_API_KEY=$USER_KEY|" "$ENV_FILE"
        else
            echo "LLM_API_KEY=$USER_KEY" >> "$ENV_FILE"
        fi
        echo "API key saved!"
    fi
fi

# --- Launch the TUI inside the Ubuntu proot ----------------------------------
proot-distro login ubuntu --bind "$PROJECT_ROOT:/root/webagent" -- bash -c '
    set -e
    export PYTHONPATH=/root/webagent
    export WEBAGENT_PROJECT_DIR=/root/webagent
    export WEBAGENT_BROWSER_BRIDGE=/root/webagent/launcher_android/.browser-bridge
    cd /root/webagent

    # Ensure textual is present (first-run install only, then cached).
    if ! python3 -c "import textual" >/dev/null 2>&1; then
        apt-get update -qq
        apt-get install -y python3 python3-pip
        python3 -m pip install --break-system-packages textual
    fi

    python3 -m launcher_android.launcher_android
'
