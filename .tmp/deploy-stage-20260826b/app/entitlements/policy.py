"""Pure policy validation and composition for experience tiers."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping, Optional

POLICY_SCHEMA_VERSION = 1
REASONING_LEVELS = ("default", "low", "medium", "high")
# Compatibility snapshot for imports. Policy validation is syntax-based so a
# drop-in page descriptor never requires editing this module.
KNOWN_PAGES = frozenset({"agents", "automations", "browser", "genui", "instances", "wiki", "admin-tools"})
_PAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
KNOWN_FEATURES = frozenset({
    "chat", "agent_create", "agent_clone", "automations", "connectors",
    "model_picker", "image_input", "image_generation", "voice_llm",
    "attachments", "genui", "user_byod", "llm_byo",
})
KNOWN_ABILITY_GROUPS = frozenset({
    "chat_core", "memory", "user_files", "web_read", "browser_control",
    "ssh_control",
    "image_vision", "image_generation", "model_switching", "automation",
    "agent_orchestration", "personal_integrations", "financial_actions",
    "developer_write", "tool_creation", "platform_admin", "platform_infra",
})
LIMIT_SPECS: dict[str, dict[str, str]] = {
    "max_agents": {"unit": "agents", "meaning": "Active owned custom agents per user", "boundary": "agent materialization"},
    "max_automations": {"unit": "automations", "meaning": "Active schedules and event subscriptions per user", "boundary": "automation creation and execution"},
    "max_connections": {"unit": "connections", "meaning": "User-scoped external connections across agents", "boundary": "connection and OAuth creation"},
    "concurrent_sessions_per_user": {"unit": "running sessions", "meaning": "Simultaneous agent/chat runs for one user", "boundary": "session lease acquisition"},
    "messages_per_window": {"unit": "messages", "meaning": "Accepted chat messages during the configured window", "boundary": "chat admission"},
    "window_seconds": {"unit": "seconds", "meaning": "Duration of the message-rate window", "boundary": "chat admission"},
    "max_attachment_bytes": {"unit": "bytes per attachment", "meaning": "Largest accepted uploaded attachment", "boundary": "upload admission"},
    "max_storage_bytes": {"unit": "bytes per account", "meaning": "Total caller-owned attachment storage", "boundary": "upload storage reservation"},
}
KNOWN_LIMITS = frozenset(LIMIT_SPECS)


class PolicyError(ValueError):
    """A malformed policy must never be used to grant capabilities."""


EMERGENCY_ANONYMOUS_POLICY: dict[str, Any] = {
    "anonymous": {
        # Public guests land on the shared Agent/Sessions experience but retain
        # a zero-ability, chat-only runtime.
        "schema_version": 1, "pages": ["agents", "wiki"], "features": ["chat"],
        "ability_groups": [], "agent_templates": ["default"],
        "models": {"roster_id": "roster-anonymous", "allowed_entry_ids": [],
                   "allow_byo": False, "max_byo_entries": 0,
                   "max_reasoning_effort": "low"},
        "limits": {"max_agents": 0, "max_automations": 0, "max_connections": 0,
                   "concurrent_sessions_per_user": 1, "messages_per_window": 10,
                   "window_seconds": 300, "max_attachment_bytes": 0,
                   "max_storage_bytes": 0},
    },
}

# Compatibility name for callers that explicitly request the emergency policy.
# Mutable Free/Pro product values intentionally do not live in Python.
SYSTEM_POLICIES: dict[str, dict[str, Any]] = EMERGENCY_ANONYMOUS_POLICY

ADMIN_OVERLAY: dict[str, Any] = {
    "pages": sorted(KNOWN_PAGES), "features": sorted(KNOWN_FEATURES),
    "ability_groups": sorted(KNOWN_ABILITY_GROUPS), "agent_templates": ["*"],
    "models": {"roster_id": "roster-admin", "allowed_entry_ids": ["*"],
               "allow_byo": True, "max_byo_entries": None,
               "max_reasoning_effort": "high"},
    "limits": {name: None for name in KNOWN_LIMITS},
}


def _string_set(value: Any, field: str, known: Optional[frozenset[str]] = None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)):
        raise PolicyError(f"{field} must be a list")
    out = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            raise PolicyError(f"{field} contains an invalid value")
        item = raw.strip()
        if known is not None and item not in known:
            raise PolicyError(f"{field} contains unknown value {item!r}")
        if item not in out:
            out.append(item)
    return sorted(out)


def installed_page_ids() -> set[str]:
    """Discover entitlement-addressable pages from drop-in descriptors."""
    try:
        from app.ui_pages import ui_catalog
        catalog = ui_catalog()
        return {
            str(page.get("id") or "")
            for kind in ("main", "admin")
            for page in (catalog.get(kind) or [])
            if page.get("id")
        }
    except Exception:
        return set(KNOWN_PAGES)


def _page_set(value: Any) -> list[str]:
    pages = _string_set(value, "pages")
    invalid = [page for page in pages if not _PAGE_ID_RE.fullmatch(page)]
    if invalid:
        raise PolicyError(f"pages contains invalid stable id {invalid[0]!r}")
    return pages


def _limit(value: Any, field: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise PolicyError(f"{field} must be a non-negative integer or null")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"{field} must be a non-negative integer or null") from exc
    if result < 0:
        raise PolicyError(f"{field} must be non-negative")
    return result


def normalize_policy(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise PolicyError("policy must be an object")
    allowed_top = {"schema_version", "pages", "features", "ability_groups",
                   "agent_templates", "models", "limits"}
    unknown = set(raw) - allowed_top
    if unknown:
        raise PolicyError(f"unknown policy fields: {sorted(unknown)}")
    try:
        version = int(raw.get("schema_version", POLICY_SCHEMA_VERSION))
    except (TypeError, ValueError) as exc:
        raise PolicyError("schema_version must be an integer") from exc
    if version != POLICY_SCHEMA_VERSION:
        raise PolicyError(f"unsupported policy schema version {version}")
    models = raw.get("models") or {}
    if not isinstance(models, Mapping):
        raise PolicyError("models must be an object")
    unknown_models = set(models) - {"roster_id", "allowed_entry_ids", "allow_byo",
                                    "max_byo_entries", "max_reasoning_effort"}
    if unknown_models:
        raise PolicyError(f"unknown model policy fields: {sorted(unknown_models)}")
    effort = str(models.get("max_reasoning_effort") or "default").strip().lower()
    if effort not in REASONING_LEVELS:
        raise PolicyError(f"invalid max_reasoning_effort {effort!r}")
    roster_id = str(models.get("roster_id") or "").strip()
    if not roster_id:
        raise PolicyError("models.roster_id is required")
    limits = raw.get("limits") or {}
    if not isinstance(limits, Mapping):
        raise PolicyError("limits must be an object")
    unknown_limits = set(limits) - KNOWN_LIMITS
    if unknown_limits:
        raise PolicyError(f"unknown limits: {sorted(unknown_limits)}")
    return {
        "schema_version": version,
        "pages": _page_set(raw.get("pages")),
        "features": _string_set(raw.get("features"), "features", KNOWN_FEATURES),
        "ability_groups": _string_set(raw.get("ability_groups"), "ability_groups", KNOWN_ABILITY_GROUPS),
        "agent_templates": _string_set(raw.get("agent_templates"), "agent_templates"),
        "models": {"roster_id": roster_id,
                   "allowed_entry_ids": _string_set(models.get("allowed_entry_ids"), "models.allowed_entry_ids"),
                   "allow_byo": models.get("allow_byo") is True,
                   "max_byo_entries": _limit(models.get("max_byo_entries"), "models.max_byo_entries"),
                   "max_reasoning_effort": effort},
        "limits": {name: _limit(limits.get(name), f"limits.{name}") for name in sorted(KNOWN_LIMITS)},
    }


def _minimum_limit(left: Optional[int], right: Optional[int]) -> Optional[int]:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def compose_policy(tier_policy: Mapping[str, Any], *, is_admin: bool = False,
                   installation_pages: Optional[set[str]] = None,
                   installation_features: Optional[set[str]] = None,
                   restrictive_policy: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    base = normalize_policy(tier_policy)
    if is_admin:
        overlay = deepcopy(ADMIN_OVERLAY)
        overlay["pages"] = sorted(installation_pages or installed_page_ids())
        base.update({key: overlay[key] for key in ("pages", "features", "ability_groups", "agent_templates", "models", "limits")})
    if installation_pages is not None:
        base["pages"] = sorted(set(base["pages"]) & set(installation_pages))
    if installation_features is not None:
        base["features"] = sorted(set(base["features"]) & set(installation_features))
    if restrictive_policy is not None:
        restriction = normalize_policy(restrictive_policy)
        for key in ("pages", "features", "ability_groups", "agent_templates"):
            base[key] = sorted(set(base[key]) & set(restriction[key]))
        allowed = set(base["models"]["allowed_entry_ids"])
        restricting = set(restriction["models"]["allowed_entry_ids"])
        if restricting:
            base["models"]["allowed_entry_ids"] = sorted(allowed & restricting) if allowed else sorted(restricting)
        base["models"]["allow_byo"] = bool(base["models"]["allow_byo"] and restriction["models"]["allow_byo"])
        base["models"]["max_byo_entries"] = _minimum_limit(base["models"]["max_byo_entries"], restriction["models"]["max_byo_entries"])
        rank = {name: i for i, name in enumerate(REASONING_LEVELS)}
        base["models"]["max_reasoning_effort"] = min(
            (base["models"]["max_reasoning_effort"], restriction["models"]["max_reasoning_effort"]),
            key=lambda value: rank[value],
        )
        for name in KNOWN_LIMITS:
            base["limits"][name] = _minimum_limit(base["limits"].get(name), restriction["limits"].get(name))
    return base


def system_policy(tier_slug: str) -> dict[str, Any]:
    """Return the emergency policy; normal tier policy is database-backed."""
    try:
        return normalize_policy(EMERGENCY_ANONYMOUS_POLICY[tier_slug])
    except KeyError as exc:
        raise PolicyError(f"unknown system tier {tier_slug!r}") from exc
