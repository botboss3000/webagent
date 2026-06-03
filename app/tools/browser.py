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
_browsers: dict[str, Browser] = {}       # session_key -> Browser
_contexts: dict[str, BrowserContext] = {} # session_key -> BrowserContext
_pages: dict[str, Page] = {}             # session_key -> Page
# Keys whose browser is an ATTACHED (CDP) connection to a user-visible window,
# not a headless instance we launched. Never tear these down — closing them
# would shut the window the user is looking at; we only disconnect.
_attached: set[str] = set()


def _session_key(user_id: str, session_id: str) -> str:
    return f"{user_id}:{session_id}"


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


async def _ensure_page(user_id: str, session_id: str) -> Page:
    """Get or create a page for this user+session.

    Order of preference:
      1. The live page we already hold for this key.
      2. ATTACH to a user-visible app-mode window if one is open (CDP port up)
         — the agent then drives the window the user is watching.
      3. Launch our own headless Chromium (default).
    """
    global _playwright_instance
    key = _session_key(user_id, session_id)

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
        context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        _contexts[key] = context

    page = await context.new_page()
    _pages[key] = page
    return page


async def get_or_create_page(user_id: str, session_id: str) -> Page:
    """Public accessor used by the live Browser panel (app/api/browser_stream.py).

    Returns the live Playwright page for this user+session, creating the
    browser/context/page on demand. Because it shares the same per-session key
    as ``browser_action``, the human's Web tab and the agent's ``browser_action``
    tool drive the **same** page — that is what makes the panel "AI-augmented":
    whatever one does, the other sees live.
    """
    return await _ensure_page(user_id, session_id)


def get_existing_page(user_id: str, session_id: str) -> Optional[Page]:
    """Return the live page for this user+session if one exists, else None.

    Does NOT create a browser — lets callers check whether the agent has
    already opened a page before the human's panel forces one.
    """
    return _pages.get(_session_key(user_id, session_id))


async def browser_action(
    user_id: str,
    session_id: str,
    action: str,
    selector: Optional[str] = None,
    text: Optional[str] = None,
    url: Optional[str] = None,
    js: Optional[str] = None,
    timeout_ms: int = 5000,
    full_page: bool = True,
) -> dict:
    """
    Execute a browser automation action.

    A persistent headless Chromium is maintained per user+session.
    Consecutive calls within the same session share the same page.

    Args:
        user_id: Injected by the tool loader
        session_id: Injected by the tool loader
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
            key = _session_key(user_id, session_id)
            if key in _attached:
                # Attached to a user-visible window — DON'T close the page/context
                # (that's the user's window). Just disconnect our CDP client.
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
                await _pages[key].close()
                del _pages[key]
            if key in _contexts:
                await _contexts[key].close()
                del _contexts[key]
            if key in _browsers:
                await _browsers[key].close()
                del _browsers[key]
            return {"success": True, "result": "Browser closed", "url": "", "title": ""}

        page = await _ensure_page(user_id, session_id)

        if action == "navigate":
            if not url:
                return {"success": False, "error": "url required for navigate action", "url": "", "title": ""}
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1000)  # let JS render settle
            return {
                "success": True,
                "result": f"Navigated to {url}",
                "url": page.url,
                "title": await page.title(),
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
            # Determine screenshots directory (project root / screenshots)
            screenshots_dir = Path(__file__).resolve().parent.parent.parent / "data" / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)

            filename = f"{uuid.uuid4().hex}.png"
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
            current_url = _pages.get(_session_key(user_id, session_id))
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
