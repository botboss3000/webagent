# Starting WebAgent on Android/Termux (Ubuntu proot)

**Why this is needed:** On Android/Termux, the host Python is 3.13+ (unsupported by WebAgent's dependency stack). The solution is an **Ubuntu proot** running Python 3.11 or 3.12. The server must be started *inside* the proot, and the proot must stay alive as long as the server runs.

## Quick start

```bash
cd ~/webagent
nohup proot-distro login ubuntu --bind ~/webagent:/root/webagent -- bash -c '
    cd /root/webagent
    source venv/bin/activate
    python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --log-level info
' > logs/server.log 2>&1 &
```

Wait ~8 seconds, then verify:

```bash
curl -s http://localhost:8080/health
# → {"status":"healthy"}
```

The web UI is at http://localhost:8080/index.html

## The venv

The virtual environment lives at `venv/` (inside the repo) and was created with **Python 3.11** from inside the proot. It symlinks to `/usr/bin/python3.11` — this only resolves inside the proot, not from the host Termux shell.

## The server log

```bash
cat logs/server.log          # full output
tail -f logs/server.log      # follow live
```

The proot process writes to the log. The server itself logs app startup, health checks, and any errors there.

## Stopping

```bash
# Kill the proot process (which kills the server)
kill $(cat .proot.pid 2>/dev/null) 2>/dev/null
# Or find and kill all proot sessions
pkill -f "proot-distro.*webagent"
```

## Checking it's running

```bash
curl -s http://localhost:8080/health            # from host
ss -tlnp | grep 8080                              # may not show from host side
ps aux | grep proot | grep webagent               # check the proot itself
```

## Things to remember

1. **The proot must stay alive.** Unlike a normal daemon, the server is a child of the proot session. If the proot dies, the server dies.
2. **No `--reload`.** The `--reload` watchdog swallows startup errors and complicates the detach. Use direct `uvicorn` without reload.
3. **Restart = kill + start again.** There's no graceful restart path across the proot boundary.
4. **Startup takes 6–10 seconds.** The app loads many plugins and the scheduler. The health endpoint will return 200 once it's fully up.
5. **The `.proot.pid` file** in the repo root stores the PID of the last-started proot session.

## Manual one-liner (if the script is lost)

```bash
cd ~/webagent && nohup proot-distro login ubuntu --bind ~/webagent:/root/webagent -- bash -c 'cd /root/webagent && source venv/bin/activate && exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --log-level info' > logs/server.log 2>&1 &
```