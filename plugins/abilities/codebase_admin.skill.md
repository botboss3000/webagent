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

## Leave comments that serve the next reader

As you add or change code, keep the map accurate — that is how this whole system
keeps working.

- **Top of a new file:** one line on what it is (and isn't).
- **Section headers** inside longer files, so the outline reads cleanly.
- **A new marker** if you create something that has siblings that must stay in
  sync — name it, say what must match, and point at the other copies.
- **Update the comment when you change the code it describes.** A stale comment
  is worse than none — it lies to the next reader (and to `search_comments`).

Acceptable vs not:

| Good — keep | Noise — don't |
|---|---|
| `Retries 3× because the payment API flakes on cold start` | `# loop over the list` (restates the obvious) |
| `SISTER-PANEL: keep in sync with the agent-card copy in agents.js` | A comment describing behaviour the code no longer has |
| `# [TOOLS] index — auto-generated, do not hand-edit` | Commented-out dead code left "just in case" |

## Before you finish

- **README + folder docs:** any change to repo structure, routes, config, or
  usage → update `README.md` **and** the relevant folder `.md` in the same work.
  Read a folder's `.md` *before* you change the folder. (CLAUDE.md →
  repo-conventions.)
- **Scratch / temp files** go under `temp/`, and `temp/` is never a source of
  truth.
- **Promote to main:** finish by pushing with `git push origin HEAD:main` — or
  use the `commit_and_push` tool — don't stop at a feature branch.
