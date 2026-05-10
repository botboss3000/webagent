"""
Visualizer module — self-contained creative coding feature.

Inject `render_visual` tool into the agent's tool registry.
Call `register_tools(tools, user_id)` from app/tools/loader.py.
"""
from typing import Dict
from app.tools.loader import ToolInfo


def register_tools(tools: Dict[str, ToolInfo], user_id: str) -> None:
    """Inject render_visual tool into the tools dict."""

    from app.visualizer.tool import render_visual as _render_visual

    async def _render_visual_wrapper(html: str, title: str = ""):
        # user_id is captured from the outer scope (set by loader's _make_handler pattern)
        # We use the user_id as session_id for file isolation
        return await _render_visual(
            html=html,
            title=title,
            session_id=user_id,
        )

    tools["render_visual"] = ToolInfo(
        name="render_visual",
        handler=_render_visual_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "html": {
                    "type": "string",
                    "description": "Full HTML document with embedded p5.js sketch. Must include <script src='https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.11.3/p5.min.js'></script> and use createCanvas(windowWidth, windowHeight).",
                },
                "title": {
                    "type": "string",
                    "description": "Descriptive title for the visualization (shown in UI)",
                },
            },
            "required": ["html"],
        },
    )
