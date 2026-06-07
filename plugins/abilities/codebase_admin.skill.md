# Working on the webAgent codebase

Attached to the **Codebase Admin** ability. Loads on demand — pull it in with
`load_skill` before any task that reads, edits, or extends this repository's
source. It tells you **how to find your way around and how to leave the code
better than you found it**. It does *not* restate the rules — those live in the
code and the docs; this skill points you to them.

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

The rule is the same every time: **the design is shared on purpose — change it
in one place and you must mirror it in every marked place.** Search the marker,
read each hit in full, apply the change everywhere. `docs/claude/ui-guidance.md`
explains the *why* behind each. (And remember tables and other repeated UI: if a
comment says formatting must be common/shared, it means common everywhere — find
the canonical block and reuse it, never fork a one-off copy.)

## Comment the destination: leave a signpost when you arrive

When a user request sends you navigating through the codebase to find a
specific spot, and you *arrive* at the file + function + line that answers
the question — leave a comment there. This comment is a signpost for every
agent that comes after you.

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
- **Promote to main:** finish by pushing with `git push origin HEAD:main` — or
  use the `commit_and_push` tool — don't stop at a feature branch.

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

## Repo map: where to find things

When the user asks about a specific UI feature, page, or backend component, use
this directory signpost to jump straight to the right area instead of guessing
from a flat listing:

| What the user is talking about | Where to look |
|---|---|
| Chat panel, message bubbles, input bar, streaming | `ui/chat.html` (HTML), `ui/js/chat.js` (logic), `ui/css/app1.css` (styling) |
| Session/agent list sidebar, pinning, renaming | `ui/js/sessions.js` (+ `ui/js/ordering.js`) |
| Main CSS, layout vars, dark/light modes | `ui/css/app1.css` |
| Page visualizer (rendered pages like home/dashboard) | `app/pages_store/` + `app/visualizer/` |
| Web terminal (TUI) — **separate** from the chat panel | `app/web-terminal/` |
| Python backend logic, agent loop, DB | `app/` (backing Python code) |
| API endpoints (chat, agents, auth, etc.) | `app/api/` |
| Agent loop, runner, prompt assembly | `app/agent/` |
| Tool implementations | `app/tools/` or `app/abilities/` |
| Plugin abilities (codebase admin, web access, etc.) | `plugins/abilities/` |
| Page visuals rendered in the browser | `app/pages_store/` |
| Host-level app control (show/hide chat panel, resize) | `app/tools/app_control_tools.py` |

**Discovery workflow — use this, not scatter-shot guessing:**

1. First `read_directory(depth=1, ".")` to see the top-level dirs.
2. Match the user's request to the table above and drill into the right dir.
3. If the table doesn't cover it, `search_source()` with domain-specific terms.
4. Only read a file after you've narrowed down — don't read multiple candidates
   in parallel hoping one is right.

## Self-improvement: keep this map accurate

The directory structure of this repo changes over time — files get moved,
renamed, or created. This map is **not authoritative**; it's a heuristic you
loaded at the start of a conversation. If you discover that a file you're
looking for is **not** where this map says it is (or the table sends you to the
wrong place), **update this skill file** (`plugins/abilities/codebase_admin.skill.md`)
with the corrected path so the next time you (or another agent) loads the
skill, the map is right. The file map is part of the codebase — keep it as
current as the code it describes.
