# webAgent — agent instructions

**DO NOT work in a workspace. Update the main repo directly in `C:\Users\Alex R\Projects\webagent`.**

**webAgent** is the AI Agent Harness app in this repo (chat, tools, WebSockets, UI). Your job is to **edit and extend the webAgent codebase** (Python backend, `ui/` frontend, config). When the user talks about the **agent process**, **flow**, **memory**, **skills**, or **tools** in this project, they mean **this application**, not the Pi Agent, Cursor, Hermes, or Claude assistants.

Use **`README.md`** for architecture, module map, setup, env vars, and HTTP/WebSocket paths.

## Terminology

- **"Chat"** — The in-app chat UI or `POST /api/v1/chat`, not this IDE chat between you and the user.
- **"Agent"** — Unless they explicitly say something like **Pi Agent**, **agent** means webAgent's runtime (loops, WebSocket stream, tools), not the coding assistant.

## Explain logic, not code

The user is **NOT a coder**. When explaining how something works or proposing a change, explain in **logic and behavior** — what happens, why, in what order, what the data flow is. Avoid:

- Code blocks (Python, SQL, bash), function signatures, variable names, import statements, schema DDL syntax.

If code is needed to understand the logic (e.g. a schema change), describe the **shape** of the data and the **rules**, not the syntax. Use tables, bullet lists, flow descriptions, and plain English. When the user asks "show me" or "what would X look like", describe the steps and the outcome — don't print source files.

## Detailed guides — read the relevant one before you act

These hold the full rules. Open the one that matches your task **before** making changes in that area.

| If your task touches… | Read |
|------------------------|------|
| Anything under `ui/` or markup in `index.html` — theming (dark/light), edge-fade masks, Lucide icons, chat pills (shared design + full-height float layout), toggle-lists (shared category + option-rows design) | [docs/claude/ui-guidance.md](docs/claude/ui-guidance.md) |
| Adding / changing / renaming agent **loop nodes** or the loop diagram | [docs/claude/agent-loop.md](docs/claude/agent-loop.md) |
| Deploy config, OAuth / secure-context APIs, or any **file the app writes at runtime** (gitignore rules) | [docs/claude/deployment.md](docs/claude/deployment.md) |
| Docs upkeep (README + folder `.md`), where to put scratch/temp files, console-log cleanup, git push rules | [docs/claude/repo-conventions.md](docs/claude/repo-conventions.md) |
| **Editions** / production-path, drop-in plugin discovery, the **feature catalog** (`FEATURE` headers), ability-bundled skills | [docs/claude/production-editions.md](docs/claude/production-editions.md) |

> **Updating these docs — touch the expanded files, not this one.** This `CLAUDE.md` is a deliberately small at-a-glance index. When a rule changes or a new convention is added, **put the detail in the relevant `docs/claude/*.md` file** (or create a new one and add a row above). Only edit `CLAUDE.md` itself for genuinely minor, index-level changes — fixing a routing row, adding a new guide link, or tweaking a one-line essential below. If you find yourself writing more than a line or two here, it belongs in an expanded file.

**Always-on essentials from those guides:**

- **README sync:** any change to repo structure, config, routes, or usage → update `README.md` in the same work. (full table → repo-conventions)
- **Folder `.md` files:** read every `.md` in a folder before changing it; update them after. (→ repo-conventions)
- **Temp / scratch files:** put non-Markdown scratch and new Markdown drafts under `temp/`; never treat `temp/` as a source of truth. (→ repo-conventions)
- **Git pushes:** always promote to `main` via `git push origin HEAD:main` as part of the same step — don't stop at the feature branch. (→ repo-conventions)
- **UI in both themes:** every `ui/` feature must be correct in dark **and** light mode; use design-system CSS variables, never hard-coded hex. (→ ui-guidance)
- **Mirror the two ability tables:** the admin **Agent Settings** ability table and the per-agent **Abilities tab** (agent card) are sister panels that must stay **mirrored in design** — any change to one (look, structure, grouping, toggle behaviour) must be applied to the other in the same work. Both carry the embedded marker `SISTER-PANEL: AGENT-ABILITY-TABLE` (grep it). (→ ui-guidance)
