### Session Management
- **DEFAULT Session**: Used exclusively for warnings, notifications, and maintenance tasks.
- **Other Sessions**: Used for normal AI-to-user chats.

### Workflow
1. Watchdog alerts and server issues are routed to the DEFAULT session.
2. After resolving an issue, the agent updates `prompt.md`.
3. The agent restarts the DEFAULT session using the `/new` command.

### Actions Taken
- Terminated stale process (PID 8108) to free port 8080.
- Checked server logs and identified PostgreSQL connection issues.
- Added alarms for zombie processes and failed start attempts.
- Adjusted watchdog configuration: interval=15s, error_rate_threshold=5, max_restarts_per_hour=3.
- Simulated a slow start and verified the alarm triggers promptly.

### Next Steps
- Monitor server health with stricter timeouts.
- Ensure alarms trigger promptly for slow starts.