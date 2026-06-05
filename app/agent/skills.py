"""
On-demand agent skills — shared helpers.

A **skill** is a named knowledge pack defined per-agent (stored in the agent's
`metadata.skills` list, seeded from `data/agents/*.json`). Each skill is either:

  - **always_on**  — its full body is injected into the system prompt every turn
    (classic context-document behaviour), or
  - **selectable** — only its name + "when to use" description appear in the
    `# [SKILLS]` catalog. The agent calls the `load_skill` tool to pull in the
    full body, which then stays in the conversation transcript (and so in
    context) until the user deactivates it.

This module is the single source of truth for the skill shapes and the strings
that the prompt-builder, the tools, and the API endpoints all share, so an
edit to (say) the loaded-result header can't drift between producers/consumers.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.context.md_seeder import normalize_skills

# Key under which the active-skill name list is stored inside a session's
# `metadata` JSON blob. The active list drives (a) which selectable skills are
# excluded from the loadable catalog, and (b) the active-skill chips in the UI.
SESSION_ACTIVE_SKILLS_KEY = "active_skills"


def parse_agent_skills(agent: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return the normalized skill list for an agent dict (raw `agents` row).

    Tolerates `metadata` being a JSON string (DB row) or an already-parsed
    dict. Returns [] when the agent has no skills.
    """
    if not agent:
        return []
    meta = agent.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta) if meta.strip() else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
    if not isinstance(meta, dict):
        return []
    return normalize_skills(meta.get("skills"))


def loaded_result_text(skill: Dict[str, Any]) -> str:
    """The text `load_skill` returns (and persists) when a skill is loaded.

    Begins with a stable header line keyed by skill name so the deactivate
    path can find and neutralize this row later via a prefix match, while
    still reading naturally to the model.
    """
    body = (skill.get("body") or "").strip()
    return (
        f'SKILL "{skill["name"]}" LOADED — its full instructions follow and stay '
        f"active for the rest of this conversation:\n\n{body}"
    )


def loaded_result_prefix(name: str) -> str:
    """Stable prefix used to match a skill's load row for neutralization."""
    return f'SKILL "{name}" LOADED'


def deactivated_text(name: str) -> str:
    """Replacement content written over a load row when the user deactivates it."""
    return (
        f'[Skill "{name}" was deactivated by the user and is no longer in '
        f"context. Call load_skill again if it becomes relevant.]"
    )


def format_skills_section(
    skills: List[Dict[str, Any]],
    active_names: Optional[List[str]] = None,
) -> str:
    """Build the `# [SKILLS]` system-prompt block.

    - always_on enabled skills → full body inline.
    - selectable enabled skills not yet active → a one-line catalog entry the
      agent can `load_skill` on demand.
    - selectable enabled skills already active → listed by name only (their
      body is already in the transcript, so we don't duplicate it here).

    Returns "" when there are no enabled skills.
    """
    active = {n for n in (active_names or [])}
    enabled = [s for s in skills if s.get("enabled", True)]
    if not enabled:
        return ""

    always = [s for s in enabled if s.get("mode") == "always_on"]
    selectable = [s for s in enabled if s.get("mode") != "always_on"]
    loadable = [s for s in selectable if s["name"] not in active]
    loaded = [s for s in selectable if s["name"] in active]

    lines: List[str] = ["# [SKILLS]"]

    for s in always:
        body = (s.get("body") or "").strip()
        lines.append(f"\n## {s['name']} (always on)")
        if s.get("description"):
            lines.append(f"_{s['description']}_")
        if body:
            lines.append(body)

    if loadable:
        lines.append(
            "\nThe following skills are available. Each is a step-by-step "
            "playbook you do NOT see yet — call `load_skill` with its name to "
            "pull in the full instructions when a task matches:"
        )
        for s in loadable:
            desc = s.get("description") or "(no description)"
            lines.append(f"- **{s['name']}** — {desc}")

    if loaded:
        names = ", ".join(s["name"] for s in loaded)
        lines.append(
            f"\nAlready loaded this conversation (instructions are in the "
            f"transcript above — don't reload): {names}."
        )

    section = "\n".join(lines).strip()
    # Guard: if only always-on skills with empty bodies existed, the block is
    # just the header — drop it.
    return section if section != "# [SKILLS]" else ""
