#!/data/data/com.termux/files/usr/bin/bash
# Start webAgent server inside proot and keep the session alive
PROJECT_ROOT="$HOME/webagent"
cd "$PROJECT_ROOT"

# Launch proot with a persistent bash that starts the server then sleeps
proot-distro login ubuntu --bind "$PROJECT_ROOT:/root/webagent" -- bash -c '
cd /root/webagent
source .venv/bin/activate
nohup python run.py > server_log.txt 2>&1 &
echo $! > /tmp/webagent.pid
echo "Server started with PID $(cat /tmp/webagent.pid)"
# Keep the session alive indefinitely
while true; do
    if ! kill -0 $(cat /tmp/webagent.pid 2>/dev/null) 2>/dev/null; then
        echo "Server died, restarting..."
        nohup python run.py >> server_log.txt 2>&1 &
        echo $! > /tmp/webagent.pid
        echo "Restarted with PID $(cat /tmp/webagent.pid)"
    fi
    sleep 30
done
' &
echo "Launcher PID: $!"
echo $! > /tmp/webagent_launcher.pid
