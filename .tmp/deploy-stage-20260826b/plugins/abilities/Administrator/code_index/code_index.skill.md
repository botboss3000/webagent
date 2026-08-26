# Code Index — Index, Search, and Review the Codebase

You have the **Code Index** ability. It gives you a persistent, queryable index of the
entire codebase at `data/db/index.db`. Use it to find code without reading every file,
track indexing progress across sessions, and log improvement opportunities.

**The index is a living thing.** Every time you read or edit a file, you should check
whether the index still matches reality and update it if not. This keeps the index
accurate without requiring a full re-index. See **Workflow 5 — The Feedback Loop** below.

## The five tools

| Tool | Purpose |
|---|---|
| `index_lookup` | **Read** — search symbols, files, features, API routes, UI components, imports, comment-standard audits, opportunities, or get summary stats |
| `index_store` | **Write** — index one file with its symbols, imports, routes, components, and feature tags |
| `index_progress` | **Track** — create batches, update status, find already-indexed files (for resume) |
| `index_features` | **Grow** — list/register feature tags so the vocabulary grows organically |
| `index_opportunity` | **Observe** — log improvement findings (dead code, missing tests, duplication, etc.) |

All five tools operate on `data/db/index.db`, which is created automatically on first
use. The DB is a plain SQLite file — safe to inspect, backup, or delete and recreate.

---

## Workflow 1: Index the codebase (or a subset)

The user says *"index the api directory with 5 workers"* or *"index everything"*.

### Step 1 — Discover files that need indexing

Run a shell command to list candidate source files, then diff against already-indexed paths:

```
git ls-files -co --exclude-standard
```

Filter by extension: `.py` `.js` `.jsx` `.mjs` `.cjs` `.ts` `.tsx` `.html` `.css` `.json` `.sql` `.md`
Skip: `node_modules/`, `.git/`, `__pycache__/`, `dist/`, `build/`, `.venv/`, `vendor/`, `*.min.js`, `*.bak`

Then call `index_progress(action="pending")` to get the set of already-indexed paths.
Subtract those from the candidate list — what's left needs indexing.

Also check for **stale files**: files in `indexed_files` whose on-disk content has
changed (compare sha256). Those need re-indexing too.

### Step 2 — Batch into N chunks

Split the pending file list into N equal chunks. Each chunk = one indexer spawn's work.
Call `index_progress(action="start", batch_id=..., files=[...])` for each batch.

### Step 3 — Spawn indexer agents

For each batch, spawn a forked helper:

```
spawn_agent(
    task="Index these files: [list each file path]. For every file: ...",
    abilities=["codebase_admin", "code_index"],
    wait=False,
    check_back_minutes=2
)
```

Each indexer needs `codebase_admin` (to `read_source` / `search_source` the files)
and `code_index` (to `index_store` the results). If a spawn fails because it lacks
codebase_admin, the error tells the user — they can enable it on the orchestrator.

The spawn prompt should instruct the indexer to process each file in its batch:

1. **Read the file** — `read_source(path)`
2. **Extract structure** — symbols (functions, classes, methods), imports, API routes (FastAPI decorators like `@router.get("/path")` or `@app.post("/path")`), UI components (React/Vue/Svelte components, or significant HTML elements). The index automatically audits UI breadcrumb comments from the supplied `source`; do not classify them manually.
3. **Tag features** — call `index_features(action="list")` to see existing tags, pick the 1-3 best matches. Register a new tag via `index_features(action="register", ...)` if nothing fits.
4. **Write summary** — one line describing what the file does in plain language
5. **Call index_store** — `index_store(action="file", file_path=..., source=..., summary=..., symbols=[...], imports=[...], api_routes=[...], ui_components=[...], features=[...])`
6. **Note opportunities** — as it reads each file, look for: dead code, missing error handling, duplication, hardcoded values, missing tests, deprecated patterns, documentation gaps. Log any findings via `index_opportunity(action="log", ...)`.
7. **Report** — return a brief summary: files indexed, symbols found, features tagged, opportunities logged

### Step 4 — Collect results

As spawns finish, read their output via `read_spawn(spawn_id)`. Mark each batch as
completed via `index_progress(action="update", batch_id=..., status="completed")`.

If a spawn errors, mark the batch as `error` with the error message. You can retry
that batch later.

### Step 5 — Report to user

Summarize: total files indexed, features found, opportunities logged. Keep it concise.
Mention any spawns that failed and why.

---

## Workflow 2: Resume a partial index

Call `index_progress(action="list")` to see all batches and their statuses. Any
`in_progress` batch that's been running for more than 10 minutes probably stalled —
treat it as a pending batch and re-spawn it.

Then follow Workflow 1 starting from Step 1 — `index_progress(action="pending")` will
skip completed files automatically.

---

## Workflow 3: Search the index

The user asks *"where is build_system_prompt?"* or *"what files handle oauth?"* or
*"show me all API routes under /api/v1/agents"*.

Use `index_lookup`:

- **Symbol search:** `index_lookup(action="symbol", query="build_system_prompt")`
- **Feature filter:** `index_lookup(action="feature", feature="oauth")`
- **Route search:** `index_lookup(action="route", query="/api/v1/agents")`
- **Component search:** `index_lookup(action="component", query="ChatPanel")`
- **Import search:** `index_lookup(action="imports", query="app.agent.loop")`
- **Comment compliance:** `index_lookup(action="comments", comment_status="stale")`
- **Consistency markers:** `index_lookup(action="comments", marker="SISTER-PANEL")`
- **Header syntax inventory:** `index_lookup(action="comments", header_style="jsdoc")`
- **File details:** `index_lookup(action="file", file_path="app/agent/prompts.py")`
- **Summary stats:** `index_lookup(action="summary")`

Always use the index first. Only fall back to reading files directly when the index
result points to a file you need to inspect deeper.

---

## Workflow 4: Review breadcrumb and comment standards

Every indexed editable file under `ui/` is audited automatically against
`docs/claude/ui-guidance.md` when `index_store` receives its source.

The audit records:

- whether the opening purpose header is present and correctly ordered;
- the comment syntax currently used (`line`, `block`, `jsdoc`, `html`, or `hash`);
- every repo-relative breadcrumb target and whether that path exists;
- standardized markers: `REMOVE-WHEN`, `DEACTIVATED (intentional)`,
  `DEACTIVATED (orphaned)`, `KEEP (intentional)`, `SISTER-PANEL`, and
  `COLOR SCHEME`;
- the standard version used for the check.

Each indexed file also records its UTF-8 byte size and total line count. File lookup
returns both values, and summary lookup returns `total_size_bytes` and `total_lines`
across the index.

Audit statuses are:

- `compliant` — purpose header and at least one valid breadcrumb are present;
- `missing` — the opening comment is absent;
- `malformed` — the purpose line or breadcrumb path is missing;
- `stale` — at least one breadcrumb points to a path that does not exist;
- `exempt` — a documented alternate banner convention applies;
- `not_checked` — the file is in scope but source was not supplied;
- `not_applicable` — the file is outside the editable UI comment standard.

Examples:

```
index_lookup(action="comments", comment_status="missing")
index_lookup(action="comments", comment_status="stale", query="ui/main-panel/")
index_lookup(action="comments", marker="REMOVE-WHEN")
index_lookup(action="comments", header_style="line")
index_lookup(action="file", file_path="ui/shared/js/state.js")
```

JSON, Markdown, SVG, and other formats that cannot safely carry the standard source
comment are `not_applicable`. Vendored/minified files, background plugins, the
documented sister-panel banners, splash subsystem, and `cursor-effects.js` are
recorded as `exempt`, not false failures.

---

## Workflow 5: Review opportunities

The user says *"show me all opportunities"* or *"what needs fixing in the api layer?"*

```
index_lookup(action="opportunity", category="missing_error_handling")
index_lookup(action="opportunity", severity="high")
```

To act on one: read the file, diagnose, propose fix, apply it. Then mark it resolved:

```
index_opportunity(action="update", opp_id=42, new_status="resolved")
```

Dismiss false positives:
```
index_opportunity(action="update", opp_id=42, new_status="dismissed")
```

---

## Workflow 6 — The Feedback Loop (keep the index alive)

**This is the most important workflow for day-to-day work.** The index is only useful
if it stays accurate. Every time you touch a file — whether you're fixing a bug,
adding a feature, refactoring, or just reading to understand something — you should
check the index and update it if reality has drifted.

### When to update

Update the index entry for a file whenever you do any of these:

1. **Edit a file** — you changed code, so symbols, imports, routes, or features may have changed.
2. **Create a file** — it's brand new and has no index entry yet.
3. **Delete a file** — its index entry is now stale and should be removed.
4. **Read a file and notice the index is wrong** — the summary is outdated, symbols are
   missing or renamed, feature tags no longer apply, etc.

### How to update (the compare-and-fix cycle)

After you finish working on a file (not before — work first, index second):

1. **Pull the current index entry:**
   ```
   index_lookup(action="file", file_path="app/agent/loop.py")
   ```
   This returns the stored summary, symbols, imports, routes, components, and feature tags.

2. **Compare against what you now know.** You just read or edited the file, so you know
   its current state. Ask yourself:
   - Are the symbols still the same? Did you add/rename/remove any functions, classes, or methods?
   - Did imports change?
   - Did you add or remove an API route (e.g., a new `@router.post(...)`)?
   - Did you add or remove UI components?
   - Did the opening purpose header, breadcrumb paths, or standardized comment markers change?
   - Is the one-line summary still accurate?
   - Do the feature tags still apply? Did the file's purpose shift?

3. **If everything matches** — do nothing. The index is correct. Move on.

4. **If something drifted** — call `index_store` with `replace=true` and the updated data:
   ```
   index_store(
       action="file",
       file_path="app/agent/loop.py",
       source="<current file content>",
       summary="<updated one-line summary>",
       symbols=[...],      # updated list
       imports=[...],       # updated list
       api_routes=[...],    # updated list
       ui_components=[...], # updated list
       features=[...],      # updated tags
       replace=true         # ← wipes old entries for this file first
   )
   ```
   You don't need to re-extract every detail from scratch — just fix what changed.
   But `replace=true` ensures no stale symbols/imports linger.

5. **If you deleted the file** — remove it from the index:
   ```
   index_store(action="delete", file_path="app/agent/loop.py")
   ```

6. **If you spotted an opportunity** while working (dead code, missing error handling,
   duplication, etc.) — log it:
   ```
   index_opportunity(action="log", file_path="...", category="...", description="...")
   ```

### Lightweight version (for quick edits)

If you just fixed a typo or made a trivial change that didn't alter structure, you can
skip the full compare cycle. But if you added/removed/renamed a function, changed an
import, added a route, or shifted the file's purpose — update the index.

### What this achieves

   This feedback loop means the index gets smarter every time you work on the codebase.
You don't need a full re-index to keep it current — just update the files you touch.
Over time, the index converges on accuracy through normal work.

---

## Feature tag conventions

Tags are lowercase, underscore-separated. Seed list (already in the DB):

```
agent_loop, api, db, auth, browser, tools, ui, abilities, orchestration,
automation, scheduler, events, wiki, visualizer, genui, terminal, deploy,
email, github, oauth, billing, encryption, diagnostics, integrations,
relay, tui, webhooks, channels, devices, optimizer, storage, models
```

When an indexer encounters a file that doesn't fit any existing tag, it registers a
new one. Examples of organic growth: `compaction`, `rate_limiting`, `attachments`,
`notifications`. Tags should be specific enough to be useful, broad enough to cover
multiple files — aim for the same granularity as the seed list.

---

## Parallelism guidelines

- **Default to spawning.** Don't index files yourself — spawn indexers.
- **Batch size: 10-20 files per spawn.** Enough to amortize spawn overhead, small enough to complete in under 2 minutes.
- **Concurrency: as many as the user asks for.** If they say "5 workers", fan out 5. If they don't specify, spawn 3-5 based on how many files are pending.
- **For huge jobs:** fan out in rounds. First round: N spawns for the first N×batch_size files. Collect. Repeat for remaining files.

---

## Error recovery

- **Spawn lacks codebase_admin:** the error message will say so. Tell the user: "The indexer spawn needs codebase_admin to read files. Enable it on this agent's Abilities tab and I'll retry."
- **A file read fails** (permission, deleted): log it as an error in the batch, skip the file, continue.
- **The index DB gets corrupted:** the user can delete `data/db/index.db` and re-index from scratch — nothing else depends on it.
- **A spawn times out:** its batch stays `in_progress`. On next run, it'll be re-queued.

---

## What NOT to do

- Do NOT read every file yourself and process them inline — spawn indexers for bulk work.
- Do NOT hardcode a specific concurrency or batch size — ask the user or use reasonable defaults based on the file count.
- Do NOT index `*.bak`, `*.min.js`, `node_modules/`, `__pycache__/`, `.git/`, logs, or test fixture DBs.
- Do NOT write to `.ast-index/` — that's a separate, simpler AST-only index. The code index DB is at `data/db/index.db` only.
- Do NOT register a new feature tag for every single file. Reuse existing tags, and only create a new one when a genuinely new functional area emerges.
- Do NOT skip the feedback loop after edits — that's how the index stays alive. A stale index is worse than no index.
