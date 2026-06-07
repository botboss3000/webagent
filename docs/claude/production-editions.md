# Production editions, drop-in features & the feature catalog

> **Status:** Phases 1–6 landed. This is the durable design + roadmap for the
> single codebase that ships as a **production** edition (only vetted features),
> a **full** edition (everything, the default), or any **fork** (a custom mix) —
> by treating every capability as a self-describing, drop-in plugin the app
> discovers at runtime and gates by edition.

This doc is the source of truth for the work. The at-a-glance index lives in
`CLAUDE.md`; the detail lives here.

---

## The problem this solves

The app ships with many capabilities that vary in maturity — some integrations,
encryption methods, payment processors, storage backends, etc. are battle-tested,
others are experimental stubs. We want:

1. A **production** release that contains/loads **only tested** features.
2. A **full / dev** build that has everything.
3. **Forks** that are any mix of the two.
4. Adding or removing a capability to be, ideally, **"copy a file into a folder"**
   (or delete it) — no central registry edits.
5. The app to be able to **discover what code is available to it** and report it.

---

## The core model: three things travel together

Every capability is a **feature**. A feature is a self-contained file (or small
folder) that carries three things:

| Part | What it is |
|------|-----------|
| **Tools** | the actions the agent can take (already how integrations/events/etc. work) |
| **Self-description header** (`FEATURE`) | id, display name, category, **status** (stable / beta / experimental), what it requires, and an optional **skill** |
| **Skill document** *(optional)* | the full how-to for the feature, injected into the agent on demand |

Drop the file in → its tools **and** its know-how appear. Delete it → both vanish.

### The `FEATURE` header

Each plugin module may expose a module-level `FEATURE` dict. All fields are
optional except an implied id (defaults to the file name):

- `id` — stable machine id (defaults to the module's file stem).
- `display_name` — friendly name for the UI.
- `category` — `integration` / `event_source` / `channel` / `connector` /
  `scheduler` / `encryption` / `payment` / `secrets` / `storage` / `tool`.
- `status` — `stable` / `beta` / `experimental`. **This is the production gate.**
  A file with no header is treated as `unknown` (and surfaced as such) so it is
  never silently assumed production-ready.
- `summary` — one line shown in the catalog.
- `requires` — list of human-readable prerequisites ("Google OAuth credentials").
- `skill` / `skill_mode` — see *Ability-bundled skills* below.

The header is **additive metadata** — the existing discovery contracts
(`TOOLS`, `source_cls`, `plugin_cls`, the hand-listed registries) are untouched,
so adding a header never changes how a feature loads.

---

## Editions

An **edition** decides which features are actually switched on.

- **full** — everything (dev / internal build). **This is the default.**
- **production** — only features marked `stable` (plus any explicitly named).
- **fork-X** — any custom mix.

The active edition is resolved from the `WEBAGENT_EDITION` env var (falling back
to `full`). The edition definitions live in `app/features/editions.json`
(committed; code has a built-in default if the file is absent).

**Hybrid cut (chosen):**

- **Runtime** — at startup the app reads the active edition and (eventually) only
  activates the features that edition allows. Flipping editions is a config
  change, not a rebuild. Dropping a file in works live.
- **Packaging (later)** — an optional build step physically strips the
  non-included files when cutting a real production artifact, so experimental
  code isn't even shipped.

> **Phase 1 does NOT gate anything.** The edition is read and *reported* only.
> Default `full` means every feature still loads exactly as today. Enforcement
> arrives in a later phase, behind the edition switch.

---

## The discovery catalog

`app/features/catalog.py` builds one **catalog** by scanning every plugin folder
and reading each feature's header. For each feature it reports: id, display name,
category, status, whether it's **drop-in** (true folder-scan) or **registry**
(needs a central edit today), whether it carries a header yet, what it requires,
and — once gating is on — whether the active edition includes it and **why not**
if excluded.

This catalog *is* the "what code is available to it" discovery the design calls
for. It's exposed admin-only at **`GET /api/v1/features`** and rendered in
**App Config → Features**.

The catalog is built **lazily and fully guarded** — a broken plugin can never
break boot or the endpoint; it just shows up as an error row.

---

## Ability-bundled skills

The app already has an on-demand **skill** system: a skill has a name, a
"when to use it" line (always shown), and a body (shown only after the agent
calls `load_skill`, unless marked always-on). See `app/agent/skills.py`.

A feature/ability can **carry its own skill**. When the ability is enabled for an
agent, its skill is folded into the agent's `# [SKILLS]` catalog automatically.
The agent always sees the "when to use" line; the moment it wants to use the
ability it loads the full body. **Default is load-on-demand**; a feature may mark
its skill always-on when the guidance is short and essential.

This co-locates the **"what" (tools)** with the **"how" (skill)** so they can
never drift apart, and means exploring an ability teaches the agent everything
about it.

## Companion `<ability>.json` — attachment-handling abilities

An ability that handles **uploaded files** ships a third package file beside its
`.py` and `.skill.md`: a companion **`<ability>.json`** (e.g.
`plugins/abilities/image_vision.json`). It declares, as pure data:

- `handles` — the mime-types / categories it routes (`"image/png"`, `"image/*"`,
  or a bare category like `"image"`);
- `worker_system` (+ optional `worker_instruction`) — the system prompt for the
  one-shot worker that reads the file, with `{context}` / `{request}` placeholders
  so the description is tailored to the conversation;
- `guidance` — the copy folded into the user turn: `switch_available` /
  `describe_only` (when the file was handled) and `not_enabled` (the
  anti-hallucination fallback when the ability is off or unconfigured).

The **attachment type-router** (`app/agent/attachment_router.py`) scans these JSONs
and maps each upload to its owning ability by mime-type — so adding a new file-type
ability (document, video, audio …) is **drop-in**: ship the `.py` + `.skill.md` +
`.json`, with **no** edit to the router or the `attachment_describe` loop node. See
[agent-loop.md](agent-loop.md) for the node's routing logic.

### Skill ids (collision-proof + stable)

- **Agent-authored skills** keep using the **name** as their identity. The save
  path rejects a duplicate name on the same agent, so a random suffix is
  unnecessary. Loaded/active tracking stays name-based — unchanged behavior.
- **Ability-contributed skills** get a unique handle minted **once** and stored
  with the ability — e.g. `automation_fj27enfiu287`. It is **random for
  uniqueness but frozen for stability**: never regenerated, so a loaded
  ability-skill still matches itself across restarts and conversation replay.
- The two live in separate id spaces (friendly names vs minted handles), so they
  can never collide; two abilities that both ship an "Email" skill simply get two
  different handles. The agent loads its own skills by name and ability skills by
  handle, so the call handle itself disambiguates; the catalog shows each skill's
  exact handle plus a friendly display name (with a source tag to break ties).

> Skill *wiring* (merging the two sources into the prompt, minting handles,
> teaching `load_skill` to find ability skills) is Phase 1/2 work. Phase 1 only
> reserves the `skill` / `skill_mode` fields in the header so the format is ready.

---

## What stays permanently core (never editioned out)

Making these optional would just be a way to brick the app:

- **Agent-loop essentials** — the tool-discovery tools (`load_tool`,
  `list_skills`, `load_skill`), `get_time` / `get_date` / `calculate` /
  `read_attachment` / `register_user`, the DB-query tool, and memory.
- **SQLite local storage** — the universal fallback every other backend falls
  back to.
- **Inline-DB secrets** — the bootstrap vault that unlocks every other vault.
- **"None" encryption** pass-through and **billing-absent = free** — already the
  defaults.

Everything else — including **web search** — becomes a demotable add-on.

---

## Current modularity scorecard (what Phase 2 fixes)

| Subsystem | Folder | Today |
|-----------|--------|-------|
| OAuth integrations | `app/integrations/` | ✅ drop-in (folder scan) |
| Event sources | `app/events/sources/` | ✅ drop-in (folder scan) |
| Communication channels | `app/communications/plugins/` | ✅ drop-in (folder scan) |
| Scheduler providers | `app/scheduler/providers/` | ✅ drop-in (drop a file with a `PROVIDER` dict) |
| Data connectors | `app/connectors/` | ✅ drop-in (auto-discovered; DB `type` CHECK relaxed) |
| Encryption methods | `app/encryption/` | ✅ drop-in (auto-discovered by `FEATURE.id`) |
| Payment processors | `app/billing/processors/` | ✅ drop-in (drop a file with `processor_cls`) |
| Secrets vaults | `app/secrets/` | ✅ drop-in (auto-discovered by `cls.name`) |
| Storage backends | `app/db/` | ⚠️ mode switch (irreducible-fallback heavy; left as-is) |
| Built-in tools (web search…) | abilities | ✅ catalogued + editionable (web search via `web_access`) |
| **Agent abilities** | **`plugins/abilities/`** | ✅ **drop-in + self-contained tools** (one file per ability carries its `FEATURE`, its own `build_tools()` handlers, `TOOL_SCHEMAS`/`DESTRUCTIVE`, and any background service; both ability panels + the loader's generic discovery + the catalog all read it — no per-ability wiring in core) |

> **The root `plugins/` tree.** As of the abilities refactor the canonical home
> for drop-in capability files is a top-level **`plugins/`** folder (sibling to
> `app/`), so "plugin vs core" is structural, not just convention. **Abilities
> live there now** (`plugins/abilities/`). The other subsystems above still sit
> under `app/` and are migrating into `plugins/` one at a time (each move keeps
> its core manager/base class in `app/` and lifts only the drop-in leaf files
> out). Until a subsystem is migrated, add its plugins in the `app/…` folder
> shown above.

**Done:** Phase 2 converted the registry subsystems to folder-scan discovery
(additive — the irreducible fallbacks stay hard-wired) and relaxed the connector
DB-schema coupling; ability-bundled skills are wired (see above). Phase 3 made
the built-in abilities (incl. web search via `web_access`) catalogued and
editionable. Phase 4 wired **edition gating** at every load site and added the
packaging step. Storage selection was intentionally left on its mode-switch
(every backend falls back to it, so folder-scan there buys little and risks the
fallback) — new storage backends still register via discovery for the catalog.

---

## Phases (all landed)

1. ✅ **Foundation** — header format, discovery catalog, edition manifest
   (default `full`), `GET /api/v1/features`, the App Config Features report,
   headers on the 3 drop-in subsystems.
2. ✅ **Unify Tier-2 to drop-in** — secrets/encryption/payments/scheduler/
   connectors now auto-discover (additive; fallbacks intact); connector DB CHECK
   relaxed; ability-bundled skills wired with minted handles.
3. ✅ **Demote built-in tools to add-ons** — abilities (incl. web search via
   `web_access`) are catalogued and editionable.
4. ✅ **Cut the production edition** — edition gating wired at every load site
   (default `full` = no-op) + `scripts/build_edition.py` packaging step.
5. ✅ **Abilities become drop-in + root `plugins/` tree** — each host ability is
   now one self-describing file in `plugins/abilities/` (declaring the tools it
   gates + its render metadata). `app/abilities/` is the core manager; the loader
   map, the feature catalog, `app/api/agents.py`'s connection rows, and **both**
   ability panels (`ui/js/app-config.js`, `ui/js/agents.js`) all read it via
   `GET /api/v1/abilities/catalog`. No per-ability constants remain in those
   files.
6. ✅ **Abilities ship their own tools (fully self-contained)** — every
   tool-bearing ability now carries its handlers in its own file via a
   `build_tools(*, user_id, session_id, agent_id, agent_template_id,
   enabled_providers=None, **ctx)` hook, plus module-level `TOOL_SCHEMAS` and
   `DESTRUCTIVE`. The loader has ONE generic discovery block (search
   "Self-contained ability tools") that calls every enabled ability's
   `build_tools` and reads its schemas/destructive set afterward; all the old
   per-ability `if "<id>" in enabled_providers:` wiring blocks were removed.
   Heavy handler logic that stays in core lives in factory files
   (`app/tools/wiki_tools.py`, `terminal_tools.py`, `automation_tools.py`,
   `agent_mgmt_tools.py`) or the admin adapter (`plugins/admin/adapter.py`); the
   ability file just wraps them and mirrors their constants. Adding/removing a
   tool-bearing ability now needs **zero** loader edits. **Next:** migrate the
   remaining `app/…` plugin subsystems into `plugins/` (managers/base classes
   stay in `app/`; only leaf files move).

## Promoting a feature / cutting a build

- **Promote a feature:** edit one word — its `FEATURE['status']` from
  `experimental`/`beta` → `stable` (or name it in an edition's `include` list in
  `app/features/editions.json`). Add a `FEATURE` header to any still-`unknown`
  plugin so it's never assumed production-ready by accident.
- **Run as an edition:** set `WEBAGENT_EDITION=production` (or `beta`, or a fork
  name). The running app then loads only that edition's features; `full` (default)
  loads everything. Watch the result in **App Config → Features**.
- **Cut a code-absent artifact:** `python -m scripts.build_edition production
  ../webagent-production` copies the repo and physically removes the drop-in
  plugin files the edition excludes (registry subsystems + abilities are
  runtime-gated, so their files stay but don't load).

### Gating chokepoints (where the edition is enforced)
- **Integrations** — `inject_integration_tools` skips excluded modules.
- **Channels / Event sources** — the managers' `get_enabled_plugins()` /
  `enabled()` filter on each plugin's stashed `FEATURE`.
- **Abilities** — the loader filters `enabled_providers` through
  `gating.ability_enabled` (one chokepoint gates every ability's tools).
All are **fail-open** (unknown → kept) and **no-op for `full`**.

---

## Key files (Phase 1)

- `app/features/descriptor.py` — the `FEATURE` shape + normalization + reading a
  header off a module.
- `app/features/editions.py` — edition definitions + active-edition resolution.
- `app/features/editions.json` — committed edition manifest.
- `app/features/catalog.py` — scans the plugin folders, builds the catalog.
- `app/api/features.py` — `GET /api/v1/features` (admin-only).
- `ui/js/app-config.js` + `ui/admin-tools/admin-configuration.html` — the
  Features report panel.
