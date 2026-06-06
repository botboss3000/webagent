"""Visualizer ability — drop-in. See app/abilities/__init__.py for the contract."""

FEATURE = {
    "id": "visualizer",
    "display_name": "Visualizer",
    "category": "ability",
    "status": "beta",
    "summary": "page-authoring tools for the Pages workspace.",
    # Visualizer tools are injected dynamically; no static tool names to gate.
    "tools": [],
    "group": "core",
    "icon": "layout",
    "color": "#7aa2f7",
    "description": "Lets the agent create, edit, rename, and delete pages in the Pages workspace.",
    "simple": False,
}
