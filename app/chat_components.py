"""Persisted declarative components shown in chat sessions."""
from __future__ import annotations

import json
import uuid

ALLOWED_TYPES = {"todo_list", "status", "choice", "approval", "form"}
ALLOWED_PLACEMENTS = {"inline", "hover", "sticky"}


def validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("Component must be an object.")
    kind = str(spec.get("type") or "").strip()
    placement = str(spec.get("placement") or "inline").strip()
    if kind not in ALLOWED_TYPES:
        raise ValueError("Unsupported component type.")
    if placement not in ALLOWED_PLACEMENTS:
        raise ValueError("Unsupported component placement.")
    data = spec.get("data") if isinstance(spec.get("data"), dict) else {}
    if kind == "todo_list":
        items = data.get("items") if isinstance(data.get("items"), list) else []
        data["items"] = [{"id": str(x.get("id") or uuid.uuid4()), "label": str(x.get("label") or "")[:240], "done": bool(x.get("done"))}
                         for x in items[:100] if isinstance(x, dict) and str(x.get("label") or "").strip()]
    elif kind == "status":
        sections = data.get("sections") if isinstance(data.get("sections"), list) else []
        data["sections"] = [{"label": str(s.get("label") or "")[:80], "value": str(s.get("value") or "")[:120],
                             "status": s.get("status") if s.get("status") in ("good", "warning", "danger") else None}
                            for s in sections[:20] if isinstance(s, dict)]
    elif kind == "choice":
        options = data.get("options") if isinstance(data.get("options"), list) else []
        data["options"] = [{"id": str(o.get("id") or uuid.uuid4()), "label": str(o.get("label") or "")[:240]}
                           for o in options[:20] if isinstance(o, dict)]
        if "selected" in data and not isinstance(data.get("selected"), str):
            data.pop("selected", None)
    elif kind == "form":
        fields = data.get("fields") if isinstance(data.get("fields"), list) else []
        data["fields"] = [{"name": str(f.get("name") or "")[:40], "label": str(f.get("label") or "")[:120],
                           "type": str(f.get("type") or "text")[:20],
                           "required": bool(f.get("required")),
                           "multiline": bool(f.get("multiline")),
                           "options": f.get("options") if isinstance(f.get("options"), list) else None}
                          for f in fields[:20] if isinstance(f, dict) and str(f.get("name") or "").strip()]
        if "submitted" in data and not isinstance(data.get("submitted"), dict):
            data.pop("submitted", None)
    elif kind == "approval":
        if "status" in data and str(data.get("status") or "") not in ("approved", "rejected"):
            data.pop("status", None)

    return {"id": str(spec.get("id") or uuid.uuid4()), "type": kind, "placement": placement,
            "title": str(spec.get("title") or kind.replace("_", " ").title())[:120], "data": data,
            "lifecycle": spec.get("lifecycle") if isinstance(spec.get("lifecycle"), dict) else {}}


async def list_components(user_id: str, session_id: str) -> list[dict]:
    from app.db import get_db
    latest = {}
    for row in await get_db().fetch_interactions(user_id, session_id):
        if getattr(row, "tool_name", None) != "chat_component":
            continue
        try:
            payload = json.loads(getattr(row, "content", "") or "{}")
            comp = payload.get("component") or {}
            if comp.get("deleted"):
                latest.pop(str(comp.get("id", "")), None)
                continue
            spec = validate_spec(comp)
            latest[spec["id"]] = spec
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return list(latest.values())


async def save_component(user_id: str, session_id: str, spec: dict) -> dict:
    from app.db import get_db
    clean = validate_spec(spec)
    await get_db().insert_interaction(user_id=user_id, session_id=session_id, role="tool",
        content=json.dumps({"component": clean}), tool_name="chat_component",
        metadata=json.dumps({"chat_component": True, "component_id": clean["id"]}), source="chat_component")
    return clean


async def apply_action(user_id: str, session_id: str, component_id: str, action: str, payload: dict) -> dict:
    current = next((x for x in await list_components(user_id, session_id) if x["id"] == component_id), None)
    if not current:
        raise LookupError("Component not found.")

    kind = current["type"]
    data = current["data"]

    if kind == "todo_list" and action == "toggle_item":
        item_id = str((payload or {}).get("item_id") or "")
        for item in data.get("items", []):
            if item["id"] == item_id:
                item["done"] = bool((payload or {}).get("done"))
                return await save_component(user_id, session_id, current)
        raise ValueError("To-do item not found.")

    if kind == "choice" and action == "select":
        option_id = str((payload or {}).get("option_id") or "")
        valid = any(o["id"] == option_id for o in data.get("options", []))
        if not valid:
            raise ValueError("Option not found.")
        data["selected"] = option_id
        return await save_component(user_id, session_id, current)

    if kind == "form" and action == "submit":
        values = (payload or {}).get("values") if isinstance((payload or {}).get("values"), dict) else {}
        data["submitted"] = values
        return await save_component(user_id, session_id, current)

    if kind == "approval" and action in ("approve", "reject"):
        data["status"] = "approved" if action == "approve" else "rejected"
        return await save_component(user_id, session_id, current)

async def delete_component(user_id: str, session_id: str, component_id: str):
    """Soft-delete by writing a tombstone record."""
    from app.db import get_db
    # Verify it exists
    components = await list_components(user_id, session_id)
    if not any(c["id"] == component_id for c in components):
        raise LookupError("Component not found.")
    await get_db().insert_interaction(user_id=user_id, session_id=session_id, role="tool",
        content=json.dumps({"component": {"id": component_id, "deleted": True}}),
        tool_name="chat_component",
        metadata=json.dumps({"chat_component": True, "component_id": component_id, "deleted": True}),
        source="chat_component")
