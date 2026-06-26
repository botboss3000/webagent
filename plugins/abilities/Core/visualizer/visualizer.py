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

    # Pass agent_id through to the registrar so render_visual / create_canvas /
    # edit_canvas record this agent as the canvas's owner (extract_injected calls
    # register_tools(staging, user_id, agent_id)).
    handlers, schemas, destructive = extract_injected(register_tools, user_id, agent_id)

    # ── screenshot_canvas ─────────────────────────────────────────────────────
    # Added HERE (not in the register_tools registrar) because it needs the live
    # session_id — the registrar only receives user_id, but the drop-in build_tools
    # contract hands us session_id too. Gives the agent *eyes*: renders a saved
    # canvas headlessly the way the app mounts it, saves the shot as a session
    # attachment (user sees it; agent can read it via process_image), and returns
    # width/console signals so the agent can catch a not-full-width / broken build.
    async def _screenshot_canvas(slug: str, theme: str = "dark", click=None):
        from app.visualizer.screenshot import screenshot_canvas as _shot
        return await _shot(slug=slug, theme=theme, click=click,
                           user_id=user_id, session_id=session_id)

    handlers["screenshot_canvas"] = _screenshot_canvas
    schemas["screenshot_canvas"] = {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": ("Slug of the canvas to render and screenshot (e.g. 'home', "
                                "'dashboard'). Use the slug you just rendered to."),
            },
            "theme": {
                "type": "string",
                "enum": ["dark", "light", "both"],
                "description": ("Which theme to render. 'both' returns a dark and a light shot "
                                "so you can confirm the canvas is legible in each. Default 'dark'."),
            },
            "click": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional. CSS selector(s) to click INSIDE the canvas before the shot, in "
                    "order — so the screenshot captures an INTERACTION's result (a name opening "
                    "a profile panel, a calendar day expanding, a tab switching, a popover). Use "
                    "this to VERIFY clickable features actually work, not just that the page looks "
                    "right at rest. Each selector is matched in the canvas's shadow root (e.g. "
                    "'#student-sarah', '.day[data-date=\"2026-06-17\"]', '[data-tab=\"profile\"]'). "
                    "A selector that matches nothing is reported back as a CLICK MISS. Pass one "
                    "selector for a single click or several for a sequence."),
            },
        },
        "required": ["slug"],
    }

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(schemas)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(destructive)
    # delete_canvas removes a saved canvas irreversibly → confirm in Ask/Plan mode
    # (matches the gated wiki_delete / cancel_automation). Create / edit / set-data
    # are reversible and run free, like memory.
    DESTRUCTIVE.add("delete_canvas")

    return handlers
