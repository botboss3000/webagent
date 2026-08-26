"""Modular, per-agent chat execution modes.

The permission policy is intentionally a two-state capability boundary:
``read_only`` never executes mutating tools and ``write`` may execute them. A
mode then layers its own prompt and structured completion contract on top of
that policy.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Dict, List, Optional

_MODE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_POLICIES = {"read_only", "write"}


def _global_prompts() -> Dict[str, str]:
    try:
        from app.util.paths import app_prompts_path
        data = json.loads(app_prompts_path().read_text(encoding="utf-8"))
        entries = data.get("execution_modes") or {}
        return {
            key: str((value or {}).get("template") or (value or {}).get("text") or "")
            for key, value in entries.items() if isinstance(value, dict)
        }
    except Exception:
        return {}


def default_execution_modes() -> List[Dict[str, Any]]:
    """Return fresh default Ask, Plan, and Auto definitions."""
    prompts = _global_prompts()
    return [
        {
            "id": "ask", "label": "Ask",
            "description": "Research freely and deliver a change proposal without executing it.",
            "permission_policy": "read_only", "prompt": prompts.get("ask", ""),
            "contract": {
                "require_plan_document": False, "carry_forward": True,
                "checklist": [
                    {"id": "ask-scope", "label": "The request, scope, and expected outcome are understood."},
                    {"id": "ask-proposal", "label": "A concrete proposal explains the intended changes and the next execution step."},
                    {"id": "ask-no-execution", "label": "No requested implementation or other mutating action was executed while Ask mode was active; execution in a write-capable segment is allowed.", "carry_forward": False},
                ],
                "max_rounds": 0, "send_back": False,
            },
            "builtin": True,
        },
        {
            "id": "plan", "label": "Plan",
            "description": "Investigate deeply and deliver an execution-ready plan.",
            "permission_policy": "read_only", "prompt": prompts.get("plan", ""),
            "contract": {
                "require_plan_document": True, "carry_forward": True,
                "checklist": [
                    {"id": "plan-document", "label": "A persistent Plan Overview document was created or updated with present_plan."},
                    {"id": "plan-steps", "label": "The persistent Plan Checklist contains concrete, ordered implementation steps."},
                    {"id": "plan-assumptions", "label": "Material assumptions and unresolved questions are stated explicitly."},
                    {"id": "plan-evidence", "label": "The plan is grounded in evidence gathered with read-only research."},
                    {"id": "plan-no-execution", "label": "No requested implementation or other mutating action was executed while Plan mode was active; Plan mode ends with an execution-ready plan.", "carry_forward": False},
                ],
                "max_rounds": 1, "send_back": True,
            },
            "builtin": True,
        },
        {
            "id": "auto", "label": "Auto",
            "description": "Work autonomously with write-capable permissions.",
            "permission_policy": "write", "prompt": prompts.get("auto", ""),
            "contract": {
                "require_plan_document": False, "carry_forward": True,
                "checklist": [
                    {"id": "auto-track", "label": "Changes are tracked against the active plan and all carried-forward checklist items."},
                    {"id": "auto-change-list", "label": "The concrete files, systems, or external state changed are listed in the final report."},
                    {"id": "auto-complete", "label": "The requested work is complete, or a concrete blocker is clearly reported."},
                    {"id": "auto-verify", "label": "Changes made were verified in proportion to their risk."},
                ],
                "max_rounds": 1, "send_back": True,
            },
            "builtin": True,
        },
    ]


def normalize_mode_id(value: Any, fallback: str = "ask") -> str:
    mode_id = str(value or "").strip().lower()
    mode_id = {"read": "plan", "write": "ask"}.get(mode_id, mode_id)
    return mode_id if _MODE_ID_RE.fullmatch(mode_id) else fallback


def _normalize_one(raw: Any, *, builtin: bool = False) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    mode_id = normalize_mode_id(raw.get("id"), fallback="")
    if not mode_id:
        return None
    policy = str(raw.get("permission_policy") or "read_only").strip().lower()
    if policy not in _POLICIES:
        policy = "read_only"
    contract_raw = raw.get("contract") if isinstance(raw.get("contract"), dict) else {}
    # Migration path for the first modular-mode draft, which stored these
    # fields at mode top-level. Explicit legacy fields override merged defaults.
    checklist = raw.get("checklist") if "checklist" in raw else contract_raw.get("checklist", [])
    if isinstance(checklist, str):
        checklist = checklist.splitlines()
    if not isinstance(checklist, list):
        checklist = []
    normalized_items = []
    used_ids = set()
    for index, item in enumerate(checklist[:30], start=1):
        if isinstance(item, dict):
            label = str(item.get("label") or item.get("requirement") or "").strip()[:500]
            item_id = normalize_mode_id(item.get("id"), fallback="")
        else:
            label = str(item).strip()[:500]
            item_id = ""
        if not label:
            continue
        base_id = item_id or f"{mode_id}-requirement-{index}"
        unique_id = base_id
        suffix = 2
        while unique_id in used_ids:
            unique_id = f"{base_id}-{suffix}"
            suffix += 1
        used_ids.add(unique_id)
        normalized_items.append({
            "id": unique_id,
            "label": label,
            "carry_forward": bool(item.get("carry_forward", True)) if isinstance(item, dict) else True,
        })
    rounds_raw = raw.get("max_rounds") if "max_rounds" in raw else contract_raw.get("max_rounds", 0)
    try:
        max_rounds = max(0, min(5, int(rounds_raw or 0)))
    except (TypeError, ValueError):
        max_rounds = 0
    send_back_raw = raw.get("send_back") if "send_back" in raw else contract_raw.get("send_back", bool(max_rounds))
    return {
        "id": mode_id,
        "label": (str(raw.get("label") or mode_id).strip() or mode_id)[:40],
        "description": str(raw.get("description") or "").strip()[:240],
        "permission_policy": policy,
        "prompt": str(raw.get("prompt") or "").strip()[:30000],
        "contract": {
            "require_plan_document": bool(contract_raw.get("require_plan_document", False)),
            "carry_forward": bool(contract_raw.get("carry_forward", True)),
            "checklist": normalized_items,
            "max_rounds": max_rounds,
            "send_back": bool(send_back_raw),
        },
        "builtin": builtin,
    }


def normalize_execution_modes(raw: Any) -> List[Dict[str, Any]]:
    """Merge stored mode overrides with the non-removable defaults."""
    defaults = default_execution_modes()
    default_map = {item["id"]: item for item in defaults}
    supplied = raw if isinstance(raw, list) else []
    custom: List[Dict[str, Any]] = []
    seen = set()
    for item in supplied:
        mode_id = normalize_mode_id(item.get("id") if isinstance(item, dict) else None, "")
        if not mode_id or mode_id in seen:
            continue
        seen.add(mode_id)
        if mode_id in default_map:
            merged = {**default_map[mode_id], **item, "id": mode_id}
            normalized = _normalize_one(merged, builtin=True)
            if normalized:
                default_map[mode_id] = normalized
        else:
            normalized = _normalize_one(item, builtin=False)
            if normalized:
                custom.append(normalized)
    return [deepcopy(default_map[key]) for key in ("ask", "plan", "auto")] + custom[:17]


def execution_modes_for_agent(agent_rec: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    meta: Dict[str, Any] = {}
    if agent_rec:
        raw = agent_rec.get("metadata")
        if isinstance(raw, str):
            try:
                meta = json.loads(raw) or {}
            except Exception:
                meta = {}
        elif isinstance(raw, dict):
            meta = raw
    return normalize_execution_modes(meta.get("execution_modes"))


def resolve_execution_mode(agent_rec: Optional[Dict[str, Any]], mode_id: Any) -> Dict[str, Any]:
    modes = execution_modes_for_agent(agent_rec)
    wanted = normalize_mode_id(mode_id)
    return next((mode for mode in modes if mode["id"] == wanted), modes[0])


def contract_for_mode(mode: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (mode or {}).get("contract") if isinstance((mode or {}).get("contract"), dict) else {}


def contract_requirements(mode: Optional[Dict[str, Any]]) -> List[str]:
    items = contract_for_mode(mode).get("checklist") or []
    return [str(item.get("label") or "").strip() for item in items
            if isinstance(item, dict) and str(item.get("label") or "").strip()]


def accumulated_contract(
    agent_rec: Optional[Dict[str, Any]], mode_ids: List[str], current_mode: Any,
    *, run_scoped: bool = False, executed_modes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Combine the active mode with carry-forward contracts used earlier."""
    current_id = normalize_mode_id(current_mode)
    ordered_ids = []
    for mode_id in [*(mode_ids or []), current_id]:
        normalized = normalize_mode_id(mode_id, fallback="")
        if normalized and normalized not in ordered_ids:
            ordered_ids.append(normalized)
    current_mode_entry = resolve_execution_mode(agent_rec, current_id)
    current_is_write = current_mode_entry.get("permission_policy") == "write"
    # Read-only turns have a terminal deliverable of their own (a proposal or
    # plan). Historical write-mode completion requirements must never leak back
    # into them and demand implementation. Planning requirements carry forward
    # only when the session advances into a write-capable mode.
    if not current_is_write:
        if run_scoped:
            executed = {
                normalize_mode_id(mode_id, fallback="")
                for mode_id in (executed_modes or [])
            }
            # A prior write-capable segment participates only when it actually
            # performed mutating work during THIS run. This prevents an unused
            # Auto start from making a later Ask/Plan turn demand execution.
            ordered_ids = [
                mode_id for mode_id in ordered_ids
                if mode_id == current_id or (
                    mode_id in executed
                    and resolve_execution_mode(agent_rec, mode_id).get("permission_policy") == "write"
                )
            ]
        else:
            ordered_ids = [current_id]

    requirements = []
    sources = []
    seen = set()
    max_rounds = 0
    send_back = False
    for mode_id in ordered_ids:
        mode = resolve_execution_mode(agent_rec, mode_id)
        if mode["id"] != mode_id:
            continue
        contract = contract_for_mode(mode)
        if mode_id != current_id and not contract.get("carry_forward", True):
            continue
        sources.append(mode_id)
        max_rounds = max(max_rounds, int(contract.get("max_rounds") or 0))
        send_back = send_back or bool(contract.get("send_back"))
        for item in contract.get("checklist") or []:
            if not isinstance(item, dict):
                continue
            if mode_id != current_id and not item.get("carry_forward", True):
                continue
            item_id = str(item.get("id") or "")
            label = str(item.get("label") or "").strip()
            key = f"{mode_id}:{item_id or label}"
            if label and key not in seen:
                seen.add(key)
                requirements.append({"id": item_id, "label": label, "mode": mode_id})
    return {"mode_ids": sources, "checklist": requirements,
            "max_rounds": max_rounds, "send_back": send_back}
