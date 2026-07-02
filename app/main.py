"""Entry point for the WebAgent server.

CORE FILE — do NOT register new integrations / capabilities here. New event
sources, channels, connectors, integrations, vaults, encryption methods,
payment processors, and scheduler providers are DROP-IN FILES in their plugin
folder and are auto-discovered; they need no router/registration here. Only add
an include_router() for a genuinely new core subsystem. See CLAUDE.md
("Core vs. plugins") and docs/claude/production-editions.md.
"""

import logging
import os
import re
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

# Consume a one-shot Danger-Zone reset marker BEFORE the DB / stores open, so the
# selected data groups (database / vault / attachments / genui / logs) can be
# wiped while nothing holds them. A missing marker is a no-op; any error is
# swallowed so a bad marker can never brick boot. See app/util/reset_boot.py.
from app.util.reset_boot import run_pending_reset
run_pending_reset()

import traceback
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
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
from app.api.connector_ws import router as connector_ws_router
from app.api.uploads import router as uploads_router
from app.api.db_viewer import router as db_viewer_router
from app.api.diagnostics import router as diagnostics_router
from app.api.recordings import router as recordings_router
from app.api.features import router as features_router
from app.api.browser_log import router as browser_log_router
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
    from app.api.deploy import router as deploy_router
except ImportError:
    deploy_router = None
try:
    from app.admin.tunnel_link import router as admin_tunnel_link_router
except ImportError:
    admin_tunnel_link_router = None
try:
    from plugins.admin.source import router as admin_source_router
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
from plugins.billing.api import router as billing_router

# ── Auth ──
from app.auth import router as auth_router

# ── Social sign-in (drop-in providers under plugins/auth_providers/) ──
from app.api.social_auth import router as social_auth_router

# ── GitHub ──
from app.api.github import router as github_router

# ── Local Claude Code engine sign-in (in-app `claude setup-token`) ──
from app.api.claude_auth import router as claude_auth_router
# ── Local Claude Code engine skills (Skills tab on the Claude agent card) ──
from app.api.claude_skills import router as claude_skills_router

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

# ── Task grouping (derive-at-read-time, no migration) ──
try:
    from app.admin.tasks import router as tasks_router
except ImportError:
    tasks_router = None
from app.api.genui import router as genui_router
from app.api.devices import router as devices_router
from app.api.wiki import router as wiki_router
from app.api.ability_delete import router as ability_delete_router

# Configure logging
# Level precedence: data/config/debug-config.json (the consolidated debug knobs)
# wins, then the LOG_LEVEL env var, then INFO. Read once at boot, so a change here
# needs a server restart.
try:
    from app.admin.debug_config import log_level as _dbg_log_level
    _log_level_name = (_dbg_log_level() or os.environ.get("LOG_LEVEL", "INFO")).upper()
except Exception:
    _log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level_name, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Diagnostic flight-recorder: mirror WARNING+ log records (with tracebacks) into
# the in-app recorder so an operator / diagnostic agent can read recent failures
# back via the API + read_diagnostics tool, not just the console. The background
# writer is started in the startup() hook below (needs the event loop).
try:
    from app.agent.diagnostics import install_log_handler as _install_diag_handler
    from app.admin.debug_config import diagnostics_capture_level as _dbg_diag_level
    # INFO by default: capture full backend detail into the now-isolated logs.db.
    # The handler applies a per-logger policy (our app.* at INFO+, noisy libs at
    # WARNING+) so this stays signal, not framework chatter. Level precedence:
    # debug-config.json > DIAGNOSTICS_CAPTURE_LEVEL env var > INFO.
    _diag_level_name = (_dbg_diag_level() or os.environ.get("DIAGNOSTICS_CAPTURE_LEVEL", "INFO")).upper()
    _diag_level = getattr(logging, _diag_level_name, logging.INFO)
    _install_diag_handler(level=_diag_level)
except Exception as _diag_err:  # never let diagnostics wiring break boot
    logger.warning("Diagnostic log handler not installed: %s", _diag_err)

app = FastAPI(
    title="WebAgent API",
    description="WebAgent — FastAPI service with tool-calling agent loop and WebSocket streaming",
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


# ── Access log ──
# Record every (non-static) HTTP request into the diagnostics `access` category
# in the dedicated logs.db, so the flight recorder shows the full HTTP timeline,
# not just errors. Static asset GETs are skipped to keep the log signal-rich.
_ACCESS_SKIP_PREFIXES = ("/ui/", "/static", "/assets", "/screenshots/",
                         "/visuals/", "/user_data/", "/favicon", "/web-terminal")
_ACCESS_SKIP_EXT = (".js", ".css", ".map", ".ico", ".svg", ".png", ".jpg",
                    ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".ttf")


@app.middleware("http")
async def _access_log_middleware(request: Request, call_next):
    import time as _t
    start = _t.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        try:
            path = request.url.path
            if not (path.startswith(_ACCESS_SKIP_PREFIXES) or path.endswith(_ACCESS_SKIP_EXT)):
                from app.api.http_diag import _user_from_request
                from app.agent.diagnostics import record_access
                record_access(
                    request.method, path, status,
                    int((_t.perf_counter() - start) * 1000),
                    user_id=_user_from_request(request),
                    client=(request.client.host if request.client else None),
                )
        except Exception:
            pass


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

# Decode the request's JWT once and stash the VERIFIED caller id in a
# contextvar, so token-less admin chokepoints (app.auth.identity.resolve_admin_uid)
# can demand a cryptographically-proven identity instead of trusting a
# client-claimed ``requesting_user_id``. Pure-ASGI so the contextvar reliably
# reaches the endpoint. See app/auth/identity.py.
from app.auth.identity import CallerIdentityMiddleware
app.add_middleware(CallerIdentityMiddleware)


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
app.include_router(connector_ws_router)
app.include_router(uploads_router)
app.include_router(db_viewer_router)
app.include_router(diagnostics_router)
app.include_router(browser_log_router)
app.include_router(recordings_router)
app.include_router(features_router)
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
# Mount any optional billing-extension routes, discovered generically from
# plugins/<group>/billing/. None exist in a standard build, so this is a no-op.
try:
    from plugins.billing.extensions import load_billing_extensions
    for _ext_mod in load_billing_extensions():
        _ext_router = getattr(_ext_mod, "router", None)
        if _ext_router is not None:
            app.include_router(_ext_router)
except Exception as _bext_err:
    logging.getLogger(__name__).warning("billing extension mount skipped: %s", _bext_err)
app.include_router(feedback_router)

# Register auth router
app.include_router(auth_router)
app.include_router(social_auth_router)

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

# Register Deploy (App Settings → Deploy: live one-click cloud deploy)
if deploy_router is not None:
    app.include_router(deploy_router)

# Register Tunnel Link (App Settings → Tunnel; talks to the hosted relay)
if admin_tunnel_link_router is not None:
    app.include_router(admin_tunnel_link_router)

# Register integrations & OAuth routers
if integrations_router is not None:
    app.include_router(integrations_router)
app.include_router(oauth_router)

# Register GitHub router
app.include_router(github_router)

# Register Local Claude Code sign-in router
app.include_router(claude_auth_router)
# Register Local Claude Code skills router (Skills tab on the Claude agent card)
app.include_router(claude_skills_router)

# Register optimizer admin router
if optimizer_router is not None:
    app.include_router(optimizer_router)

# Register task-grouping admin router
if tasks_router is not None:
    app.include_router(tasks_router)

# Register Gen UI router
app.include_router(genui_router)

# Register Devices router (presence list for the target-device picker)
app.include_router(devices_router)

# Register ability deletion API
app.include_router(ability_delete_router)

# Register the company-wide Wiki router
app.include_router(wiki_router)

# ── Drop-in page backends ──
# A main-panel / admin page folder may carry its own API (a ``server.py`` named
# in its page.json ``router`` field) exposing a FastAPI ``router``. They are
# discovered + mounted here so a page's endpoints come and go with its folder —
# adding or removing a page needs NO edit above. Each is isolated: a broken
# page backend is skipped, never failing app boot.
try:
    from app import ui_pages as _ui_pages
    for _pid, _page_router in _ui_pages.discover_routers():
        try:
            app.include_router(_page_router)
            logger.info("Registered drop-in page backend: %s", _pid)
        except Exception as _e:
            logger.warning("Could not mount page backend %s: %s", _pid, _e)
except Exception as _e:
    logger.warning("Page backend discovery failed: %s", _e)

# ── Restart endpoint ──
# POST /api/v1/restart shuts down the server process.
# Works with WebAgent.bat which loops uvicorn in a :restart cycle.
restart_router = APIRouter(prefix="/api/v1")

@restart_router.post("/restart")
async def restart_server():
    """Shut down the server. WebAgent.bat will restart it automatically."""
    import threading
    logger.warning("Restart requested via /api/v1/restart — shutting down...")
    # Give the response time to be sent before the process exits
    def _die():
        import time
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()
    return {"status": "restarting", "message": "Server is shutting down. WebAgent.bat will restart it."}

app.include_router(restart_router)

# ── Static file mounts ──
_SCREENSHOTS_DIR = _APP_DIR.parent / "data" / "screenshots"
_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=str(_SCREENSHOTS_DIR)), name="screenshots")

# Uploads, generated images (visuals), and Gen UI page bodies now all live in
# the per-user data home (data/user_data/<uid>/{uploads,visuals,genui}) and serve
# from the /user_data mount below — so the old top-level /uploads and /visuals
# mounts are retired. (Gen UI are served via the /api/v1/genui route, not a mount.)

# Per-user data home (data/user_data/<user_id>/…) — uploads, generated images,
# page bodies, agent outputs, and screenshots. See app/user_workspace.py.
try:
    from app.user_workspace import base_dir as _user_data_base
    _USER_DATA_DIR = _user_data_base()
    _USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/user_data", StaticFiles(directory=str(_USER_DATA_DIR)), name="user_data")
    logger.info("User data directory mounted at /user_data")
except Exception as e:
    logger.warning("Could not mount /user_data: %s", e)

_UI_DIR = _APP_DIR.parent / "ui"


class _UIStaticFiles(StaticFiles):
    """Serve ui/ assets but NEVER the Python source of a page's drop-in backend
    (``server.py`` etc.). Page folders may now hold a ``*.py`` router that is
    imported server-side; it must not be downloadable. 404 any .py / pyc /
    __pycache__ path so source stays private."""

    async def get_response(self, path, scope):
        norm = path.replace("\\", "/").lower()
        if norm.endswith((".py", ".pyc", ".pyo")) or "__pycache__/" in norm:
            from starlette.responses import PlainTextResponse
            return PlainTextResponse("Not Found", status_code=404)
        return await super().get_response(path, scope)


app.mount("/ui", _UIStaticFiles(directory=str(_UI_DIR)), name="ui")

_ROOT_INDEX_HTML = _APP_DIR.parent / "index.html"


# ── SEO / social-preview meta injection ─────────────────────────────────────
# Default copy for the app shell. Per-agent public pages override title + desc
# with the agent's name so a shared /{agent_id} link previews meaningfully.
_SEO_DEFAULT_TITLE = "WebAgent — your AI agent harness"
_SEO_DEFAULT_DESC = (
    "WebAgent is a self-hostable AI agent harness: chat with capable agents, "
    "give them tools and automations, and build visual pages — all in one app."
)
# Dedicated 1200×630 landscape social card — the size Open Graph / Twitter
# "summary_large_image" expects. Generated asset (regenerate with
# scripts/make_og_card.py). The square app icon is kept only as the Organisation
# LOGO in structured data, never as the share-preview image (a square icon in a
# large-image card previews badly).
_SEO_PREVIEW_IMAGE = "/ui/icons/og-card.png"
_SEO_LOGO_IMAGE = "/ui/icons/icon-512x512.png"  # square brand mark (schema.org logo)
# Authoritative external profiles for the brand entity (schema.org `sameAs` on the
# Organisation) — strengthens entity recognition and brand sitelinks. Add more as
# they come online (X, LinkedIn, Discord, …); an empty list omits the field.
_SEO_SAMEAS = ["https://github.com/botboss3000/webagent"]

# Marketing copy for the public landing front page (see _render_landing_page).
_LANDING_TITLE = "WebAgent — your own team of AI agents"
_LANDING_DESC = (
    "Chat, automate, browse the web, build live dashboards and share knowledge — "
    "all driven by tool-using agents you shape to fit the way you work."
)
_SPLASH_DIR = _APP_DIR.parent / "ui" / "splash" / "splash-page"


def _seo_origin(request: Request) -> str:
    """Absolute origin (scheme+host, no trailing slash) for Open Graph / canonical
    urls and sitemap entries. Reuses the integrations base-url resolver so it
    honours WEBHOOK_BASE_URL / webhook_base_url.txt and is https-correct behind a
    TLS-terminating proxy, falling back to the request-derived host."""
    try:
        from app.admin.integrations import _get_base_url
        return _get_base_url(request).rstrip("/")
    except Exception:
        return str(request.base_url).rstrip("/")


def _iso_ts(ts) -> str:
    """Normalise a stored timestamp to ISO 8601 for schema.org / Open Graph.
    Handles the bare SQLite `datetime('now')` form ("YYYY-MM-DD HH:MM:SS", assumed
    UTC → swap the space for "T" and append "Z") and passes an already-full ISO
    value (with "T" and/or a timezone) straight through. Empty in → empty out."""
    ts = (ts or "").strip()
    if not ts:
        return ""
    ts = ts.replace(" ", "T", 1)
    return ts + "Z" if len(ts) == 19 and ts[10] == "T" else ts


def _seo_head_block(origin: str, path: str, title: str, desc: str, noindex: bool = False,
                    og_type: str = "website", published=None, modified=None,
                    image: str = None) -> str:
    """Build the shared SEO / social-preview <head> tags (description, canonical,
    Open Graph, Twitter card) with ABSOLUTE urls — link scrapers reject relative
    og:image / og:url. Used by both the app shell and the landing page. Pass
    noindex=True for shareable-but-not-indexed pages (e.g. public agent links).
    Pass og_type="article" with published/modified timestamps for a wiki article so
    it previews as a dated article (adds article:published_time/modified_time)
    rather than a generic website. Pass image=<absolute url> to override the default
    1200×630 share card (e.g. a wiki article's own first image); the card's fixed
    width/height tags are emitted only for that default, since a custom image's
    dimensions are unknown."""
    from html import escape as _esc
    page_url = origin + (path or "/")
    image_url = image or (origin + _SEO_PREVIEW_IMAGE)
    et, ed = _esc(title, quote=True), _esc(desc, quote=True)
    eurl, eimg = _esc(page_url, quote=True), _esc(image_url, quote=True)
    robots = '<meta name="robots" content="noindex, follow">\n' if noindex else ""
    dims = "" if image else (
        '<meta property="og:image:width" content="1200">\n'
        '<meta property="og:image:height" content="630">\n'
    )
    article_meta = ""
    if og_type == "article":
        for prop, raw in (("article:published_time", published), ("article:modified_time", modified)):
            iso = _iso_ts(raw)
            if iso:
                article_meta += f'<meta property="{prop}" content="{_esc(iso, quote=True)}">\n'
    return (
        robots
        + f'<meta name="description" content="{ed}">\n'
        f'<link rel="canonical" href="{eurl}">\n'
        f'<meta property="og:type" content="{_esc(og_type, quote=True)}">\n'
        + article_meta
        + f'<meta property="og:site_name" content="WebAgent">\n'
        f'<meta property="og:title" content="{et}">\n'
        f'<meta property="og:description" content="{ed}">\n'
        f'<meta property="og:url" content="{eurl}">\n'
        f'<meta property="og:image" content="{eimg}">\n'
        + dims
        + f'<meta property="og:image:alt" content="{et}">\n'
        f'<meta property="og:locale" content="en_US">\n'
        f'<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:title" content="{et}">\n'
        f'<meta name="twitter:description" content="{ed}">\n'
        f'<meta name="twitter:image" content="{eimg}">\n'
    )


# ── Structured data (schema.org / JSON-LD) ──────────────────────────────────
# Machine-readable summaries crawlers use for rich results: the brand identity
# under the site name, dated article cards, and breadcrumb trails. Emitted as
# <script type="application/ld+json"> blocks appended to the SEO <head>.

def _jsonld_block(*objs) -> str:
    """Serialize one or more schema.org objects into a single JSON-LD <script>.
    Values are JSON-escaped, and any literal "</" is neutralised so an article's
    own text can never close the <script> tag early."""
    import json
    data = objs[0] if len(objs) == 1 else list(objs)
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f'<script type="application/ld+json">{text}</script>\n'


def _home_jsonld(origin: str) -> str:
    """Organisation + WebSite structured data for the public home page — lets
    search engines treat the site as a named entity (brand name + logo) and is
    the basis for brand sitelinks. Emitted only on indexable home surfaces."""
    org = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "WebAgent",
        "url": origin + "/",
        "logo": origin + _SEO_LOGO_IMAGE,
        "description": _SEO_DEFAULT_DESC,
    }
    if _SEO_SAMEAS:
        org["sameAs"] = list(_SEO_SAMEAS)
    site = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": "WebAgent",
        "url": origin + "/",
    }
    return _jsonld_block(org, site)


def _breadcrumb_obj(origin: str, trail) -> dict:
    """A schema.org BreadcrumbList from a list of (name, path) pairs."""
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name, "item": origin + path}
            for i, (name, path) in enumerate(trail)
        ],
    }


def _wiki_article_jsonld(origin, slug, title, desc, published, modified, image=None) -> str:
    """Article + breadcrumb structured data for one wiki page — makes it eligible
    for dated article rich results (headline, publish/updated dates, publisher).
    image overrides the default share card with the article's own first image."""
    page_url = origin + "/wiki/" + slug
    article = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": desc,
        "url": page_url,
        "mainEntityOfPage": {"@type": "WebPage", "@id": page_url},
        "image": image or (origin + _SEO_PREVIEW_IMAGE),
        "author": {"@type": "Organization", "name": "WebAgent", "url": origin + "/"},
        "publisher": {
            "@type": "Organization",
            "name": "WebAgent",
            "logo": {"@type": "ImageObject", "url": origin + _SEO_LOGO_IMAGE},
        },
    }
    # Normalise stored timestamps to ISO 8601 (schema.org wants it); see _iso_ts.
    if published:
        article["datePublished"] = _iso_ts(published)
    if modified:
        article["dateModified"] = _iso_ts(modified)
    crumb = _breadcrumb_obj(origin, [("Home", "/"), ("Wiki", "/wiki"), (title, "/wiki/" + slug)])
    return _jsonld_block(article, crumb)


def _resolve_og_image(url: str, origin: str):
    """Turn a wiki image url into an ABSOLUTE share-image url, or None to fall back
    to the default card. External http(s) urls are used as-is; a site-relative
    "/path" url is made absolute against the origin. Anything else (bare-relative,
    protocol-relative "//", data:, fragment, unknown scheme) is skipped — a broken
    image just yields no preview, which crawlers tolerate."""
    u = (url or "").strip()
    if not u:
        return None
    if u.lower().startswith(("http://", "https://")):
        return u
    if u.startswith("/") and not u.startswith("//"):
        return origin + u
    return None


def _first_article_image(body: str, origin: str):
    """The article's first Markdown image (![alt](url)) resolved to an absolute
    share image — or None. Fenced code blocks are stripped first so an image in a
    code sample doesn't count."""
    if not body:
        return None
    stripped = re.sub(r"```.*?```", " ", body, flags=re.S)
    m = re.search(r"!\[[^\]]*\]\(([^)\s]+)\)", stripped)
    if not m:
        return None
    return _resolve_og_image(m.group(1), origin)


# Conservative match for search / social crawler User-Agents. Used so the home
# page serves crawlable content even when the human welcome experience is off.
_CRAWLER_UA = re.compile(
    r"googlebot|bingbot|slurp|duckduckbot|baiduspider|yandex|sogou|exabot|ia_archiver|"
    r"facebookexternalhit|facebot|twitterbot|linkedinbot|slackbot|telegrambot|whatsapp|"
    r"discordbot|applebot|pinterest|redditbot|embedly|quora|bitlybot|skypeuripreview|"
    r"\bbot\b|crawler|spider",
    re.IGNORECASE,
)


def _is_crawler(request: Request) -> bool:
    """True if the request looks like a search/social crawler (by User-Agent)."""
    return bool(_CRAWLER_UA.search(request.headers.get("user-agent", "")))


def _render_app_shell(request: Request, agent_id=None, agent_name=None) -> HTMLResponse:
    """Serve index.html with the SEO / social-preview <head> built server-side.

    The static index.html ships only a bare <title>; this injects the full
    description / canonical / Open Graph / Twitter-card block with ABSOLUTE urls.
    For a public agent link it also swaps the title + description to that agent's
    name, so sharing the link previews as "Chat with <Agent>" rather than a
    generic "WebAgent" (and marks it noindex — shareable-only). This is the single
    source of truth for app-shell head SEO — keep it here, not in the static file
    (see the matching KEEP comment in index.html)."""
    import json
    from html import escape as _esc
    if not _ROOT_INDEX_HTML.is_file():
        return HTMLResponse("<p>Missing index.html</p>", status_code=404)
    html = _ROOT_INDEX_HTML.read_text(encoding="utf-8")
    origin = _seo_origin(request)
    path = request.url.path or "/"
    title = _SEO_DEFAULT_TITLE
    desc = _SEO_DEFAULT_DESC
    if agent_name:
        title = f"Chat with {agent_name} — WebAgent"
        desc = f"Chat with {agent_name}, an AI agent on WebAgent."
    # /app and /index.html serve the SAME home shell as / — point their canonical
    # (and og:url) at / so search engines treat the three as one page rather than
    # duplicate content. A public agent link keeps its own path (it's noindex, so
    # the canonical is moot anyway).
    canonical_path = path if agent_id else "/"
    meta = _seo_head_block(origin, canonical_path, title, desc, noindex=bool(agent_id))
    if agent_id:
        meta += (
            f"<script>window.__agentId = {json.dumps(agent_id)}; "
            f"window.__agentName = {json.dumps(agent_name or 'Agent')};</script>\n"
        )
    else:
        # Brand identity (Organisation + WebSite) for the indexable app home. A
        # public agent link is noindex, so it skips this.
        meta += _home_jsonld(origin)
    html = html.replace("<title>WebAgent</title>", f"<title>{_esc(title, quote=True)}</title>", 1)
    html = html.replace("</head>", meta + "</head>", 1)
    return HTMLResponse(content=html)


def _render_landing_page(request: Request):
    """Server-rendered marketing landing page for the front door (/).

    Reuses the splash plugin's markup + stylesheet (ui/splash/splash-page/) so it's
    the same premium welcome — but delivered as a REAL crawlable page, with all the
    copy present in the initial HTML, instead of a JavaScript overlay a search
    crawler can't see. The markup is wrapped in the same #splash-root container the
    splash CSS targets, with `is-ready` set inline so the content is visible even
    before JavaScript runs. Returns None when the splash folder is absent, so the
    caller falls back to the app shell and deleting ui/splash/splash-page/ cleanly
    removes the landing (drop-in)."""
    from html import escape as _esc
    markup_file = _SPLASH_DIR / "splash-page.html"
    if not markup_file.is_file():
        return None
    try:
        markup = markup_file.read_text(encoding="utf-8")
    except Exception:
        return None
    origin = _seo_origin(request)
    head = _seo_head_block(origin, "/", _LANDING_TITLE, _LANDING_DESC, noindex=False)
    head += _home_jsonld(origin)
    et = _esc(_LANDING_TITLE, quote=True)
    doc = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"<title>{et}</title>\n"
        + head
        + '<meta name="theme-color" content="#0d0d1a">\n'
        '<link rel="icon" href="/ui/favicon.svg" type="image/svg+xml">\n'
        '<link rel="manifest" href="/ui/manifest.json">\n'
        '<link rel="apple-touch-icon" href="/ui/icons/icon-512x512.png">\n'
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap">\n'
        # design-system tokens + the live appearance overrides so the landing is
        # correctly themed (dark/light + admin palette) exactly like the app.
        '<link rel="stylesheet" href="/ui/shared/css/design-system.css">\n'
        '<script src="/ui/shared/js/appearance.js"></script>\n'
        '<link rel="stylesheet" href="/ui/splash/splash-page/splash-page.css">\n'
        "<style>\n"
        "  /* Standalone base — the splash CSS is scoped under #splash-root and\n"
        "     assumes the app's resets around it; supply the minimum here. */\n"
        "  * { margin: 0; padding: 0; box-sizing: border-box; }\n"
        "  html, body { background: var(--bg-0); color: var(--fg-1); min-height: 100vh; }\n"
        "</style>\n"
        "</head>\n<body>\n"
        '<div id="splash-root" class="is-ready">\n'
        + markup
        + "\n</div>\n"
        '<script type="module" src="/ui/splash/splash-page/js/splash-landing.js"></script>\n'
        "</body>\n</html>\n"
    )
    return HTMLResponse(content=doc)


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
    # Stop all singleton background services (scheduler, event runtime, ability
    # pollers, watchdog, Remote Access) and release the leader lease for a clean
    # handoff. The leader runs them only in the elected worker; stop() is a no-op
    # in workers that never held leadership.
    try:
        from app.coordination.leader import get_leader
        await get_leader().stop()
    except Exception:
        pass
    # Stop the per-instance device worker (multi-device dispatch). See app/devices/.
    try:
        from app.devices import stop_device_worker
        await stop_device_worker()
    except Exception:
        pass
    # Stop the hybrid sync engine and let it flush any final pending pushes.
    try:
        _engine = getattr(app.state, "hybrid_sync_engine", None)
        if _engine is not None:
            await _engine.stop()
    except Exception:
        pass


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Front door. Serves the server-rendered marketing landing page (crawlable)
    to anyone who hasn't entered the app yet, plus to ALL search/social crawlers.

    SEO note — the crawlable landing is decoupled from the welcome toggle: a
    crawler always gets real, readable home content even when the human welcome
    experience (App Settings → Startup & Boot, stored as splash_enabled) is off,
    so the home page is never an empty JavaScript shell to a search engine.
    Humans: when the welcome is enabled, a NEW visitor (no `wa_seen_splash`
    cookie — set by the landing's Enter button / the Manage Account preference)
    sees the landing; when it's disabled, humans go straight to the app shell.
    The app also has a stable home at /app and /index.html that always bypass the
    landing."""
    try:
        from app.admin.settings import get_splash_enabled
        landing_on = get_splash_enabled()
    except Exception:
        landing_on = False
    not_entered = request.cookies.get("wa_seen_splash") != "1"
    # A crawler always gets the crawlable landing (independent of the toggle); a
    # human gets it only when the welcome is enabled and they haven't entered yet.
    if (not_entered and landing_on) or _is_crawler(request):
        page = _render_landing_page(request)
        if page is not None:
            return page
    return _render_app_shell(request)


@app.get("/app", response_class=HTMLResponse, include_in_schema=False)
async def app_shell_direct(request: Request):
    """The app's stable home — always the app shell, bypassing the landing front
    door. The installed PWA (manifest start_url) and the landing's "Enter app"
    button point here so they never bounce through the marketing page."""
    return _render_app_shell(request)


@app.get("/index.html", response_class=HTMLResponse, include_in_schema=False)
async def main_ui(request: Request):
    """Serve the main web UI directly (static assets remain under /ui/). Like
    /app, this always bypasses the landing front door."""
    return _render_app_shell(request)


@app.get("/setup.html", response_class=HTMLResponse, include_in_schema=False)
async def setup_ui():
    """Serve the admin setup page (first-run wizard)."""
    setup_html = _APP_DIR.parent / "ui" / "setup.html"
    if not setup_html.is_file():
        return HTMLResponse("<p>Missing setup.html</p>", status_code=404)
    return HTMLResponse(content=setup_html.read_text(encoding="utf-8"))


@app.get("/login.html", response_class=HTMLResponse, include_in_schema=False)
async def login_ui():
    """Serve the login page."""
    login_html = _APP_DIR.parent / "ui" / "login.html"
    if not login_html.is_file():
        return HTMLResponse("<p>Missing login.html</p>", status_code=404)
    return HTMLResponse(content=login_html.read_text(encoding="utf-8"))


def _render_static_seo_page(request: Request, rel_path: str, page_path: str,
                            title: str, desc: str) -> HTMLResponse:
    """Serve a static public HTML page (e.g. the legal pages) with the shared SEO
    head injected server-side. These files ship only a bare <title>, so on their
    own they're 'thin' to a crawler (no description / canonical / Open Graph); this
    adds the full block before </head> with absolute urls. 404s if the file is
    missing."""
    from html import escape as _esc
    f = _APP_DIR.parent / rel_path
    if not f.is_file():
        return HTMLResponse(f"<p>Missing {rel_path}</p>", status_code=404)
    html = f.read_text(encoding="utf-8")
    origin = _seo_origin(request)
    meta = _seo_head_block(origin, page_path, title, desc, noindex=False)
    # Make the route's SEO title the single source of truth: replace the file's
    # bare <title> so the visible title and the injected og:title can't diverge.
    html = re.sub(r"<title>.*?</title>", f"<title>{_esc(title, quote=True)}</title>",
                  html, count=1, flags=re.S | re.I)
    html = html.replace("</head>", meta + "</head>", 1)
    return HTMLResponse(content=html)


@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
async def privacy_page(request: Request):
    """Serve the privacy policy page (public, no auth) with SEO head injected."""
    return _render_static_seo_page(
        request, "ui/privacy.html", "/privacy", "Privacy Policy — WebAgent",
        "How WebAgent handles your data — what is collected, how it is stored and "
        "used, and the choices and controls you have.")


@app.get("/tos", response_class=HTMLResponse, include_in_schema=False)
async def tos_page(request: Request):
    """Serve the terms of service page (public, no auth) with SEO head injected."""
    return _render_static_seo_page(
        request, "ui/tos.html", "/tos", "Terms of Service — WebAgent",
        "The terms for using WebAgent — acceptable use, your responsibilities, and "
        "the terms that govern the service.")


@app.get("/termux", include_in_schema=False)
@app.get("/termux.sh", include_in_schema=False)
async def termux_installer():
    """Serve the Termux one-line installer for the standalone WebAgent TUI.

    Enables `curl -fsSL https://webagent.live/termux | bash` to install the
    Server Manager TUI on Android. Served verbatim from
    `TUI/install-termux.sh`, with line endings forced to LF so a
    Windows checkout can never ship a CRLF script that bash refuses to run.
    Registered BEFORE the `/{agent_id}` catch-all below so it isn't shadowed."""
    from fastapi.responses import PlainTextResponse
    script = _APP_DIR.parent / "TUI" / "install-termux.sh"
    if not script.is_file():
        return PlainTextResponse("# WebAgent TUI installer not found\n", status_code=404)
    body = script.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return PlainTextResponse(
        body,
        media_type="text/x-shellscript",  # Starlette appends "; charset=utf-8"
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt(request: Request):
    """Crawler directives. Allows the public surface (home + legal pages + static
    assets so render-based crawlers can paint the page), keeps crawlers out of the
    API, auth, and dev/test endpoints, and points to the sitemap. Registered
    BEFORE the /{agent_id} catch-all so it isn't shadowed by it."""
    origin = _seo_origin(request)
    body = (
        "User-agent: *\n"
        "Disallow: /api/\n"
        "Disallow: /login.html\n"
        "Disallow: /setup.html\n"
        "Disallow: /test\n"
        "Disallow: /docs\n"
        "Disallow: /redoc\n"
        "Disallow: /web-terminal/\n"
        "Disallow: /user_data/\n"
        "Disallow: /screenshots/\n"
        "Disallow: /go/\n"
        f"\nSitemap: {origin}/sitemap.xml\n"
    )
    return PlainTextResponse(body, media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml(request: Request):
    """Sitemap of the publicly indexable pages: home + legal + the Wiki index +
    one entry per PUBLISHED wiki article (with a <lastmod> from updated_at). The
    article list is read live from data/wiki.db on each request, so newly
    published / unpublished articles appear / disappear with no rebuild. Public
    per-agent pages are intentionally omitted pending the indexing-policy
    decision. Registered BEFORE the /{agent_id} catch-all so it isn't shadowed."""
    from html import escape as _esc
    from datetime import datetime, timezone
    origin = _seo_origin(request)

    def _u(path, day=None):
        loc = _esc(origin + path, quote=True)
        if day:
            return f"  <url><loc>{loc}</loc><lastmod>{_esc(day, quote=True)}</lastmod></url>\n"
        return f"  <url><loc>{loc}</loc></url>\n"

    def _file_day(fp):
        # <lastmod> for a static page = its source file's modification date (UTC).
        try:
            return datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return None

    urls = (
        _u("/", _file_day(_SPLASH_DIR / "splash-page.html"))
        + _u("/privacy", _file_day(_APP_DIR.parent / "ui" / "privacy.html"))
        + _u("/tos", _file_day(_APP_DIR.parent / "ui" / "tos.html"))
    )
    try:
        from app.wiki.store import list_articles
        articles = await list_articles(include_drafts=False)
        # The Wiki index changes whenever any published article does, so stamp it
        # with the most recent article update.
        latest = max(((a.get("updated_at") or "")[:10] for a in articles), default="")
        urls += _u("/wiki", latest if len(latest) == 10 else None)
        for a in articles:
            slug = a.get("slug")
            if not slug:
                continue
            day = (a.get("updated_at") or "")[:10]
            urls += _u("/wiki/" + slug, day if len(day) == 10 else None)
    except Exception:
        urls += _u("/wiki")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}"
        "</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml")


@app.get("/wiki", response_class=HTMLResponse, include_in_schema=False)
async def wiki_public_index(request: Request):
    """Public, crawlable index of PUBLISHED wiki articles, server-rendered.

    Companion doorway to the in-app Wiki SPA: the SAME data (data/wiki.db) but
    delivered as real HTML so search engines (which don't run the SPA's JS) can
    read it. Drawn live on each request — no build step. Drafts never appear
    (anonymous/public read path). Registered BEFORE the /{agent_id} catch-all so
    the single-segment /wiki isn't swallowed by it; see app/wiki/public_pages.py."""
    from app.wiki.store import list_articles
    from app.wiki import public_pages
    origin = _seo_origin(request)
    try:
        articles = await list_articles(include_drafts=False)
    except Exception:
        articles = []
    title = "Wiki — WebAgent"
    desc = "Browse the WebAgent knowledge base — published guides, policies and reference."
    head = _seo_head_block(origin, "/wiki", title, desc, noindex=False)
    head += _jsonld_block(_breadcrumb_obj(origin, [("Home", "/"), ("Wiki", "/wiki")]))
    return HTMLResponse(public_pages.build_index_html(
        articles=articles, origin=origin, head_html=head, title=title))


@app.get("/wiki/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def wiki_public_article(slug: str, request: Request):
    """Public, crawlable page for ONE published wiki article, server-rendered.

    Two path segments, so the single-segment /{agent_id} catch-all never matches
    it. Returns 404 for a missing slug OR a draft — the store hides a draft's very
    existence from public callers (include_drafts=False). Per-article title +
    description + canonical + Open Graph come from the shared _seo_head_block."""
    from app.wiki.store import get_article, get_backlinks, list_articles
    from app.wiki import public_pages
    origin = _seo_origin(request)
    article = await get_article(slug, include_drafts=False)
    if not article:
        head = _seo_head_block(
            origin, f"/wiki/{slug}", "Not found — WebAgent Wiki",
            "This wiki article doesn't exist or isn't public.", noindex=True)
        return HTMLResponse(
            public_pages.build_notfound_html(origin=origin, head_html=head),
            status_code=404)
    # Build a (title/slug → slug) index from the published set so [[wiki-links]]
    # in the body resolve to other PUBLIC pages (unknown targets render as text).
    try:
        published = await list_articles(include_drafts=False)
    except Exception:
        published = []
    link_index = {}
    for a in published:
        s = a.get("slug")
        if not s:
            continue
        link_index[s.lower()] = s
        t = a.get("title")
        if t:
            link_index[t.lower()] = s
    try:
        related = await get_backlinks(slug, include_drafts=False)
    except Exception:
        related = []
    page_title = f"{article.get('title') or 'Untitled'} — WebAgent Wiki"
    desc = public_pages.make_description(article.get("body") or "")
    # Prefer the article's own first image as the share preview; fall back to the
    # default 1200×630 card when it has none (or none that verifiably exists).
    og_image = _first_article_image(article.get("body") or "", origin)
    head = _seo_head_block(
        origin, f"/wiki/{slug}", page_title, desc, noindex=False, og_type="article",
        published=article.get("created_at"), modified=article.get("updated_at"),
        image=og_image)
    head += _wiki_article_jsonld(
        origin, slug, article.get("title") or "Untitled", desc,
        article.get("created_at"), article.get("updated_at"), image=og_image)
    return HTMLResponse(public_pages.build_article_html(
        article=article, related=related, link_index=link_index,
        origin=origin, head_html=head, title=page_title))


import re as _re
_AGENT_UUID_RE = _re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    _re.IGNORECASE,
)

@app.get("/{agent_id}", response_class=HTMLResponse, include_in_schema=False)
async def public_agent_chat(agent_id: str, request: Request):
    """Serve the main UI with a specific agent pre-selected (public access).

    Renders through `_render_app_shell` so the shared link also gets a per-agent
    title + Open Graph / Twitter preview (in addition to the __agentId /
    __agentName bootstrap the app reads)."""
    if not _AGENT_UUID_RE.match(agent_id):
        return HTMLResponse("<p>Not found</p>", status_code=404)
    from app.db import get_db as _get_db
    db = _get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        return HTMLResponse("<p>Agent not found</p>", status_code=404)
    return _render_app_shell(request, agent_id=agent_id, agent_name=agent.get("name", "Agent"))

@app.get("/test", response_class=HTMLResponse)
async def test_interface():
    """Serve the test interface HTML page."""
    test_html = _APP_DIR.parent / "ui" / "test_interface.html"
    return HTMLResponse(content=test_html.read_text(encoding="utf-8"))


# ── Start-up: register Telegram webhook ──
@app.on_event("startup")
async def startup():
    """Register communication webhooks or start polling on server start."""
    # ── Full-DB encryption reconcile (MUST be first) ──
    # Before ANY store opens its SQLite files, bring each database file into line
    # with the configured at-rest encryption state (encrypt newly-enabled files,
    # decrypt newly-disabled ones) — backup-first, atomic, no plaintext residue.
    # No-op (and instant) when nothing is enabled, which is the default. Runs here
    # so the diagnostics recorder + every backend below open already-correct files.
    try:
        from app.db import db_crypto
        if db_crypto.any_enabled():
            _enc_actions = db_crypto.reconcile()
            _changed = [a for a in _enc_actions if a.get("action") in ("encrypted", "decrypted")]
            if _changed:
                logger.info("Full-DB encryption reconcile: %s", _changed)
            _failed = [a for a in _enc_actions if not a.get("ok")]
            if _failed:
                logger.warning("Full-DB encryption reconcile had failures: %s", _failed)
    except Exception as _enc_err:
        logger.warning("Full-DB encryption reconcile failed: %s", _enc_err)

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

    # Warm the memory embedding client in the background so the FIRST chat turn's
    # memory_search doesn't pay the cold-start penalty (~7.5s cold vs ~0.3s warm).
    # Fire-and-forget — never blocks boot, and is a no-op if no provider key is set.
    try:
        import asyncio as _asyncio
        from app.agent.embed import warm_embed_client
        _asyncio.create_task(warm_embed_client())
    except Exception as _embed_warm_err:
        logger.warning("Embed warmup scheduling failed: %s", _embed_warm_err)

    # Launch the terminal idle-session GC. Reaps PTYs whose WebSocket has
    # been detached longer than TERMINAL_IDLE_TIMEOUT_HOURS (default 24h).
    try:
        from app.api.terminal import start_idle_gc
        start_idle_gc()
    except Exception as _gc_err:
        logger.warning("Failed to start terminal idle GC: %s", _gc_err)

    # First-boot: provision from a bootstrap.json dropped next to the clone (an
    # encrypted setup bundle → DB + vault + LLM + admin). Runs BEFORE the security
    # and LLM-from-env seeds below so the bundle's choices win; a strict freshness
    # gate (no LLM configured yet) makes it a no-op on any real install, and the
    # file is renamed once applied so it never re-fires. See
    # app/admin/bootstrap_bundle.py.
    try:
        from app.admin.bootstrap_bundle import apply_boot_file
        _boot_res = await apply_boot_file()
        if _boot_res:
            logger.info("Bootstrap file provisioned fresh install: %s", _boot_res.get("results"))
    except Exception as _boot_err:
        logger.warning("Bootstrap-file provisioning at startup failed: %s", _boot_err)

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

    # First-boot: enable all admin Agent Tools so the app ships ready to "do
    # everything". Runs once (marker-guarded) so later admin disables persist.
    try:
        from app.admin.integrations import seed_default_abilities
        _ab_seed = await seed_default_abilities()
        if _ab_seed.get("seeded"):
            logger.info("Default admin abilities seeded ON: %s", _ab_seed.get("enabled"))
    except Exception as _ab_err:
        logger.warning("Default ability seed at startup failed: %s", _ab_err)

    # First-boot: on a FRESH, keyring-capable install, turn on the OS-keyring
    # secrets vault + per-tenant field encryption by default. Strictly guarded
    # (no-op if anything's already configured, no keyring, or secrets already
    # exist) so it never disturbs an existing install. See
    # app/encryption/defaults.py for the full guard list.
    try:
        from app.encryption.defaults import seed_security_defaults
        _sec_seed = await seed_security_defaults()
        if _sec_seed.get("seeded"):
            logger.info("Security defaults seeded ON (os_keyring + field encryption)")
        else:
            logger.info("Security defaults not seeded: %s", _sec_seed.get("reason"))
    except Exception as _sec_err:
        logger.warning("Security-defaults seed at startup failed: %s", _sec_err)

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

    # ── Hybrid local-first sync engine (Stage 2) ──
    # When the hybrid backend is active (SQLite hot store in front of the Postgres
    # authority), interaction writes land locally first and are queued in a local
    # OUTBOX. Start the background engine that drains that outbox to the remote as
    # a stripped skeleton within a few seconds. Stage 4 also turns the remote→local
    # PULLER on (pull_enabled=True) so shared content/config edits made on another
    # device land in this machine's local replica continuously. This is safe: the
    # Stage-4 security audit confirmed no authorization/identity/billing decision
    # resolves from the local cache — the agent access-mode selector is read from
    # the authority (chat._enforce_agent_access_policy), billing/roles/admin/tier
    # all resolve remote, and the puller only warms Synced-tier rows (it never
    # pulls money/identity tables). No-op unless the active db is a HybridBackend.
    # The engine binds to THIS machine's local outbox, so it runs per-process (not
    # via the cluster leader, which would wrongly elect a single device for everyone).
    try:
        from app.db import get_db as _get_db_sync
        from app.db.hybrid import hybrid_enabled as _hybrid_on, HybridBackend
        _sync_db = _get_db_sync()
        # Reach the HybridBackend — it's either the active backend directly, or
        # wrapped by the encryption decorator (composition is Enc(Hybrid(...))).
        _inner = _sync_db
        if not isinstance(_inner, HybridBackend):
            _inner = getattr(_sync_db, "_inner", None)
        if _hybrid_on() and isinstance(_inner, HybridBackend):
            from app.db.sync import SyncEngine
            _engine = SyncEngine(_inner, pull_enabled=True)
            _engine.start()
            app.state.hybrid_sync_engine = _engine
            logger.info("Hybrid sync engine started (push + pull, syncing local replica <-> remote)")
    except Exception as _sync_err:
        logger.warning("Hybrid sync engine failed to start: %s", _sync_err)

    # ── Singleton background services, gated by a single-worker leader ──
    # The scheduler, event runtime, ability pollers, boot orphan-resume, watchdog
    # and Remote Access must run in EXACTLY ONE worker. Under gunicorn --workers N
    # (or multiple instances) running them in every worker double-fires automations
    # and re-ignites the same orphaned runs N times. The leader elects one worker
    # via a TTL'd DB lock and runs them only there; if it dies another worker takes
    # over. Single-process dev/prod wins leadership instantly, so behaviour is
    # unchanged. Services run in registration order when leadership is acquired.
    #
    # ⚠ DROP-IN POLICY — do NOT add a per-ability startup line here. If a drop-in
    # ability needs a long-lived service, define start_background()/stop_background()
    # in its plugin file (plugins/abilities/<id>.py); the "ability-background"
    # leader service below discovers and runs it. See CLAUDE.md "Core vs. plugins".
    try:
        from app.coordination.leader import get_leader
        _leader = get_leader()

        async def _start_scheduler():
            from app.scheduler import start_scheduler
            await start_scheduler()

        async def _stop_scheduler():
            from app.scheduler import stop_scheduler
            await stop_scheduler()

        _leader.register("scheduler", _start_scheduler, _stop_scheduler)

        async def _start_events():
            from app.events import start_event_runtime
            await start_event_runtime()

        async def _stop_events():
            from app.events import stop_event_runtime
            await stop_event_runtime()

        _leader.register("event-runtime", _start_events, _stop_events)

        async def _start_ability_bg():
            from app import abilities as _abilities_mgr
            for _svc in _abilities_mgr.background_service_hooks():
                try:
                    await _svc["start"]()
                except Exception as _svc_err:
                    logger.warning("Ability '%s' background start failed: %s", _svc["id"], _svc_err)

        async def _stop_ability_bg():
            from app import abilities as _abilities_mgr
            for _svc in _abilities_mgr.background_service_hooks():
                if callable(_svc.get("stop")):
                    try:
                        await _svc["stop"]()
                    except Exception:
                        pass

        _leader.register("ability-background", _start_ability_bg, _stop_ability_bg)

        # Self-healing (step 2 of 2): re-ignite resumable orphans. One-shot, run
        # AFTER scheduler/events are up (a resumed turn may use their machinery).
        # Backend-driven — recovers background/sub-agent/delegated runs too. Only
        # involuntary stops with a resumable cause + retry budget are relaunched.
        async def _resume_orphans():
            from app.agent.runner import resume_orphans_at_boot
            _n_resumed = await resume_orphans_at_boot()
            if _n_resumed:
                logger.info("Self-healing: re-ignited %d orphaned run(s) at boot", _n_resumed)

        _leader.register("boot-orphan-resume", _resume_orphans, None, oneshot=True)

        async def _start_watchdog():
            from app.agent.watchdog import start_watchdog
            await start_watchdog()

        async def _stop_watchdog():
            from app.agent.watchdog import stop_watchdog
            await stop_watchdog()

        _leader.register("watchdog", _start_watchdog, _stop_watchdog)

        async def _start_remote():
            from app.remote_access import start_remote_access
            await start_remote_access()

        async def _stop_remote():
            from app.remote_access import stop_remote_access
            await stop_remote_access()

        _leader.register("remote-access", _start_remote, _stop_remote)

        await _leader.start()
    except Exception as _leader_err:
        logger.warning("Failed to start background leader: %s", _leader_err)

    # ── Device worker — runs on EVERY instance (NOT leader-gated) ──
    # Multi-device: when several instances share one database, each runs its own
    # worker that heartbeats presence and claims only the dispatch jobs addressed
    # to its own device id. Deliberately outside the leader block above (those are
    # GLOBAL singletons elected to one worker; this one is per-device by design).
    # See app/devices/. Harmless on a single, unshared instance — there are simply
    # no other devices, so it just heartbeats itself.
    try:
        from app.devices import start_device_worker
        await start_device_worker()
    except Exception as _dev_err:
        logger.warning("Failed to start device worker: %s", _dev_err)

    # ── Seed LLM config from env vars into auth_elements (cloud-first deploy) ──
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
    if api_key:
        try:
            from app.db import get_db
            db = get_db()
            existing = await db.auth_element_get("admin", "llm", "default")
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
                    user_id="admin",
                    service="llm",
                    config=config,
                    secret_ref=api_key,
                )
                logger.info("LLM config seeded into auth_elements from env vars")
        except Exception as e:
            logger.warning("Failed to seed LLM config: %s", e)

    # ── Scrub any plaintext LLM key (old provider.json / config-blob copies) into
    #    the encrypted vault. Idempotent; no-op once nothing plaintext remains. ──
    try:
        from app.admin.settings import migrate_llm_secrets
        await migrate_llm_secrets()
    except Exception as _mig_err:
        logger.warning("LLM secret migration skipped: %s", _mig_err)


if __name__ == "__main__":
    import uvicorn

    _port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=_port, reload=True)
