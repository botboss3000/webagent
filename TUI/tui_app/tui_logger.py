"""Persistent TUI log — a human-readable, append-only log of the manager's
lifetime: start/stop times, user messages, assistant replies, warnings, errors,
and notable events. Written alongside the server log as ``tui.log`` in the
project root (or the current dir during onboarding).

Format: one line per event, ISO-8601 timestamps, pipe-delimited fields::

    2026-06-03T15:23:30 | START   | webAgent TUI v0.2.0 | project=~/webagent
    2026-06-03T15:23:35 | USER    | why did the server crash?
    2026-06-03T15:23:40 | ASSIST  | Let me check the logs…
    2026-06-03T15:23:42 | WARN    | No AI key configured
    2026-06-03T15:24:00 | SERVER  | server started at http://localhost:8080
    2026-06-03T15:30:00 | STOP    | session ended (uptime 6m 30s)

The log is rotated at 5 MB (old file gets a .1 suffix). All writes are
line-buffered so a crash loses at most one line.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_MAX_BYTES = 5 * 1024 * 1024  # 5 MB before rotation
_LOG_FILENAME = "tui.log"


class TuiLogger:
    """Append-only structured logger for the TUI manager's lifetime."""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._path = log_dir / _LOG_FILENAME
        self._start_time = time.time()
        self._fp: Optional[TextIO] = None
        self._open()

    def _open(self) -> None:
        """Open (or reopen) the log file in append mode, creating the dir if needed."""
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._maybe_rotate()
        try:
            self._fp = open(self._path, "a", encoding="utf-8", buffering=1)
        except OSError:
            self._fp = None  # silently degrade — don't crash the TUI over a log

    def _maybe_rotate(self) -> None:
        """Rename to .1 if over the size limit."""
        try:
            if self._path.stat().st_size > _MAX_BYTES:
                bak = self._path.with_suffix(".log.1")
                self._path.replace(bak)
        except OSError:
            pass

    def _ts(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _write(self, kind: str, message: str) -> None:
        if self._fp is None:
            return
        try:
            line = f"{self._ts()} | {kind:<7} | {message}\n"
            self._fp.write(line)
        except OSError:
            pass  # don't crash the TUI

    # ── public API ─────────────────────────────────────────────────────────

    def start(self, version: str, project: str = "", model: str = "") -> None:
        """Log the TUI session start."""
        parts = [f"webAgent TUI {version}"]
        if project:
            parts.append(f"project={project}")
        if model:
            parts.append(f"model={model}")
        self._write("START", " | ".join(parts))

    def stop(self) -> None:
        """Log the TUI session end with uptime."""
        uptime = time.time() - self._start_time
        if uptime < 120:
            dur = f"{uptime:.0f}s"
        elif uptime < 7200:
            dur = f"{uptime / 60:.0f}m {uptime % 60:.0f}s"
        else:
            dur = f"{uptime / 3600:.1f}h"
        self._write("STOP", f"session ended (uptime {dur})")
        self.close()

    def user(self, text: str) -> None:
        """Log a user message (first 500 chars)."""
        self._write("USER", text[:500])

    def assistant(self, text: str) -> None:
        """Log an assistant reply (first 500 chars)."""
        self._write("ASSIST", text[:500])

    def warn(self, text: str) -> None:
        """Log a warning or notable note."""
        self._write("WARN", text)

    def error(self, text: str) -> None:
        """Log an error."""
        self._write("ERROR", text)

    def server(self, text: str) -> None:
        """Log a server state change."""
        self._write("SERVER", text)

    def event(self, text: str) -> None:
        """Log a general notable event (tool call, watchdog action, etc.)."""
        self._write("EVENT", text)

    def close(self) -> None:
        if self._fp is not None:
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None

    def read_back(self, lines: int = 50) -> str:
        """Read the last N lines of the log (for display in the TUI)."""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
            return "".join(all_lines[-lines:])
        except (OSError, FileNotFoundError):
            return "(log unavailable)"


# Avoid a circular import for the type hint above
from typing import TextIO  # noqa: E402, F811
