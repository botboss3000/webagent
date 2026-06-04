"""Load the manager's human-readable configuration (prompt, tool list, monitor
defaults) from files instead of baking it into Python.

Two locations, in priority order:

* **user override** — a copy in the per-user data dir (``config.data_dir()``).
  Editable without touching the source tree / rebuilding the exe. Wins when present.
* **packaged default** — files shipped beside this package under ``manager/``
  (bundled into the frozen exe via ``scripts/build_exe.py``). The fallback.

If neither is readable we degrade gracefully: the prompt falls back to a short
built-in string, the tool overlay to ``{}`` (Python's own descriptions stand),
and monitor defaults to a built-in dict — so a missing/corrupt file never stops
the manager from running.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import data_dir

# Files shipped beside the package (source tree AND frozen bundle).
_PKG_MANAGER = Path(__file__).resolve().parent / "manager"

PROMPT_FILE = "prompt.md"
TOOLS_FILE = "tools.json"
MONITOR_DEFAULTS_FILE = "monitor.defaults.json"

# Last-ditch prompt if both the user copy and the packaged copy are unreadable.
_FALLBACK_PROMPT = (
    "You are webAgent Server Manager — a server-independent agent that helps "
    "install, link, run, diagnose, monitor, and update a local webAgent server, "
    "and can handle general coding tasks. Only offer to perform an action if you "
    "have a tool for it; otherwise guide in plain text."
)

_FALLBACK_MONITOR_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "interval_seconds": 20,
    "autonomy": "auto_restart",        # notify | auto_restart | self_heal
    "channels": ["desktop"],
    "auto_restart": True,
    "restart_backoff_seconds": 15,
    "max_restarts_per_hour": 5,
    "error_rate_threshold": 10,        # new error-level diagnostics per interval → alert
    "disk_percent_threshold": 90,      # host disk usage % → alert (0 disables)
    "mem_percent_threshold": 90,       # host memory usage % → alert (0 disables)
    "cpu_percent_threshold": 0,        # host CPU % → alert (0 disables; CPU is noisy)
    "notify_on_server_down": True,
    "notify_on_recovery": True,
    "digest_minutes": 60,
    # ── Self-healing Playbook policy ──
    "remediation_mode": "safe_auto",            # document | safe_auto | autonomous
    "remediation_confidence_threshold": 0.6,    # a learned command must clear this to keep auto-running
    "program_trigger_after": 3,                 # recurrences before a diagnostic issue auto-programs an alarm
}


def _read_text(*candidates: Path) -> str:
    for p in candidates:
        try:
            t = p.read_text(encoding="utf-8")
            if t.strip():
                return t
        except OSError:
            continue
    return ""


def _read_json(*candidates: Path) -> Any:
    for p in candidates:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return None


# ── system prompt ──────────────────────────────────────────────────────────
def load_prompt() -> str:
    """The manager's system prompt: user override → packaged default → fallback."""
    text = _read_text(data_dir() / PROMPT_FILE, _PKG_MANAGER / PROMPT_FILE)
    return text if text else _FALLBACK_PROMPT


# ── tool overlay (descriptions + enabled + category) ─────────────────────────
def load_tool_overrides() -> dict[str, dict]:
    """Map of ``{tool_name: {description, enabled, category}}`` from tools.json.

    User override wins over the packaged copy. Returns ``{}`` when neither is
    readable — the registry then keeps Python's own descriptions and leaves
    every tool enabled.
    """
    data = _read_json(data_dir() / TOOLS_FILE, _PKG_MANAGER / TOOLS_FILE)
    if not isinstance(data, dict):
        return {}
    # Accept either {tools: {...}} or a flat {name: {...}} mapping.
    tools = data.get("tools") if isinstance(data.get("tools"), dict) else data
    out: dict[str, dict] = {}
    for name, spec in tools.items():
        if isinstance(spec, dict):
            out[str(name)] = spec
    return out


# ── monitor defaults ─────────────────────────────────────────────────────────
def load_monitor_defaults() -> dict[str, Any]:
    """Shipped watchdog defaults (packaged monitor.defaults.json), merged onto the
    built-in fallback so a partial file still yields a complete config."""
    data = _read_json(_PKG_MANAGER / MONITOR_DEFAULTS_FILE)
    merged = dict(_FALLBACK_MONITOR_DEFAULTS)
    if isinstance(data, dict):
        merged.update({k: v for k, v in data.items() if k in merged})
    return merged
