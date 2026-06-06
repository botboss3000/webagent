"""
Live browser streaming WebSocket for the in-app "Web" page.

Streams a browser SESSION's Playwright page — the SAME page the agent's
``browser_action`` tool drives (see app/tools/browser.py) — into the web UI as
a live JPEG screencast over the Chrome DevTools Protocol, and forwards the
user's mouse / keyboard / navigation back to that page.

A browser session (``bs_id``) is a first-class, persistent tab that lives BESIDE
chat (rows in the ``browser_sessions`` table). The human's Web tab and the agent
both address a tab by this id, so whatever the agent does shows up live in the
panel and vice-versa: an "AI-augmented" browser the two drive together. This
module also exposes the REST CRUD for those sessions (list / create / patch /
delete) so the UI can manage tabs and toggle the per-tab ``shared`` flag.

Works on a headless server (including the production Cloud VM): CDP screencast
does not need a visible window. No loopback guard — Caddy proxies the WS in
production, and frames are scoped to the authenticated caller's own session.
"""

import asyncio
import logging
from typing import Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request, HTTPException
from pydantic import BaseModel

from app.auth.jwt import decode_token
from app.tools.browser import get_or_create_page, persist_state

logger = logging.getLogger(__name__)

router = APIRouter()

# Fixed page viewport — matches the context viewport in app/tools/browser.py.
# The client maps its click/scroll coordinates into this space.
VIEW_W = 1280
VIEW_H = 720


def _verify_token(token: str) -> Optional[str]:
    """Return the caller's user_id from a valid JWT, else None."""
    if not token:
        return None
    try:
        payload = decode_token(token)
    except Exception:
        return None
    if not payload:
        return None
    return payload.get("user_id") or payload.get("sub")


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


@router.websocket("/api/v1/browser/ws")
async def browser_ws(websocket: WebSocket):
    # Authenticate BEFORE accepting so a hostile client never opens a stream.
    token = websocket.query_params.get("token", "")
    user_id = _verify_token(token)
    if not user_id:
        await websocket.close(code=4401)
        return

    await websocket.accept()

    # The Web tab connects by browser-session id (bs_id). Back-compat: if none is
    # supplied, fall back to the caller's first tab (creating one if they have
    # none) so an older client still gets a working browser.
    raw_bs = websocket.query_params.get("bs_id") or websocket.query_params.get("browser_session_id")
    bs_id = _resolve_user_bs_id(user_id, raw_bs)
    if not bs_id:
        try:
            await websocket.send_json({"type": "error", "message": "Browser session not found"})
        finally:
            await websocket.close(code=4404)
        return

    # Shared page (created on demand). Same bs_id the agent resolves to, so the
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

    # CDP session for the screencast.
    try:
        cdp = await page.context.new_cdp_session(page)
    except Exception as e:
        logger.warning("browser_ws: CDP session failed: %s", e)
        try:
            await websocket.send_json({"type": "error", "message": f"Screencast unavailable: {e}"})
        finally:
            await websocket.close(code=1011)
        return

    # Latest-frame queue (size 1: always drop stale frames so the view never
    # lags behind the live page).
    frame_q: "asyncio.Queue[str]" = asyncio.Queue(maxsize=1)
    loop = asyncio.get_running_loop()
    input_lock = asyncio.Lock()
    stopped = asyncio.Event()

    def _on_frame(params: dict):
        # Invoked in the event loop by Playwright. Ack immediately so frames
        # keep flowing, then publish the latest frame for the sender task.
        sid = params.get("sessionId")
        data = params.get("data")

        async def _pump():
            try:
                await cdp.send("Page.screencastFrameAck", {"sessionId": sid})
            except Exception:
                pass
            if not data:
                return
            if frame_q.full():
                try:
                    frame_q.get_nowait()
                except Exception:
                    pass
            try:
                frame_q.put_nowait(data)
            except Exception:
                pass

        try:
            loop.create_task(_pump())
        except RuntimeError:
            pass

    cdp.on("Page.screencastFrame", _on_frame)

    async def _emit_nav():
        try:
            title = await page.title()
            await websocket.send_json({"type": "nav", "url": page.url, "title": title})
            # Remember where this tab is and snapshot its (possibly logged-in)
            # cookie jar so the row survives a restart. Best-effort.
            try:
                from app.db import browser_sessions_store as _store
                _store.update(bs_id, url=page.url, title=title)
            except Exception:
                pass
            await persist_state(bs_id)
        except Exception:
            pass

    async def _sender():
        while not stopped.is_set():
            try:
                data = await asyncio.wait_for(frame_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break
            try:
                await websocket.send_json({"type": "frame", "data": data})
            except Exception:
                break

    async def _handle(msg: dict):
        action = msg.get("action")
        if not action:
            return
        x, y = msg.get("x"), msg.get("y")
        if isinstance(x, (int, float)):
            x = max(0.0, min(float(VIEW_W), float(x)))
        if isinstance(y, (int, float)):
            y = max(0.0, min(float(VIEW_H), float(y)))
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
                elif action == "click":
                    await page.mouse.click(x, y)
                elif action == "mousedown":
                    await page.mouse.move(x, y)
                    await page.mouse.down()
                elif action == "mousemove":
                    await page.mouse.move(x, y)
                elif action == "mouseup":
                    await page.mouse.move(x, y)
                    await page.mouse.up()
                elif action == "scroll":
                    await page.mouse.wheel(float(msg.get("dx", 0) or 0), float(msg.get("dy", 0) or 0))
                elif action == "type":
                    await page.keyboard.type(str(msg.get("text", "")))
                elif action == "key":
                    key = str(msg.get("key", "")).strip()
                    if key:
                        await page.keyboard.press(key)
            except Exception as e:
                # A single bad action must never kill the stream.
                try:
                    await websocket.send_json({"type": "action_error", "action": action, "message": str(e)})
                except Exception:
                    pass

    sender_task = asyncio.create_task(_sender())
    try:
        await cdp.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": 55,
            "maxWidth": VIEW_W,
            "maxHeight": VIEW_H,
            "everyNthFrame": 1,
        })
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
        sender_task.cancel()
        try:
            cdp.remove_listener("Page.screencastFrame", _on_frame)
        except Exception:
            pass
        try:
            await cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            await cdp.detach()
        except Exception:
            pass
        # NB: never close the page/browser here. The agent may still be using
        # it, and for an attached app-window it is the user's own window.


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
    row = store.create(
        user_id,
        title=body.title,
        url=body.url,
        agent_id=body.agent_id,
        shared=body.shared,
    )
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
