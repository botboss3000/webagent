# Deployment & runtime state guidance

Read this before touching deploy config, OAuth/secure-context code, or introducing any file the app writes at runtime.

## Shipping updated JS/CSS — the service-worker update path

The old development "unregister + clear-caches on every load" bypass has been **removed** (it caused reload thrash). The current, production-safe update path is:

1. **`sw.js` caches static assets stale-while-revalidate**, keyed by the `CACHE` constant (e.g. `webagent-v63`). On `activate` it deletes every cache whose name isn't the current `CACHE`.
2. **The backend serves `/sw.js` and all `/ui/*` + `/index.html` with `no-store`/`no-cache`** (`app/main.py`), so the browser's HTTP layer never pins a stale service worker or shell — the staleness only ever lives in the SW's own Cache API copy.
3. **`index.html` registers the worker and auto-reloads once when an updated worker takes control** — via both `controllerchange` and `updatefound → 'activated'`, guarded by `_hadController` (no reload on first-ever install) and a one-shot `_swReloaded` flag (can never loop). Navigation is network-first, so an ordinary reload always fetches the fresh `index.html` (and thus the latest registration script).

**To ship a frontend change: bump the `CACHE` constant in `sw.js`** (e.g. `webagent-v63 → webagent-v64`) in the same commit. That single byte change is what the browser's update check detects → the new worker installs (`skipWaiting`) → activates (`clients.claim`, drops old caches) → the page auto-reloads once on fresh code. Without the bump, an open tab keeps serving the previously cached JS/CSS until its next version change.

> **Do NOT re-add an unregister/clear-caches bypass block in `index.html`.** It fights the registration above and turns every load into unregister → re-register → controllerchange → reload — a thrash loop that resets the UI mid-interaction. If updates seem stale, the fix is almost always "bump `CACHE`", not a bypass.

## Production deployment

The app runs on a **Google Cloud Compute Engine VM** (project `webagent-495517`, instance `webagent-development`, zone `us-central1-a`, static external IP `34.69.22.204`). It is **not** on Cloud Run, despite the codebase being Cloud-Run-compatible.

**Public URL:** `https://webagent.live` (also `www.webagent.live`). Domain registered at Namecheap. DNS: two A records (`@` and `www`) → `34.69.22.204` via Namecheap BasicDNS.

**Stack on the VM:**

| Layer | What | Where |
|-------|------|-------|
| TLS / reverse proxy | **Caddy 2.x** with automatic Let's Encrypt | `/etc/caddy/Caddyfile` — `webagent.live, www.webagent.live { reverse_proxy localhost:8080 }` |
| App server | **uvicorn/gunicorn** running `app.main:app` bound to `127.0.0.1:8080` (loopback only — Caddy is the only public ingress). May run **multiple workers** (`uvicorn --workers N`, see Dockerfile). | systemd unit `/etc/systemd/system/webagent.service` |
| GCE firewall | `allow-http-https` rule opens tcp 80, 443 to `0.0.0.0/0`. Port 8080 is **not** exposed publicly. | Created from Cloud Shell, not from VM SSH (VM SA lacks compute scope) |

**Multi-worker correctness (two things to know).** The live chat WebSocket broadcast is **per-process in-memory** — with `--workers N` the browser's socket and the agent run often land on different workers, so live events don't reach the browser. This is handled by the **DB-tail reconcile** on the client (see README "DB is the source of truth"): cross-worker turns stream from the shared DB in ~1–2s chunks, same-worker turns stream smoothly over the WS. Single-worker gives the smoothest streaming; multi-worker is correct but leans on the reconcile. Second: the **singleton background loops** (scheduler, event runtime, ability pollers, watchdog, Remote Access, boot orphan-resume) must run in exactly one worker or automations double-fire and orphans re-ignite N times. `app/coordination/leader.py` elects one worker via a TTL'd lock row (`background_leader`) in the shared DB and runs them only there; if that worker dies another takes over within ~30s. No config needed — single-process always wins leadership instantly.

**Repo on VM:** `~/webagent` (user `botboss3000`). Python venv inside. Deploy via `git pull` on `main`. **All runtime databases live under `data/db/`** (`local.db`, `logs.db`, `recordings.db`, `vault.db`, optimizer scratch) and are gitignored — runtime artifacts, not tracked. The VM generates its own on first run. **Migration note:** older installs kept these in `app/db/`; on the first restart after this change the app **auto-relocates** any `app/db/*.db` into `data/db/` (only when the file isn't already there), so the VM keeps its data — **stop the server before deploying** so the DB files aren't locked during the move. Since both old and new locations are gitignored, neither blocks a `git pull`.

**Google OAuth:** redirect URI must match the public HTTPS URL → `https://webagent.live/api/v1/oauth/callback/google`. JS origin `https://webagent.live`. OAuth never works against `http://<vm-ip>:8080`.

**Secure-context APIs (`crypto.randomUUID`, `crypto.subtle`, clipboard, etc.)** require HTTPS or `localhost`. Plain `http://<ip>:8080` will throw `crypto.randomUUID is not a function` and break `bindDom()` in `ui/js/state.js`, leaving the agent WebSocket dot stuck on yellow (`Connecting...`). The polyfill in `ui/js/uuid.js` handles non-secure contexts — use `randomUUID()` from there, not `crypto.randomUUID()` directly. Any new secure-context API added to the UI needs a similar fallback or must be guarded behind a feature check.

**Status dots in the chat header:** green = WS subscribed (`agentWs.js` got the `subscribed` event); yellow = WS opening or no subscribe reply yet; red = WS closed or `currentUserId` missing. Yellow that never goes green almost always means a JS exception during init prevented `currentUserId` from being set, or the WS handshake never completed — check the browser console first.

## In-app Deploy panel (App Config → App Settings → Deploy)

Admins can stand up a *new* production server from inside the app, instead of doing the manual VM setup above by hand. The **Deploy** card picks a cloud **target**, fills in a settings form + a cloud key, and clicks **Deploy now**; the app creates the server, installs webAgent on it (the same Caddy + systemd + uvicorn stack described above), and streams a live log.

- **Drop-in cloud targets.** Each cloud target is a self-describing drop-in under `app/deploy/providers/<id>.py` (a `BaseDeployProvider` instance exposed as `PROVIDER` + a `FEATURE` header), auto-discovered by `app/deploy/registry.py` — add a target by dropping one file, no central edit (mirrors `app/scheduler/providers/`). Shipped cloud target: **`google_vm`** (Google Compute Engine VM). AWS, a plain Linux box (SSH) and Docker are the planned follow-on targets. The cloud dropdown lists only **non-`manual`** targets (see below).
- **Manual targets (`manual = True`) get their OWN row, not the dropdown.** A "manual" target creates nothing on a cloud account (no cloud key, nothing billable) and instead produces a **copy-paste command** the admin runs themselves. The shared `manual` flag on `BaseDeployProvider` (surfaced in the catalog) is what `deploy.js` uses to **filter it out of the cloud dropdown**. **`termux`** ("Run webAgent on Linux or Termux") is the first manual target and renders as a **separate sibling row** below "Deploy this app to the cloud" in the same `.ac-list`. Its bespoke form: a **GitHub URL**, a **public/private** select, and a **token** field shown only for private. Each field label carries a `data-tip` that `deploy.js` `_wirePhoneTips()` turns into a circled **"?"** help badge (same affordance as the cloud row), including the token's "used once, never saved" reassurance. **There is no "generate" button — the command is shown LIVE.** It is **built in the browser** (`deploy.js` `_txBuild`, a deliberate mirror of `termux.build_command`) so the box is **never empty and updates the instant a field changes**, with NO dependency on a server round-trip (an un-restarted / unreachable server must not leave the box blank — that was the bug). A blank URL / not-yet-typed token render obvious fill-in placeholders (`https://github.com/YOUR-NAME/YOUR-REPO` / `YOUR_ACCESS_TOKEN`) so the box always shows the command's shape, with a one-line nudge underneath; an invalid token warns in red but keeps a valid command on screen. The **ONE command works on both Termux and a plain Linux box** — it installs git with whatever package manager is present (Termux `pkg`, or apt/dnf/pacman with sudo when not root), clones the repo, and runs the repo's `deploy/termux-setup.sh`, which **detects which it's on**: on **Termux** it builds the venv inside an Ubuntu `proot-distro` sandbox (the only Termux-specific "customization") + wake-lock/auto-restart via `start_server_termux.sh`; on **plain Linux** it installs natively (system packages + venv + a **systemd service `webagent`** when systemd is live + root/sudo are available, else a `nohup` keep-alive loop via `deploy/start_server_linux.sh`, no proot). Both install the **Playwright-free** deps `req_no_playwright.txt`, port 8080, and set up **reboot survival** — systemd on Linux (or a `@reboot` cron entry pointing at `start_server_linux.sh` as the no-systemd fallback), and a **Termux:Boot** script (`~/.termux/boot/webagent-boot.sh`, fires once the free Termux:Boot add-on is installed) on the phone. Beside the command sit two **icon-only buttons** mirroring Remote Access (`.ac-ra-icon-btn`, Lucide `copy` + `qr-code`, no text): **Copy**, and a **QR** that toggles a **click-to-show popover** (mirrors Remote Access → Same network — same `_txShowQr`/`_txPlaceQr` pattern, `.ac-ra-qr-pop` plate). The QR itself is still generated **server-side on demand** — opening the popover (and any field change while it's open, debounced) POSTs to **`/admin/deploy/termux/command`** which calls the same `build_command()` and returns a scannable QR via `app/remote_access/netinfo.qr_svg`; if the server can't make one (e.g. not yet restarted) the popover shows a clear message while the command stays usable. `build_command()` is kept placeholder-tolerant (never errors, returns `placeholder_repo`/`placeholder_token`/`warning`) so the QR's command matches the browser's. For a **private** repo the access token is embedded in the clone URL (so the phone can fetch it) and is **never stored**; only the non-secret github_url + visibility persist (saved on blur via `/admin/deploy/config`, provider `termux`), so the row pre-fills next time. On a phone, `termux-setup.sh` now ALSO installs the standalone **Server Manager TUI** alongside the server — it reuses `TUI/install-termux.sh` with `WA_TUI_NO_SUPERVISE=1` so the `webagent` command opens the manager in observe/restart-only mode (it bakes `WEBAGENT_TUI_NO_SUPERVISE=1` into the launcher, so no second keep-alive guardian fights the existing loop). The TUI step is non-fatal (the server is already up). `TUI/install-termux.sh` (`/termux`) run on its own still installs just the manager (no server).
- **Shared install recipe.** Every fresh-VM target injects the SAME bootstrap (`app/deploy/bootstrap.py`) — install Python 3.12 + git + Caddy on Ubuntu 24.04, clone the chosen repo/branch into `/opt/webagent`, build a venv, write a minimal `.env`, install the systemd unit, start the app service. It then installs the **Server Manager TUI** into its own venv (`/opt/webagent/TUI/.venv`) and writes `/usr/local/bin/webagent` (with `WEBAGENT_TUI_NO_SUPERVISE=1` baked in, so it never auto-starts/keep-alives a SECOND server) — an admin who SSHes into the box can run `webagent` to inspect/restart/diagnose; this step is wrapped so a hiccup never blocks the deploy. Handed to the box as the cloud "startup-script" today; over SSH for a plain box later. Docker uses the repo's `Dockerfile` instead.
  - **Caddy is configured FIRST, before the slow dependency install** (auto-HTTPS for a domain, else plain `:80`). The apt package starts Caddy on its default "Congratulations, configure me" **welcome page** the instant it installs; left until the end of a multi-minute pip install, that welcome page is all a visitor sees the whole time — and *forever* if a later step fails. So the bootstrap writes the reverse-proxy Caddyfile immediately, with a `handle_errors` fallback that serves a friendly **"installing…" holding page** (`/var/www/webagent-status/index.html`, auto-refreshing) whenever the backend isn't answering. The holding page turns into the real app the moment uvicorn comes up; a `set -e` **`ERR` trap** rewrites it to a clear "install didn't finish — read `/var/log/webagent-bootstrap.log`" page on any failure. The misleading Caddy welcome page is never shown.
- **`google_vm` needs no new Python dependency** — it drives the Compute Engine REST API with `httpx`, authenticating with the service-account JSON via the same JWT-bearer token exchange the Google Cloud Scheduler provider uses (`cryptography` signs the JWT). It ensures a `webagent-allow-http-https` firewall rule (tag `webagent-http`), creates the instance, polls the zone operation, and reports the external IP. Tear-down deletes the instance.
- **Ephemeral cloud keys.** The key is a secret → it goes into the encrypted vault (`app/deploy/credentials.py`, admin scope, service `deploy_cred:<id>`), never into `deploy.json`, never returned to the browser. On a **successful** deploy it is **auto-discarded** from the vault (the per-target "Forget keys after deploy" setting, default on), so the app holds no standing cloud access; tearing down later re-asks for the key. Non-secret settings live in `data/config/deploy.json`.
- **Admin-only + confirm-gated.** All `/admin/deploy/*` endpoints (`app/api/deploy.py`), including `termux/command`, resolve the caller via `resolve_admin_uid` (same chokepoint as Remote Access); cloud Deploy/Tear-down show a confirm dialog because they create/delete billable resources (the Termux row just generates a command — no confirm). The cloud deploy/tear-down responses stream NDJSON progress (like the Commit ⭐ button) into the card's live log.
- **Files:** `app/deploy/` (store/credentials/base/bootstrap/registry/manager + `providers/google_vm.py`, `providers/termux.py` — `termux.build_command()` is the command source of truth), the Linux/Termux on-device script `deploy/termux-setup.sh` (committed; Termux-detecting — proot path reuses `start_server_termux.sh`, Linux path installs natively), `app/api/deploy.py` (mounted in `app/main.py`; `/admin/deploy/termux/command` builds the command + QR), UI in `ui/admin-tools/app-config/app-settings/` (`deploy.js` cloud row + phone row + the two Deploy rows in `app-settings.html`).

## Runtime state files — gitignored

Any file the **running app writes to** is per-machine runtime data and, as a rule, must never be tracked by git. Tracking such files causes `error: Your local changes to the following files would be overwritten by merge` on `git pull` on the production VM, blocking deploys.

**`data/db/local.db` is gitignored** (whole `data/db/` tree is) — it was previously tracked at `app/db/local.db`, but is now a per-machine runtime artifact under `data/db/` (`data/` = the app's stored state; `app/` = logic only). On a fresh checkout the app recreates it on first run via the migration/seed logic, pulling agent templates from the bundled `app/defaults/agents/` (a `data/agents/` folder overrides them if present — resolved by `app/util/paths.agents_seed_dir`). Its **transient sidecars** (`-journal`, `-wal`, `-shm`, `.preprompt-bak`) and any **stray root `local.db`** are gitignored too. Older installs that still have DBs in `app/db/` are auto-relocated into `data/db/` on the next startup (the legacy `app/db/` paths stay gitignored for the transition).

**Rule (every other runtime file):** before introducing a new file the backend writes during normal operation (auth blobs, caches, per-machine config, runtime backups, user-generated artifacts), add it to `.gitignore` in the same commit. If you discover one already tracked, untrack it: `git rm --cached <path>` + add to `.gitignore` + commit.

**Currently gitignored runtime files (do not re-add to repo):**

| Path | What it is |
|------|-----------|
| `data/db/local.db` (+ sidecars `-journal`, `-wal`, `-shm`, `.preprompt-bak`) | Runtime SQLite DB + transient write logs — all gitignored (whole `data/db/` tree); recreated on first run via the seed/migration logic. Relocated from the legacy `app/db/` on first startup. |
| `local.db` (root) | Stray runtime SQLite |
| `data/db/logs.db` (+ `-wal`/`-shm`/`-journal`) | **Dedicated, always-local logs DB** — server diagnostics (`diagnostics`) + tool metrics (`tool_executions`). Own WAL, separate from `local.db`. Per-machine; recreated on first run. See `app/db/logs_store.py`. |
| `data/db/recordings.db` (+ `-wal`/`-shm`/`-journal`) | Browser render recorder (`render_recordings`) — separate firehose file, off by default. Per-machine; recreated on first run. |
| `data/db/instance_id.txt` | Per-box identity stamped on every log record (multi-instance disambiguation). Generated once on first run. |
| `data/db/vault.db` (+ `-wal`/`-shm`/`-journal`) | **Credentials vault** — `auth_elements` (integration OAuth tokens + inline secret values), kept OUT of `local.db` so a user-data reset is non-destructive. Own WAL, attached as `vault`. Per-machine; `auth_elements` is migrated here from `local.db` on first run of the new code. See `app/db/local.py` (`VAULT_SCHEMA`). |
| `data/db/optimizer_*.db`, `data/db/tests.db`, `data/db/closer_*.db` | Optimizer / self-improvement scratch DBs. Transient, per-machine. |
| `app/auth/users.json` (+ `.bak`) | Password hashes, remember tokens |
| `app/db_mode.json` | Per-machine DB target switch |
| `app/secrets_mode.json` | Per-machine secrets-vault provider (`inline_db`/`os_keyring`/…). Self-writes on first admin change **or** when the first-boot seeder enables `os_keyring` on a fresh install (see below). |
| `app/encryption_mode.json` | Per-machine encryption level (`none`/`field`/…). Self-writes on first admin change **or** when the first-boot seeder enables `field` on a fresh install (see below). |
| `app/db_encryption.json` | Per-machine **full-database (SQLCipher) encryption** state — which database files (`local`/`vault`/`logs`/`recordings`/`wiki`) are encrypted at rest. Self-writes when an admin toggles one in Data Management. The per-file keys live in the keyring (wrapped under the KEK), never in this file. |
| `app/db/.fuse_hidden*`, `**/.fuse_hidden*` | FUSE/SSHFS temp leftovers |
| `data/visuals/users/` | Per-user generated pages and artifacts |
| `data/uploads/` | User-uploaded files (runtime) |
| `data/config/provider.json` | LLM + GitHub tokens (gitignored, shared cred store) |
| `data/config/scheduler_config.json` | Scheduler runtime state |
| `data/config/suggestions.json` | Suggested-Replies engine tunables (mode / chip count / idle seconds); defaults derive from `app/defaults/agents/` (or `data/agents/`) metadata when absent |
| `data/config/main-panel-pages.json`, `admin-panel-pages.json` | Per-page overrides (order/label/icon/visibility) — self-create on first admin edit (`app/admin/page_config.py`) |
| `data/config/agent-abilities.json` | Admin ability on/off + order + per-tool defaults — self-creates / self-heals (`app/admin/ability_config.py`) |
| `data/config/debug-config.json` | Debug-knob override file — absent = all defaults (`app/admin/debug_config.py`) |
| `data/config/optimizer.json` | Optimizer config — absent = built-in defaults (`app/optimizer/config.py`) |
| `data/config/remote_access.json` (+ `.bak`, `_pointers.json`) | Remote-access tunnel config (per-machine) |
| `data/config/deploy.json` | Deploy panel's non-secret per-target settings + last-deployment record (cloud keys are NOT here — they're in the vault and auto-discarded). See `app/deploy/store.py` |
| `data/config/production-mirror.json` | Production-mirror (dual-repo "Release to production") settings: production folder path, production remote URL, the dev-only **exclude list**, and the last-release record. No secrets (the GitHub token stays in `provider.json`). See `app/production_mirror.py` |
| `.env` | Local env vars |

**Checklist when adding a new backend write target:**

1. Is the file written by the app at runtime (not committed by a human)? → must be gitignored.
2. Does it contain secrets, hashes, tokens, or per-user state? → must be gitignored.
3. Does the test fixture or seed flow need a default? → ship a `*.example` or `*.template` variant that IS tracked, and have the app copy/derive from it on first boot.

If you skip this and a *should-be-ignored* file gets committed, the VM's edited copy will collide on every `git pull` and someone has to do the backup/restore dance documented above in **Production deployment**.

## First-boot security defaults (encryption on by default — safely)

A **fresh** deployment turns on the **OS-keyring secrets vault + per-tenant `field` encryption** automatically on first boot, so new installs protect stored credentials without anyone flipping a switch. This is a guarded one-time seed (`app/encryption/defaults.py`, called from `app.main:startup` next to the other first-boot seeds), **not** a change to the in-code fallback default (which stays `inline_db`/`none`).

It fires **only when every guard passes**, and is otherwise a clean no-op that leaves the install exactly as it was (re-seedable on a later boot):

- **not env-locked** (`WEBAGENT_CONFIG_SOURCE=env` installs configure the vault via env vars explicitly — the seed is skipped);
- **nothing decided yet** — neither `app/secrets_mode.json` nor `app/encryption_mode.json` exists (persisting them is the "already decided" marker, so an admin's later choice always wins and the seed never re-runs);
- **a real, secure OS keyring is present** (`keyring` resolves to a genuine backend — Windows Credential Manager / macOS Keychain / Secret Service — not the no-op `fail` backend or an insecure plaintext fallback) **and a live write/read/delete round-trip succeeds**;
- **the crypto dependency is present** (the `field` backend constructs);
- **the database has no stored secrets yet** (an empty `auth_elements`), so nothing existing is machine-bound or stranded.

Why guarded rather than always-on: `field` encryption keys live in **that machine's** OS keyring, not the DB. That's what makes it secure, but it means silently encrypting an *existing* install would bind credentials the admin never chose to encrypt — and a later DB restore on another box would strand them. The guards ensure encryption-by-default only ever lands where it's safe (fresh + keyring-capable), and headless servers with no keyring quietly stay on the plaintext default instead of failing to boot. **Operational caveat:** because the keys are machine-bound, a backup/restore or host migration must carry the OS-keyring entries too (master key at `wa:kek:active` under service `webagent_vault`), or back up the master key out-of-band — otherwise encrypted rows become unreadable on the new host.

## Full-database encryption at rest (SQLCipher) — opt-in, per-database

Separate from the per-tenant `field` encryption above (which encrypts only the credential `secret_ref` column): this encrypts each **entire SQLite database file** at rest — `local.db`, the attached `vault.db`, `logs.db`, `recordings.db`, `wiki.db` — chosen **per database** from App Config → Data Management → *Full-database encryption*. OFF by default.

How it works (`app/db/db_crypto.py` + `app/db/db_keys.py`):

- Each database file gets its own random 256-bit key. That key is **wrapped under the install KEK** (the same master key field encryption uses) and stored in the OS keyring at `wa:dbkey:<id>`. The vault is the single root of trust — the DB files hold no key material.
- Every SQLite connection site (`local._get_conn`, `logs_store._connect`, `wiki/db._connect`) routes through `db_crypto.connect()`, which picks the driver (SQLCipher when a file is/should-be encrypted, else stdlib `sqlite3`) and applies the key. **When nothing is enabled it is byte-for-byte the old `sqlite3.connect`** — zero behaviour change for installs that never turn it on.
- Toggling a database writes `app/db_encryption.json` (intent) and needs a **server restart**. At startup, `db_crypto.reconcile()` runs *before any store opens its files* and converts each file to match — plaintext→encrypted or back — **backup-first, atomic, with rollback and no plaintext residue** (the discarded plaintext is overwritten then deleted).
- Key application is driven by each file's **actual on-disk header**, not just config, so a config/file mismatch (e.g. enabled-but-not-yet-migrated) degrades safely instead of failing to open.

Requirements + guards: needs the optional **SQLCipher engine** (`sqlcipher3` on Windows / `sqlcipher3-binary` on Linux — see `requirements.txt`) **and** the `os_keyring` secrets vault. If either is missing the admin UI refuses to enable it (501, clear reason) rather than faking success. `wiki.db` defaults OFF and is flagged in the UI because it is git-tracked and served as public pages.

**Operational caveat (bigger than field encryption):** the keys are machine-bound in the keyring. With whole-file encryption on, losing the keyring loses **everything** in those files, not just credential secrets — so backing up the KEK (`wa:kek:active` under service `webagent_vault`), or carrying the keyring on a host migration, is **mandatory**. A headless host with no keyring cannot enable this.

## Writing JSON config — route through the shared helper

The app is meant to ship with **no `data/` folder** — config files materialize lazily, only when a setting first changes. To make that safe, **every JSON config writer goes through `app/util/config_io.py`**, never a raw `open(...)` + `json.dump`:

- **`safe_write_json(path, data)`** — whole-blob write. Creates `data/config/` (and any missing parent) on demand, then writes atomically (temp file + `os.replace`). A save into an absent `data/` self-heals instead of raising `FileNotFoundError`, and a crash mid-write can never leave a truncated config.
- **`set_config_key(path, key, value)`** — change **only** that key (or nested path) and leave every sibling key intact, creating the file if absent. Use this for a single-setting save (e.g. one user's provider config) so concurrent saves of *other* keys aren't clobbered.
- **`read_json(path, default)`** — the matching read: returns `default` (never raises) on a missing or corrupt file.

Do **not** hand-roll `open(...) + json.dump` for a new config writer — you'll reintroduce the missing-folder crash and the truncate-on-crash risk this helper exists to remove. Cached config modules (e.g. `app/admin/ability_config.py`, `app/admin/page_config.py`) keep their in-process cache and merge logic, but their final disk write still goes through `safe_write_json`.

## Shipping with no `data/` folder — where read-only seeds live

`data/` is **purely runtime state**: nothing the app *needs* at boot lives there, so the app can ship with `data/` entirely absent and self-heal (every config reader falls back to a built-in default; the writers above create `data/config/` on first change; the SQLite DBs and `wiki.db` create themselves; `data/wiki.db` is the one deliberately-tracked seed and is optional at runtime).

The read-only **defaults the app genuinely needs** are therefore kept **out of `data/` and bundled under `app/defaults/`** (tracked, always present):

- `app/defaults/app-prompts.json` — the system/UI prompt catalog (served by `app/api/features.py`; read by `loop.py`, `suggestions.py`, `system_prompt_fragments.py`, `tasks.py`, `diag_investigate.py`, `communications/auth.py`).
- `app/defaults/agents/*.json` — the default + system agent-template seeds.

Both are resolved through **`app/util/paths.py`** (`app_prompts_path()`, `agents_seed_dir()`), which prefers a `data/` copy if one exists (a deployment override) and otherwise uses the bundled `app/defaults/` copy. **Rule:** a read-only file the app needs at boot belongs in `app/defaults/` (resolved via `paths.py`), never in `data/`; only files the app *writes at runtime* belong in `data/`.
