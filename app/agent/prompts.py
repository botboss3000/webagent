"""
Prompt construction with dynamic tool descriptions from database.
"""

from typing import List, Dict, Optional
from app.db.system_prompt_fragments import (
    format_tool_subheadings_markdown,
    get_prompt_fragments,
)
import json

# Section titles for public.context.context_type (Web Portal schema)
CONTEXT_TYPE_TO_TITLE = {
    "agent": "AGENT IDENTITY",
    "user": "USER",
    "optimizer": "OPTIMIZER INSTRUCTIONS",
    "skills": "SKILLS",
    "tools": "TOOLS",
    "tasks": "TASKS",
    "memory": "MEMORY",
    "project": "PROJECT",
    "jobs": "JOBS",
}

CONTEXT_SECTION_TYPES = list(CONTEXT_TYPE_TO_TITLE.keys())


def _row_context_type(doc: Dict) -> str:
    return (doc.get("context_type") or doc.get("doc_type") or "").strip()


async def build_system_prompt(
    docs: List[Dict],
    brain_context: Optional[str] = None,
    user_id: Optional[str] = None,
    agent_system_prompt: Optional[str] = None,
    bootstrap_tools: Optional[str] = None,
) -> str:
    """
    Assemble a system prompt from context rows, brain context, and tool descriptions.

    Args:
        docs: List of context documents from the context table
        brain_context: Optional formatted brain search results to inject
        user_id: User ID for loading personal tools (optional)
        agent_system_prompt: Non-editable system prompt from the agent record (injected first)
        bootstrap_tools: Non-editable tool list from the agent record (not optimizer-modifiable)
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

    order = ["agent", "user", "optimizer", "skills", "tools", "tasks", "memory", "project", "jobs"]
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

    # ---- Bootstrap tools (non-editable, from agent record) ----
    if bootstrap_tools and bootstrap_tools.strip():
        sections.append(bootstrap_tools.strip())
        sections.append("")
    elif not user_id:
        # Fallback tool list for unauthenticated paths
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


def format_attachments_for_prompt(attachments: List[Dict]) -> str:
    """
    Format attached files into a concise summary for the system prompt.

    Args:
        attachments: List of attachment dicts with original_name, mime_type, size_bytes, id

    Returns:
        Formatted string the agent can read to know what user attached.
    """
    if not attachments:
        return ""
    lines = ["# [USER ATTACHMENTS]"]
    lines.append("The user attached the following files to this message:")
    lines.append("")
    for att in attachments:
        name = att.get("original_name", "unknown")
        mime = att.get("mime_type", "application/octet-stream")
        size_kb = att.get("size_bytes", 0) / 1024
        size_str = f"{size_kb:.1f}KB" if size_kb < 1024 else f"{size_kb/1024:.1f}MB"
        att_id = att.get("id", "?")
        # Categorize for agent clarity
        if mime.startswith("image/"):
            icon = "🖼"
        elif mime.startswith("audio/"):
            icon = "🎤"
        elif mime.startswith("video/"):
            icon = "🎬"
        else:
            icon = "📎"
        lines.append(f"  - {icon} **{name}** ({mime}, {size_str})")
        lines.append(f"    attachment_id: `{att_id}` — use `read_attachment` tool to process")
    lines.append("")
    lines.append("Use the `read_attachment` tool to read content from attached files.")
    return "\n".join(lines)



