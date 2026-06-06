"""Terminal Control ability — drop-in. See app/abilities/__init__.py for the contract."""

FEATURE = {
    "id": "terminal_control",
    "display_name": "Terminal Control",
    "category": "ability",
    "status": "experimental",
    "summary": "drive interactive terminal programs (effectively shell access).",
    "tools": ["terminal_open", "terminal_read", "terminal_send",
              "terminal_wait", "terminal_list", "terminal_close"],
    "group": "administrator",
    "icon": "terminal",
    "color": "#f7768e",
    "description": "Lets the agent open and drive interactive terminal programs (REPLs, CLIs) — effectively shell access, so grant per agent with care.",
    "simple": True,
    # Bundled skill: a load-on-demand how-to for driving interactive programs.
    # Body lives in the sibling file terminal_control.skill.md (found by
    # convention). Handle is minted once and frozen here.
    "skill_mode": "selectable",
    "skill_handle": "terminal_control_9x4k2p",
}
