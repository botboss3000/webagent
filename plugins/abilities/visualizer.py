"""Visualizer ability — SELF-CONTAINED drop-in.

Everything this capability needs to expose its tools lives here: the FEATURE
descriptor, and a build_tools() that produces the page-authoring handlers. The
actual handler bodies still live in app/visualizer/ (register_tools), which
mutates a tools dict with ToolInfo objects rather than returning handlers.
build_tools() runs that registrar against a throwaway dict and adapts the result
into the (handlers, schemas, destructive) shape the generic drop-in loader wants,
populating module-level TOOL_SCHEMAS/DESTRUCTIVE in the process so they never
drift from the registrar.

Discovered generically by core via the three optional module hooks (see
app/abilities/__init__.py "Self-contained abilities"):
  • FEATURE        — catalog + UI + which tool names it gates
  • build_tools(...) — its tool handlers (app/tools/loader.py injects them)
  • TOOL_SCHEMAS / DESTRUCTIVE — read by the loader AFTER build_tools().
"""

from __future__ import annotations

FEATURE = {
    "id": "visualizer",
    "display_name": "Visualizer",
    "category": "ability",
    "status": "beta",
    "summary": "page-authoring tools for the Pages workspace.",
    # The page-authoring tools this ability gates (handlers produced via
    # build_tools(); bodies live in app/visualizer/register_tools).
    "tools": [
        "render_visual", "list_pages", "get_page",
        "create_page", "delete_page", "rename_page",
    ],
    "group": "core",
    "icon": "layout",
    "color": "#7aa2f7",
    "description": "Lets the agent create, edit, rename, and delete pages in the Pages workspace.",
    "simple": False,
}


# Populated INSIDE build_tools() from the ToolInfo objects the registrar emits,
# so they never drift from app/visualizer/register_tools. The loader reads these
# AFTER calling build_tools().
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the visualizer page tools.

    Lazily imports the registrar + adapter so scanning FEATURE stays cheap. Runs
    app/visualizer/register_tools against a throwaway dict, then extracts the
    handlers and (re)populates module-level TOOL_SCHEMAS/DESTRUCTIVE from each
    ToolInfo's parameters / destructive / requires_confirmation flags.
    """
    try:
        from app.visualizer import register_tools
        from plugins.admin.adapter import extract_injected
    except ImportError:
        # app/visualizer/ not available — visual rendering disabled.
        return {}

    handlers, schemas, destructive = extract_injected(register_tools, user_id)

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(schemas)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(destructive)

    return handlers
