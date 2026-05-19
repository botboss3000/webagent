"""
Admin endpoints for managing communication plugins.

- GET /admin/communications/plugins — list all plugins with status
- POST /admin/communications/plugins/{name}/enable — enable a plugin
- POST /admin/communications/plugins/{name}/disable — disable a plugin
- PUT /admin/communications/webhook-url — set public webhook base URL
- POST /admin/communications/plugins/{name}/token — save a single bot token (Telegram compat)
- POST /admin/communications/plugins/{name}/credentials — save arbitrary credentials for any plugin
- POST /admin/communications/plugins/reload — re-discover plugins
"""

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Request
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


class CredentialsRequest(BaseModel):
    credentials: Dict[str, Any]


@router.get("/plugins")
async def list_plugins():
    """List all discovered plugins with their enabled status."""
    pm = get_plugin_manager()
    plugins = []
    for p in pm.get_all_plugins():
        has_token = False
        if hasattr(p, '_bot_token'):
            has_token = bool(p._bot_token)
        elif hasattr(p, 'has_token'):
            has_token = p.has_token
        plugins.append({
            "name": p.name,
            "enabled": p.enabled,
            "has_token": has_token,
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
    """Enable a plugin. Uses webhook if base URL is set, otherwise polls."""
    pm = get_plugin_manager()
    ok = pm.enable_plugin(name)
    if not ok:
        return {"status": "error", "message": f"Plugin '{name}' not found"}

    plugin = pm.get_plugin(name)
    if plugin and hasattr(plugin, 's') and hasattr(plugin, 'set_webhook_url'):
        registry = getattr(pm, "_registry", {})
        base_url = registry.get("webhook_base_url", "")
        # If a reachable webhook URL is configured, use webhook
        if base_url and not ("localhost" in base_url or "127.0.0.1" in base_url):
            await plugin.set_webhook_url(base_url)
            logger.info("%s enabled with webhook at %s", name, base_url)
        else:
            # No reachable webhook → start polling
            if hasattr(plugin, 'start_polling'):
                await plugin.start_polling()
                logger.info("%s enabled with polling (no public webhook URL)", name)

    return {"status": "ok", "message": f"Plugin '{name}' enabled"}


@router.post("/plugins/{name}/disable")
async def disable_plugin(name: str):
    """Disable a plugin. Stops polling if active."""
    pm = get_plugin_manager()

    # Stop polling first (if any)
    plugin = pm.get_plugin(name)
    if plugin and hasattr(plugin, 'stop_polling'):
        await plugin.stop_polling()
    # Remove webhook
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


@router.post("/plugins/{name}/token")
async def set_plugin_token(name: str, req: WebhookUrlRequest, http_request: Request):
    """Set a bot token for a plugin. Saves to registry.json, auto-detects
    the server URL from the incoming request, and registers the webhook."""
    pm = get_plugin_manager()
    plugin = pm.get_plugin(name)
    if not plugin:
        return {"status": "error", "message": f"Plugin '{name}' not found"}

    token = req.url  # reuse WebhookUrlRequest for a single string field

    # Save to registry
    registry = getattr(pm, "_registry", {})
    registry.setdefault("plugins", {}).setdefault(name, {})
    registry["plugins"][name]["bot_token"] = token

    from pathlib import Path
    import json as _json
    reg_path = Path(__file__).resolve().parent.parent / "communications" / "registry.json"

    # Auto-detect server URL from the incoming request if base URL not set
    server_url = registry.get("webhook_base_url", "")
    if not server_url:
        # Build base URL from the request: scheme://host
        scheme = str(http_request.url.scheme)
        host = str(http_request.url.hostname)
        port = http_request.url.port
        if port and port not in (80, 443):
            server_url = f"{scheme}://{host}:{port}"
        else:
            server_url = f"{scheme}://{host}"
        registry["webhook_base_url"] = server_url
        logger.info("Auto-detected server URL: %s", server_url)

    reg_path.write_text(_json.dumps(registry, indent=2), encoding="utf-8")

    # Re-init plugin so it picks up the new token
    plugin._bot_token = token

    # Auto-enable the plugin
    pm.enable_plugin(name)

    # Decide: webhook (public URL) or polling (localhost / no URL)
    is_local = not server_url or "localhost" in server_url or "127.0.0.1" in server_url
    if is_local:
        # Start polling
        if hasattr(plugin, 'start_polling'):
            await plugin.start_polling()
            logger.info("Telegram polling started (server URL is localhost)")
        webhook_ok = False
        webhook_mode = False
    else:
        # Register webhook
        if hasattr(plugin, 'set_webhook_url'):
            webhook_ok = await plugin.set_webhook_url(server_url)
            logger.info("Telegram webhook registration at %s: %s", server_url + "/api/v1/webhooks/telegram", "ok" if webhook_ok else "failed")
        else:
            webhook_ok = False
        webhook_mode = True

    mode = "polling" if is_local else "webhook"
    return {
        "status": "ok",
        "message": f"Token saved for plugin '{name}', mode: {mode}",
        "has_token": True,
        "webhook_base_url": server_url,
        "webhook_registered": webhook_ok if not is_local else False,
        "mode": mode,
    }


@router.post("/plugins/{name}/credentials")
async def set_plugin_credentials(name: str, req: CredentialsRequest, http_request: Request):
    """
    Save arbitrary credentials for any plugin (account_sid/auth_token/from_number for Twilio,
    bot_token/public_key for Discord, bot_token/signing_secret for Slack, etc.).
    Saves to registry.json, auto-enables the plugin, and registers webhooks where applicable.
    """
    pm = get_plugin_manager()
    plugin = pm.get_plugin(name)
    if not plugin:
        return {"status": "error", "message": f"Plugin '{name}' not found"}

    # Save credentials to registry
    registry = getattr(pm, "_registry", {})
    plugin_cfg = registry.setdefault("plugins", {}).setdefault(name, {})
    plugin_cfg.update(req.credentials)

    from pathlib import Path
    import json as _json
    reg_path = Path(__file__).resolve().parent.parent / "communications" / "registry.json"

    # Auto-detect server URL if not set
    server_url = registry.get("webhook_base_url", "")
    if not server_url:
        scheme = str(http_request.url.scheme)
        host = str(http_request.url.hostname)
        port = http_request.url.port
        if port and port not in (80, 443):
            server_url = f"{scheme}://{host}:{port}"
        else:
            server_url = f"{scheme}://{host}"
        registry["webhook_base_url"] = server_url

    reg_path.write_text(_json.dumps(registry, indent=2), encoding="utf-8")

    # Re-init plugin with new credentials
    plugin._registry = registry

    # Auto-enable
    pm.enable_plugin(name)

    # Register webhook if possible and URL is public
    is_local = not server_url or any(h in server_url for h in ("localhost", "127.0.0.1", "0.0.0.0"))
    webhook_registered = False
    mode = "webhook"

    if hasattr(plugin, 'start_polling') and is_local:
        await plugin.start_polling()
        mode = "polling"
    elif hasattr(plugin, 'set_webhook_url') and not is_local:
        webhook_registered = await plugin.set_webhook_url(server_url)
        mode = "webhook"

    return {
        "status": "ok",
        "message": f"Credentials saved for '{name}', mode: {mode}",
        "plugin": name,
        "webhook_base_url": server_url,
        "webhook_registered": webhook_registered,
        "mode": mode,
    }


@router.post("/plugins/reload")
async def reload_plugins_endpoint():
    """Re-discover plugins from the filesystem."""
    reload_plugins()
    pm = get_plugin_manager()
    return {
        "status": "ok",
        "plugins": [{"name": p.name, "enabled": p.enabled} for p in pm.get_all_plugins()],
    }
