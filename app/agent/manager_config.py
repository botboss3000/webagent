"""Normalization for the per-agent, mode-aware Manager Loop configuration.

The canonical object is stored at ``metadata['manager']``.  The normalizer also
understands the original flat Manager keys, so existing agents remain opt-in and
keep their current behaviour.
"""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Dict, Optional

from app.agent.execution_modes import normalize_mode_id


TRIGGER_KINDS = ("plan_gate", "edit_gate", "commit_gate")
POLICIES = {"off", "async", "blocking"}
WATCHDOG_ACTIONS = {"observe", "advise", "replan", "verify", "pause_and_ask"}
REASONING_EFFORTS = {"minimal", "low", "medium", "high"}
DEFAULT_KIND_BUDGETS = {
    "plan_gate": 1, "edit_gate": 4, "watchdog": 3, "commit_gate": 1,
}


def default_manager_loop() -> Dict[str, Any]:
    return {
        "enabled": False,
        "model": None,
        "effort": None,
        "budgets": {
            "max_checks": 9,
            "max_blocks": 3,
            "by_trigger": dict(DEFAULT_KIND_BUDGETS),
        },
        "starter": {
            "enabled": False,
            "parallel": True,
            "first_response": True,
            "wait_before_write": False,
            "inherit_prior_summary": True,
            "seed_plan": True,
            "seed_checklist": True,
            "prompt": "",
        },
        "triggers": {
            "plan_gate": {"policy": "off", "modes": ["plan", "auto"], "prompt": ""},
            "edit_gate": {"policy": "off", "modes": ["auto"], "prompt": ""},
            "commit_gate": {"policy": "off", "modes": ["auto"], "prompt": ""},
        },
        "watchdog": {
            "enabled": False,
            "every_n_turns": 0,
            "on_errors": 0,
            "error_window": 8,
            "on_stall": True,
            "cooldown_turns": 2,
            "action": "advise",
            "modes": ["ask", "plan", "auto"],
            "prompt": "",
        },
        "closer": {
            "audit_mode_contract": True,
            "audit_agent_checklist": True,
            "require_plan_document": True,
            "require_manager_clear": False,
            "max_rounds": 1,
            "send_back": True,
        },
        "contracts": {
            "enabled": False,
            "engine": "subagent",
            "failure_policy": "hybrid",
            "scout": {
                "enabled": True,
                "timeout_seconds": 60,
                "model": None,
            },
            "edit_review": {
                "enabled": True,
                "policy": "blocking",
                "timeout_seconds": 60,
                "max_checks": 4,
                "model": None,
            },
            "close_review": {
                "enabled": True,
                "policy": "blocking",
                "timeout_seconds": 120,
                "max_rounds": 1,
                "model": None,
            },
        },
        "mode_overrides": {},
    }


def _metadata(agent_rec: Optional[dict]) -> Dict[str, Any]:
    raw: Any = (agent_rec or {}).get("metadata", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) or {}
        except Exception:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def _deep_merge(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _integer(value: Any, default: int, low: int, high: int, *, strict: bool) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        if strict:
            raise ValueError("must be an integer")
        return default
    if strict and not low <= number <= high:
        raise ValueError(f"must be between {low} and {high}")
    return max(low, min(high, number))


def _modes(value: Any, default: list[str], *, strict: bool) -> list[str]:
    if value is None:
        return list(default)
    if not isinstance(value, list):
        if strict:
            raise ValueError("modes must be a list")
        return list(default)
    out: list[str] = []
    for item in value:
        mode_id = normalize_mode_id(item, fallback="")
        if not mode_id:
            if strict:
                raise ValueError(f"invalid mode id: {item!r}")
            continue
        if mode_id not in out:
            out.append(mode_id)
    return out


def _legacy_to_canonical(raw: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay original flat keys onto the canonical shape."""
    out = deepcopy(raw)
    triggers = out.setdefault("triggers", {}) if isinstance(out.get("triggers"), dict) else {}
    out["triggers"] = triggers
    saw_active = False
    for kind in TRIGGER_KINDS:
        if kind in raw:
            policy = str(raw.get(kind) or "off").lower()
            if policy == "on":
                policy = "async"
            trigger = triggers.get(kind) if isinstance(triggers.get(kind), dict) else {}
            trigger = dict(trigger)
            trigger.setdefault("policy", policy)
            triggers[kind] = trigger
            saw_active = saw_active or policy in ("async", "blocking")
        legacy_prompt = meta.get(f"manager_{kind}_prompt")
        if isinstance(legacy_prompt, str) and legacy_prompt.strip():
            trigger = triggers.get(kind) if isinstance(triggers.get(kind), dict) else {}
            trigger = dict(trigger)
            trigger.setdefault("prompt", legacy_prompt.strip())
            triggers[kind] = trigger
    if "max_checks" in raw or "max_blocks" in raw or "max_checks_by_kind" in raw:
        budgets = out.get("budgets") if isinstance(out.get("budgets"), dict) else {}
        budgets = dict(budgets)
        budgets.setdefault("max_checks", raw.get("max_checks"))
        budgets.setdefault("max_blocks", raw.get("max_blocks"))
        budgets.setdefault("by_trigger", raw.get("max_checks_by_kind"))
        out["budgets"] = budgets
    legacy_wd = raw.get("watchdog")
    if legacy_wd == "on" or legacy_wd == "async":
        legacy_wd = {"every_n_turns": 6, "on_stall": True}
    if isinstance(legacy_wd, dict) and not (
        isinstance(out.get("watchdog"), dict) and "enabled" in out["watchdog"]
    ):
        wd = dict(legacy_wd)
        active = bool(wd.get("every_n_turns") or wd.get("on_errors") or wd.get("on_stall", True))
        wd["enabled"] = active
        out["watchdog"] = wd
        saw_active = saw_active or active
    legacy_wd_prompt = meta.get("manager_watchdog_prompt")
    if isinstance(legacy_wd_prompt, str) and legacy_wd_prompt.strip():
        wd = out.get("watchdog") if isinstance(out.get("watchdog"), dict) else {}
        out["watchdog"] = {**wd, "prompt": legacy_wd_prompt.strip()}
    # Old configurations had no master switch. Active triggers imply enabled.
    if "enabled" not in raw and saw_active:
        out["enabled"] = True
    return out


def normalize_manager_loop(
    raw: Any,
    *,
    strict: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a complete canonical configuration.

    ``strict=True`` is intended for API writes and rejects malformed shapes or
    enums. Reads are deliberately forgiving so a bad historical row fails off.
    """
    if raw is None:
        raw = {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) or {}
        except Exception as exc:
            if strict:
                raise ValueError("manager_loop must be an object") from exc
            raw = {}
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("manager_loop must be an object")
        raw = {}
    raw = _legacy_to_canonical(raw, metadata or {})
    cfg = _deep_merge(default_manager_loop(), raw)

    cfg["enabled"] = bool(cfg.get("enabled", False))
    model = cfg.get("model")
    effort = cfg.get("effort")
    cfg["model"] = str(model).strip()[:200] if model not in (None, "") else None
    cfg["effort"] = str(effort).strip().lower()[:40] if effort not in (None, "") else None
    if cfg["effort"] == "default":
        cfg["effort"] = None
    if cfg["effort"] is not None and cfg["effort"] not in REASONING_EFFORTS:
        if strict:
            raise ValueError("effort must be minimal, low, medium, high, or null")
        cfg["effort"] = None

    budgets = cfg.get("budgets")
    if not isinstance(budgets, dict):
        if strict:
            raise ValueError("budgets must be an object")
        budgets = {}
    by_kind = budgets.get("by_trigger")
    if not isinstance(by_kind, dict):
        if strict:
            raise ValueError("budgets.by_trigger must be an object")
        by_kind = {}
    normalized_by_kind = {}
    for kind, default in DEFAULT_KIND_BUDGETS.items():
        try:
            normalized_by_kind[kind] = _integer(by_kind.get(kind, default), default, 1, 100, strict=strict)
        except ValueError as exc:
            raise ValueError(f"budgets.by_trigger.{kind} {exc}") from exc
    try:
        max_checks = _integer(budgets.get("max_checks", 9), 9, 1, 500, strict=strict)
        max_blocks = _integer(budgets.get("max_blocks", 3), 3, 1, 100, strict=strict)
    except ValueError as exc:
        raise ValueError(f"budgets {exc}") from exc
    cfg["budgets"] = {"max_checks": max_checks, "max_blocks": max_blocks, "by_trigger": normalized_by_kind}

    starter = cfg.get("starter")
    if not isinstance(starter, dict):
        if strict:
            raise ValueError("starter must be an object")
        starter = {}
    starter_defaults = default_manager_loop()["starter"]
    cfg["starter"] = {
        key: (str(starter.get(key) or "").strip()[:30000] if key == "prompt" else bool(starter.get(key, default)))
        for key, default in starter_defaults.items()
    }

    trigger_defaults = default_manager_loop()["triggers"]
    triggers = cfg.get("triggers")
    if not isinstance(triggers, dict):
        if strict:
            raise ValueError("triggers must be an object")
        triggers = {}
    normalized_triggers: Dict[str, Any] = {}
    for kind in TRIGGER_KINDS:
        item = triggers.get(kind)
        if not isinstance(item, dict):
            if strict:
                raise ValueError(f"triggers.{kind} must be an object")
            item = {}
        policy = str(item.get("policy") or "off").strip().lower()
        if policy == "on":
            policy = "async"
        if policy not in POLICIES:
            if strict:
                raise ValueError(f"triggers.{kind}.policy must be off, async, or blocking")
            policy = "off"
        normalized_triggers[kind] = {
            "policy": policy,
            "modes": _modes(item.get("modes"), trigger_defaults[kind]["modes"], strict=strict),
            "prompt": str(item.get("prompt") or "").strip()[:30000],
        }
    cfg["triggers"] = normalized_triggers

    wd = cfg.get("watchdog")
    if not isinstance(wd, dict):
        if strict:
            raise ValueError("watchdog must be an object")
        wd = {}
    action = str(wd.get("action") or "advise").strip().lower()
    if action not in WATCHDOG_ACTIONS:
        if strict:
            raise ValueError("watchdog.action is invalid")
        action = "advise"
    try:
        cfg["watchdog"] = {
            "enabled": bool(wd.get("enabled", False)),
            "every_n_turns": _integer(wd.get("every_n_turns", 0), 0, 0, 1000, strict=strict),
            "on_errors": _integer(wd.get("on_errors", 0), 0, 0, 100, strict=strict),
            "error_window": _integer(wd.get("error_window", 8), 8, 1, 1000, strict=strict),
            "on_stall": bool(wd.get("on_stall", True)),
            "cooldown_turns": _integer(wd.get("cooldown_turns", 2), 2, 0, 1000, strict=strict),
            "action": action,
            "modes": _modes(wd.get("modes"), ["ask", "plan", "auto"], strict=strict),
            "prompt": str(wd.get("prompt") or "").strip()[:30000],
        }
    except ValueError as exc:
        raise ValueError(f"watchdog {exc}") from exc

    closer = cfg.get("closer")
    if not isinstance(closer, dict):
        if strict:
            raise ValueError("closer must be an object")
        closer = {}
    closer_defaults = default_manager_loop()["closer"]
    try:
        cfg["closer"] = {
            **{key: bool(closer.get(key, default)) for key, default in closer_defaults.items()
               if key != "max_rounds"},
            "max_rounds": _integer(closer.get("max_rounds", 1), 1, 0, 5, strict=strict),
        }
    except ValueError as exc:
        raise ValueError(f"closer.max_rounds {exc}") from exc

    contracts = cfg.get("contracts")
    if not isinstance(contracts, dict):
        if strict:
            raise ValueError("contracts must be an object")
        contracts = {}
    contract_defaults = default_manager_loop()["contracts"]
    failure_policy = str(
        contracts.get("failure_policy") or contract_defaults["failure_policy"]
    ).strip().lower()
    if failure_policy not in {"hybrid", "fail_closed", "advisory"}:
        if strict:
            raise ValueError("contracts.failure_policy is invalid")
        failure_policy = "hybrid"
    engine = str(contracts.get("engine") or "subagent").strip().lower()
    if engine not in {"subagent", "single_call"}:
        if strict:
            raise ValueError("contracts.engine must be subagent or single_call")
        engine = "subagent"

    def _contract_lane(name: str, *, has_policy: bool = False,
                       has_rounds: bool = False, has_checks: bool = False) -> Dict[str, Any]:
        default = contract_defaults[name]
        value = contracts.get(name)
        if not isinstance(value, dict):
            if strict and value is not None:
                raise ValueError(f"contracts.{name} must be an object")
            value = {}
        lane: Dict[str, Any] = {
            "enabled": bool(value.get("enabled", default["enabled"])),
            "timeout_seconds": _integer(
                value.get("timeout_seconds", default["timeout_seconds"]),
                default["timeout_seconds"], 1, 600, strict=strict,
            ),
            "model": (
                str(value.get("model")).strip()[:200]
                if value.get("model") not in (None, "") else None
            ),
        }
        if has_policy:
            policy = str(value.get("policy") or default["policy"]).strip().lower()
            if policy not in POLICIES:
                if strict:
                    raise ValueError(f"contracts.{name}.policy is invalid")
                policy = default["policy"]
            lane["policy"] = policy
        if has_rounds:
            lane["max_rounds"] = _integer(
                value.get("max_rounds", default["max_rounds"]),
                default["max_rounds"], 0, 5, strict=strict,
            )
        if has_checks:
            lane["max_checks"] = _integer(
                value.get("max_checks", default["max_checks"]),
                default["max_checks"], 1, 100, strict=strict,
            )
        return lane

    cfg["contracts"] = {
        "enabled": bool(contracts.get("enabled", False)),
        "engine": engine,
        "failure_policy": failure_policy,
        "scout": _contract_lane("scout"),
        "edit_review": _contract_lane(
            "edit_review", has_policy=True, has_checks=True,
        ),
        "close_review": _contract_lane(
            "close_review", has_policy=True, has_rounds=True,
        ),
    }

    overrides = cfg.get("mode_overrides")
    if not isinstance(overrides, dict):
        if strict:
            raise ValueError("mode_overrides must be an object")
        overrides = {}
    normalized_overrides: Dict[str, Any] = {}
    for raw_mode, value in overrides.items():
        mode_id = normalize_mode_id(raw_mode, fallback="")
        if not mode_id or not isinstance(value, dict):
            if strict:
                raise ValueError(f"invalid mode_overrides entry: {raw_mode!r}")
            continue
        item: Dict[str, Any] = {}
        trigger_overrides = value.get("triggers", {})
        if trigger_overrides is not None and not isinstance(trigger_overrides, dict):
            if strict:
                raise ValueError(f"mode_overrides.{mode_id}.triggers must be an object")
            trigger_overrides = {}
        clean_triggers = {}
        for kind, policy_value in trigger_overrides.items():
            if kind not in TRIGGER_KINDS:
                continue
            policy = str(policy_value or "off").lower()
            if policy == "on":
                policy = "async"
            if policy not in POLICIES:
                if strict:
                    raise ValueError(f"mode_overrides.{mode_id}.triggers.{kind} is invalid")
                continue
            clean_triggers[kind] = policy
        if clean_triggers:
            item["triggers"] = clean_triggers
        if "watchdog" in value:
            watchdog_policy = str(value.get("watchdog") or "off").lower()
            if watchdog_policy == "on":
                watchdog_policy = "async"
            if watchdog_policy not in {"off", "async"}:
                if strict:
                    raise ValueError(f"mode_overrides.{mode_id}.watchdog is invalid")
            else:
                item["watchdog"] = watchdog_policy
        normalized_overrides[mode_id] = item
    cfg["mode_overrides"] = normalized_overrides
    # Do not leak legacy flat aliases or unknown keys into the public API.
    return {key: cfg[key] for key in default_manager_loop()}


def manager_loop_for_agent(agent_rec: Optional[dict]) -> Dict[str, Any]:
    meta = _metadata(agent_rec)
    return normalize_manager_loop(meta.get("manager"), metadata=meta)


def merge_manager_loop_update(agent_rec: Optional[dict], incoming: Any) -> Dict[str, Any]:
    """Validate and deep-merge a partial API update over effective config."""
    if not isinstance(incoming, dict):
        raise ValueError("manager_loop must be an object")
    current = manager_loop_for_agent(agent_rec)
    return normalize_manager_loop(_deep_merge(current, incoming), strict=True)


def resolve_manager_loop(agent_rec: Optional[dict], mode_id: Any = None) -> Dict[str, Any]:
    """Resolve mode applicability/overrides without changing mode permissions."""
    cfg = manager_loop_for_agent(agent_rec)
    selected = normalize_mode_id(mode_id, fallback="") if mode_id is not None else ""
    resolved = deepcopy(cfg)
    for kind, trigger in resolved["triggers"].items():
        policy = trigger["policy"] if cfg["enabled"] else "off"
        if selected and selected not in trigger["modes"]:
            policy = "off"
        override = cfg["mode_overrides"].get(selected, {}).get("triggers", {}).get(kind)
        if override is not None:
            policy = override if cfg["enabled"] else "off"
        trigger["policy"] = policy
    wd = resolved["watchdog"]
    wd_enabled = cfg["enabled"] and wd["enabled"] and (not selected or selected in wd["modes"])
    wd_override = cfg["mode_overrides"].get(selected, {}).get("watchdog")
    if wd_override is not None:
        wd_enabled = cfg["enabled"] and wd_override == "async"
    wd["enabled"] = wd_enabled
    if not cfg["enabled"]:
        resolved["contracts"]["enabled"] = False
    return resolved


def legacy_manager_view(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Flat keys consumed by the existing loop, plus the canonical config."""
    out = deepcopy(cfg)
    for kind in TRIGGER_KINDS:
        out[kind] = cfg["triggers"][kind]["policy"]
    out["max_checks_by_kind"] = dict(cfg["budgets"]["by_trigger"])
    contracts = cfg.get("contracts") or {}
    edit_contract = contracts.get("edit_review") or {}
    if (cfg.get("enabled") and contracts.get("enabled")
            and contracts.get("engine") == "subagent"
            and edit_contract.get("enabled")
            and edit_contract.get("policy") != "off"):
        # The legacy loop remains the lifecycle hook. Contract policy makes
        # the two edit gates mandatory even when their single-call switches
        # are off; run_manager_check then chooses contract or fallback.
        out["plan_gate"] = edit_contract["policy"]
        out["edit_gate"] = edit_contract["policy"]
        out["max_checks_by_kind"]["plan_gate"] = max(
            1, int(edit_contract.get("max_checks") or 1),
        )
        out["max_checks_by_kind"]["edit_gate"] = max(
            1, int(edit_contract.get("max_checks") or 1),
        )
    wd = cfg["watchdog"]
    out["watchdog"] = ({
        "every_n_turns": wd["every_n_turns"],
        "on_errors": wd["on_errors"],
        "error_window": wd["error_window"],
        "on_stall": wd["on_stall"],
        "cooldown_turns": wd["cooldown_turns"],
    } if wd["enabled"] else "off")
    out["max_checks"] = cfg["budgets"]["max_checks"]
    out["max_blocks"] = cfg["budgets"]["max_blocks"]
    return out
