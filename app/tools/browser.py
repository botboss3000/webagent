"""
Persistent browser manager for Playwright-based automation.

Maintains one browser instance per user+session so the agent can
navigate, click, type, scrape, and screenshot across multiple turns.

By default the browser is headless (invisible). If a visible app-mode Chromium
is running with a remote-debugging port open — e.g. the launcher's "App Window"
button (see launcher/webagent_launcher/app_window.py) — the manager ATTACHES to
that window over CDP instead, so the agent drives the same window the user is
watching. Close the window and it transparently reverts to headless.
"""

# Defer annotation evaluation so the playwright type names (Browser /
# BrowserContext / Page) used in the annotations below don't force an import at
# module load. Playwright is a heavy import (lots of files to scan on a cold
# disk) and is only needed when the browser tools actually run — async_playwright
# is imported lazily inside _ensure_page. This keeps server startup fast.
from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:  # imported only for type checkers, never at runtime
    from playwright.async_api import Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

# ── Globals ──────────────────────────────────────────────────────────────────

_playwright_instance = None
# Keyed by browser_session_id (bs_id) — the id of a row in the browser_sessions
# table. One persistent tab per id; its own context is its own cookie jar (loaded
# from / saved to that row's storage_state), so logins survive restarts. The Web
# tab and the agent's browser_action both address a tab by this id, so they drive
# the SAME page.
_browsers: dict[str, Browser] = {}        # bs_id -> Browser
_contexts: dict[str, BrowserContext] = {} # bs_id -> BrowserContext
_pages: dict[str, Page] = {}              # bs_id -> Page
# Keys whose browser is an ATTACHED (CDP) connection to a user-visible window,
# not a headless instance we launched. Never tear these down — closing them
# would shut the window the user is looking at; we only disconnect.
_attached: set[str] = set()


def _cdp_port() -> int:
    """Remote-debugging port to look for a user-visible app window on.

    Matches the launcher's app_window.CDP_PORT (same env var, same default), so
    when the launcher opens the App Window the agent meets it on the same port.
    """
    try:
        return int(os.environ.get("WEBAGENT_BROWSER_CDP_PORT", "9222") or "9222")
    except ValueError:
        return 9222


def _cdp_endpoint_open(port: int) -> bool:
    """Fast, non-blocking check for a CDP endpoint on localhost:port.

    Cheaper than letting connect_over_cdp time out: when nothing is listening
    (the common case — no visible window, or a headless server deployment) we
    skip the attach attempt and go straight to headless.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        s.close()


async def _ensure_page(bs_id: str) -> Page:
    """Get or create the live page for a browser session (bs_id).

    Order of preference:
      1. The live page we already hold for this id.
      2. ATTACH to a user-visible app-mode window if one is open (CDP port up)
         — the agent then drives the window the user is watching.
      3. Launch our own headless Chromium (default). The context is seeded with
         the row's saved storage_state (cookies/localStorage) so a login persists
         across restarts.
    """
    global _playwright_instance
    key = bs_id

    if key in _pages:
        page = _pages[key]
        try:
            # Quick health check
            await page.evaluate("1")
            return page
        except Exception:
            logger.info(f"Page for {key} died, re-creating")
            _pages.pop(key, None)
            # A dead attached page means the window was closed — drop the stale
            # connection so we can re-attach or fall back to headless.
            if key in _attached:
                _attached.discard(key)
                _contexts.pop(key, None)
                _browsers.pop(key, None)

    if _playwright_instance is None:
        from playwright.async_api import async_playwright  # lazy: heavy import
        _playwright_instance = await async_playwright().start()

    # ── (2) Attach to a visible app window if the launcher opened one ─────────
    browser = _browsers.get(key)
    if browser is None and _cdp_endpoint_open(_cdp_port()):
        try:
            browser = await _playwright_instance.chromium.connect_over_cdp(
                f"http://127.0.0.1:{_cdp_port()}", timeout=3000
            )
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            # Reuse the window's existing page (the app); else open one in it.
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            _browsers[key] = browser
            _contexts[key] = ctx
            _pages[key] = page
            _attached.add(key)
            logger.info(f"browser_action attached to visible window (CDP :{_cdp_port()}) for {key}")
            return page
        except Exception as e:
            logger.info(f"CDP attach failed ({e}); using headless")
            _browsers.pop(key, None)
            _contexts.pop(key, None)
            _attached.discard(key)
            browser = None

    # ── (3) Headless launch (default / no visible window) ────────────────────
    if browser is None or not browser.is_connected():
        browser = await _playwright_instance.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        _browsers[key] = browser

    context = _contexts.get(key)
    if context is None:
        ctx_kwargs = {
            "viewport": {"width": 1280, "height": 720},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        # Seed cookies/localStorage from the saved row so a login persists across
        # restarts. Best-effort: a missing/invalid blob just starts a clean jar.
        try:
            from app.db import browser_sessions_store as _store
            saved = _store.get_storage_state(bs_id)
            if saved:
                ctx_kwargs["storage_state"] = saved
        except Exception as _se:  # noqa: BLE001
            logger.debug("storage_state load for %s skipped: %s", bs_id, _se)
        context = await browser.new_context(**ctx_kwargs)
        _contexts[key] = context

    page = await context.new_page()
    _pages[key] = page
    return page


async def get_or_create_page(bs_id: str) -> Page:
    """Public accessor used by the live Browser panel (app/api/browser_stream.py).

    Returns the live Playwright page for this browser session, creating the
    browser/context/page on demand. Because it keys on the same bs_id the agent's
    ``browser_action`` resolves to, the human's Web tab and the agent drive the
    **same** page — whatever one does, the other sees live.
    """
    return await _ensure_page(bs_id)


def get_existing_page(bs_id: str) -> Optional[Page]:
    """Return the live page for this browser session if one exists, else None.

    Does NOT create a browser — lets callers check whether a page is already open
    before forcing one.
    """
    return _pages.get(bs_id)


async def persist_state(bs_id: str) -> None:
    """Snapshot the context's cookies/localStorage back to the row so a login
    survives a server restart. No-op for attached (user-window) contexts."""
    if bs_id in _attached:
        return  # the user's own window — not ours to snapshot
    ctx = _contexts.get(bs_id)
    if ctx is None:
        return
    try:
        state = await ctx.storage_state()
        from app.db import browser_sessions_store as _store
        _store.set_storage_state(bs_id, state)
    except Exception as e:  # noqa: BLE001
        logger.debug("persist_state(%s) failed: %s", bs_id, e)


async def close(bs_id: str) -> dict:
    """Persist login state, then tear down the browser for this session.

    Attached (CDP) connections to a user-visible window are only DISCONNECTED —
    closing their page/context would shut the window the user is looking at.
    """
    key = bs_id
    await persist_state(bs_id)
    if key in _attached:
        _pages.pop(key, None)
        _contexts.pop(key, None)
        b = _browsers.pop(key, None)
        _attached.discard(key)
        if b is not None:
            try:
                await b.close()  # CDP: disconnects; the window stays open
            except Exception:
                pass
        return {"success": True, "result": "Detached from app window", "url": "", "title": ""}
    if key in _pages:
        try:
            await _pages[key].close()
        except Exception:
            pass
        _pages.pop(key, None)
    if key in _contexts:
        try:
            await _contexts[key].close()
        except Exception:
            pass
        _contexts.pop(key, None)
    if key in _browsers:
        try:
            await _browsers[key].close()
        except Exception:
            pass
        _browsers.pop(key, None)
    return {"success": True, "result": "Browser closed", "url": "", "title": ""}


def resolve_agent_session(user_id: str, agent_id: str,
                          browser_session_id: Optional[str] = None) -> str:
    """Resolve the browser-session id an agent is allowed to drive — the single
    enforcement point for the sharing gate.

    Rules: a tab is reachable by the agent only if it is owned by ``user_id``,
    linked to THIS ``agent_id``, and ``shared``. If ``browser_session_id`` is
    given it must satisfy that (else PermissionError). Otherwise the agent's first
    shared tab is used; if it has none, a fresh shared tab linked to the agent is
    created (so the user sees it as "the agent's tab"). Private and unlinked tabs
    are invisible here.
    """
    from app.db import browser_sessions_store as _store
    if browser_session_id:
        row = _store.get(browser_session_id)
        if not row or row.get("user_id") != user_id:
            raise PermissionError("browser session not found")
        if row.get("agent_id") != agent_id or not row.get("shared"):
            raise PermissionError("browser session is not shared with this agent")
        return browser_session_id
    shared = _store.list_shared_for_agent(user_id, agent_id)
    if shared:
        return shared[0]["id"]
    created = _store.create(user_id, agent_id=agent_id, title="Agent browser", shared=True)
    return created["id"]


async def browser_action(
    bs_id: str,
    action: str,
    selector: Optional[str] = None,
    text: Optional[str] = None,
    url: Optional[str] = None,
    js: Optional[str] = None,
    timeout_ms: int = 5000,
    full_page: bool = True,
) -> dict:
    """
    Execute a browser automation action against a browser session (bs_id).

    A persistent headless Chromium is maintained per browser session. Consecutive
    calls with the same bs_id share the same page, and the user's Web tab streams
    that same page live.

    Args:
        bs_id: The browser-session id to drive (resolved by the caller/loader
            from the agent's sharing-gated session).
        action: One of:
            - "navigate"    → url (required)
            - "click"       → selector (required)
            - "type"        → selector (required), text (required)
            - "get_text"    → selector (required)
            - "get_html"    → selector (optional, defaults to body)
            - "screenshot"  → full_page (optional, default true)
            - "wait"        → timeout_ms (optional, default 5000)
            - "evaluate"    → js (required)
            - "title"       → returns page title
            - "url"         → returns current URL
            - "close"       → closes browser for this session
        selector: CSS selector for click/type/get_text actions
        text: Text to type (for type action)
        url: URL to navigate to (for navigate action)
        js: JavaScript code to evaluate (for evaluate action)
        timeout_ms: Timeout in ms for wait action (default 5000)
        full_page: Capture full scrollable page for screenshot (default true)

    Returns:
        dict with:
            - success: bool
            - result: str (text content, screenshot as base64, HTML, etc.)
            - error: str (if failed)
            - url: str (current page URL)
            - title: str (current page title)
    """
    try:
        if action == "close":
            return await close(bs_id)

        page = await _ensure_page(bs_id)

        if action == "navigate":
            if not url:
                return {"success": False, "error": "url required for navigate action", "url": "", "title": ""}
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1000)  # let JS render settle
            title = await page.title()
            # Remember where this tab is + snapshot the (now possibly logged-in)
            # cookie jar so the row survives a restart.
            try:
                from app.db import browser_sessions_store as _store
                _store.update(bs_id, url=page.url, title=title)
            except Exception:  # noqa: BLE001
                pass
            await persist_state(bs_id)
            return {
                "success": True,
                "result": f"Navigated to {url}",
                "url": page.url,
                "title": title,
            }

        elif action == "click":
            if not selector:
                return {"success": False, "error": "selector required for click action", "url": page.url, "title": await page.title()}
            await page.wait_for_selector(selector, timeout=10000)
            await page.click(selector)
            await page.wait_for_timeout(500)
            return {
                "success": True,
                "result": f"Clicked '{selector}'",
                "url": page.url,
                "title": await page.title(),
            }

        elif action == "type":
            if not selector or text is None:
                return {"success": False, "error": "selector and text required for type action", "url": page.url, "title": await page.title()}
            await page.wait_for_selector(selector, timeout=10000)
            await page.fill(selector, "")
            await page.type(selector, text, delay=20)
            return {
                "success": True,
                "result": f"Typed '{text[:100]}' into '{selector}'",
                "url": page.url,
                "title": await page.title(),
            }

        elif action == "get_text":
            if not selector:
                return {"success": False, "error": "selector required for get_text action", "url": page.url, "title": await page.title()}
            try:
                await page.wait_for_selector(selector, timeout=5000)
                el = await page.query_selector(selector)
                if el:
                    text = await el.inner_text()
                else:
                    text = ""
            except Exception:
                text = ""
            return {
                "success": True,
                "result": text,
                "url": page.url,
                "title": await page.title(),
            }

        elif action == "get_html":
            if selector:
                try:
                    el = await page.query_selector(selector)
                    html = await el.inner_html() if el else ""
                except Exception:
                    html = ""
            else:
                html = await page.content()
            return {
                "success": True,
                "result": html,
                "url": page.url,
                "title": await page.title(),
            }

        elif action == "screenshot":
            filename = f"{uuid.uuid4().hex}.png"
            # Land the screenshot in the OWNING user's home so each user's files
            # stay separate. Resolve the owner from the browser-session row; if
            # that can't be found, fall back to the legacy flat data/screenshots
            # pile so a screenshot never fails to save.
            uri = None
            try:
                from app.db import browser_sessions_store as _bstore
                from app import user_workspace as _ws
                row = _bstore.get(bs_id) or {}
                owner = row.get("user_id")
                if owner:
                    shots_dir = _ws.user_dir(owner, "screenshots")
                    filepath = shots_dir / filename
                    await page.screenshot(path=str(filepath), full_page=full_page)
                    uri = _ws.public_url(owner, "screenshots", filename)
            except Exception as _e:  # noqa: BLE001
                logger.debug("per-user screenshot path failed (%s); using legacy dir", _e)
            if uri is None:
                screenshots_dir = Path(__file__).resolve().parent.parent.parent / "data" / "screenshots"
                screenshots_dir.mkdir(parents=True, exist_ok=True)
                filepath = screenshots_dir / filename
                await page.screenshot(path=str(filepath), full_page=full_page)
                uri = f"/screenshots/{filename}"
            return {
                "success": True,
                "result": uri,
                "url": page.url,
                "title": await page.title(),
                "mime_type": "image/png",
            }

        elif action == "wait":
            await page.wait_for_timeout(timeout_ms)
            return {
                "success": True,
                "result": f"Waited {timeout_ms}ms",
                "url": page.url,
                "title": await page.title(),
            }

        elif action == "evaluate":
            if not js:
                return {"success": False, "error": "js required for evaluate action", "url": page.url, "title": await page.title()}
            result = await page.evaluate(js)
            return {
                "success": True,
                "result": str(result),
                "url": page.url,
                "title": await page.title(),
            }

        elif action == "title":
            title = await page.title()
            return {
                "success": True,
                "result": title,
                "url": page.url,
                "title": title,
            }

        elif action == "url":
            return {
                "success": True,
                "result": page.url,
                "url": page.url,
                "title": await page.title(),
            }

        else:
            return {"success": False, "error": f"Unknown action '{action}'", "url": "", "title": ""}

    except Exception as e:
        logger.error(f"browser_action ({action}) failed: {e}")
        # Try to get current URL/title even on failure
        try:
            current_url = _pages.get(bs_id)
            url_str = current_url.url if current_url else ""
            title_str = await current_url.title() if current_url else ""
        except Exception:
            url_str = ""
            title_str = ""
        return {
            "success": False,
            "error": str(e),
            "url": url_str,
            "title": title_str,
        }


async def close_all():
    """Clean up all browser instances. Call on server shutdown.

    Attached (CDP) connections to user-visible windows are only DISCONNECTED —
    we never close their page/context, which would shut the user's window.
    """
    global _playwright_instance
    for key in list(_pages.keys()):
        if key in _attached:
            continue  # never close a window the user owns
        try:
            await _pages[key].close()
        except Exception:
            pass
    for key in list(_contexts.keys()):
        if key in _attached:
            continue
        try:
            await _contexts[key].close()
        except Exception:
            pass
    for key in list(_browsers.keys()):
        try:
            await _browsers[key].close()  # CDP-connected → just disconnects
        except Exception:
            pass
    _pages.clear()
    _contexts.clear()
    _browsers.clear()
    _attached.clear()
    if _playwright_instance:
        await _playwright_instance.stop()
        _playwright_instance = None
    logger.info("All browser instances closed")
