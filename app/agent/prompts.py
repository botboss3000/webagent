"""
Prompt construction with dynamic tool descriptions from database.
"""

import base64
import logging
from typing import Any, Dict, List, Optional, Union

from app.db.system_prompt_fragments import (
    format_tool_subheadings_markdown,
    get_prompt_fragments,
)

logger = logging.getLogger(__name__)

_VISION_INLINE_MAX_BYTES = 20 * 1024 * 1024
_VISION_INLINE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


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
    agent_id: Optional[str] = None,
) -> str:
    """Assemble the final system prompt from resolved slot docs + brain context.

    `docs` is the resolved slot list returned by `_docs_for_caller()` —
    each entry is already a fully merged admin-base + user-override slot,
    in the admin's chosen order. We concatenate the contents as-is so admins
    keep full control over headings and formatting.

    When `agent_id` is provided, any external data sources attached to that
    agent (with inject_schema_in_prompt = 1) get summarized into a
    `# [DATA SOURCES]` section.
    """
    sections: List[str] = []
    for doc in docs:
        # The `__skills__` slot holds skills as raw JSON — never dump it into the
        # prompt; it's rendered separately by append_skills_section().
        if (doc.get("context_type") or doc.get("slot_name")) == "__skills__":
            continue
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

    if agent_id:
        ds_block = await format_data_sources_for_prompt(agent_id)
        if ds_block:
            sections.append(ds_block)

    if brain_context:
        sections.append("# [BRAIN CONTEXT]")
        intro = (fr.get("brain_context_intro") or "").strip()
        if intro:
            sections.append(intro + "\n")
        sections.append(brain_context)

    return "\n\n".join(s for s in sections if s.strip()).strip()


async def append_skills_section(
    system_prompt: str,
    agent: Optional[Dict] = None,
    session_id: Optional[str] = None,
) -> str:
    """Append the `# [SKILLS]` block to a built system prompt.

    Reads the agent's skills (from its `__skills__` prompt slot) and the
    session's active (loaded) skill list, then renders name + description for
    every skill, plus the body for always-on/loaded skills and a placeholder for
    selectable ones. A no-op when the agent has no skills.
    """
    from app.agent.skills import format_skills_section

    agent_id = (agent or {}).get("id")
    if not agent_id:
        return system_prompt

    try:
        from app.db import get_db
        db = get_db()
        skills = await db.get_agent_skills(agent_id, user_id=None)
    except Exception as e:
        logger.debug("Could not load skills for agent %s: %s", agent_id, e)
        return system_prompt
    if not skills:
        return system_prompt

    active: List[str] = []
    if session_id:
        try:
            active = await db.get_session_active_skills(session_id)
        except Exception as e:
            logger.debug("Could not load active skills for session %s: %s", session_id, e)

    section = format_skills_section(skills, active)
    if not section:
        return system_prompt
    return f"{system_prompt}\n\n{section}".strip() if system_prompt else section


async def format_data_sources_for_prompt(agent_id: str) -> str:
    """Build the `# [DATA SOURCES]` block for an agent's enabled attachments.

    Returns "" when the agent has no attached sources or every attachment has
    inject_schema_in_prompt = 0.
    """
    from app.db import get_db
    from app.connectors import get_connector

    db = get_db()
    try:
        attachments = await db.agent_data_source_list(agent_id, enabled_only=True)
    except Exception:
        return ""
    snippets: List[str] = []
    for att in attachments:
        if not att.get("inject_schema_in_prompt"):
            continue
        ds = {
            "id": att.get("data_source_id"),
            "name": att.get("name"),
            "type": att.get("type"),
            "config": att.get("config") or {},
            "schema_cache": att.get("schema_cache") or {},
        }
        try:
            connector = get_connector(ds["type"])
            snippet = connector.prompt_snippet(ds, att)
        except Exception:
            snippet = ""
        if snippet:
            snippets.append(snippet.strip())
    if not snippets:
        return ""
    return "# [DATA SOURCES]\n\n" + "\n\n".join(snippets)


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
    lines.append(
        "Image attachments are inlined directly with this user message and "
        "are visible to vision-capable models — no need to call `read_attachment` "
        "for them on this turn."
    )
    return "\n".join(lines)


async def _attachment_to_image_data_url(att: Dict) -> Optional[str]:
    """Read an image attachment's bytes and return a base64 data URL, or None when
    the attachment isn't an inlinable image or can't be read. Shared by
    `build_user_message_content` (native inline) and `describe_image_attachment`
    (the vision sub-step) so the mime/size/storage guards live in one place."""
    mime = (att.get("mime_type") or "").lower()
    if mime not in _VISION_INLINE_MIMES:
        return None
    size = att.get("size_bytes") or 0
    if size and size > _VISION_INLINE_MAX_BYTES:
        logger.info(
            "Skipping inline of attachment %s: %d bytes exceeds %d limit",
            att.get("id"), size, _VISION_INLINE_MAX_BYTES,
        )
        return None
    storage_provider = att.get("storage_provider") or "local"
    if storage_provider == "browser":
        # Bytes live in the user's browser IndexedDB; the server can't read them.
        return None
    storage_path = att.get("storage_path")
    if not storage_path:
        return None
    try:
        from app.db.attachments import read_file
        file_bytes = await read_file(storage_path, storage_provider=storage_provider)
    except Exception as e:
        logger.warning("read_file failed for attachment %s: %s", att.get("id"), e)
        return None
    if not file_bytes:
        return None
    b64 = base64.b64encode(file_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def build_user_message_content(
    user_text: str,
    attachment_docs: Optional[List[Dict]] = None,
) -> Union[str, List[Dict[str, Any]]]:
    """Build the LLM `content` field for the user message.

    Inlines image attachments as `image_url` parts (data URLs) so vision-capable
    models can see them directly. Non-image attachments are left to the
    `read_attachment` tool. Returns a plain string when there are no inlinable
    images, preserving the original message shape.
    """
    if not attachment_docs:
        return user_text

    image_parts: List[Dict[str, Any]] = []
    for att in attachment_docs:
        url = await _attachment_to_image_data_url(att)
        if url:
            image_parts.append({"type": "image_url", "image_url": {"url": url}})

    if not image_parts:
        return user_text

    text_part = {"type": "text", "text": user_text or ""}
    return [text_part, *image_parts]


async def describe_image_attachment(
    att: Dict,
    describer_cfg: Dict[str, Any],
    user_text_hint: str = "",
) -> Optional[str]:
    """Call a configured vision model ONCE to describe a single image attachment.

    Returns the description text, or None when the image can't be read, the
    describer config is incomplete, or the call fails. This is a single
    in-process completion — no session, no agent loop, no HTTP self-call.
    Generic by design: future voice/video describers follow the same shape.
    """
    url = await _attachment_to_image_data_url(att)
    if not url:
        return None
    base_url = describer_cfg.get("base_url", "")
    api_key = describer_cfg.get("api_key", "")
    model = describer_cfg.get("model", "")
    if not (base_url and api_key and model):
        return None

    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    sys_line = (
        "You are an image-description assistant. Describe the attached image in "
        "thorough, faithful detail so that another AI model which cannot see the "
        "image can fully understand it. Include any visible text verbatim, plus "
        "layout, objects, people, colours, charts and notable details. Do not add "
        "commentary or speculation beyond what is visible."
    )
    user_parts: List[Dict[str, Any]] = []
    hint = (user_text_hint or "").strip()
    if hint:
        user_parts.append({"type": "text",
                           "text": f"The user's message accompanying this image: {hint}"})
    user_parts.append({"type": "text", "text": "Describe this image in detail."})
    user_parts.append({"type": "image_url", "image_url": {"url": url}})

    try:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=30.0)
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sys_line},
                {"role": "user", "content": user_parts},
            ],
            temperature=0.0,
            max_tokens=768,
        )
        if resp and resp.choices:
            text = (resp.choices[0].message.content or "").strip()
            return text or None
    except Exception as e:
        logger.warning("describe_image_attachment failed for %s: %s", att.get("id"), e)
    return None



