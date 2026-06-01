"""Safety helpers shared by the tool layer.

The server manager runs with the user's own shell privileges, so the guardrails here
are about *predictability*, not sandboxing:

* **path resolution** scoped to the target project (relative paths can't escape;
  absolute paths are allowed but flagged), mirroring ``app/admin/source.py``.
* **secret scanning** of diffs/content before a commit, mirroring the
  Source Controller's pre-commit check.
* **sensitive-file** awareness so per-machine runtime files aren't committed.
* **git checkpointing** so every autonomous edit is revertible.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

# Per-machine runtime / secret files that should never be auto-committed and
# warrant a warning on edit. Mirrors the Source Controller agent's list.
SENSITIVE_FILES = {
    ".env",
    "app/db/local.db",
    "app/auth/users.json",
    "provider.json",
    "app/db_mode.json",
    "scheduler_config.json",
}

# Heuristic secret patterns for the pre-commit scan.
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                 # OpenAI-style keys
    re.compile(r"sk-or-[A-Za-z0-9-]{20,}"),             # OpenRouter
    re.compile(r"AKIA[0-9A-Z]{16}"),                    # AWS access key id
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                 # GitHub PAT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # private keys
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{12,}['\"]"),
]


def resolve_path(raw_path: str, project_root: Path) -> Path:
    """Resolve ``raw_path`` against the project root.

    Relative paths are joined to the root; bare-rooted paths like ``/tmp`` on
    Windows are stripped first so they land inside the tree rather than on C:\\.
    """
    p = Path(raw_path)
    if not p.is_absolute() or (
        os.name == "nt" and not p.drive and p.root and len(p.root) == 1
    ):
        p = project_root / str(p).lstrip("\\/")
    return p.resolve()


def is_within(path: Path, project_root: Path) -> bool:
    try:
        path.resolve().relative_to(project_root.resolve())
        return True
    except ValueError:
        return False


def scan_secrets(text: str) -> list[str]:
    """Return a list of matched secret-like substrings (truncated) in ``text``."""
    hits: list[str] = []
    for pat in _SECRET_PATTERNS:
        for m in pat.finditer(text):
            frag = m.group(0)
            hits.append(frag[:12] + "…")
    return hits


def is_sensitive(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/").lstrip("./")
    return norm in SENSITIVE_FILES or norm.endswith(".env")


def git_checkpoint(project_root: Path, label: str) -> Optional[str]:
    """Create a revertible checkpoint commit of the current tree.

    Returns the short commit hash, or None if there was nothing to commit / git
    failed. Used before an autonomous edit so the change can always be undone.
    """
    try:
        subprocess.run(
            ["git", "add", "-A"], cwd=str(project_root),
            capture_output=True, timeout=30, check=False,
        )
        r = subprocess.run(
            ["git", "commit", "-m", f"[webagent-tui checkpoint] {label}", "--no-verify"],
            cwd=str(project_root), capture_output=True, text=True, timeout=30, check=False,
        )
        if r.returncode != 0:
            return None  # usually "nothing to commit"
        h = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(project_root),
            capture_output=True, text=True, timeout=10, check=False,
        )
        return h.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None
