"""Entry point for the webAgent server."""

import logging
import os
import sys
from pathlib import Path
from fastapi import APIRouter

# ── Load .env BEFORE any imports that need environment variables ──
# The .env file lives in the project root (parent of app/).
# This must happen before importing app.api.chat (which imports loop.py,
# which creates an AsyncOpenAI client at module level using OPENROUTER_API_KEY).
_APP_DIR = Path(__file__).resolve().parent
_DOTENV_PATH = _APP_DIR.parent / ".env"

from dotenv import load_dotenv
load_dotenv(dotenv_path=_DOTENV_PATH)

# Apply saved provider config (model, API key) from provider.json
# so environment vars are set before any imports that need them
from app.admin.settings import apply_provider_config
apply_provider_config()

import traceback
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        p = request.url.path
        if p.startswith("/ui/") or p == "/index.html" or p == "/":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

from app.api.chat import router as chat_router
from app.api.terminal import router as terminal_router
from app.api.agent import router as agent_router
from app.api.uploads import router as uploads_router
from app.api.db_viewer import router as db_viewer_router
from app.admin.review import router as admin_router
from app.admin.db_mode import router as admin_db_router
from app.admin.storage import router as admin_storage_router
from app.api.webhooks import router as webhooks_router
from app.api.webhooks_generic import router as webhooks_generic_router
try:
    from app.admin.source import router as admin_source_router
    _HAS_SOURCE_TOOLS = True
except ImportError:
    _HAS_SOURCE_TOOLS = False
    admin_source_router = None

try:
    from app.admin.settings import router as admin_settings_router
    _HAS_SETTINGS = True
except ImportError:
    _HAS_SETTINGS = False
    admin_settings_router = None

from app.admin.communications import router as admin_communications_router
from app.admin.webhooks_admin import router as admin_webhooks_router
from app.admin.scheduler_config import router as scheduler_admin_router
from app.api.agents import router as agents_router
from app.api.data_sources import router as data_sources_router
from app.admin.users import router as admin_users_router

# ── Auth ──
from app.auth import router as auth_router

# ── GitHub ──
from app.api.github import router as github_router

# ── Integrations & OAuth ──
from app.admin.integrations import router as integrations_router
from app.api.oauth import router as oauth_router

# ── Optimizer ──
from app.admin.optimizer import router as optimizer_router
from app.api.pages import router as pages_router
optimizer_router.prefix="/api/v1"

# Configure logging
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="webAgent API",
    description="webAgent — FastAPI service with tool-calling agent loop and WebSocket streaming",
    version="0.1.0"
)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("Unhandled exception on %s %s\n%s", request.method, request.url.path, tb)
    return JSONResponse(status_code=500, content={"detail": "Internal server error", "error": str(exc)})


# ── Favicon ──
@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    from fastapi.responses import FileResponse
    return FileResponse(str(_APP_DIR.parent / "ui" / "favicon.svg"), media_type="image/svg+xml")


# CORS middleware (adjust origins as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*", "null"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(NoCacheMiddleware)


@app.middleware("http")
async def _capture_public_base_url(request, call_next):
    """Cache the public base URL of every incoming request so background code
    paths (agent tools, scheduler) that have no Request object can still build
    correct OAuth redirect URIs instead of falling back to http://localhost:8000."""
    try:
        from app.admin import integrations as _integ
        derived = str(request.base_url).rstrip("/")
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        if forwarded_proto and derived.startswith("http://"):
            derived = "https://" + derived[len("http://"):]
        if derived and not derived.startswith("http://localhost") and not derived.startswith("http://127."):
            _integ._LAST_SEEN_BASE_URL = derived
    except Exception:
        pass
    return await call_next(request)


# Include routers
app.include_router(chat_router)
app.include_router(terminal_router)
app.include_router(agent_router)
app.include_router(uploads_router)
app.include_router(db_viewer_router)
app.include_router(admin_router)
app.include_router(admin_db_router)
app.include_router(admin_storage_router)
if _HAS_SOURCE_TOOLS and admin_source_router is not None:
    app.include_router(admin_source_router)

if _HAS_SETTINGS and admin_settings_router is not None:
    app.include_router(admin_settings_router)

# Register communications admin router
app.include_router(admin_communications_router)

# Register generic webhook admin router
app.include_router(admin_webhooks_router)
app.include_router(scheduler_admin_router)
app.include_router(agents_router)
app.include_router(data_sources_router)
app.include_router(admin_users_router)

# Register auth router
app.include_router(auth_router)

# Register generic webhook router (more specific paths first)
app.include_router(webhooks_generic_router)

# Register communication plugin webhook router (for Telegram, WhatsApp, SMS etc.)
app.include_router(webhooks_router)

# Register integrations & OAuth routers
app.include_router(integrations_router)
app.include_router(oauth_router)

# Register GitHub router
app.include_router(github_router)

# Register optimizer admin router
app.include_router(optimizer_router)

# Register AutoAgent pages router
app.include_router(pages_router)

# ── Restart endpoint ──
# POST /api/v1/restart shuts down the server process.
# Works with webAgent.bat which loops uvicorn in a :restart cycle.
restart_router = APIRouter(prefix="/api/v1")

@restart_router.post("/restart")
async def restart_server():
    """Shut down the server. webAgent.bat will restart it automatically."""
    import threading
    logger.warning("Restart requested via /api/v1/restart — shutting down...")
    # Give the response time to be sent before the process exits
    def _die():
        import time
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()
    return {"status": "restarting", "message": "Server is shutting down. webAgent.bat will restart it."}

app.include_router(restart_router)

# ── Static file mounts ──
_SCREENSHOTS_DIR = _APP_DIR.parent / "screenshots"
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=str(_SCREENSHOTS_DIR)), name="screenshots")

_UPLOAD_DIR = _APP_DIR.parent / "uploads"
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

try:
    app.mount("/uploads", StaticFiles(directory=str(_UPLOAD_DIR)), name="uploads")
    logger.info("Uploads directory mounted at /uploads")
except Exception as e:
    logger.warning("Could not mount /uploads: %s", e)

_VISUALS_DIR = _APP_DIR.parent / "visuals"
_VISUALS_DIR.mkdir(parents=True, exist_ok=True)
try:
    app.mount("/visuals", StaticFiles(directory=str(_VISUALS_DIR)), name="visuals")
    logger.info("Visuals directory mounted at /visuals")
except Exception as e:
    logger.warning("Could not mount /visuals: %s", e)

_UI_DIR = _APP_DIR.parent / "ui"
app.mount("/ui", StaticFiles(directory=str(_UI_DIR)), name="ui")

_ROOT_INDEX_HTML = _APP_DIR.parent / "index.html"


# ── Cleanup on shutdown ──
@app.on_event("shutdown")
async def shutdown():
    """Close browser instances and persistent terminal session on server shutdown."""
    try:
        from app.tools.browser import close_all
        await close_all()
    except Exception:
        pass
    try:
        from app.api.terminal import close_persistent_session
        await close_persistent_session()
    except Exception:
        pass
    # Remove Telegram webhook on shutdown
    try:
        from app.communications.manager import get_plugin_manager
        pm = get_plugin_manager()
        tg = pm.get_plugin("telegram")
        if tg and tg.enabled:
            await tg.delete_webhook_url()
    except Exception:
        pass
    # Stop the automation scheduler.
    try:
        from app.scheduler import stop_scheduler
        await stop_scheduler()
    except Exception:
        pass


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the main web UI at the root. API: `/docs`, `/health`."""
    if not _ROOT_INDEX_HTML.is_file():
        return HTMLResponse("<p>Missing index.html</p>", status_code=404)
    return HTMLResponse(content=_ROOT_INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/index.html", response_class=HTMLResponse, include_in_schema=False)
async def main_ui():
    """Serve the main web UI (static assets remain under /ui/)."""
    if not _ROOT_INDEX_HTML.is_file():
        return HTMLResponse("<p>Missing index.html</p>", status_code=404)
    return HTMLResponse(content=_ROOT_INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/login.html", response_class=HTMLResponse, include_in_schema=False)
async def login_ui():
    """Serve the login page."""
    login_html = _APP_DIR.parent / "ui" / "login.html"
    if not login_html.is_file():
        return HTMLResponse("<p>Missing login.html</p>", status_code=404)
    return HTMLResponse(content=login_html.read_text(encoding="utf-8"))

@app.get("/health")
async def health_check():
    return {"status": "healthy"}


import re as _re
_AGENT_UUID_RE = _re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    _re.IGNORECASE,
)

@app.get("/{agent_id}", response_class=HTMLResponse, include_in_schema=False)
async def public_agent_chat(agent_id: str):
    """Serve the main UI with a specific agent pre-selected (public access)."""
    if not _AGENT_UUID_RE.match(agent_id):
        return HTMLResponse("<p>Not found</p>", status_code=404)
    from app.db import get_db as _get_db
    db = _get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        return HTMLResponse("<p>Agent not found</p>", status_code=404)
    if not _ROOT_INDEX_HTML.is_file():
        return HTMLResponse("<p>Missing index.html</p>", status_code=404)
    html = _ROOT_INDEX_HTML.read_text(encoding="utf-8")
    agent_name = agent.get("name", "Agent")
    inject = f'<script>window.__agentId = "{agent_id}"; window.__agentName = {repr(agent_name)};</script>\n</head>'
    html = html.replace("</head>", inject, 1)
    return HTMLResponse(content=html)

@app.get("/test", response_class=HTMLResponse)
async def test_interface():
    """Serve the test interface HTML page."""
    test_html = _APP_DIR.parent / "ui" / "test_interface.html"
    return HTMLResponse(content=test_html.read_text(encoding="utf-8"))


# ── Start-up: register Telegram webhook ──
@app.on_event("startup")
async def startup():
    """Register communication webhooks or start polling on server start."""
    # Build trigger routing index from agent_templates
    try:
        from app.agent import trigger_index
        trigger_index.build()
    except Exception as _ti_err:
        logger.warning("Failed to build trigger index on startup: %s", _ti_err)

    try:
        from app.communications.manager import get_plugin_manager
        pm = get_plugin_manager()

        # Priority: WEBHOOK_BASE_URL env var > registry value
        base_url = os.environ.get("WEBHOOK_BASE_URL", "").rstrip("/")
        if not base_url:
            registry = getattr(pm, "_registry", {})
            base_url = registry.get("webhook_base_url", "").rstrip("/")

        _local_hints = ("localhost", "127.0.0.1", "0.0.0.0")
        is_public = bool(base_url) and not any(h in base_url for h in _local_hints)

        if is_public:
            # Register webhook for all enabled plugins that support it
            for plugin in pm.get_enabled_plugins():
                if hasattr(plugin, "set_webhook_url"):
                    await plugin.set_webhook_url(base_url)
                    logger.info("Registered webhook for %s at %s", plugin.name, base_url)
        else:
            # No reachable public URL — start polling (local dev only)
            await pm.start_polling_for_offline_plugins()
    except Exception as e:
        logger.warning("Failed to register/poll on startup: %s", e)

    # ── Backfill admin_users for existing agents ──
    try:
        from app.db import get_db as _get_db_backfill
        _db_bf = _get_db_backfill()
        _n = await _db_bf.backfill_agent_admin_users()
        if _n:
            logger.info("Backfilled admin_users for %d agents", _n)
    except Exception as _bf_err:
        logger.warning("admin_users backfill failed: %s", _bf_err)

    # ── Start agent automation scheduler ──
    try:
        from app.scheduler import start_scheduler
        await start_scheduler()
    except Exception as _sch_err:
        logger.warning("Failed to start automation scheduler: %s", _sch_err)

    # ── Seed LLM config from env vars into auth_elements (cloud-first deploy) ──
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
    if api_key:
        try:
            from app.db import get_db
            db = get_db()
            existing = await db.auth_element_get("admin_default", "llm", "default")
            if existing and existing.get("secret_ref"):
                logger.info("LLM config already in auth_elements, skipping seed")
            else:
                config = {
                    "provider": os.environ.get("LLM_PROVIDER", "openrouter"),
                    "base_url": os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
                    "model": os.environ.get("LLM_MODEL", ""),
                    "providers": {},
                }
                await db.auth_element_set(
                    user_id="admin_default",
                    service="llm",
                    config=config,
                    secret_ref=api_key,
                )
                logger.info("LLM config seeded into auth_elements from env vars")
        except Exception as e:
            logger.warning("Failed to seed LLM config: %s", e)


if __name__ == "__main__":
    import uvicorn

    _port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=_port, reload=True)
