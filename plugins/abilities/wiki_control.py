"""Wiki Control ability — drop-in. See app/abilities/__init__.py for the contract."""

FEATURE = {
    "id": "wiki_control",
    "display_name": "Wiki Control",
    "category": "ability",
    "status": "beta",
    "summary": "read/search/create/edit/delete company-wide Wiki entries.",
    "tools": ["wiki_search", "wiki_list", "wiki_get", "wiki_create",
              "wiki_update", "wiki_delete", "wiki_history", "wiki_get_revision",
              "wiki_restore", "wiki_backlinks"],
    "group": "productivity",
    "icon": "book-open",
    "color": "#7dcfff",
    "description": "Lets the agent read, search, create, edit, and delete entries in the company-wide Wiki. Writes change shared data everyone sees, so grant per agent with care.",
    "simple": True,
}
