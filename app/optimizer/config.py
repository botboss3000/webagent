"""
Load/save optimizer.json — configuration for the skill optimizer.
Lives in project root, same pattern as provider.json.

CONFIGURABLE VALUES:
- schedule: interval (per-interaction/hourly/daily/weekly), min_interactions
- mode: "live" (after every msg) or "scheduled" (on timer)
- user_feedback: "always" / "on_failure" / "never"
- sessions: show_in_list (bool), allow_pause_for_input (bool)
- models: analyzer, proposer, validator model names (all use same provider/key as main agent)
- target_metrics: list of enabled metrics
- intensity: 1-5 (conservative → aggressive) — adjusts thresholds automatically
- app_wide: min_sample, min_skill_age_days, auto_deploy
- per_user: min_sample, min_skill_age_days, auto_deploy
- notifications: notify_user, notify_devs, channel
- state: last_run_at, improvements_deployed, last_run_status
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "data" / "config" / "optimizer.json"


DEFAULTS: Dict[str, Any] = {
    "mode": "live",
    "user_feedback": "always",
    "intensity": 3,
    "sessions": {
        "show_in_list": True,
        "allow_pause_for_input": True,
    },
    "schedule": {
        "interval": "per-interaction",
        "min_interactions": 0,
    },
    "models": {
        "analyzer": None,   # None = use same as main agent
        "proposer": None,
        "validator": None,
    },
    "trials": {"per_change": 2},
    "target_metrics": ["turns", "tokens", "time", "failures", "rating"],
    "app_wide": {
        "auto_deploy": True,
        "min_sample": 5,         # low for dev/testing
        "min_skill_age_days": 0,  # allow immediate optimization in dev
    },
    "per_user": {
        "auto_deploy": True,
        "min_sample": 3,
        "min_skill_age_days": 0,
    },
    "notifications": {
        "notify_user": True,
        "notify_devs": False,
        "channel": "email",
    },
    "state": {
        "last_run_at": None,
        "improvements_deployed": 0,
        "last_run_status": None,
    },
}

# ── Intensity threshold map ──
INTENSITY_MAP: Dict[int, Dict[str, Any]] = {
    1: {"min_improvement_pct": 20, "strictness": "strict",  "min_sample_multiplier": 2.0,
        "targets": {"failures": 10, "turns": 8, "tokens": 1000, "time_ms": 8000}},
    2: {"min_improvement_pct": 15, "strictness": "strict",  "min_sample_multiplier": 1.5,
        "targets": {"failures": 8,  "turns": 6, "tokens": 800,  "time_ms": 6000}},
    3: {"min_improvement_pct": 10, "strictness": "balanced","min_sample_multiplier": 1.0,
        "targets": {"failures": 5,  "turns": 4, "tokens": 500,  "time_ms": 5000}},
    4: {"min_improvement_pct": 7,  "strictness": "relaxed", "min_sample_multiplier": 0.7,
        "targets": {"failures": 3,  "turns": 3, "tokens": 300,  "time_ms": 3000}},
    5: {"min_improvement_pct": 5,  "strictness": "lenient", "min_sample_multiplier": 0.5,
        "targets": {"failures": 1,  "turns": 2, "tokens": 200,  "time_ms": 2000}},
}


def load_config() -> dict:
    """Load optimizer config, merging with defaults for missing keys."""
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.is_file():
        try:
            raw = CONFIG_PATH.read_text(encoding="utf-8")
            loaded = json.loads(raw)
            _deep_merge(cfg, loaded)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load optimizer.json: %s", e)
    return cfg


def save_config(data: dict) -> None:
    """Save optimizer config to disk."""
    try:
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        logger.error("Failed to save optimizer.json: %s", e)
        raise


def update_state(**kwargs) -> None:
    """Update just the state section of the config."""
    cfg = load_config()
    cfg.setdefault("state", {}).update(kwargs)
    save_config(cfg)


def get_intensity_thresholds(intensity: int) -> dict:
    """Get threshold values for a given intensity level (1-5)."""
    return INTENSITY_MAP.get(intensity, INTENSITY_MAP[3])


def _deep_merge(base: dict, override: dict) -> None:
    """Merge override into base in-place, recursively."""
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val
