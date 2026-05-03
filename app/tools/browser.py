"""
Persistent headless browser manager for Playwright-based automation.

Maintains one browser instance per user+session so the agent can
navigate, click, type, scrape, and screenshot across multiple turns.
"""

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

# ── Globals ──────────────────────────────────────────────────────────────────

_playwright_instance = None
_browsers: dict[str, Browser] = {}       # session_key -> Browser
_contexts: dict[str, BrowserContext] = {} # session_key -> BrowserContext
_pages: dict[str, Page] = {}             # session_key -> Page


def _session_key(user_id: str, session_id: str) -> str:
    return f"{user_id}:{session_id}"


async def _ensure_page(user_id: str, session_id: str) -> Page:
    """Get or create a page for this user+session."""
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
            del _pages[key]

    if _playwright_instance is None:
        _playwright_instance = await async_playwright().start()

    browser = _browsers.get(key)
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
            screenshots_dir = Path(__file__).resolve().parent.parent.parent / "screenshots"
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
    """Clean up all browser instances. Call on server shutdown."""
    global _playwright_instance
    for key in list(_pages.keys()):
        try:
            await _pages[key].close()
        except Exception:
            pass
    _pages.clear()
    for key in list(_contexts.keys()):
        try:
            await _contexts[key].close()
        except Exception:
            pass
    _contexts.clear()
    for key in list(_browsers.keys()):
        try:
            await _browsers[key].close()
        except Exception:
            pass
    _browsers.clear()
    if _playwright_instance:
        await _playwright_instance.stop()
        _playwright_instance = None
    logger.info("All browser instances closed")
