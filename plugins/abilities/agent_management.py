"""Agent Management ability — drop-in. See app/abilities/__init__.py for the contract."""

FEATURE = {
    "id": "agent_management",
    "display_name": "Agent Management",
    "category": "ability",
    "status": "stable",
    "summary": "user-scoped agent CRUD + prompt/ability/tool edits.",
    "tools": ["list_agent_templates", "list_my_agents", "get_agent",
              "list_agent_tools", "create_agent", "update_agent",
              "set_agent_tool", "edit_agent_prompt",
              "set_agent_ability", "manage_agent_skills"],
    "group": "core",
    "icon": "users",
    "color": "#9ece6a",
    "description": "Lets the agent list, create, and update the user's own agents — their prompts, abilities, per-tool availability/permissions, and skills. On by default; switch off to remove it platform-wide.",
    "simple": True,
    # Bundled skill: a how-to that folds into the agent's # [SKILLS] catalog the
    # moment this ability is enabled (body lives in agent_management.skill.md).
    # 'selectable' keeps it lean — the agent sees the summary every turn and pulls
    # the full guide with load_skill the moment it actually manages an agent.
    "skill_mode": "selectable",
    "skill_handle": "agent_management_guide_v1",
    "skill_summary": "How to inspect, create, and fully configure other agents — "
                     "the field model, abilities vs skills vs tools, per-tool "
                     "availability/permission, and safe-edit workflow. Load this "
                     "before creating or editing any agent.",
}
