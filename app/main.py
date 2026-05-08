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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        p = request.url.path
        if p.startswith("/ui/") or p == "/index.html":
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

# ── Auth ──
from app.auth import router as auth_router

# ── GitHub ──
from app.api.github import router as github_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="webAgent API",
    description="webAgent — FastAPI service with tool-calling agent loop and WebSocket streaming",
    version="0.1.0"
)


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

# Include routers
app.include_router(chat_router)
app.include_router(terminal_router)
app.include_router(agent_router)
app.include_router(uploads_router)
app.include_router(db_viewer_router)
app.include_router(admin_router)
app.include_router(admin_db_router)
if _HAS_SOURCE_TOOLS and admin_source_router is not None:
    app.include_router(admin_source_router)

if _HAS_SETTINGS and admin_settings_router is not None:
    app.include_router(admin_settings_router)

# Register communications admin router
app.include_router(admin_communications_router)

# Register generic webhook admin router
app.include_router(admin_webhooks_router)

# Register auth router
app.include_router(auth_router)

# Register generic webhook router (more specific paths first)
app.include_router(webhooks_generic_router)

# Register communication plugin webhook router (for Telegram, WhatsApp, SMS etc.)
app.include_router(webhooks_router)

# Register GitHub router
app.include_router(github_router)

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


@app.get("/")
async def root():
    """Open the main web UI in a browser. API: `/docs`, `/health`."""
    return RedirectResponse(url="/index.html", status_code=307)


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

@app.get("/test", response_class=HTMLResponse)
async def test_interface():
    """Serve the test interface HTML page."""
    test_html = _APP_DIR.parent / "ui" / "test_interface.html"
    return HTMLResponse(content=test_html.read_text(encoding="utf-8"))


@app.get("/terminal")
async def terminal_legacy_redirect():
    """Old bookmark path; redirect to the main UI."""
    return RedirectResponse(url="/index.html", status_code=307)

# ── Start-up: register Telegram webhook ──
@app.on_event("startup")
async def startup():
    """Register communication webhooks on server start."""
    try:
        from app.communications.manager import get_plugin_manager
        pm = get_plugin_manager()
        registry = getattr(pm, "_registry", {})
        base_url = registry.get("webhook_base_url", "")
        if base_url:
            tg = pm.get_plugin("telegram")
            if tg and tg.enabled:
                await tg.set_webhook_url(base_url)
    except Exception as e:
        logger.warning("Failed to register webhooks on startup: %s", e)


if __name__ == "__main__":
    import uvicorn

    _port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=_port, reload=True)