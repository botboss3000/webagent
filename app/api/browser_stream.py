"""
Live browser proxy + WebSocket for the in-app "Web" page.

Two paths serve the shared browser:

1. **Iframe proxy** — ``GET /api/v1/browser/proxy`` fetches the current page
   through Playwright, injects a bridge script that intercepts link clicks, and
   returns the HTML to an ``<iframe>`` inside the Web tab.  Native cursor,
   text selection, scrolling.  This is the primary path.

2. **Command WebSocket** — ``/api/v1/browser/ws`` carries navigation commands
   from the user (navigate / back / forward / reload) to the server and pushes
   ``nav`` events back when the agent's ``browser_action`` changes the URL,
   detected by a polling loop.

A browser session (``bs_id``) is a first-class, persistent tab that lives BESIDE
chat (rows in the ``browser_sessions`` table).  The human's Web tab and the agent
both address a tab by this id, so whatever the agent does shows up live in the
panel and vice-versa: an "AI-augmented" browser the two drive together.  This
module also exposes the REST CRUD for those sessions (list / create / patch /
delete) so the UI can manage tabs and toggle the per-tab ``shared`` flag.
"""

import asyncio
import logging
from typing import Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.auth.jwt import decode_token
from app.tools.browser import get_or_create_page, persist_state, note_navigation

logger = logging.getLogger(__name__)

router = APIRouter()

# Fixed page viewport — matches the context viewport in app/tools/browser.py.
VIEW_W = 1280
VIEW_H = 720

# ── Bridge script injected into every proxied page ──────────────────────────
# Runs inside the iframe.  Intercepts link clicks and reports them to the
# parent frame so the shared browser stays in sync with the agent's Playwright
# page.  Also monitors pushState / replaceState for SPA navigation.
BRIDGE_SCRIPT = r"""
(function(){
  function tell(msg){ window.parent.postMessage(msg, '*'); }

  // Report the page we landed on.
  tell({ type: 'webagent-ready', url: location.href, title: document.title });

  // Intercept link clicks so they go through the shared-browser proxy instead
  // of navigating the iframe directly.  javascript: and hash-only links are
  // left alone; target=_blank links open in a new tab.
  document.addEventListener('click', function(e){
    var a = e.target.closest('a');
    if (!a) return;
    var href = a.href;
    if (!href || href.startsWith('javascript:') || href === '#' || a.target === '_blank') return;
    e.preventDefault();
    e.stopPropagation();
    tell({ type: 'webagent-navigate', url: href });
  }, true);

  // Watch for SPA pushState / replaceState / popstate.
  var _push = history.pushState, _replace = history.replaceState;
  history.pushState = function(){
    _push.apply(this, arguments);
    tell({ type: 'webagent-urlchange', url: location.href, title: document.title });
  };
  history.replaceState = function(){
    _replace.apply(this, arguments);
    tell({ type: 'webagent-urlchange', url: location.href, title: document.title });
  };
  window.addEventListener('popstate', function(){
    tell({ type: 'webagent-urlchange', url: location.href, title: document.title });
  });
})();
"""


def _verify_token(token: str) -> Optional[str]:
    """Return the caller's user_id from a valid JWT, else None.

    In 'open' access mode a tokenless caller resolves to the bootstrap admin
    (single-user / local convenience) so the browser stream works over a
    tunnel, where the frontend can't mint a JWT. See open_mode_admin_id."""
    if token:
        try:
            payload = decode_token(token)
        except Exception:
            payload = None
        if payload:
            uid = payload.get("user_id") or payload.get("sub")
            if uid:
                return uid
    from app.auth.identity import open_mode_admin_id
    return open_mode_admin_id()


def _resolve_user_bs_id(user_id: str, bs_id: Optional[str]) -> Optional[str]:
    """Resolve the browser-session id the Web tab should stream.

    If ``bs_id`` is given it must be owned by ``user_id`` (else None → not
    authorized / not found). If omitted, fall back to the user's first tab,
    creating a private one if they have none.
    """
    from app.db import browser_sessions_store as _store
    if bs_id:
        row = _store.get(bs_id)
        if not row or row.get("user_id") != user_id:
            return None
        return bs_id
    rows = _store.list_for_user(user_id)
    if rows:
        return rows[0]["id"]
    return _store.create(user_id, title="My browser")["id"]


async def _browser_page_allowed(user_id: str) -> bool:
    """Whether this caller may use the Browser page, per its 3-state visibility.

    Mirrors the Canvas gate (registered users / admins / open mode pass). Because
    every main page defaults to 'auth' (registration required), an anonymous
    guest is blocked from spinning up or driving a server-side browser — closing
    the SSRF/abuse surface — unless an admin opens the Browser page to 'all'.
    """
    from app.auth.identity import user_may_access_page
    return await user_may_access_page(user_id, "main", "browser")


async def _require_browser_page_access(user_id: str) -> None:
    """HTTP-route form of _browser_page_allowed: raise 403 when excluded."""
    if not await _browser_page_allowed(user_id):
        raise HTTPException(status_code=403, detail="The Browser page isn't enabled for your account.")


def _normalize_url(raw: str) -> str:
    """Turn whatever the user typed into a navigable URL.

    Bare domain -> https://; free text -> a DuckDuckGo search.
    """
    url = (raw or "").strip()
    if not url:
        return "about:blank"
    if "://" not in url:
        if " " not in url and "." in url:
            url = "https://" + url
        else:
            url = "https://duckduckgo.com/?q=" + quote_plus(url)
    return url


# ── Iframe proxy ────────────────────────────────────────────────────────────
# When the Web tab opens a page, the iframe points here.  The endpoint navigates
# Playwright to the target, grabs the rendered HTML, injects a <base> tag so
# relative resources load from the real origin, and injects the bridge script
# that forwards link clicks back to the parent frame.  The iframe gets a real
# interactive page — native cursor, text selection, scrolling.


@router.get("/api/v1/browser/proxy")
async def browser_proxy(request: Request):
    """Proxy a page through the shared Playwright browser.

    Query params:
        bs_id  — browser session id (required)
        url    — the URL to load (required)
        token  — JWT for auth (required, in query param since iframes can't set headers)
    """
    token = request.query_params.get("token", "")
    user_id = _verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await _require_browser_page_access(user_id)

    bs_id = request.query_params.get("bs_id", "").strip()
    raw_url = request.query_params.get("url", "").strip()
    if not bs_id or not raw_url:
        raise HTTPException(status_code=400, detail="bs_id and url are required")
    # Ownership: the bs_id must belong to this caller. The WS handlers enforce
    # this via _resolve_user_bs_id; the proxy previously did NOT, so any
    # token-holder could pass another user's bs_id and drive/read their live
    # logged-in browser. Resolving with the explicit bs_id returns None when it
    # isn't the caller's.
    if _resolve_user_bs_id(user_id, bs_id) is None:
        raise HTTPException(status_code=404, detail="Browser session not found")

    url = _normalize_url(raw_url)

    try:
        page = await get_or_create_page(bs_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not open browser: {e}")

    # Navigate Playwright to the target so the agent stays in sync.
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Let JavaScript settle — SPAs need a moment to render their content.
        await page.wait_for_timeout(2000)
    except Exception as e:
        logger.warning("browser_proxy: navigation failed for %s: %s", url, e)
        # Return a graceful error page rather than a raw 500.
        return HTMLResponse(
            content=f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Navigation error</title>
<style>body{{font-family:system-ui;padding:40px;color:#888}}</style></head>
<body><h2>Could not load page</h2><p>{e}</p><p><a href="javascript:history.back()">Go back</a></p></body></html>""",
            status_code=502,
        )

    # Remember where this tab is.
    try:
        from app.db import browser_sessions_store as _store
        title = await page.title()
        note_navigation(bs_id, page.url)  # mark this site first-party
        _store.update(bs_id, url=page.url, title=title)
    except Exception:
        pass

    try:
        html = await page.content()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not read page content: {e}")

    # Inject <base href> so relative resources (images, CSS, JS) load from the
    # real origin.  If the page already has a <base> tag we replace it; otherwise
    # we insert one at the top of <head>.
    base_tag = f'<base href="{page.url}">'
    import re as _re
    # language=HTML
    if _re.search(r'<base\b', html, _re.IGNORECASE):
        html = _re.sub(r'<base\b[^>]*>', base_tag, html, count=1, flags=_re.IGNORECASE)
    elif '<head>' in html:
        html = html.replace('<head>', f'<head>\n{base_tag}', 1)
    elif '<html>' in html:
        html = html.replace('<html>', f'<html><head>{base_tag}</head>', 1)
    else:
        html = f'<!DOCTYPE html><html><head>{base_tag}</head><body>{html}</body></html>'

    # Inject the bridge script.
    bridge_tag = f'<script>{BRIDGE_SCRIPT}</script>'
    if '</body>' in html:
        html = html.replace('</body>', f'{bridge_tag}\n</body>', 1)
    elif '</html>' in html:
        html = html.replace('</html>', f'{bridge_tag}\n</html>', 1)
    else:
        html += bridge_tag

    return HTMLResponse(content=html)


# ── Command WebSocket ───────────────────────────────────────────────────────
# The Web tab opens a WebSocket so navigation commands from the user reach the
# shared Playwright page and the client learns when the agent navigated (via a
# polling loop — the iframe has no live stream, so we check the URL every second).


@router.websocket("/api/v1/browser/ws")
async def browser_ws(websocket: WebSocket):
    # Authenticate BEFORE accepting so a hostile client never opens a stream.
    token = websocket.query_params.get("token", "")
    user_id = _verify_token(token)
    if not user_id:
        await websocket.close(code=4401)
        return
    # Page-visibility gate: anon guests are excluded by default (the Browser page
    # defaults to 'auth'), so they can't open a server-side browser stream.
    if not await _browser_page_allowed(user_id):
        await websocket.close(code=4403)
        return

    await websocket.accept()

    raw_bs = websocket.query_params.get("bs_id") or websocket.query_params.get("browser_session_id")
    bs_id = _resolve_user_bs_id(user_id, raw_bs)
    if not bs_id:
        try:
            await websocket.send_json({"type": "error", "message": "Browser session not found"})
        finally:
            await websocket.close(code=4404)
        return

    # Shared page (created on demand).  Same bs_id the agent resolves to, so the
    # human and the agent drive ONE page.
    try:
        page = await get_or_create_page(bs_id)
    except Exception as e:
        logger.warning("browser_ws: could not open page for %s: %s", bs_id, e)
        try:
            await websocket.send_json({"type": "error", "message": f"Could not open browser: {e}"})
        finally:
            await websocket.close(code=1011)
        return

    input_lock = asyncio.Lock()
    stopped = asyncio.Event()

    async def _emit_nav():
        try:
            title = await page.title()
            note_navigation(bs_id, page.url)  # mark this site first-party
            await websocket.send_json({"type": "nav", "url": page.url, "title": title})
            try:
                from app.db import browser_sessions_store as _store
                _store.update(bs_id, url=page.url, title=title)
            except Exception:
                pass
            await persist_state(bs_id)
        except Exception:
            pass

    async def _handle(msg: dict):
        action = msg.get("action")
        if not action:
            return
        async with input_lock:
            try:
                if action == "navigate":
                    await page.goto(_normalize_url(msg.get("url", "")), wait_until="domcontentloaded", timeout=30000)
                    await _emit_nav()
                elif action == "back":
                    await page.go_back(timeout=15000)
                    await _emit_nav()
                elif action == "forward":
                    await page.go_forward(timeout=15000)
                    await _emit_nav()
                elif action == "reload":
                    await page.reload(timeout=30000)
                    await _emit_nav()
            except Exception as e:
                try:
                    await websocket.send_json({"type": "action_error", "action": action, "message": str(e)})
                except Exception:
                    pass

    # Poll the page URL so the client knows when the agent navigated.
    async def _poll_url():
        last_url = page.url
        while not stopped.is_set():
            try:
                await asyncio.sleep(1.0)
                if stopped.is_set():
                    break
                current = page.url
                if current != last_url:
                    last_url = current
                    await _emit_nav()
            except Exception:
                pass

    poll_task = asyncio.create_task(_poll_url())
    try:
        try:
            await websocket.send_json({
                "type": "ready",
                "viewport": {"width": VIEW_W, "height": VIEW_H},
                "url": page.url,
                "title": await page.title(),
            })
        except Exception:
            pass

        while True:
            msg = await websocket.receive_json()
            await _handle(msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.info("browser_ws closed: %s", e)
    finally:
        stopped.set()
        poll_task.cancel()
        # NB: never close the page/browser here. The agent may still be using
        # it, and for an attached app-window it is the user's own window.


# ── Live pixel mirror (CDP screencast) ────────────────────────────────────────
# A second WebSocket that streams ACTUAL frames of the browser (headless OR the
# real on-device window) as JPEGs, and forwards the user's clicks / typing /
# scrolling back to that page. Unlike the HTML proxy this is a faithful picture of
# a real Chromium, so sites that refuse to be framed still show, and it mirrors
# the real device window when the session is on the "local" backend.


@router.websocket("/api/v1/browser/screencast")
async def browser_screencast(websocket: WebSocket):
    token = websocket.query_params.get("token", "")
    user_id = _verify_token(token)
    if not user_id:
        await websocket.close(code=4401)
        return
    # Page-visibility gate: anon guests are excluded by default (the Browser page
    # defaults to 'auth'), so they can't open a server-side browser stream.
    if not await _browser_page_allowed(user_id):
        await websocket.close(code=4403)
        return

    await websocket.accept()

    raw_bs = websocket.query_params.get("bs_id") or websocket.query_params.get("browser_session_id")
    bs_id = _resolve_user_bs_id(user_id, raw_bs)
    if not bs_id:
        try:
            await websocket.send_json({"type": "error", "message": "Browser session not found"})
        finally:
            await websocket.close(code=4404)
        return

    try:
        page = await get_or_create_page(bs_id)
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": f"Could not open browser: {e}"})
        finally:
            await websocket.close(code=1011)
        return

    # Coordinate space the client maps pointer events into (CSS pixels of the
    # page viewport). For headless this is the fixed viewport; for an attached
    # window it's the real inner size.
    space = {"width": VIEW_W, "height": VIEW_H}
    try:
        space = await page.evaluate("() => ({width: window.innerWidth, height: window.innerHeight})") or space
    except Exception:
        pass

    cdp = None
    frame_q: asyncio.Queue = asyncio.Queue(maxsize=2)
    stopped = asyncio.Event()

    async def _ack(session_id):
        try:
            if cdp is not None:
                await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:
            pass

    def _on_frame(params):
        # Ack EVERY frame (else Chrome pauses), but only keep the latest for
        # sending so a slow client never backs up the stream.
        sid = params.get("sessionId")
        if sid is not None:
            asyncio.create_task(_ack(sid))
        try:
            frame_q.put_nowait(params)
        except asyncio.QueueFull:
            try:
                frame_q.get_nowait()
                frame_q.put_nowait(params)
            except Exception:
                pass

    async def _sender():
        while not stopped.is_set():
            try:
                params = await asyncio.wait_for(frame_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
            md = params.get("metadata", {}) or {}
            try:
                await websocket.send_json({
                    "type": "frame",
                    "data": params.get("data", ""),
                    "deviceWidth": md.get("deviceWidth"),
                    "deviceHeight": md.get("deviceHeight"),
                    "pageScaleFactor": md.get("pageScaleFactor", 1),
                    "offsetTop": md.get("offsetTop", 0),
                })
            except Exception:
                break

    async def _dispatch(msg: dict):
        kind = msg.get("kind")
        try:
            if kind == "mousemove":
                await page.mouse.move(float(msg.get("x", 0)), float(msg.get("y", 0)))
            elif kind == "mousedown":
                await page.mouse.move(float(msg.get("x", 0)), float(msg.get("y", 0)))
                await page.mouse.down(button=msg.get("button", "left"))
            elif kind == "mouseup":
                await page.mouse.move(float(msg.get("x", 0)), float(msg.get("y", 0)))
                await page.mouse.up(button=msg.get("button", "left"))
            elif kind == "click":
                await page.mouse.click(float(msg.get("x", 0)), float(msg.get("y", 0)),
                                       button=msg.get("button", "left"),
                                       click_count=int(msg.get("clickCount", 1)))
            elif kind == "wheel":
                await page.mouse.move(float(msg.get("x", 0)), float(msg.get("y", 0)))
                await page.mouse.wheel(float(msg.get("deltaX", 0)), float(msg.get("deltaY", 0)))
            elif kind == "text":
                t = msg.get("text", "")
                if t:
                    await page.keyboard.insert_text(t)
            elif kind == "key":
                k = msg.get("key", "")
                if k:
                    await page.keyboard.press(k)
        except Exception as e:
            logger.debug("screencast input (%s) failed: %s", kind, e)

    try:
        cdp = await page.context.new_cdp_session(page)
        try:
            await cdp.send("Page.enable")
        except Exception:
            pass
        cdp.on("Page.screencastFrame", _on_frame)
        await cdp.send("Page.startScreencast", {
            "format": "jpeg", "quality": 55,
            "maxWidth": VIEW_W, "maxHeight": VIEW_H, "everyNthFrame": 1,
        })
    except Exception as e:
        try:
            await websocket.send_json({"type": "error", "message": f"Could not start screencast: {e}"})
        finally:
            await websocket.close(code=1011)
        return

    send_task = asyncio.create_task(_sender())
    try:
        await websocket.send_json({"type": "ready", "space": space, "url": page.url})
        while True:
            msg = await websocket.receive_json()
            if msg.get("type") == "input":
                await _dispatch(msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.info("browser_screencast closed: %s", e)
    finally:
        stopped.set()
        send_task.cancel()
        try:
            if cdp is not None:
                await cdp.send("Page.stopScreencast")
                await cdp.detach()
        except Exception:
            pass


# ── REST: manage browser sessions (tabs) ──────────────────────────────────────
# These let the Web tab list the user's browser sessions, open a new one, toggle
# its `shared` flag / linked agent / title, and close it. Auth via the standard
# JWT (Authorization header or ?token=); a session is only ever visible to its
# owner. The engine (app/tools/browser.py) holds no per-row lifecycle of its own,
# so DELETE also tears down any live page for that id.


def _public(row: dict) -> dict:
    """Shape a stored row for the client — never leak the raw cookie jar."""
    return {
        "id": row.get("id"),
        "agent_id": row.get("agent_id"),
        "title": row.get("title"),
        "url": row.get("url"),
        "shared": bool(row.get("shared")),
        "status": row.get("status"),
        "position": row.get("position"),
        "has_login": bool(row.get("storage_state")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


class _CreateBrowserSession(BaseModel):
    title: Optional[str] = None
    url: Optional[str] = None
    agent_id: Optional[str] = None
    shared: bool = False  # private by default


class _PatchBrowserSession(BaseModel):
    title: Optional[str] = None
    agent_id: Optional[str] = None
    shared: Optional[bool] = None
    status: Optional[str] = None
    position: Optional[int] = None


class _ResolveBrowserSession(BaseModel):
    agent_id: Optional[str] = None


@router.get("/api/v1/browser/sessions")
async def list_browser_sessions(request: Request):
    from app.auth.identity import assert_caller_is
    from app.db import browser_sessions_store as store
    user_id = await assert_caller_is(request, None)
    return {"sessions": [_public(r) for r in store.list_for_user(user_id)]}


@router.post("/api/v1/browser/sessions")
async def create_browser_session(request: Request, body: _CreateBrowserSession):
    from app.auth.identity import assert_caller_is
    from app.db import browser_sessions_store as store
    user_id = await assert_caller_is(request, None)
    await _require_browser_page_access(user_id)
    row = store.create(
        user_id,
        title=body.title,
        url=body.url,
        agent_id=body.agent_id,
        shared=body.shared,
    )
    return _public(row)


@router.post("/api/v1/browser/sessions/resolve")
async def resolve_browser_session(request: Request, body: _ResolveBrowserSession):
    """Resolve the tab the Web panel should open so it MATCHES what the agent drives.

    With an ``agent_id``, this returns the SAME tab ``browser_action`` resolves to
    via ``resolve_agent_session`` — the agent's shared+linked tab, auto-created (and
    linked + shared) if it has none. So opening the Web tab shows exactly the page
    the agent is driving, with no manual "Share with agent" step. Without an agent
    (or if resolution fails) it falls back to the user's first tab, creating a
    private one if they have none — the old behaviour.
    """
    from app.auth.identity import assert_caller_is
    from app.db import browser_sessions_store as store
    user_id = await assert_caller_is(request, None)
    await _require_browser_page_access(user_id)
    if body.agent_id:
        from app.tools.browser import resolve_agent_session
        try:
            bs_id = resolve_agent_session(user_id, body.agent_id, None)
        except PermissionError:
            bs_id = None
        if bs_id:
            row = store.get(bs_id)
            if row:
                return _public(row)
    # No agent / resolution failed → the user's first tab (private one if none).
    bs_id = _resolve_user_bs_id(user_id, None)
    row = store.get(bs_id) if bs_id else None
    if not row:
        raise HTTPException(status_code=404, detail="Browser session not found")
    return _public(row)


@router.patch("/api/v1/browser/sessions/{bs_id}")
async def patch_browser_session(bs_id: str, request: Request, body: _PatchBrowserSession):
    from app.auth.identity import assert_caller_is
    from app.db import browser_sessions_store as store
    user_id = await assert_caller_is(request, None)
    row = store.get(bs_id)
    if not row or row.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Browser session not found")
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    updated = store.update(bs_id, **fields) if fields else row
    return _public(updated)


@router.delete("/api/v1/browser/sessions/{bs_id}")
async def delete_browser_session(bs_id: str, request: Request):
    from app.auth.identity import assert_caller_is
    from app.db import browser_sessions_store as store
    from app.tools import browser as _engine
    user_id = await assert_caller_is(request, None)
    row = store.get(bs_id)
    if not row or row.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Browser session not found")
    # Persist + tear down any live page, then drop the row.
    try:
        await _engine.close(bs_id)
    except Exception:
        pass
    store.delete(bs_id)
    return {"success": True}


# ── REST: backend switcher (headless ⇄ on-device browser) ─────────────────────
# The Web tab flips a session between the in-app headless browser and the real
# Chromium on the user's device. The agent can request the same via browser_action
# (a confirmed action). Same-machine only for now.


class _SwitchBackend(BaseModel):
    mode: str                       # "local" | "headless" | "connector"
    profile: Optional[str] = None   # "default" | "dedicated" (local only)


@router.get("/api/v1/browser/sessions/{bs_id}/backend")
async def get_browser_backend(bs_id: str, request: Request):
    from app.auth.identity import assert_caller_is
    from app.db import browser_sessions_store as store
    from app.tools import browser as _engine
    user_id = await assert_caller_is(request, None)
    row = store.get(bs_id)
    if not row or row.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Browser session not found")
    return _engine.backend_status(bs_id)


@router.post("/api/v1/browser/sessions/{bs_id}/backend")
async def set_browser_backend(bs_id: str, request: Request, body: _SwitchBackend):
    from app.auth.identity import assert_caller_is
    from app.db import browser_sessions_store as store
    from app.tools import browser as _engine
    user_id = await assert_caller_is(request, None)
    row = store.get(bs_id)
    if not row or row.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="Browser session not found")
    mode = (body.mode or "").lower()
    if mode == "local":
        return await _engine.open_local_browser(bs_id, profile=(body.profile or "default"))
    if mode == "headless":
        return await _engine.use_remote_browser(bs_id)
    if mode == "connector":
        # The user's own browser, driven through their installed extension over the
        # connector WebSocket. Requires a live extension for this user.
        return await _engine.use_connector_browser(bs_id, user_id=user_id)
    raise HTTPException(status_code=400, detail="mode must be 'local', 'headless' or 'connector'")
