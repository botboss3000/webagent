"""
Guardrails for filesystem access.

Blocks dangerous paths so the agent can't read/write/delete sensitive files.
Update DENIED_PATTERNS to add or remove restrictions.

To disable guardrails entirely: delete this file. source.py falls through
to unrestricted access when guardrails.py is missing.
"""

import fnmatch
import logging

logger = logging.getLogger(__name__)

# ── Denied path patterns (fnmatch syntax) ─────────────────────────────────────
# These apply to read, write, edit, and delete operations.
DENIED_PATTERNS = [
    # Environment files — contain API keys, secrets, DB passwords
    ".env",
    ".env.*",
    "*.env",
    # Shell configs — contain tokens, history
    ".bash_history",
    ".zsh_history",
    ".gitconfig",
    # SSH keys
    ".ssh/*",
    ".ssh/**/*",
]

# ── Denied command patterns (substring match on shell commands) ───────────────
DENIED_COMMANDS = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf --no-preserve-root",
    ":(){ :|:& };:",  # fork bomb
]


async def check_path(path: str, action: str = "read") -> None:
    """
    Check if a path is allowed. Raises PermissionError if denied.
    Called by source.py endpoints before every read/write/delete operation.

    Args:
        path: Absolute filesystem path
        action: 'read', 'write', or 'delete'

    Raises:
        PermissionError if the path matches a denied pattern
    """
    import os
    normalized = path.replace("\\", "/")

    for pattern in DENIED_PATTERNS:
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(
            os.path.basename(normalized), pattern
        ):
            logger.warning("Blocked %s on denied path: %s (matched: %s)", action, path, pattern)
            raise PermissionError(
                f"Access denied: '{path}' matches restricted pattern '{pattern}'. "
                f"This path contains sensitive information."
            )


async def check_command(command: str) -> None:
    """
    Check if a shell command is allowed. Raises PermissionError if denied.

    Args:
        command: Shell command string

    Raises:
        PermissionError if the command matches a denied pattern
    """
    for pattern in DENIED_COMMANDS:
        if pattern in command:
            logger.warning("Blocked denied command: %s", command[:100])
            raise PermissionError(
                f"Command blocked: matches restricted pattern '{pattern}'"
            )
