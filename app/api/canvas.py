"""
REST API for the Canvas workspace.

Endpoints:
  GET    /api/v1/canvases                            — list canvases for a user
  POST   /api/v1/canvases                            — create a new canvas
  DELETE /api/v1/canvases/{slug}                     — delete a canvas
  GET    /api/v1/canvases/{user_id}/{slug}/html      — serve a canvas's HTML
  POST   /api/v1/canvases/{user_id}/{slug}/logs      — record a canvas's own console output
  GET    /api/v1/canvases/{user_id}/{slug}/logs      — read back a canvas's console output

The HTML is fetched as text by the Canvas tab (ui/main-panel/canvas/js/canvas.js)
and grafted into the app inside a shadow root (first-class rendering) — it is no
longer loaded into a sandboxed iframe.
"""
import ipaddress
import json
import socket
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from fastapi import APIRouter, HTTPException, Query, Header, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional

from app.visualizer.canvas import (
    list_canvases,
    create_canvas,
    delete_canvas,
    rename_canvas,
    get_canvas_html,
    get_canvas_data,
    append_canvas_logs,
    read_canvas_logs,
)
from app.auth.identity import caller_may_access_page, assert_caller_is

router = APIRouter(prefix="/api/v1/canvases", tags=["canvases"])


async def _require_canvas_access(request: Request, user_id: str) -> str:
    """Gate EVERY canvas endpoint server-side (a canvas runs agent-authored code
    with the viewer's own app trust, so the tab being hidden in the UI is not
    enough — a direct API call must be refused too). Two checks:

      1. The Canvas page's VISIBILITY (set by the admin) must permit this caller.
         Registration is required by default, so an anonymous visitor is refused
         unless the admin opened Canvas to "all"; "off" is admins-only. Admins and
         'open' single-user/local mode always pass.
      2. A caller may only read/write their OWN canvases — admins may act for any
         user (assert_caller_is enforces caller == user_id, or admin).

    Returns the verified caller user_id. Raises 401 (no identity) / 403 (excluded
    or not the owner)."""
    if not await caller_may_access_page(request, "main", "canvas"):
        raise HTTPException(status_code=403, detail="Canvas isn't enabled for your account.")
    return await assert_caller_is(request, user_id)


# Slim themed scrollbar injected into canvas pages. Harmless under first-class
# rendering (it becomes one of the <style> nodes lifted into the shadow root, and
# the client also injects its own scrollbar style), and it keeps a canvas opened
# standalone in a browser tab from showing the OS default scrollbar. Uses
# prefers-color-scheme so it adapts to the theme (orange in light docs, blue in dark).
_SCROLLBAR_STYLE = """<style id="webagent-scrollbar-inject">
*{scrollbar-width:thin;scrollbar-color:rgba(125,207,255,0.32) transparent;}
*::-webkit-scrollbar{width:6px;height:6px;}
*::-webkit-scrollbar-track{background:transparent;}
*::-webkit-scrollbar-thumb{background:rgba(125,207,255,0.38);border-radius:999px;}
*::-webkit-scrollbar-thumb:hover{background:rgba(125,207,255,0.62);}
*::-webkit-scrollbar-corner{background:transparent;}
@media (prefers-color-scheme: light){
*{scrollbar-color:rgba(255,140,66,0.40) transparent;}
*::-webkit-scrollbar-thumb{background:rgba(255,140,66,0.48);}
*::-webkit-scrollbar-thumb:hover{background:rgba(255,140,66,0.72);}
}
</style>"""


def _inject_scrollbar_style(html: str) -> str:
    """Inject a slim themed scrollbar stylesheet into the canvas page so a
    standalone view doesn't render the OS default scrollbar. Inserts right
    after <head> when present, otherwise prepends."""
    if not html:
        return html
    lower = html.lower()
    idx = lower.find("<head>")
    if idx != -1:
        insert_at = idx + len("<head>")
        return html[:insert_at] + _SCROLLBAR_STYLE + html[insert_at:]
    return _SCROLLBAR_STYLE + html


def _inject_canvas_data(html: str, data: Optional[dict]) -> str:
    """Bake a canvas's data bag into the served page as ``window.__CANVAS_DATA``.

    This is the trick that makes a separate data file feel instant: the page's
    content lives in data.json (so the agent edits data without touching the
    markup), but it's merged into the HTML right before sending — so the page
    arrives with its data already present (no second fetch, no loading state).
    The canvas reads it via the mount toolbox's ``api.getData()``. Inserted as
    the FIRST thing after <head> so it runs before the canvas's own scripts.
    A canvas with no data file gets ``{}`` (harmless; the page falls back)."""
    if not html:
        return html
    # Escape "<" so a value containing "</script>" can't break out of the tag.
    payload = json.dumps(data if isinstance(data, dict) else {}).replace("<", "\\u003c")
    block = '<script id="webagent-canvas-data">window.__CANVAS_DATA=' + payload + ';</script>'
    lower = html.lower()
    idx = lower.find("<head>")
    if idx != -1:
        insert_at = idx + len("<head>")
        return html[:insert_at] + block + html[insert_at:]
    return block + html


class CreateCanvasRequest(BaseModel):
    user_id: str
    slug: str
    title: str
    agent_context: Optional[str] = ""
    initial_html: Optional[str] = ""


class RenameCanvasRequest(BaseModel):
    title: str


@router.get("")
async def api_list_canvases(request: Request, user_id: str = Query(..., description="User ID")):
    """Return all canvases for the given user. Seeds the home canvas if missing."""
    await _require_canvas_access(request, user_id)
    pages = await list_canvases(user_id)
    return {"status": "ok", "canvases": pages, "count": len(pages)}


@router.post("")
async def api_create_canvas(request: Request, body: CreateCanvasRequest):
    """Create a new canvas for the user."""
    await _require_canvas_access(request, body.user_id)
    try:
        entry = await create_canvas(
            user_id=body.user_id,
            slug=body.slug,
            title=body.title,
            agent_context=body.agent_context or "",
            initial_html=body.initial_html or "",
        )
        return {"status": "ok", "canvas": entry}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.delete("/{slug}")
async def api_delete_canvas(request: Request, slug: str, user_id: str = Query(..., description="User ID")):
    """Delete a canvas. The home canvas cannot be deleted."""
    await _require_canvas_access(request, user_id)
    if slug == "home":
        raise HTTPException(status_code=403, detail="The home canvas cannot be deleted.")
    ok = await delete_canvas(user_id=user_id, slug=slug)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Canvas '{slug}' not found.")
    return {"status": "ok", "message": f"Canvas '{slug}' deleted."}


@router.patch("/{slug}")
async def api_rename_canvas(
    request: Request,
    slug: str,
    body: RenameCanvasRequest,
    user_id: str = Query(..., description="User ID"),
):
    """Rename a page's display title. The slug (URL) is preserved."""
    await _require_canvas_access(request, user_id)
    new_title = (body.title or "").strip()
    if not new_title:
        raise HTTPException(status_code=400, detail="Title cannot be empty.")
    ok = await rename_canvas(user_id=user_id, slug=slug, new_title=new_title)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Canvas '{slug}' not found.")
    return {"status": "ok", "title": new_title}


@router.get("/{user_id}/{slug}/html", response_class=HTMLResponse)
async def api_get_canvas_html(request: Request, user_id: str, slug: str):
    """Serve a canvas's HTML body. Works the same across all CanvasStore
    backends — filesystem, database, or hybrid. Gated: the caller must be
    permitted Canvas by its visibility setting AND own this canvas (or be admin),
    so one user can't fetch another's canvas by guessing user_id + slug."""
    await _require_canvas_access(request, user_id)
    # Seed the home canvas on first request through this endpoint, mirroring
    # the behavior of /api/v1/canvases so a direct deep-link to /home/html
    # doesn't 404 a new user.
    if slug == "home":
        await list_canvases(user_id)
    html = await get_canvas_html(user_id, slug)
    if html is None:
        raise HTTPException(status_code=404, detail=f"Canvas '{slug}' not found.")
    data = await get_canvas_data(user_id, slug)
    return HTMLResponse(content=_inject_canvas_data(_inject_scrollbar_style(html), data))


@router.post("/{user_id}/{slug}/logs")
async def api_append_canvas_logs(request: Request, user_id: str, slug: str, body: dict):
    """Record the console output a canvas produced in the browser.

    The Canvas tab captures each page's own console.log/warn/error (and uncaught
    script errors) as it runs and POSTs them here in small batches; we append them
    to that canvas's page-scoped log file (beside index.html) — NOT the global
    logs.db. This is what lets the design agent read the errors of the one canvas
    it built via get_canvas_logs, with no codebase-admin access. Bounded: at most
    a couple hundred entries per call, then the file itself is capped."""
    await _require_canvas_access(request, user_id)
    entries = body.get("entries") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        entries = []
    kept = append_canvas_logs(user_id, slug, entries[:200])
    return {"status": "ok", "received": len(entries), "kept": kept}


@router.get("/{user_id}/{slug}/logs")
async def api_read_canvas_logs(
    request: Request,
    user_id: str,
    slug: str,
    limit: int = Query(100, ge=1, le=500),
    level: Optional[str] = Query(None, description="Filter to one level (error/warn/log/info/debug)"),
):
    """Read back a canvas's recorded console output (newest last). Mirrors what the
    get_canvas_logs agent tool returns, for the UI / debugging."""
    await _require_canvas_access(request, user_id)
    logs = read_canvas_logs(user_id, slug, limit=limit, level=level)
    return {"status": "ok", "slug": slug, "count": len(logs), "logs": logs}


# ─────────────────────────────────────────────────────────────────────────────
# Vault Capture — agent-defined credentials for canvases (see
# app/abilities/vault_store.py + the Visualizer ability's request_credential /
# list_vault_keys tools). Nested under the canvases router so no new core router
# include is needed. THE SECURITY MODEL:
#   • The agent only ever holds a key id (it reserves the key server-side via the
#     tool). It never sees the secret.
#   • The user types the secret into the secure chat card, which POSTs it STRAIGHT
#     here (save) — it never round-trips through the agent or the transcript.
#   • A canvas uses the key by id through /vault/proxy: the server reads the
#     secret, attaches it per the key's binding, and makes the outbound call. The
#     plaintext never reaches the page. The key is locked to its bound base_url.
# All routes resolve the caller from the Bearer token (same as the Abilities →
# Credentials endpoints) and operate ONLY on that user's own vault.
# ─────────────────────────────────────────────────────────────────────────────

class VaultSaveRequest(BaseModel):
    values: dict


class VaultProxyRequest(BaseModel):
    key_id: str
    url: Optional[str] = None          # absolute target; must sit under the bound base_url
    path: Optional[str] = None         # or a path appended to the bound base_url
    method: Optional[str] = "GET"
    headers: Optional[dict] = None
    query: Optional[dict] = None
    json_body: Optional[object] = None  # parsed JSON body to send
    body: Optional[str] = None          # or a raw string body


def _vault_caller(authorization: Optional[str], token: Optional[str]) -> str:
    """Resolve the signed-in user; 401 for anonymous (same gate as cred saves)."""
    from app.admin.integrations import resolve_user_id, ANONYMOUS_KEY
    uid = resolve_user_id(authorization or "", token or "")
    if not uid or uid == ANONYMOUS_KEY:
        raise HTTPException(status_code=401, detail="Sign in to use the vault.")
    return uid


@router.get("/vault/keys")
async def api_vault_list_keys(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """List the caller's vault keys as agent-safe metadata (never any secret)."""
    from app.abilities import vault_store
    uid = _vault_caller(authorization, token)
    keys = await vault_store.list_keys(uid)
    return {"status": "ok", "keys": keys, "count": len(keys)}


@router.get("/vault/keys/{key_id}")
async def api_vault_get_key(
    key_id: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Public view of one vault key — metadata + which secrets are set. No values."""
    from app.abilities import vault_store
    uid = _vault_caller(authorization, token)
    view = await vault_store.public_view(uid, key_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"Vault key '{key_id}' not found.")
    return {"status": "ok", **view}


@router.post("/vault/keys/{key_id}")
async def api_vault_save_key(
    key_id: str,
    body: VaultSaveRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Save the secret value(s) the user typed into the secure card. Browser→server
    only — this is the ONE write path for the plaintext, and it never goes near the
    agent. A blank secret keeps the stored one."""
    from app.abilities import vault_store
    uid = _vault_caller(authorization, token)
    ok = await vault_store.save_values(uid, key_id, body.values or {})
    if not ok:
        raise HTTPException(status_code=404, detail=f"Vault key '{key_id}' not found.")
    view = await vault_store.public_view(uid, key_id)
    return {"status": "ok", "filled": bool(view and view.get("filled"))}


@router.delete("/vault/keys/{key_id}")
async def api_vault_delete_key(
    key_id: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Remove a vault key (metadata + secret)."""
    from app.abilities import vault_store
    uid = _vault_caller(authorization, token)
    ok = await vault_store.delete_key(uid, key_id)
    return {"status": "ok" if ok else "error"}


def _host_is_blocked(host: str) -> bool:
    """Block loopback / private / link-local destinations so a bound base_url can't
    be pointed at the server's own internal network (basic SSRF guard)."""
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        # Can't resolve — let the outbound call fail naturally rather than here,
        # but DO block obvious local names.
        return host.lower() in {"localhost", "localhost.localdomain"}
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


@router.post("/vault/proxy")
async def api_vault_proxy(
    body: VaultProxyRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Make an outbound request on a canvas's behalf, attaching a vault secret by
    id — server-side, so the plaintext never reaches the page or the agent.

    The key's stored *binding* is authoritative: the target must sit under the
    bound base_url, and the secret attaches exactly the way the binding says
    (bearer / basic / a named header / a query param). A key with no base_url
    can't be proxied at all."""
    from app.abilities import vault_store
    uid = _vault_caller(authorization, token)

    secret_info = await vault_store.read_secret(uid, body.key_id)
    if not secret_info:
        raise HTTPException(status_code=404, detail=f"Vault key '{body.key_id}' not found.")
    binding = secret_info.get("binding") or {}
    base_url = (binding.get("base_url") or "").strip()
    if not base_url:
        raise HTTPException(status_code=400, detail="This key has no service URL bound, so it can't be used to make a call.")
    secrets = secret_info.get("secrets") or {}
    secret_field = binding.get("secret_field") or ""
    secret_val = str(secrets.get(secret_field, "") or "")
    if not secret_val:
        raise HTTPException(status_code=409, detail="This key hasn't been filled in by the user yet.")

    # Resolve the target URL: absolute `url` must live under the bound base_url;
    # otherwise append `path` to base_url. Either way the host is pinned by the
    # binding, so the secret can only ever travel to its intended service.
    base = urlsplit(base_url)
    if body.url:
        target = urlsplit(body.url)
        if target.scheme != base.scheme or target.netloc != base.netloc \
                or not (target.path or "/").startswith(base.path or "/"):
            raise HTTPException(status_code=403, detail="That URL is outside the service this key is bound to.")
        target_url = body.url
    else:
        path = body.path or ""
        joined_path = (base.path.rstrip("/") + "/" + path.lstrip("/")) if path else base.path
        target_url = urlunsplit((base.scheme, base.netloc, joined_path, base.query, ""))

    if base.scheme not in ("http", "https") or _host_is_blocked(base.hostname or ""):
        raise HTTPException(status_code=403, detail="This key's service URL is not an allowed destination.")

    # Build headers / query with the secret attached per the binding.
    headers = {str(k): str(v) for k, v in (body.headers or {}).items()}
    parts = urlsplit(target_url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    for k, v in (body.query or {}).items():
        query_pairs.append((str(k), str(v)))

    attach = binding.get("attach") or "bearer"
    if attach == "bearer":
        headers["Authorization"] = "Bearer " + secret_val
    elif attach == "basic":
        headers["Authorization"] = "Basic " + secret_val
    elif attach.startswith("header:"):
        headers[attach.split(":", 1)[1]] = secret_val
    elif attach.startswith("query:"):
        query_pairs.append((attach.split(":", 1)[1], secret_val))

    final_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_pairs), parts.fragment))
    method = (body.method or "GET").upper()

    import httpx
    req_kwargs: dict = {"headers": headers}
    if body.json_body is not None:
        req_kwargs["json"] = body.json_body
    elif body.body is not None:
        req_kwargs["content"] = body.body

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            resp = await client.request(method, final_url, **req_kwargs)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"The call to the service failed: {e}")

    # Hand back status + body. Try JSON; fall back to text. We deliberately do NOT
    # forward upstream auth-bearing response headers.
    ctype = resp.headers.get("content-type", "")
    out: dict = {"status": "ok", "http_status": resp.status_code, "content_type": ctype}
    if "application/json" in ctype:
        try:
            out["json"] = resp.json()
        except Exception:
            out["text"] = resp.text
    else:
        out["text"] = resp.text
    return out
