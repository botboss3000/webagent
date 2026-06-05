"""
Per-agent tool exposure modes — the single source of truth for *how* a tool is
presented to the model each turn.

Background
----------
Every tool the agent is allowed to use (after ability gating) is loaded into the
tool set. But not every tool's full JSON schema needs to be sent to the model on
every turn — that costs context tokens and, historically, encouraged the model
to "discover" tools it didn't have. Instead each tool has a **mode**:

  - ``core``         — fundamental, always sent, NOT toggleable. The discovery /
                       activation tools live here (without ``load_tool`` the
                       model could never reach anything else).
  - ``always``       — full schema sent every turn. This is the default for any
                       tool with no explicit per-agent setting, so existing
                       agents behave exactly as before.
  - ``discoverable`` — only the name + one-line description appear in the
                       generated ``# [TOOLS]`` index. The full schema is withheld
                       until the model calls ``load_tool("<name>")``, which marks
                       it active for the rest of the session.

The per-agent overrides live in ``agents.metadata`` under ``tool_modes`` —
a flat ``{tool_name: "always" | "discoverable"}`` map. The session's
``active_tools`` list (in ``sessions.metadata``) records which discoverable
tools the model has loaded so far. Both are written by dedicated DB methods.

This module is intentionally dependency-light: it imports the built-in tool
metadata lazily so it can be used from the loop, the loader, and the API layer
without creating import cycles.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

# ── Core tools: always sent, never toggleable ──────────────────────────────────
# Keep this set tiny — only the meta tools the agent cannot function without.
# load_tool is what makes every discoverable tool reachable; list_skills /
# load_skill are the equivalent discovery/activation tools for skills.
CORE_TOOLS = {"load_tool", "list_skills", "load_skill"}

# Valid explicit modes a user can pick for a non-core tool.
TOGGLEABLE_MODES = ("always", "discoverable")

# Key under which the per-agent tool-mode map lives in agents.metadata.
AGENT_TOOL_MODES_KEY = "tool_modes"

# Key under which the active (loaded) tool-name list lives in sessions.metadata.
SESSION_ACTIVE_TOOLS_KEY = "active_tools"


def resolve_mode(name: str, modes_map: Optional[Dict[str, str]]) -> str:
    """Resolve a tool's effective mode for an agent.

    core            → if the tool is in CORE_TOOLS (locked).
    always/discoverable → the agent's explicit setting, when present and valid.
    always          → the hybrid default for any tool with no explicit setting,
                      so nothing an agent already had silently stops being sent.
    """
    if name in CORE_TOOLS:
        return "core"
    explicit = (modes_map or {}).get(name)
    if explicit in TOGGLEABLE_MODES:
        return explicit
    return "always"


def is_sent(name: str, modes_map: Optional[Dict[str, str]],
            active_names: Optional[Iterable[str]]) -> bool:
    """True when this tool's full schema should be sent to the model this turn.

    Sent when it is core/always, OR when it is discoverable and the model has
    already loaded it this session (so a freshly loaded tool becomes callable on
    the next turn).
    """
    mode = resolve_mode(name, modes_map)
    if mode in ("core", "always"):
        return True
    return name in set(active_names or ())


def is_locked(name: str) -> bool:
    """True when the tool's mode cannot be changed (core tools)."""
    return name in CORE_TOOLS


# ── Ability ↔ tool mapping (derived from BUILTIN_TOOL_METADATA) ────────────────

def ability_for_tool(name: str) -> Optional[str]:
    """The ability that gates a built-in tool, or None for always-on utilities /
    DB tools / unknown names. Reverse lookup over loader.ABILITY_TOOLS."""
    try:
        from app.tools.loader import ABILITY_TOOLS
    except Exception:
        return None
    for ability, names in ABILITY_TOOLS.items():
        if name in names:
            return ability
    return None


def tools_for_ability(ability: str) -> List[str]:
    """All built-in tool names an ability provides — used to seed 'discoverable'
    defaults the moment an ability is toggled on for an agent."""
    if not ability:
        return []
    try:
        from app.tools.loader import ABILITY_TOOLS
    except Exception:
        return []
    return list(ABILITY_TOOLS.get(ability, []))


# ── Prompt index rendering ─────────────────────────────────────────────────────

def render_index(entries: List[Dict]) -> str:
    """Build the ``# [TOOLS]`` system-prompt block from the agent's loaded tools.

    ``entries`` is a list of ``{name, desc, mode, active}`` dicts (one per loaded
    tool). Tools that are core/always (or already loaded) are listed as ready to
    use; discoverable-but-unloaded tools are listed under a load-on-demand
    heading that points the model at ``load_tool``. Returns "" when there are no
    tools at all.

    The catalog is generated from the real tool set, so it can never name a tool
    the agent doesn't have, and authors never hand-maintain it.
    """
    if not entries:
        return ""

    ready: List[str] = []
    loadable: List[str] = []
    for e in entries:
        name = e.get("name") or ""
        if not name:
            continue
        desc = (e.get("desc") or "").strip().split("\n")[0]
        line = f"- `{name}` — {desc}" if desc else f"- `{name}`"
        mode = e.get("mode")
        if mode == "discoverable" and not e.get("active"):
            loadable.append(line)
        else:
            ready.append(line)

    if not ready and not loadable:
        return ""

    lines: List[str] = [
        "# [TOOLS]",
        "These are the tools available to you on this agent. Tools under "
        "“Ready to use” can be called directly right now.",
    ]
    if ready:
        lines.append("\n## Ready to use")
        lines.extend(sorted(ready))
    if loadable:
        lines.append(
            "\n## Load on demand"
            "\nThese tools are available but their full input schema is withheld "
            "to keep your context lean. Call `load_tool(\"<name>\")` the moment a "
            "task needs one — its parameters arrive immediately and it stays "
            "callable for the rest of this conversation. Don't guess a tool's "
            "arguments before loading it, and never call a tool that isn't listed "
            "here."
        )
        lines.extend(sorted(loadable))

    return "\n".join(lines).strip()
