"""Persisted monitor configuration + alarm rules for the watchdog.

Two human-readable files in the per-user data dir (never inside the repo):

* ``monitor.json`` — the live watchdog config. Seeded from the packaged
  ``manager/monitor.defaults.json`` on first run, then editable by the agent
  (``set_monitor_config``) or the webapp admin panel.
* ``alarms.json`` — the live watch list the watchdog evaluates each heartbeat.
  Grows as the user says "tell me every time X happens" (``add_alarm``).

Both are loaded fresh by the watchdog each tick, so edits are picked up live with
no restart. Writes are atomic (temp + replace) so a crash never leaves a half file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import data_dir
from .resources import load_monitor_defaults

_VALID_AUTONOMY = ("notify", "auto_restart", "self_heal")
_VALID_ACTION = ("notify", "auto_restart")
_VALID_LOUDNESS = ("every", "once", "digest")
_VALID_REMEDIATION_MODE = ("document", "safe_auto", "autonomous")


def monitor_path() -> Path:
    return data_dir() / "monitor.json"


def alarms_path() -> Path:
    return data_dir() / "alarms.json"


def _write_atomic(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


# ── monitor config ───────────────────────────────────────────────────────────
def load_monitor_config() -> dict[str, Any]:
    """The live watchdog config, merged onto the shipped defaults so every key is
    present. Seeds ``monitor.json`` from the defaults on first run."""
    defaults = load_monitor_defaults()
    try:
        raw = monitor_path().read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        _write_atomic(monitor_path(), defaults)
        return dict(defaults)
    if not isinstance(data, dict):
        return dict(defaults)
    merged = dict(defaults)
    merged.update({k: v for k, v in data.items() if k in defaults})
    return merged


def save_monitor_config(cfg: dict[str, Any]) -> None:
    _write_atomic(monitor_path(), cfg)


def update_monitor_config(**changes: Any) -> dict[str, Any]:
    """Apply validated changes to the monitor config and persist. Unknown keys and
    invalid values are dropped. Returns the new config."""
    cfg = load_monitor_config()
    for key, val in changes.items():
        if val is None or key not in cfg:
            continue
        if key == "autonomy" and val not in _VALID_AUTONOMY:
            continue
        if key == "remediation_mode":
            if val in _VALID_REMEDIATION_MODE:
                cfg[key] = val
            continue
        if key == "remediation_confidence_threshold":
            try:
                cfg[key] = max(0.0, min(1.0, float(val)))
            except (TypeError, ValueError):
                pass
            continue
        if key == "program_trigger_after":
            try:
                cfg[key] = max(1, int(val))
            except (TypeError, ValueError):
                pass
            continue
        if key == "channels":
            if isinstance(val, list):
                cfg[key] = [str(c) for c in val if str(c).strip()]
            continue
        if key == "readiness_recovery":
            cfg[key] = bool(val)
            continue
        if key in ("interval_seconds", "error_rate_threshold", "max_restarts_per_hour",
                   "restart_backoff_seconds", "digest_minutes", "readiness_fail_ticks"):
            try:
                cfg[key] = max(1, int(val))
            except (TypeError, ValueError):
                pass
            continue
        if key in ("disk_percent_threshold", "mem_percent_threshold", "cpu_percent_threshold"):
            # Percent thresholds: 0 disables the check, otherwise clamp to 1..100.
            try:
                v = int(val)
                cfg[key] = 0 if v <= 0 else min(100, v)
            except (TypeError, ValueError):
                pass
            continue
        cfg[key] = val
    save_monitor_config(cfg)
    return cfg


# ── alarm rules ────────────────────────────────────────────────────────────
def load_alarms() -> list[dict[str, Any]]:
    try:
        data = json.loads(alarms_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    alarms = data.get("alarms") if isinstance(data, dict) else data
    return [a for a in alarms if isinstance(a, dict)] if isinstance(alarms, list) else []


def save_alarms(alarms: list[dict[str, Any]]) -> None:
    _write_atomic(alarms_path(), {"version": 1, "alarms": alarms})


def _next_id(alarms: list[dict[str, Any]]) -> str:
    nums = []
    for a in alarms:
        aid = str(a.get("id", ""))
        if aid.startswith("a") and aid[1:].isdigit():
            nums.append(int(aid[1:]))
    return f"a{(max(nums) + 1) if nums else 1}"


def add_alarm(contains: str = "", level: str = "", category: str = "",
              action: str = "notify", loudness: str = "every",
              channels: list[str] | None = None, label: str = "") -> dict[str, Any]:
    """Append a new alarm rule and persist. Returns the created rule (with its id).
    Raises ValueError if the rule has no match criteria at all."""
    contains = (contains or "").strip()
    level = (level or "").strip().lower()
    category = (category or "").strip().lower()
    if not (contains or level or category):
        raise ValueError("an alarm needs at least one of: contains, level, category")
    if action not in _VALID_ACTION:
        action = "notify"
    if loudness not in _VALID_LOUDNESS:
        loudness = "every"
    alarms = load_alarms()
    rule = {
        "id": _next_id(alarms),
        "label": (label or "").strip() or (contains or f"{level or category} events"),
        "contains": contains,
        "level": level,
        "category": category,
        "action": action,
        "loudness": loudness,
        "channels": [str(c) for c in (channels or []) if str(c).strip()],
    }
    alarms.append(rule)
    save_alarms(alarms)
    return rule


def remove_alarm(alarm_id: str) -> bool:
    """Delete the rule with this id. Returns True if one was removed."""
    alarms = load_alarms()
    kept = [a for a in alarms if str(a.get("id")) != str(alarm_id)]
    if len(kept) == len(alarms):
        return False
    save_alarms(kept)
    return True
