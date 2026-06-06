"""COPY ME to add a new agent ability.  ← read this whole file first.

────────────────────────────────────────────────────────────────────────────
THE PHILOSOPHY (why this folder exists)
────────────────────────────────────────────────────────────────────────────
webAgent is a small **core** plus many **drop-in plugins**. A new capability is
a NEW FILE in a plugin folder — never an edit to the core. The app discovers the
file at runtime, so you register nothing by hand. This is what lets a production
build ship only the tested features (see docs/claude/production-editions.md).

An **ability** is a host-side capability you can grant an agent (Codebase Admin,
Web Access, Terminal Control, ...). Each ability is ONE file in this folder. When
you drop it in, it automatically appears in:
  • the agent's tool gate          (app/tools/loader.py reads `tools` below)
  • the feature catalog / editions (reads `status` below)
  • the connections API            (app/api/agents.py)
  • the admin **Agent Settings** panel AND the per-agent **Abilities tab**
    (both fetch GET /api/v1/abilities/catalog and render generically)

So you do **NOT** edit any of those files. Do NOT add this ability to a list,
registry, or if/elif anywhere. Drop the file → it works. Delete the file → it's
gone. That is the entire contract.

────────────────────────────────────────────────────────────────────────────
HOW TO ADD ONE
────────────────────────────────────────────────────────────────────────────
  1. Copy this file to  plugins/abilities/<your_ability_id>.py
  2. Fill in the FEATURE dict below.
  3. Ship the tools. The CANONICAL way is SELF-CONTAINED — this file carries its
     own handlers, exactly like an integration carries its TOOLS:
       • add a module-level `build_tools(*, user_id, session_id, agent_id,
         agent_template_id, enabled_providers=None, **ctx) -> {name: handler}`,
       • add a module-level `TOOL_SCHEMAS` dict ({name: json-schema}) and, if any
         tool writes/deletes, a `DESTRUCTIVE` set of those names,
       • optionally `start_background()` / `stop_background()` for a long-lived
         service.
     Core discovers all of this GENERICALLY — the one block in app/tools/loader.py
     (search "Self-contained ability tools") calls your build_tools for every
     enabled ability and reads TOOL_SCHEMAS/DESTRUCTIVE AFTER the call, so you wire
     NOTHING in app/. A copy-paste skeleton is at the bottom of this file. Keep
     heavy imports LAZY (inside build_tools) so the FEATURE scan stays cheap; create
     any table you need lazily with CREATE TABLE IF NOT EXISTS (no core schema edit).
     See plugins/abilities/agent_orchestration.py for a full worked example.

     (Legacy shortcut — DECLARE-ONLY: if every handler you name in `tools` ALREADY
     lives in core, you may omit build_tools and just list the NAMES. Prefer
     self-contained for anything new so the ability stays a true drop-in.)
  4. That's it. (Files whose name starts with "_", like this one, are skipped.)

────────────────────────────────────────────────────────────────────────────
HOW GROUPS WORK  (groups are emergent — there is no master list to edit)
────────────────────────────────────────────────────────────────────────────
The `group` id you set below decides which UI bucket the ability sits in:
  • Use an EXISTING id  → the ability JOINS that group.
        existing ids: "administrator", "core",
                      "productivity", "web".
  • Use a NEW id        → a NEW group is CREATED automatically.
A brand-new group gets a default look (Title-cased name, neutral icon/colour).
To style it properly, set the optional `group_*` fields below — the first
ability that defines a new group's look wins. No core file changes either way.

See app/abilities/__init__.py for the full resolution rules, CLAUDE.md
("Core vs. plugins"), and the "Agent Abilities (drop-in)" wiki article.
"""

FEATURE = {
    # ── identity ──
    "id": "example_ability",            # stable id; defaults to the file stem
    "display_name": "Example Ability",
    "category": "ability",              # always "ability" for this folder
    "status": "experimental",           # stable | beta | experimental  (the production gate)
    "summary": "One-line summary for the feature catalog.",

    # ── runtime: the tool NAMES this ability unlocks (handlers live in core) ──
    "tools": [],                        # e.g. ["my_tool_a", "my_tool_b"]; [] is fine

    # ── UI: how the ability renders in BOTH ability panels ──
    "group": "core",                    # reuse an id (administrator/core/productivity/web) OR invent one
    "icon": "puzzle",                   # lucide icon name (https://lucide.dev/icons)
    "color": "#7aa2f7",                 # accent colour (a hex; design-system accents look best)
    "description": "What this ability lets the agent do, in one sentence.",
    "simple": True,                     # True = toggles directly; False = needs a config panel first

    # ── optional: ONLY when `group` above is a NEW id — style the new group ──
    # "group_label": "Communication",
    # "group_icon":  "message-circle",
    # "group_color": "#7aa2f7",
    # "group_desc":  "Reach people on messaging channels.",

    # ── optional bundled skill ──
    # A skill = a knowledge pack folded into the agent's `# [SKILLS]` catalog
    # whenever THIS ability is enabled. The agent always sees the one-line
    # "when to use it"; the full body loads only when needed:
    #   "skill_mode": "selectable"  → load-on-demand (the agent calls load_skill). DEFAULT.
    #   "skill_mode": "always_on"   → body injected every turn (use sparingly — costs context).
    # The body can be INLINE here, or in a separate file so long bodies stay out
    # of the .py — put it in  plugins/abilities/<this_id>.skill.md  (found by
    # convention; no field needed) or point at one with "skill_file".
    # The handle is minted ONCE and never regenerated (defaults to a stable hash
    # of the id if you omit it). `skill_summary` is the catalog "when to use it"
    # line shown for the skill — set it (separate from `summary` above, which is
    # the ability's tool blurb) when the skill needs its own load-me prompt;
    # otherwise it falls back to `summary`.
    # "skill": "Full how-to text the agent can load on demand…",   # OR a *.skill.md file
    # "skill_mode": "selectable",
    # "skill_handle": "example_ability_0000",
    # "skill_summary": "When to load this skill, in one line.",
}


# ─────────────────────────────────────────────────────────────────────────────
# SELF-CONTAINED TOOLS (canonical) — uncomment and fill in. Core discovers this
# generically; you wire nothing in app/. Keep the names in FEATURE["tools"] equal
# to what build_tools returns (that seeds each tool to "discoverable" on enable).
# ─────────────────────────────────────────────────────────────────────────────
#
# TOOL_SCHEMAS: dict = {}      # {tool_name: json-schema}; filled below or inline
# DESTRUCTIVE: set = set()     # names of tools that write/delete (need confirm)
#
# def build_tools(*, user_id="", session_id="", agent_id="",
#                 agent_template_id="", enabled_providers=None, **_ctx):
#     """Return {tool_name: handler} for this ability. Import handlers LAZILY here.
#     Return {} to inject nothing this call (e.g. when a broader ability wins)."""
#     async def my_tool_a(arg: str):
#         """One-line description the agent sees."""
#         return "…"
#
#     TOOL_SCHEMAS.clear()
#     TOOL_SCHEMAS.update({
#         "my_tool_a": {"type": "object",
#                       "properties": {"arg": {"type": "string"}},
#                       "required": ["arg"]},
#     })
#     # DESTRUCTIVE.update({"my_tool_a"})   # only if it writes/deletes
#     return {"my_tool_a": my_tool_a}
#
# If your handlers come from a core factory that also exports its schema/destructive
# constants, populate TOOL_SCHEMAS/DESTRUCTIVE from those INSIDE build_tools (so they
# never drift), e.g.:
#     from app.tools.my_factory import build_x, X_SCHEMAS, X_DESTRUCTIVE
#     handlers = build_x(user_id)
#     TOOL_SCHEMAS.clear(); TOOL_SCHEMAS.update(X_SCHEMAS)
#     DESTRUCTIVE.clear(); DESTRUCTIVE.update(X_DESTRUCTIVE)
#     return handlers
