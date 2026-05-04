"""
Prompt construction with dynamic tool descriptions from database.
"""

from typing import List, Dict, Optional
from app.tools.loader import load_tools
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

    # ---- CONFIRMATION RULE (always first in prompt so model sees it) ----
    sections.append("# [CRITICAL RULE]")
    sections.append("BEFORE calling ANY destructive tool (edit_source, write_source, delete_source, create_tool, run_command, restart_server), you MUST:")
    sections.append("1. Explain to the user exactly what you plan to change and show the proposed change.")
    sections.append("2. Wait for the user to explicitly approve before making the tool call.")
    sections.append("3. If the user says no or expresses doubt — do NOT call the tool.")
    sections.append("Safe tools like read_source, web_search, db_query, memory, session_search do NOT need confirmation.")
    sections.append("")

    # ---- Tool descriptions ----
    if user_id:
        tool_descriptions = await _get_tool_descriptions_from_db(user_id)
        sections.append(tool_descriptions)
    else:
        # Fallback to basic tool descriptions
        sections.append("# [TOOLS]")
        sections.append("## web_search")
        sections.append("Search the web for current information.")
        sections.append("## db_query")
        sections.append("Read or edit context documents in Supabase.")
        sections.append("## memory")
        sections.append("Manage persistent memories across sessions.")
        sections.append("## session_search")
        sections.append("Search across past conversation sessions and messages.")
        sections.append("")

    # ---- Brain context injection ----
    if brain_context:
        sections.append("# [BRAIN CONTEXT]")
        sections.append(
            "The following is retrieved from your personal knowledge base, "
            "relevant to the user's current message. Use it to inform your response.\n"
        )
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

    # Built-in tools (always shown at end)
    sections.append("## create_tool  [⚠ REQUIRES CONFIRMATION]")
    sections.append("Create or update a Python tool in the agent tools library. ")
    sections.append("When you need a capability that doesn't exist yet, write Python code for it ")
    sections.append("and call this tool to save it. The tool will be available in the next turn. ")
    sections.append("If the tool already exists, it auto-increments the version (v1 -> v2 -> v3).")
    sections.append("⚠ You MUST ask the user to review the code and get approval before calling this tool.")
    sections.append("Parameters: name, description, parameters (JSON Schema), code (full async function).")
    sections.append("")
    sections.append("## browser_open")
    sections.append("Open a browser to a given URL, typically for authentication flows.")
    sections.append("")
    sections.append("## rate_skill")
    sections.append("Record user feedback on the last tool execution. Call this when the user ")
    sections.append("expresses satisfaction (positive) or dissatisfaction (negative) with a result. ")
    sections.append("Parameters: skill_name (str), feedback_type ('positive'|'negative'|'correction'), ")
    sections.append("message (str, optional).")
    sections.append("")

    return "\n".join(sections)
