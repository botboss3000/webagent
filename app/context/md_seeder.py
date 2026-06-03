"""
Scan files from app/context/ and parse into DB rows.

Three scanners:

1. scan_context_files()
   - Reads app/context/context_templates/*.md
   - Each file → one context_templates row (agent identity, skills, project, etc.)
   - Frontmatter can set: context_type, title, tags
   - Filename stem = default context_type

2. scan_agent_system_prompt_files()
   - Reads app/context/agent_system_prompt/*.md (legacy, kept for compat)
   - Returns content of first .md file found
   - Used to seed agent_templates.system_prompt only

3. scan_agent_json_files()
   - Reads data/agents/*.json (NEW, preferred)
   - Each JSON file provides FULL agent template schema:
     id, system_prompt, max_turn_count, model, provider,
     temperature, max_tokens, metadata
   - Replaces scan_agent_system_prompt_files() for new deployments
"""

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Directories relative to this file (app/context/md_seeder.py)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# Repo root is two levels up from app/context/ (app/context -> app -> root)
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))

DEFAULT_CONTEXT_DIR = os.path.join(_THIS_DIR, "context_templates")
DEFAULT_AGENT_SYSTEM_PROMPT_DIR = os.path.join(_THIS_DIR, "agent_system_prompt")
# Agent template JSON source of truth lives at repo-root data/agents/.
DEFAULT_AGENTS_DIR = os.path.join(_REPO_ROOT, "data", "agents")

_FM_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


def _parse_frontmatter(raw: str) -> tuple[dict, str]:
    """
    Parse optional YAML-like frontmatter from markdown content.

    Returns (fm_dict, body_string). Body is full content if no frontmatter.
    Supports: string values, quoted strings, simple lists [a, b, c].
    """
    m = _FM_PATTERN.match(raw)
    if not m:
        return {}, raw.strip()

    fm_text = m.group(1).strip()
    body = m.group(2).strip()
    fm: dict = {}

    for line in fm_text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not key:
            continue

        # Strip quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]

        # Parse simple lists: [a, b, c]
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1]
            val = [
                item.strip().strip("\"'")
                for item in inner.split(",")
                if item.strip()
            ]

        fm[key] = val

    return fm, body


def scan_context_files(directory: Optional[str] = None) -> List[Dict]:
    """
    Scan a directory for .md files and parse into context_templates rows.

    Args:
        directory: Path to scan. Defaults to app/context/context_templates/.

    Returns:
        List of dicts: {context_type, title, content, tags (Python list)}.
        Empty list if directory doesn't exist or no files found.
    """
    if directory is None:
        directory = DEFAULT_CONTEXT_DIR

    if not os.path.isdir(directory):
        logger.debug(
            "Context templates directory %s does not exist — skipping scan",
            directory,
        )
        return []

    results: List[Dict] = []

    for fname in sorted(os.listdir(directory)):
        # Skip hidden files and non-.md
        if fname.startswith(".") or not fname.lower().endswith(".md"):
            continue

        fpath = os.path.join(directory, fname)
        if not os.path.isfile(fpath):
            continue

        stem = os.path.splitext(fname)[0]

        try:
            with open(fpath, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            logger.warning("Cannot read %s: %s", fpath, e)
            continue

        # Defaults from filename
        context_type = stem
        title = stem.replace("_", " ").replace("-", " ").title()
        tags: list = []
        content = raw.strip()

        # Parse optional frontmatter
        fm, body = _parse_frontmatter(raw)
        if fm:
            context_type = fm.get("context_type", context_type)
            title = fm.get("title", title)
            raw_tags = fm.get("tags", tags)
            tags = raw_tags if isinstance(raw_tags, list) else tags
            if body:
                content = body

        results.append({
            "context_type": context_type,
            "title": title,
            "content": content,
            "tags": tags,  # Python list, caller serializes per backend
        })
        logger.debug(
            "Scanned context template: %s → type=%s title=%s",
            fname, context_type, title,
        )

    logger.info(
        "Scanned %d context template files from %s", len(results), directory
    )
    return results


def scan_agent_system_prompt_files(directory: Optional[str] = None) -> Dict[str, str]:
    """
    Scan a directory for .md files and return a dict of {template_id: content}.

    Each file's template_id comes from:
      1. The ``id`` field in YAML frontmatter, if present
      2. Otherwise, the filename stem (e.g., ``default.md`` → ``"default"``)

    Content is the body after frontmatter stripping.

    Args:
        directory: Path to scan. Defaults to app/context/agent_system_prompt/.

    Returns:
        Dict[str, str] mapping template_id → body content.
        Empty dict if directory missing or no .md files found.
    """
    if directory is None:
        directory = DEFAULT_AGENT_SYSTEM_PROMPT_DIR

    if not os.path.isdir(directory):
        logger.debug(
            "Agent system prompt directory %s does not exist — skipping scan",
            directory,
        )
        return {}

    result: Dict[str, str] = {}

    for fname in sorted(os.listdir(directory)):
        if fname.startswith(".") or not fname.lower().endswith(".md"):
            continue
        fpath = os.path.join(directory, fname)
        if not os.path.isfile(fpath):
            continue

        stem = os.path.splitext(fname)[0]

        try:
            with open(fpath, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            logger.warning("Cannot read %s: %s", fpath, e)
            continue

        # Parse frontmatter to check for explicit id
        fm, body = _parse_frontmatter(raw)
        template_id = fm.get("id", stem) if fm else stem
        content = body.strip() if body else raw.strip()

        result[template_id] = content
        logger.debug(
            "Loaded agent system prompt: %s → id=%s (%d chars)",
            fname, template_id, len(content),
        )

    if result:
        logger.info(
            "Loaded %d agent system prompt templates from %s",
            len(result), directory,
        )
    else:
        logger.debug("No .md files found in %s", directory)

    return result


def scan_agent_json_files(directory: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Scan a directory for .json files and parse into full agent template rows.

    Each JSON file must contain an object with agent template fields:
      - id (str, required)
      - system_prompt (str)
      - max_turn_count (int, default 0)
      - model (str or null)
      - provider (str or null)
      - temperature (float, default 0.0)
      - max_tokens (int, default 4096)
      - metadata (dict or str, default {})
      - agent_prompt (str, default "")  — agent identity context
      - user_prompt (str, default "")   — user profile context
      - skills_prompt (str, default "") — skills/tools context
      - tasks_prompt (str, default "")  — task workflows context
      - misc_prompt (str, default "")   — miscellaneous context

    Args:
        directory: Path to scan. Defaults to data/agents/.

    Returns:
        List of dicts, each a full agent template row suitable for
        INSERT into agent_templates.
        Empty list if directory missing or no .json files found.
    """
    if directory is None:
        directory = DEFAULT_AGENTS_DIR

    if not os.path.isdir(directory):
        logger.debug(
            "Agent JSON files directory %s does not exist — skipping scan",
            directory,
        )
        return []

    results: List[Dict[str, Any]] = []

    for fname in sorted(os.listdir(directory)):
        if fname.startswith(".") or not fname.lower().endswith(".json"):
            continue
        fpath = os.path.join(directory, fname)
        if not os.path.isfile(fpath):
            continue

        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Cannot parse agent file %s: %s", fpath, e)
            continue

        if not isinstance(data, dict):
            logger.warning("Agent file %s is not a JSON object — skipping", fname)
            continue

        agent_id = data.get("id", "")
        if not agent_id:
            logger.warning(
                "Agent file %s has no 'id' field — skipping", fname
            )
            continue

        # Serialize metadata dict to JSON string
        meta = data.get("metadata", {})
        if isinstance(meta, dict):
            meta_str = json.dumps(meta)
        else:
            meta_str = str(meta)
        # Validate metadata is valid JSON
        try:
            json.loads(meta_str)
        except (TypeError, json.JSONDecodeError):
            meta_str = "{}"

        # Version is required for the manifest-hash short-circuit + the
        # per-slot version-gated upsert in the seeder. Missing → default to 1
        # but log a warning so authors know to add it.
        version = data.get("version")
        if version is None:
            logger.warning(
                "Agent JSON %s has no 'version' field — defaulting to 1. "
                "Add a top-level integer 'version' so future bumps trigger re-seed.",
                fname,
            )
            version = 1
        try:
            version = int(version)
        except (TypeError, ValueError):
            logger.warning(
                "Agent JSON %s has non-integer 'version' (%r) — coercing to 1",
                fname, version,
            )
            version = 1

        row = {
            "id": agent_id,
            "version": version,
            "name": data.get("name", agent_id),
            "description": data.get("description", ""),
            "icon": data.get("icon", ""),
            "system_prompt": data.get("system_prompt", ""),
            "max_turn_count": data.get("max_turn_count", 0),
            "max_wall_seconds": data.get("max_wall_seconds"),
            "model": data.get("model"),
            "provider": data.get("provider"),
            "temperature": data.get("temperature", 0.0),
            "max_tokens": data.get("max_tokens", 4096),
            "metadata": meta_str,
            "agent_prompt": data.get("agent_prompt", ""),
            "user_prompt": data.get("user_prompt", ""),
            "skills_prompt": data.get("skills_prompt", ""),
            "tasks_prompt": data.get("tasks_prompt", ""),
            "misc_prompt": data.get("misc_prompt", ""),
            "automation_prompt": data.get("automation_prompt", ""),
            "bootstrap_tools": data.get("bootstrap_tools", ""),
            "can_be_default": 1 if data.get("can_be_default", True) else 0,
            "is_system": 1 if data.get("is_system", False) else 0,
            "is_pipeline": 1 if data.get("is_pipeline", False) else 0,
            "access_level": data.get("access_level", "all"),
            "trigger_type": data.get("trigger_type", "user_input"),
            "trigger_key": data.get("trigger_key", None),
            "loop_logic": json.dumps(data.get("loop_logic", [])),
        }

        results.append(row)

        logger.debug(
            "Loaded agent template: %s → id=%s (%d chars system_prompt, %d max_tokens)",
            fname, agent_id, len(row["system_prompt"]), row["max_tokens"],
        )

    if results:
        logger.info(
            "Loaded %d agent template(s) from %s",
            len(results), directory,
        )
    else:
        logger.debug("No .json files found in %s", directory)

    return results


def compute_agent_manifest_hash(directory: Optional[str] = None) -> str:
    """
    Compute a stable hash over the agent JSON files in *directory*.

    Hashes (filename, declared version, raw bytes) per file in sorted-filename
    order. The result is the short-circuit key for the seeder: if it matches
    what's already stored in app_meta, the seeder skips the entire JSON →
    DB upsert pass and returns immediately.

    Returned as a hex SHA-256 string. Empty directory → empty-input hash
    (still stable). Returned hash is intentionally byte-sensitive so a JSON
    edit without a version bump still triggers a re-seed pass (which will
    then no-op per-row because the version match short-circuits inside).
    """
    import hashlib
    if directory is None:
        directory = DEFAULT_AGENTS_DIR

    h = hashlib.sha256()
    if not os.path.isdir(directory):
        return h.hexdigest()

    for fname in sorted(os.listdir(directory)):
        if fname.startswith(".") or not fname.lower().endswith(".json"):
            continue
        fpath = os.path.join(directory, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            with open(fpath, "rb") as f:
                raw = f.read()
        except OSError:
            continue
        # Pull declared version cheaply (best-effort parse).
        version: Any = 1
        try:
            doc = json.loads(raw.decode("utf-8"))
            if isinstance(doc, dict) and "version" in doc:
                version = doc["version"]
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        h.update(fname.encode("utf-8"))
        h.update(b"\x00")
        h.update(str(version).encode("utf-8"))
        h.update(b"\x00")
        h.update(raw)
        h.update(b"\x00")
    return h.hexdigest()
