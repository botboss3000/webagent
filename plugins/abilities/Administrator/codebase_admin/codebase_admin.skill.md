# Working on the WebAgent codebase

Attached to the **Codebase Admin** ability. Loads on demand — pull it in with
`load_skill` before any task that reads, edits, or extends this repository's
source. It tells you **how to find your way around and how to leave the code
better than you found it**. It does *not* restate the rules — those live in the
code and the docs; this skill points you to them.

## Destructive actions: who decides

File writes, edits, and deletes are **not hard-gated** for this ability — the app
trusts you to act on a clear instruction. The rule the app relies on:

- **The user directly told you to** create/overwrite/delete a specific file →
  just do it. No need to ask "are you sure?" for an explicit command.
- **You decided on your own initiative** to delete or overwrite something (a
  cleanup you weren't asked for, a file you think is stale) → **say what you'll
  remove and why, and ask first.** When unsure whether a file is dead, mark it
  (see the robustness markers below) rather than deleting silently.

Two things stay confirmation-gated regardless: **arbitrary shell commands**
(`run_command`) and **`restart_server`** — except read-only inspection commands
(`git status`, `ls`, `cat`, `python --version`, …) which run freely. So prefer a
purpose-built tool (`read_source`, `read_directory` — plus `git_tool` if the Git
Control ability is also enabled) over shelling out: it is both safer and never
blocked. The admin can tune how cautious you are
with the **"Caution on destructive actions"** setting on this ability.

## First move, always: read CLAUDE.md

Before you touch anything, read **`CLAUDE.md`** at the repo root. It is the
index, not the rulebook. It routes you — by the kind of task you're doing — to
the one `docs/claude/*.md` guide that holds the full rules for that area (UI,
agent loop, deployment, repo conventions, production editions). **Open that
guide before you change anything in its area.** Work from the guide, not from
memory — the rules change and the guides are the source of truth.

Two things from CLAUDE.md that are always true and easy to get wrong:

- **New capabilities are drop-in files, never core edits.** A new integration,
  connector, ability, channel, secrets vault, scheduler, etc. is its *own new
  file* in the matching plugin folder, carrying a `FEATURE = {...}` header — it
  is auto-discovered, you register it nowhere. Copy the nearest `_TEMPLATE.py`
  and fill it in. Never wire a capability into a central list / registry /
  if-elif. (CLAUDE.md → "Core vs. plugins".)
- **Every UI feature must be correct in dark AND light mode**, using
  design-system CSS variables — never hard-coded colours. (CLAUDE.md →
  ui-guidance.)

## Explore by comments first, then read the code

Comments in this repo are written as a **map**: a file's comments are its table
of contents, and shared rules are flagged with grep-able marker words. Use the
**`search_comments`** tool to read the map before you read the territory:

- **Outline a file before opening it whole** — `search_comments` with just a
  file `path` and no pattern returns that file's comments in order. In a few
  lines you learn what the file does and where each part lives, so you jump
  straight to the right spot instead of reading top to bottom.
- **Find a rule across the whole repo** — `search_comments` with a `pattern`
  returns only the comment lines that match, with no code-line noise. This is
  how you find every place a shared rule applies.

The tool is a **heuristic guide, not the truth** — it reads comment lines, not
the code itself. **When a hit matters, open the file and read the full comment
plus the surrounding code.** The complete instruction always lives there, in
place. The map points; the code tells.

## The breadcrumb comment standard (write the map as you go)

The map only works if every file feeds it. When you create or edit a file under
`ui/`, follow the **breadcrumb comment standard** (full rule:
`docs/claude/ui-guidance.md` → "Breadcrumb comment standard"):

- **Open every file with a purpose header** — one line `Title — what this is.`,
  then 1–4 lines of *what lives here / how it fits*, naming key sibling files as
  breadcrumbs (repo-relative real paths like `app/api/wiki.py`,
  `ui/shared/js/state.js`). A shared reminder like `COLOR SCHEME →` sits **below**
  the purpose, never instead of it.
- **Keep breadcrumb paths current.** After any move/rename, fix the pointers — a
  breadcrumb aimed at a path that no longer exists actively misleads the next
  agent. (Grep `ui/js/`/`ui/css/` to catch pointers left behind by the page
  restructure.)
- **Robustness markers — say what to remove and what to keep:**
  - `REMOVE-WHEN: <condition>` — delete this when the feature it serves is gone.
  - `DEACTIVATED (intentional|orphaned): <why>` — inactive but kept on purpose
    (or left by a half-done move); pair with `REMOVE-WHEN` if it should go.
  - `KEEP (intentional): <why>` — looks redundant/unused but must not be deleted
    (e.g. `ui/diagnostics.html`, `ui/test_interface.html`).
  Confirmed-dead code: **remove it** rather than leaving it silent; unsure: mark it.

## "This must match X" — the consistency markers

Some things in this app are built **once and deliberately repeated**, so they
must stay identical everywhere. Each such rule is planted in the code as a
marker comment. You are **not** expected to memorise the rules here — you are
expected to know the markers exist, **search for one when you touch its area,
read every instance's comment + code, and mirror your change across all of
them.**

| When your task touches… | Search this marker |
|---|---|
| A chat "pill" (rounded input row: attach / mic / send) on any page | `CHAT-PILL-SYNC` |
| The layout of a panel that hosts a floating pill | `PILL-PANEL-LAYOUT` |
| Two pills sitting side-by-side that must line up | `PILL-PANEL-ALIGN` |
| Either ability table (admin Agent Settings + agent-card Abilities tab) | `SISTER-PANEL` |
| A category-and-rows toggle list, or a collapsible config section | `SHARED EXPANDABLE OPTION-ROW LIST`, `SHARED COLLAPSIBLE SECTION PATTERN` |
| An inline-rename field (session title, genui page, terminal tab) | `RENAME-FIELD PATTERN` |
| A drag-to-scroll horizontal carousel (agent squares, tab bar) | `CAROUSEL-WIRING PATTERN` |
| HTML escaping, button factory, relative time formatting, status badges | `shared/dom-utils.js` |
| Any intentional-duplicate pattern not covered above | `PREVENTIVE-COMMENTS` — see `docs/claude/preventive-comments.md` |

The rule is the same every time: **the design is shared on purpose — change it
in one place and you must mirror it in every marked place.** Search the marker,
read each hit in full, apply the change everywhere. `docs/claude/ui-guidance.md`
explains the *why* behind each. (And remember tables and other repeated UI: if a
comment says formatting must be common/shared, it means common everywhere — find
the canonical block and reuse it, never fork a one-off copy.)

### Shared utility convention: `shared/dom-utils.js`

The file `ui/shared/js/dom-utils.js` is the canonical home for commonly
duplicated utility functions: `_esc` (HTML escaping), `_btn` (button factory),
`_fmtTime` (relative time), `_statusBadge`, `_typeIcon`, and `_enabledToggle`.

Never redefine these locally in a new file. Import from
`../dom-utils.js` (relative to a sibling in `ui/shared/js/`). If you need a similar utility
that isn't there yet, add it to the shared module and update all existing
callers — don't leave a private copy.

### Preventive-comments registry

The file `docs/claude/preventive-comments.md` is the registry of every
intentional-duplicate pattern in the codebase (RENAME-FIELD, CAROUSEL-WIRING,
SISTER-PANEL, etc.). Each pattern links to its locations and the rule for
keeping copies in sync.

**Use it in two directions:**

1. When you find a new duplicate pattern that cannot be extracted into a
   shared function (closures, state binding, etc.), add it to the registry
   and plant the invariant marker box (`╔═╗ PATTERN-NAME ═╗`) at every
   copy site.
2. When you fix a bug in a marked copy, check the registry for siblings
   and apply the same fix to all of them.

## Comment the destination: leave a signpost when you arrive

When a user request sends you navigating through the codebase to find a
specific spot, and you *arrive* at the file + function + line that answers
the question — leave a comment there AFTER you succesfully resolve the request. 
This comment is a signpost for every agent that comes after you.

Every destination comment must include:

1. **What this code does** — one or two lines describing the purpose, not
   the mechanics.
2. **The connection path** — how you got here. What files did you read
   along the way? What calls what? What data flows in/out?
3. **A searchable tag** — a `WHEN-<TOPIC>` keyword that an agent can find
   with `search_source` or `search_comments` later. This is the key that
   ties a future user request to this exact line.

Example of a good destination comment:

```python
# WHEN-CHANGE-CHAT-AREA: This is the spot for chat panel sizing and layout.
# The user's "chat area" is the right-side panel defined here. It connects
# from chat.js → app_control_tools.py → this CSS rule. Nav path:
# user said "chat area" → searched "chat" in ui/ → read chat.js line 42
# (pill init) → app1.css#L210 (panel layout). Change width/height here.
```

**Rules of thumb:**

- Only leave one of these when you've *arrived* at the code that directly
  answers the user's question — not for intermediate stops along the way.
- The tag `WHEN-<TOPIC>` must use plain terms a future agent might search
  for (e.g. `WHEN-CHAT-PILL`, `WHEN-AGENT-LIST`, `WHEN-DARK-MODE`).
- Include the navigation path concisely — enough that the next agent can
  retrace your steps or understand the connection.
- If you also changed something at the destination, update the comment to
  reflect the new behaviour.

## JS strict-mode discipline: every bare name must resolve

These `ui/` JS modules all carry `'use strict';`. In strict mode, every
bare-name reference must be declared (a `let`, `const`, `function`, or
`import`). A missing declaration is a `ReferenceError` crash.

**One missing declaration means there are more.** Bugs cluster in the
same file. When you find one, `grep -n '^let \|^const \|^var \|^function
\|^async function'` on that file first, then verify every bare-name reference
resolves to something in that list or an import. For every
`addEventListener('click', _loadEvents)`, confirm `function _loadEvents`
exists. For every `_parallelProviders.push()`, confirm `let _parallelProviders`
is declared.

**Always import from shared modules before defining locally.** The file
`ui/shared/js/dom-utils.js` is the canonical home for utility functions.
Before writing a new utility or a local no-op, check if the function already
ships in that module. If it does, add the `import` — don't write a local
copy. If it doesn't, add it there and wire the import in every caller.

**The stale-export trap:** When you extract a function into a shared module,
you must add the `import` to every file that calls it. The extraction is not
done until the imports are wired — grep for callers across the whole `ui/`
tree (`rg -rn "yourFunctionName" ui/`).

**Checklist before pushing any `ui/` JS change:**

- [ ] Does every bare-name reference in a `'use strict';` file resolve to a
      declaration, import, or `window.*` global?
- [ ] Have you grepped for other missing declarations after finding one?
- [ ] If you added a function to a shared module, did you add `import` to
      every caller?
- [ ] If you extracted code into `shared/dom-utils.js`, does
      `docs/claude/preventive-comments.md` need updating?

## Before you finish

- **README + folder docs:** any change to repo structure, routes, config, or
  usage → update `README.md` **and** the relevant folder `.md` in the same work.
  Read a folder's `.md` *before* you change the folder. (CLAUDE.md →
  repo-conventions.)
- **Only write to the repo what *is* the program.** Source code, program docs
  that live with the code (`README.md`, `CLAUDE.md`, files under `docs/`), and the
  real test suite (`tests/`) belong in the repo. **Everything else you produce —
  ad-hoc test scripts, scratch Python, analysis notes, supporting `.md` write-ups,
  reports, exports — is *not* part of the codebase: save it to the user's files
  with `save_file`** (the User Files ability), not into the project tree. Don't
  leave working files scattered in the root. (In-repo throwaway tied to a code
  change you're mid-way through may use `temp/`, which is never a source of truth —
  but a deliverable or supporting artifact for the user goes to `save_file`.)
- **Promote to main:** finish by pushing with `git push origin HEAD:main` (via
  `run_command`) — or, if the Git Control ability is also enabled, the one-shot
  `commit_and_push` tool — don't stop at a feature branch.

## Opening folders on the user's machine

When you want the user to browse a local directory in their OS file manager, don't
just print the path — **ask if they want it opened**, then use `run_python` to
call the platform-native opener:

| Platform | Python code |
|---|---|
| **Windows** | `os.startfile(r"C:\path\to\folder")` |
| **macOS** | `import subprocess; subprocess.run(["open", "/path/to/folder"])` |
| **Linux** | `import subprocess; subprocess.run(["xdg-open", "/path/to/folder"])` |

Always resolve relative paths to absolute first, and **always ask permission**
before opening anything on the user's machine. When they say yes, call
`run_python` with the appropriate one-liner.

## Duplicate code discipline: detect, extract, or document

Duplication scanners (suffix-array analysis, clone detectors) flag files that
share long identical blocks. Before adding new code to an area flagged as
duplicate:

### Step 1 — can it be a shared import?

If the duplicate is a pure function (no closure state, no DOM context binding),
extract it to `ui/shared/js/dom-utils.js` and import it in both files. This is
the first-choice resolution. The SISTER-PANEL files used to carry private copies
of `_esc`, `_iconHtml`, `_chevronSvg`, `_buildTriToggle`, `_wireTriToggle`, and
`_noop` — all of which are now imported from `shared/dom-utils.js`.

### Step 2 — if it must stay duplicate, use the clone-block format

When extraction would add unacceptable complexity (e.g., closures over
different state, DOM selectors that can't be parameterised), leave the
duplicate in place but **document it with the clone-group fingerprint** so
future developers can find all copies. Use this format in comments or
documentation:

```
XXX lines  Y instances  dup:<hash>
    ui/js/file-a.js:10-50
    ui/js/file-b.js:20-45
```

Add this to:
- The `preventive-comments.md` registry entry for that pattern
- A comment at each copy site, near the invariant marker box

### Step 3 — guard against drift

Every intentional-duplicate copy must carry the invariant marker comment box
(`╔═╗ PATTERN-NAME ═╗`). When you fix a bug in one copy, check the registry
for siblings and apply the same fix to all of them.

### Step 4 — watch for dead code in partial copies

When a pattern is duplicated but one copy is a subset (e.g., the agent table
omits admin-only features), functions that are **not called** in the subset
but are carried over from the superset are dead code. Remove them. The agent
file previously carried dead copies of `_buildMemberRow` and `_appendToggle`
that were only called from the admin file — these have been cleaned up.

Running `git diff` before committing is a good check: if you see a function
you added that isn't referenced anywhere else in the file, it's probably dead.

When the user asks about a specific UI feature, page, or backend component, use
this directory signpost to jump straight to the right area instead of guessing
from a flat listing:

| What the user is talking about | Where to look |
|---|---|
| Plugin abilities (codebase admin, web access, etc.), designed to be "plugin" capable by adding necessary files into plugins/abilities/ and would be self discoverable. UI shown in 2 places (Admin Tools > App Configuration > Agent Settings for app level control or Agents > abilities.js. It MUST be enforced that all ability related changes remain within /plugins/abilities directory and all new plugins follow existing methodology! | `plugins/abilities/` |
| Chat panel - has bubble transcript, header with sessions and footer with pill & buttons, message bubbles, input bar, streaming | `ui/chat-side-panel/chat-side-panel.html` (HTML), `ui/chat-side-panel/js/` (logic — split into `chat-*`/`session-*` modules, see its `README.md`), `ui/shared/css/app1.css` (styling) |
| Session/agent list dropdown in chat header, pinning, renaming | `ui/chat-side-panel/js/session-*.js` (+ `ui/shared/js/ordering.js`) |
| Main Panel CSS, layout vars, dark/light modes | `ui/shared/css/app1.css` |
| Admin Tools sidebar — all views live under the Admin Tools tab | `ui/admin-tools/` (each file is a `<template>` loaded into sidebar + main panel) |
| → File Explorer (folder-tree icon) | `ui/admin-tools/file-manager.html` — sidebar file tree + main editor with open-file tabs |
| → Source Control / Git (git-branch icon) | `ui/admin-tools/git/source-control.html` — sidebar status/diff list + main commit/diff/compare view |
| → Database (database icon) | `ui/admin-tools/database/database.html` — sidebar table list + main table viewer with pagination, reset, download |
| → Terminal Launcher (terminal icon) | `ui/admin-tools/terminal.html` — sidebar launcher/sessions list + main terminal tabs with xterm + keybar |
| → Interactions / Pipeline (square-menu icon) | `ui/admin-tools/interactions/interactions.html` — sidebar event-type filter + turn list + main pipeline visualizer |
| → Runtime Loop (repeat icon) | `ui/admin-tools/runtime-loop/runtime.html` — sidebar run-history scrubber + node list + main loop graph visualizer |
| → Diagnostics (stethoscope icon) | `ui/admin-tools/diagnostics/diagnostics.html` — sidebar severity/category filters + main searchable log feed with live refresh |
| → Admin Configuration (settings icon) | `ui/admin-tools/app-config/app-config.html` — multi-tab app config shell; each tab has its own `ui/admin-tools/app-config/<tab>/<tab>.html` |
| Page visualizer (rendered pages like home/dashboard) | `app/pages_store/` + `app/visualizer/` |
| Web terminal (TUI) — **separate** from the chat panel, defunct, not production, avoid coming here unless SPECIFICALLY requested | `app/web-terminal/` |
| Python backend logic, agent loop, DB | `app/` (backing Python code) |
| API endpoints (chat, agents, auth, etc.) | `app/api/` |
| Agent loop, runner, prompt assembly | `app/agent/` |
| Tool implementations | `plugins/abilities/` (ability tools) or `app/tools/` (host infra) |
| Page visuals rendered in the browser | `app/pages_store/` |
| Host-level app control (show/hide chat panel, resize) | `plugins/abilities/Core/app_control.py` |

**Discovery workflow — use this, not scatter-shot guessing:**

1. First `read_directory(depth=1, ".")` to see the top-level dirs.
2. Match the user's request to the table above and drill into the right dir.
3. If the table doesn't cover it, `search_source()` with domain-specific terms.
4. Only read a file after you've narrowed down — don't read multiple candidates
   in parallel hoping one is right.

## Console logs — always tag them for discovery

Every `console.log`, `console.debug`, `console.warn`, or `console.error` call
you add to the frontend **must carry a `DEBUG-TAG:` prefix** in its first
argument. This makes every temporary diagnostic line instantly findable across
the entire codebase with a single grep.

**Format:**

```javascript
console.debug('DEBUG-TAG:<module>-<what>', { ...context });
console.warn('DEBUG-TAG:<module>-<what>', { ...context });
```

**Rules:**

- The tag is `DEBUG-TAG:<module>-<what>` — kebab-case, unique within the file.
- The second argument is always an object with the diagnostic context (values,
  IDs, type checks).
- Never leave an untagged `console.log(...)` — it's invisible to future agents
  and indistinguishable from browser noise.
- When the diagnostic has served its purpose, delete it. Don't leave tagged
  logs in production code permanently.

**Finding them later:**

```bash
grep -rn "DEBUG-TAG:" --include="*.js" ui/
```

Every log is traceable to its exact line and purpose in one command.

## Self-improvement: keep this map accurate

The directory structure of this repo changes over time — files get moved,
renamed, or created. This map is **not authoritative**; it's a heuristic you
loaded at the start of a conversation. If you discover that a file you're
looking for is **not** where this map says it is (or the table sends you to the
wrong place), **update this skill file** (`plugins/abilities/Administrator/codebase_admin/codebase_admin.skill.md`)
with the corrected path so the next time you (or another agent) loads the
skill, the map is right. The file map is part of the codebase — keep it as
current as the code it describes.
