# Production editions, drop-in features & the feature catalog

> **Status:** Phase 1 landing. This is the durable design + roadmap for turning
> webAgent into a single codebase that can ship as a **production** edition
> (only vetted features), a **full** edition (everything), or any **fork**
> (a custom mix) — by treating every capability as a self-describing, drop-in
> plugin that the app discovers at runtime.

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
| Scheduler providers | `app/scheduler/providers/` | ⚠️ registry edit |
| Data connectors | `app/connectors/` | ⚠️ registry edit **+ DB schema CHECK** |
| Encryption methods | `app/encryption/` | ⚠️ registry edit |
| Payment processors | `app/billing/processors/` | ⚠️ registry edit (forgiving) |
| Secrets vaults | `app/secrets/` | ⚠️ registry edit |
| Storage backends | `app/db/` | ⚠️ mode switch |
| Built-in tools (web search…) | `app/tools/` | ❌ hard-imported at boot |

**Phase 2** converts the ⚠️ rows to the same folder-scan pattern the ✅ rows use
(the connectors' DB-schema coupling is the fiddliest single item). **Phase 3**
demotes the ❌ built-in tools to drop-in add-ons (web search is the reference
example). **Phase 4** defines the `production` edition + the packaging step.

---

## Phases

1. **Foundation (no behavior change)** — header format, discovery catalog,
   edition manifest (default `full`, no gating), `GET /api/v1/features`, the
   App Config Features report, and headers on the 3 already-drop-in subsystems.
2. **Unify Tier-2 to drop-in** — convert the registry-based subsystems to
   folder-scan; merge ability skills into the prompt; mint skill handles; relax
   the connector DB-schema coupling. Keep SQLite / inline-secrets / none-encryption
   hard-wired as the irreducible fallbacks.
3. **Demote built-in tools to add-ons** — move web search & friends out of the
   boot-time hard imports into the discovered add-on set; shrink core to the loop
   essentials.
4. **Cut the production edition** — define `production` = stable-only, build the
   packaging step, document how to promote a feature (flip its `status`).

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
