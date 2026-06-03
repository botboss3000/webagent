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

# Apply saved provider config (model, API key) from data/config/provider.json
# so environment vars are set before any imports that need them. Imported from
# app.provider_boot (NOT app.admin.settings) so the boot path doesn't hard-depend
# on the admin package — removing app/admin/ must not break the core LLM.
from app.provider_boot import apply_provider_config
apply_provider_config()

import traceback
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        p = request.url.path
        # Don't no-store the PWA manifest — Chrome needs it cacheable for "Add to Home Screen"
        if p == "/ui/manifest.json":
            return response
        if p.startswith("/ui/") or p.startswith("/web-terminal") or p == "/index.html" or p == "/":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

from app.api.chat import router as chat_router
from app.api.terminal import router as terminal_router
from app.api.agent import router as agent_router
from app.api.browser_stream import router as browser_stream_router
from app.api.uploads import router as uploads_router
from app.api.db_viewer import router as db_viewer_router
from app.api.diagnostics import router as diagnostics_router
from app.api.recordings import router as recordings_router
try:
    from app.admin.review import router as admin_router
    from app.admin.db_mode import router as admin_db_router
    from app.admin.storage import router as admin_storage_router
except ImportError:
    admin_router = admin_db_router = admin_storage_router = None
from app.api.webhooks import router as webhooks_router
from app.api.webhooks_generic import router as webhooks_generic_router
from app.api.events import router as events_router
from app.api.remote_access import router as remote_access_router
try:
    from app.admin.remote_access import router as admin_remote_access_router
except ImportError:
    admin_remote_access_router = None
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

try:
    from app.admin.communications import router as admin_communications_router
    from app.admin.webhooks_admin import router as admin_webhooks_router
    from app.admin.events_admin import router as admin_events_router
    from app.admin.scheduler_config import router as scheduler_admin_router
except ImportError:
    admin_communications_router = admin_webhooks_router = admin_events_router = scheduler_admin_router = None
from app.api.agents import router as agents_router, agent_pages_router
from app.api.data_sources import router as data_sources_router
from app.api.files import router as files_router
try:
    from app.admin.users import router as admin_users_router
except ImportError:
    admin_users_router = None
from app.api.billing import router as billing_router

# ── Auth ──
from app.auth import router as auth_router

# ── GitHub ──
from app.api.github import router as github_router

# ── Feedback (relayed to GitHub issues) ──
from app.api.feedback import router as feedback_router

# ── Integrations & OAuth ──
try:
    from app.admin.integrations import router as integrations_router
except ImportError:
    integrations_router = None
from app.api.oauth import router as oauth_router

# ── Optimizer ──
try:
    from app.admin.optimizer import router as optimizer_router
    optimizer_router.prefix = "/api/v1"
except ImportError:
    optimizer_router = None
from app.api.pages import router as pages_router

# Configure logging
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Diagnostic flight-recorder: mirror WARNING+ log records (with tracebacks) into
# the in-app recorder so an operator / diagnostic agent can read recent failures
# back via the API + read_diagnostics tool, not just the console. The background
# writer is started in the startup() hook below (needs the event loop).
try:
    from app.agent.diagnostics import install_log_handler as _install_diag_handler
    _diag_level = getattr(logging, os.environ.get("DIAGNOSTICS_CAPTURE_LEVEL", "WARNING").upper(), logging.WARNING)
    _install_diag_handler(level=_diag_level)
except Exception as _diag_err:  # never let diagnostics wiring break boot
    logger.warning("Diagnostic log handler not installed: %s", _diag_err)

app = FastAPI(
    title="webAgent API",
    description="webAgent — FastAPI service with tool-calling agent loop and WebSocket streaming",
    version="0.1.0"
)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("Unhandled exception on %s %s\n%s", request.method, request.url.path, tb)
    # Also record the 500 in the flight-recorder's http category (the logger.error
    # above lands in the `server` category via the diagnostics log handler).
    try:
        from app.api.http_diag import record_server_error
        record_server_error(request, exc, tb)
    except Exception:
        pass
    return JSONResponse(status_code=500, content={"detail": "Internal server error", "error": str(exc)})


# ── Favicon ──
@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    from fastapi.responses import FileResponse
    return FileResponse(str(_APP_DIR.parent / "ui" / "favicon.svg"), media_type="image/svg+xml")


# ── PWA: service worker + manifest (correct MIME types) ──
@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    sw_path = _APP_DIR.parent / "sw.js"
    if not sw_path.is_file():
        return HTMLResponse("", status_code=404)
    # Never let the browser serve a stale service-worker script from HTTP cache —
    # a new sw.js (e.g. a cache-version bump) must be discovered on next load.
    return FileResponse(
        str(sw_path),
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/ui/manifest.json", include_in_schema=False)
async def pwa_manifest():
    m_path = _APP_DIR.parent / "ui" / "manifest.json"
    if not m_path.is_file():
        return HTMLResponse("", status_code=404)
    return FileResponse(
        str(m_path),
        media_type="application/manifest+json",
        headers={
            "Cache-Control": "max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


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
    """Cache the base URL of every incoming request so background code paths
    (agent tools, scheduler) that have no Request object can still build
    correct OAuth redirect URIs instead of falling back to a hardcoded port.

    Localhost requests update the cache only when nothing public has been seen
    yet — that way local dev works on whatever port uvicorn is bound to, but
    production internal pings on 127.0.0.1 don't poison the cached domain."""
    try:
        from app.admin import integrations as _integ
        derived = str(request.base_url).rstrip("/")
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        if forwarded_proto and derived.startswith("http://"):
            derived = "https://" + derived[len("http://"):]
        if derived:
            is_local = derived.startswith("http://localhost") or derived.startswith("http://127.")
            cached = _integ._LAST_SEEN_BASE_URL
            cache_is_public = bool(cached) and not (
                cached.startswith("http://localhost") or cached.startswith("http://127.")
            )
            if not (is_local and cache_is_public):
                _integ._LAST_SEEN_BASE_URL = derived
    except Exception:
        pass
    return await call_next(request)


@app.middleware("http")
async def _canonical_host_redirect(request, call_next):
    """301-redirect requests that arrive on a non-canonical host.

    When the deployment sets WEBHOOK_BASE_URL (or webhook_base_url.txt) to its
    public URL — e.g. `https://www.example.com` — any request reaching the app
    on a different host (apex `example.com`, a preview URL, etc.) is sent a
    301 to the same path on the canonical host. This keeps OAuth `redirect_uri`
    values aligned with what's registered at the provider regardless of which
    DNS name the user typed in.

    No-op when no canonical URL is configured; localhost requests are exempt
    so local development is never redirected to production."""
    # Decide whether to redirect *without* invoking the downstream app, so a
    # failure in the redirect logic can never swallow (and retry) call_next.
    # Calling call_next twice on one request deadlocks: the ASGI receive stream
    # is already consumed, so the second await blocks forever — which is how any
    # downstream 500 used to turn into a silent hang instead of an error.
    redirect_target = None
    try:
        from app.admin.integrations import _get_configured_base_url
        canonical = _get_configured_base_url()
        if canonical:
            host = (request.headers.get("x-forwarded-host") or request.url.netloc or "").split(",")[0].strip()
            if host and not host.startswith("localhost") and not host.startswith("127."):
                scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
                actual = f"{scheme}://{host}".rstrip("/")
                if actual.lower() != canonical.rstrip("/").lower():
                    target = canonical.rstrip("/") + request.url.path
                    if request.url.query:
                        target += "?" + request.url.query
                    redirect_target = target
    except Exception:
        redirect_target = None
    if redirect_target:
        return RedirectResponse(redirect_target, status_code=301)
    # Single call_next, outside the try — downstream exceptions propagate to the
    # real exception handlers (→ proper 500) instead of being caught here.
    return await call_next(request)


# HTTP-error capture for the flight-recorder — records every raised 4xx/5xx
# (status, method, path, cause, user) into the recorder's `http` category +
# logs/http.log, via exception handlers that delegate to FastAPI's defaults
# (so responses are unchanged). 500s are recorded from the global Exception
# handler above. Reliable + stream-safe (no middleware in the request path).
try:
    from app.api.http_diag import install_http_diagnostics
    install_http_diagnostics(app)
except Exception as _httpdiag_err:  # never let diagnostics wiring break boot
    logger.warning("HTTP diagnostics handlers not installed: %s", _httpdiag_err)


# Include routers
app.include_router(chat_router)
app.include_router(terminal_router)
app.include_router(agent_router)
app.include_router(browser_stream_router)
app.include_router(uploads_router)
app.include_router(db_viewer_router)
app.include_router(diagnostics_router)
app.include_router(recordings_router)
for _admin_r in (admin_router, admin_db_router, admin_storage_router):
    if _admin_r is not None:
        app.include_router(_admin_r)
if _HAS_SOURCE_TOOLS and admin_source_router is not None:
    app.include_router(admin_source_router)

if _HAS_SETTINGS and admin_settings_router is not None:
    app.include_router(admin_settings_router)

# Register communications / webhook / event / scheduler admin routers (skipped if app/admin/ removed)
for _admin_r in (admin_communications_router, admin_webhooks_router, admin_events_router, scheduler_admin_router):
    if _admin_r is not None:
        app.include_router(_admin_r)
app.include_router(agents_router)
app.include_router(agent_pages_router)
app.include_router(data_sources_router)
app.include_router(files_router)
if admin_users_router is not None:
    app.include_router(admin_users_router)
app.include_router(billing_router)
app.include_router(feedback_router)

# Register auth router
app.include_router(auth_router)

# Register generic webhook router (more specific paths first)
app.include_router(webhooks_generic_router)

# Register communication plugin webhook router (for Telegram, WhatsApp, SMS etc.)
app.include_router(webhooks_router)

# Register event trigger intake (Gmail Pub/Sub, Graph subscriptions, etc.)
app.include_router(events_router)

# Register Remote Access (signpost endpoints + admin config API)
app.include_router(remote_access_router)
if admin_remote_access_router is not None:
    app.include_router(admin_remote_access_router)

# Register integrations & OAuth routers
if integrations_router is not None:
    app.include_router(integrations_router)
app.include_router(oauth_router)

# Register GitHub router
app.include_router(github_router)

# Register optimizer admin router
if optimizer_router is not None:
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

# Self-contained "web-terminal" page (app/web-terminal/), embedded as the
# Terminal tab via an <iframe>. html=True so /web-terminal/ serves index.html.
_WEB_TERMINAL_DIR = _APP_DIR / "web-terminal"
try:
    app.mount("/web-terminal", StaticFiles(directory=str(_WEB_TERMINAL_DIR), html=True), name="web-terminal")
    logger.info("Web terminal mounted at /web-terminal")
except Exception as e:
    logger.warning("Could not mount /web-terminal: %s", e)

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
        from app.api.terminal import close_persistent_session, stop_idle_gc
        stop_idle_gc()
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
    # Stop the event runtime.
    try:
        from app.events import stop_event_runtime
        await stop_event_runtime()
    except Exception:
        pass
    # Stop the liveness watchdog.
    try:
        from app.agent.watchdog import stop_watchdog
        await stop_watchdog()
    except Exception:
        pass
    # Stop Remote Access (watcher + any managed tunnel).
    try:
        from app.remote_access import stop_remote_access
        await stop_remote_access()
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


@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
async def privacy_page():
    """Serve the privacy policy page (public, no auth)."""
    privacy_html = _APP_DIR.parent / "ui" / "privacy.html"
    if not privacy_html.is_file():
        return HTMLResponse("<p>Missing privacy.html</p>", status_code=404)
    return HTMLResponse(content=privacy_html.read_text(encoding="utf-8"))


@app.get("/tos", response_class=HTMLResponse, include_in_schema=False)
async def tos_page():
    """Serve the terms of service page (public, no auth)."""
    tos_html = _APP_DIR.parent / "ui" / "tos.html"
    if not tos_html.is_file():
        return HTMLResponse("<p>Missing tos.html</p>", status_code=404)
    return HTMLResponse(content=tos_html.read_text(encoding="utf-8"))


@app.get("/termux", include_in_schema=False)
@app.get("/termux.sh", include_in_schema=False)
async def termux_installer():
    """Serve the Termux one-line installer for the standalone webAgent TUI.

    Enables `curl -fsSL https://webagent.live/termux | bash` to install the
    Server Manager TUI on Android. Served verbatim from
    `TUI/install-termux.sh`, with line endings forced to LF so a
    Windows checkout can never ship a CRLF script that bash refuses to run.
    Registered BEFORE the `/{agent_id}` catch-all below so it isn't shadowed."""
    from fastapi.responses import PlainTextResponse
    script = _APP_DIR.parent / "TUI" / "install-termux.sh"
    if not script.is_file():
        return PlainTextResponse("# webAgent TUI installer not found\n", status_code=404)
    body = script.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return PlainTextResponse(
        body,
        media_type="text/x-shellscript",  # Starlette appends "; charset=utf-8"
        headers={"Cache-Control": "no-cache"},
    )


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
    # Start the diagnostic flight-recorder's background batch-writer + pruner.
    try:
        from app.agent.diagnostics import start_recorder, record_run_lifecycle  # noqa: F401
        start_recorder()
        from app.agent.diagnostics import record as _diag_record
        _diag_record("info", "server", "Server starting up", source="startup")
    except Exception as _diag_err:
        logger.warning("Diagnostic recorder failed to start: %s", _diag_err)

    # Start the client render recorder's background batch-writer + pruner. It is
    # idle (accepts nothing) until render_recording_enabled is turned on.
    try:
        from app.agent.render_recorder import start_render_recorder
        start_render_recorder()
    except Exception as _rr_err:
        logger.warning("Render recorder failed to start: %s", _rr_err)

    # Launch the terminal idle-session GC. Reaps PTYs whose WebSocket has
    # been detached longer than TERMINAL_IDLE_TIMEOUT_HOURS (default 24h).
    try:
        from app.api.terminal import start_idle_gc
        start_idle_gc()
    except Exception as _gc_err:
        logger.warning("Failed to start terminal idle GC: %s", _gc_err)

    # Seed agent templates from JSON (manifest-gated short-circuit makes this
    # cheap when unchanged). LocalBackend self-seeds in __init__, so this call
    # is mainly the trigger for SupabaseBackend; for Local it's still a
    # belt-and-braces no-op when the hash matches.
    try:
        from app.db import get_db as _get_db_seed
        _seed_db = _get_db_seed()
        _seed_summary = await _seed_db.seed_agent_templates(force=False)
        if _seed_summary.get("cached"):
            logger.info("Agent template seed: cached (manifest hash unchanged)")
        else:
            logger.info(
                "Agent template seed: changed=%s skipped_admin=%s templates=%s",
                _seed_summary.get("changed"),
                _seed_summary.get("skipped_admin"),
                _seed_summary.get("templates"),
            )
    except Exception as _seed_err:
        logger.warning("Agent template seed at startup failed: %s", _seed_err)

    # Build trigger routing index from agent_templates
    try:
        from app.agent import trigger_index
        trigger_index.build()
    except Exception as _ti_err:
        logger.warning("Failed to build trigger index on startup: %s", _ti_err)

    # ── Self-healing recovery (step 1 of 2): mark mid-flight runs as resumable ──
    # A server restart is the one thing that ends an in-flight run. On boot we
    # flip any 'running' session_runs to 'interrupted' WITH stop_cause=
    # 'server_restart' (and streaming assistant rows to 'interrupted'). That tags
    # them as resume candidates; the actual re-ignition happens in step 2 below,
    # after the scheduler/event runtimes are up. See app/agent/runner.py.
    try:
        from app.db import get_db as _get_db_orphan
        _n_orphan = await _get_db_orphan().mark_orphans_for_resume()
        if _n_orphan:
            logger.info("Marked %d orphaned agent run(s) for resume (left by previous process)", _n_orphan)
    except Exception as _orphan_err:
        logger.warning("Orphaned-run marking failed: %s", _orphan_err)

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

    # ── Start event runtime (poll loop + subscription renewer) ──
    try:
        from app.events import start_event_runtime
        await start_event_runtime()
    except Exception as _evt_err:
        logger.warning("Failed to start event runtime: %s", _evt_err)

    # ── Self-healing recovery (step 2 of 2): re-ignite resumable orphans ──
    # Runs AFTER scheduler/events are up (a resumed turn may use their machinery).
    # Fully backend-driven — no user WebSocket required — so background, sub-agent
    # and delegated runs are recovered too. Only involuntary stops with a
    # resumable cause and remaining retry budget are relaunched; user-stopped,
    # replaced, throwaway, and opted-out runs are skipped. See app/agent/runner.py.
    try:
        from app.agent.runner import resume_orphans_at_boot
        _n_resumed = await resume_orphans_at_boot()
        if _n_resumed:
            logger.info("Self-healing: re-ignited %d orphaned run(s) at boot", _n_resumed)
    except Exception as _resume_err:
        logger.warning("Boot resume failed: %s", _resume_err)

    # ── Start the liveness watchdog (heals frozen/zombie runs while alive) ──
    try:
        from app.agent.watchdog import start_watchdog
        await start_watchdog()
    except Exception as _wd_err:
        logger.warning("Failed to start run watchdog: %s", _wd_err)

    # ── Start Remote Access (auto-start managed tunnel if configured) ──
    try:
        from app.remote_access import start_remote_access
        await start_remote_access()
    except Exception as _ra_err:
        logger.warning("Failed to start Remote Access: %s", _ra_err)

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
