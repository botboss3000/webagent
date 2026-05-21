"""
Prompt construction with dynamic tool descriptions from database.
"""

from typing import List, Dict, Optional
from app.db.system_prompt_fragments import (
    format_tool_subheadings_markdown,
    get_prompt_fragments,
)


# Legacy slot names — kept as a public list so the prompt-build pipeline (and
# any external scripts that gate on context types) can still ask "what slots
# might exist". With free-form admin slots this is just a hint, not a filter.
CONTEXT_SECTION_TYPES = [
    "system", "agent", "user", "skills", "tasks", "misc",
    "bootstrap_tools", "optimizer", "memory", "project", "jobs", "tools",
]


def _row_content(doc: Dict) -> str:
    return (doc.get("content") or "").strip()


async def build_system_prompt(
    docs: List[Dict],
    brain_context: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """Assemble the final system prompt from resolved slot docs + brain context.

    `docs` is the resolved slot list returned by `_docs_for_caller()` —
    each entry is already a fully merged admin-base + user-override slot,
    in the admin's chosen order. We concatenate the contents as-is so admins
    keep full control over headings and formatting.
    """
    sections: List[str] = []
    for doc in docs:
        content = _row_content(doc)
        if content:
            sections.append(content)

    fr = get_prompt_fragments()

    # If no docs supplied a tools list and we have no caller, inject the
    # fallback tool list so anonymous endpoints still see something useful.
    has_any_content = any(s.strip() for s in sections)
    if not has_any_content and not user_id:
        fallback = format_tool_subheadings_markdown(fr.get("fallback_tools") or "")
        if fallback:
            sections.append(fallback)

    if brain_context:
        sections.append("# [BRAIN CONTEXT]")
        intro = (fr.get("brain_context_intro") or "").strip()
        if intro:
            sections.append(intro + "\n")
        sections.append(brain_context)

    return "\n\n".join(s for s in sections if s.strip()).strip()


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



