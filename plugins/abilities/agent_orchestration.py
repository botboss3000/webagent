"""Agent Orchestration ability — drop-in. See app/abilities/__init__.py for the contract."""

FEATURE = {
    "id": "agent_orchestration",
    "display_name": "Agent Orchestration",
    "category": "ability",
    "status": "beta",
    "summary": "delegate to another agent + run the optimizer.",
    "tools": ["run_optimizer", "delegate_to_agent", "list_delegatable_agents"],
    "group": "basic",
    "icon": "share-2",
    "color": "#7dcfff",
    "description": "Lets the agent delegate the session to other agents and run the prompt optimizer. On by default; switch off to remove it platform-wide.",
    "simple": True,
}
