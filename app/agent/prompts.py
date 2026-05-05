"""
Prompt construction with dynamic tool descriptions from database.
"""

from typing import List, Dict, Optional
from app.tools.loader import load_tools
from app.db.system_prompt_fragments import (
    format_tool_subheadings_markdown,
    get_prompt_fragments,
)
import json

# Section titles for public.context.context_type (Web Portal schema)
CONTEXT_TYPE_TO_TITLE = {
    "agent": "AGENT IDENTITY",
    "user": "USER",
    "skills": "SKILLS",
    "tools": "TOOLS",
    "tasks": "TASKS",
    "memory": "MEMORY",
    "project": "PROJECT",
    "jobs": "JOBS",
}


def _row_context_type(doc: Dict) -> str:
    return (doc.get("context_type") or doc.get("doc_type") or "").strip()


async def build_system_prompt(
    docs: List[Dict],
    brain_context: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_system_prompt: Optional[str] = None,
) -> str:
    """
    Assemble a system prompt from context rows, brain context, and tool descriptions.

    Args:
        docs: List of context documents from the context table
        brain_context: Optional formatted brain search results to inject
        user_id: User ID for loading personal tools (optional)
        agent_system_prompt: Non-editable system prompt from the agent record (injected first)
    """
    sections: List[str] = []

    # ---- Agent system prompt (non-editable, from agents table) ----
    if agent_system_prompt:
        sections.append("# [AGENT DIRECTIVE]")
        sections.append(agent_system_prompt)
        sections.append("")

    # ---- Context documents ----
    grouped: Dict[str, List[Dict]] = {}
    for doc in docs:
        ct = _row_context_type(doc)
        if not ct:
            continue
        grouped.setdefault(ct, []).append(doc)

    order = ["agent", "user", "skills", "tools", "tasks", "memory", "project", "jobs"]
    for context_type in order:
        if context_type not in grouped:
            continue
        title = CONTEXT_TYPE_TO_TITLE.get(context_type, context_type.upper())
        sections.append(f"# [{title}]")
        for doc in grouped[context_type]:
            content = doc.get("content", "").strip()
            if content:
                sections.append(content)
        sections.append("")

    for context_type, doc_list in grouped.items():
        if context_type in order:
            continue
        title = context_type.upper()
        sections.append(f"# [{title}]")
        for doc in doc_list:
            content = doc.get("content", "").strip()
            if content:
                sections.append(content)
        sections.append("")

    fr = get_prompt_fragments()

    # ---- CONFIRMATION RULE (from app/db/system_prompt.md) ----
    critical = (fr.get("critical_rule") or "").strip()
    if critical:
        sections.append(critical)
        sections.append("")

    # ---- Tool descriptions ----
    if user_id:
        tool_descriptions = await _get_tool_descriptions_from_db(user_id)
        sections.append(tool_descriptions)
    else:
        fallback = format_tool_subheadings_markdown(fr.get("fallback_tools") or "")
        if fallback:
            sections.append(fallback)
            sections.append("")

    # ---- Brain context injection ----
    if brain_context:
        sections.append("# [BRAIN CONTEXT]")
        intro = (fr.get("brain_context_intro") or "").strip()
        if intro:
            sections.append(intro + "\n")
        sections.append(brain_context)
        sections.append("")

    return "\n".join(sections).strip()


async def _get_tool_descriptions_from_db(user_id: str) -> str:
    """
    Get formatted tool descriptions from the database.

    Args:
        user_id: User ID to load personal tools for

    Returns:
        Formatted string of tool descriptions
    """
    tools = await load_tools(user_id)

    # Sort tools by rating (highest first, unrated at end)
    sorted_tools = sorted(
        tools.items(),
        key=lambda kv: (
            kv[1].rating["score"] if kv[1].rating and kv[1].rating["score"] is not None else -1
        ),
        reverse=True,
    )

    sections = ["# [TOOLS]"]

    for name, func in sorted_tools:
        if hasattr(func, "__doc__") and func.__doc__:
            description = func.__doc__.strip()
        else:
            description = f"Execute {name} tool"

        # Append rating badge if available
        if func.rating and func.rating["execution_count"] > 0:
            score = func.rating["score"]
            rating_str = f"[rating: {score:.0f}%]" if score is not None else "[unrated]"
            description = f"[{rating_str}] {description}"

        sections.append(f"## {name}")
        sections.append(description)
        sections.append("")

    fr = get_prompt_fragments()
    builtin = format_tool_subheadings_markdown(fr.get("builtin_tools_append") or "")
    if builtin:
        sections.append(builtin)
        sections.append("")

    return "\n".join(sections)
