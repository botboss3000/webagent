#DO NOT work in a workspace. you are to update the main repo directly in C:\Users\Alex R\Projects\webagent

# Agent instructions for the webAgent workspace

**webAgent** is the AI Agent Harness app in this repository (chat, tools, WebSockets, UI). When the user talks about the **agent process**, **flow**, **memory**, **skills**, or **tools** in this project, they mean **that application**, not the Pi Agent, Cursor, Hermes, Claude assistants.

Your job here is to **edit and extend the webAgent codebase** (Python backend, `ui/` frontend, config).

## Terminology

- **"Chat"** — The in-app chat UI or `POST /api/v1/chat`, not this IDE chat between you and the user.
- **"Agent"** — Unless they explicitly say something like **Pi Agent**, **agent** means webAgent’s runtime (loops, WebSocket stream, tools), not the coding assistant.

Use **`README.md`** for architecture, module map, setup, env vars, and HTTP/WebSocket paths.

## README upkeep (required with code changes)

After **any** change that affects how the repo is structured, configured, or used, **update `README.md` in the same work** so it stays the single accurate guide. Do not leave README stale while landing features or refactors.

**Always review and adjust when your edit touches:**

| Area of change | Update in `README.md` |
|----------------|----------------------|
| New / removed / renamed **directories** or notable **root files** | **Directory tree** (abbreviated block) and any prose that lists those paths. |
| New / removed **`app/`** modules, routers, endpoints, WebSockets, mounts | **Architecture → Backend** table; **Features** bullets if behavior is user-visible; **Useful URLs** if routes or ports change. |
| **`ui/`** entry points, major tabs, or default DB / API strings | **Frontend** paragraph; tree; **Quick test** if flows change. |
| **`.env.example`**, config knobs, default model, DB mode / paths | **Environment variables** table and related sentences (e.g. local vs cloud, `local.db`, `db_mode.json`). |
| **Install / run** commands, Docker, scripts, ports | **Installation**, **Deployment**, tree (`scripts/`, `Dockerfile`, `webAgent.bat`), and **Useful URLs**. |
| **Cloud Run compatibility** — adding new Python deps, persistent storage assumptions, WebSocket origin checks, file write persistence | **`README.md` → Google Cloud Run checklist** — ensure new deps are in `requirements.txt`, no local-filesystem assumptions for persistent state, no loopback guards on WS endpoints. |
| **Migrations**, schema location, seeding / context behavior | Sections on **Installation** (Supabase / `migrations/`), **Context defaults**, **Adding custom context** as applicable. |

**Process:** Before you consider the task done, re-read the sections above and patch whatever is now wrong or missing. If the change is tiny (e.g. typo in a comment only) and **nothing** in the table applies, you may skip README—but default to **brief README sync** whenever behavior, paths, or layout moved.

## Folder .md files: read before act, update after change

**Before** modifying, analyzing, or creating files in any folder:

1. **Read every `.md` file** in that folder first. They contain context, instructions, config, and docs you need to follow.
2. **Apply what you learn** — if a `.md` file specifies an approach, use it.
3. **Recurse** — same rule for subdirectories you enter (skip `.git/`, `node_modules/`, `.venv*`, `__pycache__`, `.source-backups/`, `temp/`).

**After** completing work that touches any folder:

1. **Identify** all `.md` files in the affected folder(s) that describe the changed area.
2. **Re-read** them (content may now be stale vs your changes).
3. **Update** any stale descriptions, tables, paths, config keys, env vars, usage examples, or architecture notes.
4. **If a root `.md` file** (like `README.md` or this file) references the changed area, update it too.

## Temporary files (non-Markdown)

- Put **all temporary non-Markdown** artifacts here: scratch exports, one-off data dumps, downloaded samples, throwaway scripts output, ad-hoc logs, etc. — under **`temp/`**, unless the user explicitly authorizes a different path.
- **Do not read by default:** Do not treat **`temp/`** as reliable context for the codebase. Only read or search there if the user clearly asks you to.

## Markdown files (drafts, notes, scratch)

- **Where to write:** Put **new** Markdown you create in this workspace (analysis drafts, scratch specs, ad-hoc notes, roadmap drafts, etc.) under **`temp/`** (for example `temp/my-notes.md`), unless the user explicitly tells you to use a different directory or path.
- **Do not read by default:** **Do not** open, search, grep, or treat **`temp/`** as a source of truth for how the product works. It may contain obsolete, incorrect, or conflicting material that would confuse reasoning about the real codebase. **Only** read files there if the user clearly asks you to (e.g. “read my notes in `temp/…`”).

## Explain Logic, Not Code

The user is NOT a coder. When explaining how something works or proposing a change, explain in **logic and behavior** — what happens, why, in what order, what the data flow is. Avoid:
- Code blocks (Python, SQL, bash)
- Function signatures
- Variable names
- Import statements
- Schema DDL syntax

If code is needed to understand the logic (e.g. a schema change), describe the **shape** of the data and the **rules**, not the SQL/Python syntax. Use tables, bullet lists, flow descriptions, and plain English.

When the user asks "show me" or "what would X look like", describe the steps and the outcome, don't print source files.

## Agent loop diagram — keep both views in sync

The agent loop diagram (`ui/js/loop-diagram.js`) has **two completely independent, hardcoded layouts**. Adding or changing any node requires updating **both**:

| Layout | Location in file | Description |
|--------|-----------------|-------------|
| Horizontal | `LOOP_NODES` array + `LOOP_EDGES` | Used when the diagram is zoomed out |
| Vertical | Inline array inside `buildVerticalLayout()` | Used when the diagram is zoomed in |

Neither layout is derived from the other. A node missing from one view simply won't render there.

**Full checklist for adding a new loop node:**

1. `LOOP_NODES` array — add node with correct `cx`, `cy`, `hw`, `hh`
2. `LOOP_EDGES` — wire in/out edges
3. `_H_GROUPS` — add node ID to the correct stage group (controls horizontal compression)
4. `buildVerticalLayout()` inline array — add node with correct `cy`, `hw`, `hh`; shift all subsequent `cy` values down; extend stage band `y2`; update `canvasH`
5. Update the comment at the top of `buildVerticalLayout()` with the new node count
6. `ui/js/loop-logic.js` `eventToNodeId()` — add a `case` mapping the pipeline event name to the node ID
7. `ui/loop-nodes.json` — add entries to `NODE_STATIC_ITEMS` and `NODE_PANEL_INFO`
8. `ui/js/agents.js` `_INFO_NODES` set — add node ID if it should show info-only panel (no edit bar)
9. `app/agent/loop_executor.py` `DEFAULT_NODE_ORDER` list — add node in the correct position
10. All agent JSON templates in `app/context/agents/` — add node ID to each `loop_logic` array
11. Any existing agents' `loop_logic` DB field — update via migration or seed script

## Production deployment

The app runs on a **Google Cloud Compute Engine VM** (project `webagent-495517`, instance `webagent-development`, zone `us-central1-a`, static external IP `34.69.22.204`). It is **not** on Cloud Run, despite the codebase being Cloud-Run-compatible.

**Public URL:** `https://webagent.live` (also `www.webagent.live`). Domain registered at Namecheap. DNS: two A records (`@` and `www`) → `34.69.22.204` via Namecheap BasicDNS.

**Stack on the VM:**

| Layer | What | Where |
|-------|------|-------|
| TLS / reverse proxy | **Caddy 2.x** with automatic Let's Encrypt | `/etc/caddy/Caddyfile` — `webagent.live, www.webagent.live { reverse_proxy localhost:8080 }` |
| App server | **uvicorn** running `app.main:app` bound to `127.0.0.1:8080` (loopback only — Caddy is the only public ingress) | systemd unit `/etc/systemd/system/webagent.service` |
| GCE firewall | `allow-http-https` rule opens tcp 80, 443 to `0.0.0.0/0`. Port 8080 is **not** exposed publicly. | Created from Cloud Shell, not from VM SSH (VM SA lacks compute scope) |

**Repo on VM:** `~/webagent` (user `botboss3000`). Python venv inside. Deploy via `git pull` on `main`. If `app/db/local.db` blocks pull, `git stash push -- app/db/local.db` then pull.

**Google OAuth:** redirect URI must match the public HTTPS URL → `https://webagent.live/api/v1/oauth/callback/google`. JS origin `https://webagent.live`. OAuth never works against `http://<vm-ip>:8080`.

**Secure-context APIs (`crypto.randomUUID`, `crypto.subtle`, clipboard, etc.)** require HTTPS or `localhost`. Plain `http://<ip>:8080` will throw `crypto.randomUUID is not a function` and break `bindDom()` in `ui/js/state.js`, leaving the agent WebSocket dot stuck on yellow (`Connecting...`). The polyfill in `ui/js/uuid.js` handles non-secure contexts — use `randomUUID()` from there, not `crypto.randomUUID()` directly. Any new secure-context API added to the UI needs a similar fallback or must be guarded behind a feature check.

**Status dots in the chat header:** green = WS subscribed (`agentWs.js` got the `subscribed` event); yellow = WS opening or no subscribe reply yet; red = WS closed or `currentUserId` missing. Yellow that never goes green almost always means a JS exception during init prevented `currentUserId` from being set, or the WS handshake never completed — check the browser console first.

## Misc Directions

- **Console logs:** If adding console logs to investigate issue, remove the logging after the issue is resolved