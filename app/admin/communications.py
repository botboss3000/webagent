"""
Admin endpoints for managing communication plugins.

- GET /admin/communications/plugins — list all plugins
- POST /admin/communications/plugins/{name}/enable — enable a plugin
- POST /admin/communications/plugins/{name}/disable — disable a plugin
- PUT /admin/communications/webhook-url — set public webhook base URL
- POST /admin/communications/plugins/reload — re-discover plugins
"""

import json
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from app.communications.manager import get_plugin_manager, reload_plugins

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/communications", tags=["admin"])


class WebhookUrlRequest(BaseModel):
    url: str


class PluginStatus(BaseModel):
    name: str
    enabled: bool
    has_token: bool


@router.get("/plugins")
async def list_plugins():
    """List all discovered plugins with their enabled status."""
    pm = get_plugin_manager()
    plugins = []
    for p in pm.get_all_plugins():
        plugins.append({
            "name": p.name,
            "enabled": p.enabled,
            "webhook_path": p.webhook_path,
            "tool_count": len(p.get_tools()),
        })
    # Also include the base URL
    registry = getattr(pm, "_registry", {})
    return {
        "plugins": plugins,
        "webhook_base_url": registry.get("webhook_base_url", ""),
    }


@router.post("/plugins/{name}/enable")
async def enable_plugin(name: str):
    """Enable a plugin."""
    pm = get_plugin_manager()
    ok = pm.enable_plugin(name)
    if not ok:
        return {"status": "error", "message": f"Plugin '{name}' not found"}
    
    # If Telegram, set up webhook
    plugin = pm.get_plugin(name)
    if plugin and hasattr(plugin, 'set_webhook_url'):
        registry = getattr(pm, "_registry", {})
        base_url = registry.get("webhook_base_url", "")
        if base_url:
            await plugin.set_webhook_url(base_url)
    
    return {"status": "ok", "message": f"Plugin '{name}' enabled"}


@router.post("/plugins/{name}/disable")
async def disable_plugin(name: str):
    """Disable a plugin."""
    pm = get_plugin_manager()
    
    # If Telegram, remove webhook
    plugin = pm.get_plugin(name)
    if plugin and hasattr(plugin, 'delete_webhook_url'):
        await plugin.delete_webhook_url()
    
    ok = pm.disable_plugin(name)
    if not ok:
        return {"status": "error", "message": f"Plugin '{name}' not found"}
    return {"status": "ok", "message": f"Plugin '{name}' disabled"}


@router.put("/webhook-url")
async def set_webhook_url(req: WebhookUrlRequest):
    """Set the public webhook base URL and re-register all webhooks."""
    pm = get_plugin_manager()
    registry = getattr(pm, "_registry", {})
    registry["webhook_base_url"] = req.url.rstrip("/")
    
    import json as _json
    from pathlib import Path
    reg_path = Path(__file__).resolve().parent.parent / "communications" / "registry.json"
    reg_path.write_text(_json.dumps(registry, indent=2), encoding="utf-8")
    
    results = {}
    for plugin in pm.get_enabled_plugins():
        if hasattr(plugin, 'set_webhook_url'):
            ok = await plugin.set_webhook_url(req.url)
            results[plugin.name] = "ok" if ok else "failed"
    
    return {"status": "ok", "webhook_base_url": req.url, "results": results}


@router.post("/plugins/reload")
async def reload_plugins_endpoint():
    """Re-discover plugins from the filesystem."""
    reload_plugins()
    pm = get_plugin_manager()
    return {
        "status": "ok",
        "plugins": [{"name": p.name, "enabled": p.enabled} for p in pm.get_all_plugins()],
    }
