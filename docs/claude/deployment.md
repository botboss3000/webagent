# Deployment & runtime state guidance

Read this before touching deploy config, OAuth/secure-context code, or introducing any file the app writes at runtime.

## Pre-production cleanup — Service Worker dev bypass

The page boot sequence in `index.html` currently includes an **inline script** that unregisters any previously-installed service worker and clears all caches on every hard reload. This was added to work around stale SW caches that served old JS files after code changes, causing the UI to hang on a spinner (the JS boot sequence was fetching HTML partials from cache while the server had changed).

**This bypass is meant for development only.** Remove it before production:

1. Open `index.html`.
2. Delete the block between `<!-- ╔══════════════════════════════════ ... ╗ -->` and `<!-- ╚════════════════════════════ ... ╝ -->` (it sits right after the `<title>` / `<link rel="icon">` lines and before the manifest link).
3. Keep the manifest + SW registration code at the bottom of `<head>` — that's the normal PWA path used in production.

When the bypass is removed, bump the `CACHE` constant in `sw.js` (e.g. `webagent-v12`) on each deploy so the activate handler drops stale caches.

## Production deployment

The app runs on a **Google Cloud Compute Engine VM** (project `webagent-495517`, instance `webagent-development`, zone `us-central1-a`, static external IP `34.69.22.204`). It is **not** on Cloud Run, despite the codebase being Cloud-Run-compatible.

**Public URL:** `https://webagent.live` (also `www.webagent.live`). Domain registered at Namecheap. DNS: two A records (`@` and `www`) → `34.69.22.204` via Namecheap BasicDNS.

**Stack on the VM:**

| Layer | What | Where |
|-------|------|-------|
| TLS / reverse proxy | **Caddy 2.x** with automatic Let's Encrypt | `/etc/caddy/Caddyfile` — `webagent.live, www.webagent.live { reverse_proxy localhost:8080 }` |
| App server | **uvicorn** running `app.main:app` bound to `127.0.0.1:8080` (loopback only — Caddy is the only public ingress) | systemd unit `/etc/systemd/system/webagent.service` |
| GCE firewall | `allow-http-https` rule opens tcp 80, 443 to `0.0.0.0/0`. Port 8080 is **not** exposed publicly. | Created from Cloud Shell, not from VM SSH (VM SA lacks compute scope) |

**Repo on VM:** `~/webagent` (user `botboss3000`). Python venv inside. Deploy via `git pull` on `main`. **`app/db/local.db` is now gitignored** — it is a runtime artifact, not tracked in the repo. The VM generates its own `local.db` on first run. When pulling, if a stale `local.db` blocks the pull, discard it first (`git stash push -- app/db/local.db` or simply delete it) then pull. The initial seed DB is created by the app's migration logic on startup.

**Google OAuth:** redirect URI must match the public HTTPS URL → `https://webagent.live/api/v1/oauth/callback/google`. JS origin `https://webagent.live`. OAuth never works against `http://<vm-ip>:8080`.

**Secure-context APIs (`crypto.randomUUID`, `crypto.subtle`, clipboard, etc.)** require HTTPS or `localhost`. Plain `http://<ip>:8080` will throw `crypto.randomUUID is not a function` and break `bindDom()` in `ui/js/state.js`, leaving the agent WebSocket dot stuck on yellow (`Connecting...`). The polyfill in `ui/js/uuid.js` handles non-secure contexts — use `randomUUID()` from there, not `crypto.randomUUID()` directly. Any new secure-context API added to the UI needs a similar fallback or must be guarded behind a feature check.

**Status dots in the chat header:** green = WS subscribed (`agentWs.js` got the `subscribed` event); yellow = WS opening or no subscribe reply yet; red = WS closed or `currentUserId` missing. Yellow that never goes green almost always means a JS exception during init prevented `currentUserId` from being set, or the WS handshake never completed — check the browser console first.

## Runtime state files — gitignored

Any file the **running app writes to** is per-machine runtime data and, as a rule, must never be tracked by git. Tracking such files causes `error: Your local changes to the following files would be overwritten by merge` on `git pull` on the production VM, blocking deploys.

**`app/db/local.db` is gitignored** — it was previously tracked, but is now treated as a per-machine runtime artifact (the `.gitignore` says so explicitly). On a fresh checkout the app recreates it on first run via the migration/seed logic, pulling agent templates from `data/agents/`. Its **transient sidecars** (`-journal`, `-wal`, `-shm`, `.preprompt-bak`) and any **stray root `local.db`** are gitignored too: never commit a live write-ahead log or shared-memory file.

**Rule (every other runtime file):** before introducing a new file the backend writes during normal operation (auth blobs, caches, per-machine config, runtime backups, user-generated artifacts), add it to `.gitignore` in the same commit. If you discover one already tracked, untrack it: `git rm --cached <path>` + add to `.gitignore` + commit.

**Currently gitignored runtime files (do not re-add to repo):**

| Path | What it is |
|------|-----------|
| `app/db/local.db` (+ sidecars `-journal`, `-wal`, `-shm`, `.preprompt-bak`) | Runtime SQLite DB + transient write logs — all gitignored; recreated on first run via the seed/migration logic. |
| `local.db` (root) | Stray runtime SQLite |
| `app/db/logs.db` (+ `-wal`/`-shm`/`-journal`) | **Dedicated, always-local logs DB** — server diagnostics (`diagnostics`) + tool metrics (`tool_executions`). Own WAL, separate from `local.db`. Per-machine; recreated on first run. See `app/db/logs_store.py`. |
| `app/db/recordings.db` (+ `-wal`/`-shm`/`-journal`) | Browser render recorder (`render_recordings`) — separate firehose file, off by default. Per-machine; recreated on first run. |
| `app/db/instance_id.txt` | Per-box identity stamped on every log record (multi-instance disambiguation). Generated once on first run. |
| `app/auth/users.json` (+ `.bak`) | Password hashes, remember tokens |
| `app/db_mode.json` | Per-machine DB target switch |
| `app/db/.fuse_hidden*`, `**/.fuse_hidden*` | FUSE/SSHFS temp leftovers |
| `data/visuals/users/` | Per-user generated pages and artifacts |
| `data/uploads/` | User-uploaded files (runtime) |
| `data/config/provider.json` | LLM + GitHub tokens (gitignored, shared cred store) |
| `data/config/scheduler_config.json` | Scheduler runtime state |
| `data/config/suggestions.json` | Suggested-Replies engine tunables (mode / chip count / idle seconds); defaults derive from `data/agents/user-impersonator.json` metadata when absent |
| `data/config/remote_access.json` (+ `.bak`, `_pointers.json`) | Remote-access tunnel config (per-machine) |
| `.env` | Local env vars |

**Checklist when adding a new backend write target:**

1. Is the file written by the app at runtime (not committed by a human)? → must be gitignored.
2. Does it contain secrets, hashes, tokens, or per-user state? → must be gitignored.
3. Does the test fixture or seed flow need a default? → ship a `*.example` or `*.template` variant that IS tracked, and have the app copy/derive from it on first boot.

If you skip this and a *should-be-ignored* file gets committed, the VM's edited copy will collide on every `git pull` and someone has to do the backup/restore dance documented above in **Production deployment**.
