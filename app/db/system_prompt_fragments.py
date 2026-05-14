"""
Load system-prompt instructional fragments from app/db/system_prompt.md.

Sections are delimited by markdown H2 headers: ## Section name
Keys are derived as lower_snake_case from the header text after "## ".

There are no built-in defaults: missing file, read errors, or absent sections
yield empty strings for those keys (callers treat them as optional).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

_MD_PATH = Path(__file__).resolve().parent / "prompt_fragments.md"

_cache: Optional[Dict[str, str]] = None


def _header_to_key(header_line: str) -> Optional[str]:
    line = header_line.strip()
    if not line.startswith("## "):
        return None
    # `###` is a tool subheading inside a fragment body, not a top-level section.
    if line.startswith("###"):
        return None
    rest = line[3:].strip()
    if not rest:
        return None
    return rest.lower().replace(" ", "_")


def _parse_md_sections(text: str) -> Dict[str, str]:
    fragments: Dict[str, str] = {}
    current_key: Optional[str] = None
    current_lines: List[str] = []

    for raw in text.splitlines():
        key = _header_to_key(raw)
        if key is not None:
            if current_key:
                fragments[current_key] = "\n".join(current_lines).strip()
            current_key = key
            current_lines = []
        else:
            current_lines.append(raw)

    if current_key:
        fragments[current_key] = "\n".join(current_lines).strip()

    return fragments


def _load_from_disk() -> Dict[str, str]:
    if not _MD_PATH.is_file():
        return {}
    try:
        text = _MD_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    return _parse_md_sections(text)


def format_tool_subheadings_markdown(raw: str) -> str:
    """Fragment bodies use ### for tool lines so they are not parsed as top-level ## sections."""
    return raw.strip().replace("### ", "## ")


def get_prompt_fragments() -> Dict[str, str]:
    """Return fragments parsed from ``system_prompt.md`` (possibly empty)."""
    global _cache
    if _cache is None:
        _cache = _load_from_disk()
    return _cache


def clear_prompt_fragments_cache() -> None:
    """Reload system_prompt.md on next get_prompt_fragments() call."""
    global _cache
    _cache = None
