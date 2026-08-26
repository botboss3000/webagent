"""Browser tools — Playwright-powered web browsing for the TUI agent.

Provides a shared headless Chromium browser that stays alive across tool calls
within a session, letting the agent navigate, inspect pages, take screenshots,
click elements, type text, and run JavaScript.  All tools are read-only from
the server's perspective (they don't touch the linked checkout) so they work
in onboarding mode too.

Playwright is imported lazily — the first call to any tool warms up the
browser, and a final ``browser_close`` (or process exit) tears it down.
"""

from __future__ import annotations

import asyncio
import base64
import tempfile
from pathlib import Path

from .base import ToolContext

# Module-level singleton — the browser + page live for the lifetime of the
# agent process.  All calls share one browser instance under a lock so
# concurrent tool dispatches don't race.
_lock = asyncio.Lock()
_browser: object | None = None       # playwright.async_api.Browser
_page: object | None = None          # playwright.async_api.Page
_playwright_api: object | None = None  # playwright.async_api.Playwright


async def _ensure_browser() -> object:
    """Return a ready page, launching the browser on first call."""
    global _browser, _page, _playwright_api
    async with _lock:
        if _page is None:
            # Lazy import keeps Playwright out of the import path until needed.
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            _playwright_api = pw
            _browser = await pw.chromium.launch(headless=True)
            ctx = await _browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
            )
            _page = await ctx.new_page()
        return _page


def _text_snapshot(content: str, title: str, url: str) -> str:
    """Build a compact text snapshot: title, URL, then cleaned body text."""
    import re
    # Strip script/style tags and their content
    cleaned = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', content, flags=re.DOTALL | re.IGNORECASE)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', cleaned)
    # Remove remaining HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Compact whitespace again
    text = re.sub(r'\s+', ' ', text).strip()
    header = f"[title] {title}\n[url] {url}"
    body = text[:6000] + ("\n... (truncated)" if len(text) > 6000 else "")
    return f"{header}\n\n{body}"


async def browser_navigate(ctx: ToolContext, url: str) -> str:
    """Navigate the shared browser to a URL and return a text snapshot of the page.

    Returns the page title, URL, and visible text content (up to 6000 chars).
    Use this first, then browser_snapshot for an interactive-element breakdown,
    or browser_screenshot for a visual.
    """
    if not url:
        return "Error: url is required."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        page = await _ensure_browser()
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(0.3)  # let any initial JS settle
        title = await page.title()
        content = await page.content()
        final_url = page.url
        status = resp.status if resp else "unknown"
        ctx.audit("browser_navigate", {"url": url}, True, f"status={status}")
        header = f"[navigated] {final_url}  (HTTP {status})"
        return header + "\n\n" + _text_snapshot(content, title, final_url)
    except Exception as e:
        ctx.audit("browser_navigate", {"url": url}, False, str(e))
        return f"Error navigating to {url}: {type(e).__name__}: {e}"


async def browser_snapshot(ctx: ToolContext) -> str:
    """Capture the accessibility snapshot of the current page.

    Returns a structured listing of interactive elements (buttons, links,
    inputs, selects) with their labels, roles, and text — the agent can use
    these to decide what to click or fill. Also includes the page title and URL.
    """
    try:
        page = await _ensure_browser()
        title = await page.title()
        url = page.url
        # Grab interactive elements via evaluate
        elements = await page.evaluate("""() => {
            const roles = ['button', 'link', 'textbox', 'searchbox', 'combobox',
                          'checkbox', 'radio', 'select', 'option', 'heading',
                          'img', 'text', 'navigation', 'list', 'listitem'];
            const out = [];
            const seen = new Set();
            function walk(el, depth) {
                if (depth > 20) return;
                const tag = el.tagName ? el.tagName.toLowerCase() : '';
                if (tag === 'script' || tag === 'style' || tag === 'noscript') return;
                const visible = el.offsetParent !== null || el.checkVisibility ? 
                    (el.checkVisibility ? el.checkVisibility() : true) : true;
                if (!visible) return;
                const role = el.getAttribute('role') || '';
                const type = el.getAttribute('type') || '';
                const name = el.getAttribute('name') || el.getAttribute('aria-label') || '';
                const placeholder = el.getAttribute('placeholder') || '';
                const href = (tag === 'a') ? el.getAttribute('href') || '' : '';
                let text = (el.textContent || '').trim().substring(0, 80);
                const id = el.id || '';
                
                const aria = role || type || (tag === 'a' ? 'link' : '') || 
                             (tag === 'input' ? 'textbox' : '') ||
                             (tag === 'button' ? 'button' : '') ||
                             (tag === 'select' ? 'select' : '') ||
                             (tag === 'textarea' ? 'textbox' : '');
                
                if (aria || href || id || name || placeholder) {
                    const desc = [];
                    desc.push(tag);
                    if (id) desc.push('#' + id);
                    if (aria) desc.push('[' + aria + ']');
                    if (name) desc.push('name="' + name + '"');
                    if (placeholder) desc.push('placeholder="' + placeholder + '"');
                    if (href) desc.push('href="' + href.substring(0, 60) + '"');
                    if (text) desc.push('"' + text + '"');
                    const key = desc.join(' ');
                    if (!seen.has(key)) {
                        seen.add(key);
                        out.push(key);
                    }
                }
                for (const child of el.children) {
                    walk(child, depth + 1);
                }
            }
            walk(document.body || document.documentElement, 0);
            return out.slice(0, 100);
        }""")
        header = f"[snapshot] {url}  —  {title}"
        if not elements:
            return f"{header}\n\n(no interactive elements found)"

        numbered = "\n".join(f"  [{i}] {el}" for i, el in enumerate(elements))
        return f"{header}\n\nInteractive elements:\n{numbered}"
    except Exception as e:
        ctx.audit("browser_snapshot", {}, False, str(e))
        return f"Error capturing snapshot: {type(e).__name__}: {e}"


async def browser_screenshot(ctx: ToolContext, selector: str = "") -> str:
    """Take a screenshot of the current page (or an element matching selector).

    Saves a PNG to a temp file and returns the absolute path. The agent can
    then read the file (or tell the user where to find it). If the browser is
    on the same machine as the user they can open it directly.
    """
    try:
        page = await _ensure_browser()
        fd, path = tempfile.mkstemp(suffix=".png", prefix="webagent_screenshot_")
        await asyncio.to_thread(lambda: None)  # workaround — close fd on this thread
        import os
        os.close(fd)
        if selector:
            el = await page.query_selector(selector)
            if el is None:
                return f"Error: no element matching '{selector}' found."
            await el.screenshot(path=path)
        else:
            await page.screenshot(path=path, full_page=True)
        ctx.audit("browser_screenshot", {"selector": selector}, True, f"saved={path}")
        return f"Screenshot saved: {path}"
    except Exception as e:
        ctx.audit("browser_screenshot", {"selector": selector}, False, str(e))
        return f"Error taking screenshot: {type(e).__name__}: {e}"


async def browser_click(ctx: ToolContext, selector: str, index: int = 0) -> str:
    """Click an element on the current page by CSS selector.

    Use ``browser_snapshot`` first to find the selectors of interactive
    elements.  If the selector matches multiple elements, ``index`` picks which
    one (0 = first).  Waits for navigation if the click triggers one.
    """
    if not selector:
        return "Error: selector is required."
    try:
        page = await _ensure_browser()
        elements = await page.query_selector_all(selector)
        if not elements:
            return f"Error: no elements match '{selector}'."
        if index >= len(elements):
            return f"Error: index {index} out of range (found {len(elements)} elements)."
        el = elements[index]
        text = (await el.text_content() or "").strip()[:80]
        try:
            await el.click(timeout=5000)
        except Exception:
            # If click triggers navigation, Playwright may throw — that's ok.
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        ctx.audit("browser_click", {"selector": selector, "index": index}, True, f"text={text}")
        title = await page.title()
        url = page.url
        return f"Clicked [{index}] '{selector}' (text: \"{text}\")\nNow at: {url}  —  {title}"
    except Exception as e:
        ctx.audit("browser_click", {"selector": selector, "index": index}, False, str(e))
        return f"Error clicking '{selector}': {type(e).__name__}: {e}"


async def browser_type(ctx: ToolContext, selector: str, text: str, submit: bool = False) -> str:
    """Type text into an input/textarea element, optionally pressing Enter.

    Clears the field first.  Use ``browser_snapshot`` to find the right
    selector.  If ``submit=True``, presses Enter after typing.
    """
    if not selector:
        return "Error: selector is required."
    if not text:
        return "Error: text is required."
    try:
        page = await _ensure_browser()
        el = await page.query_selector(selector)
        if el is None:
            return f"Error: no element matching '{selector}' found."
        await el.click()
        await el.fill("")
        await el.type(text, delay=30)
        if submit:
            await el.press("Enter")
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        ctx.audit("browser_type", {"selector": selector, "text": text[:80], "submit": submit},
                  True, "")
        title = await page.title()
        url = page.url
        return f"Typed into '{selector}' (submit={submit})\nNow at: {url}  —  {title}"
    except Exception as e:
        ctx.audit("browser_type", {"selector": selector, "text": text[:80]}, False, str(e))
        return f"Error typing into '{selector}': {type(e).__name__}: {e}"


async def browser_evaluate(ctx: ToolContext, expression: str) -> str:
    """Run a JavaScript expression in the current page and return the result.

    The expression runs in the browser context.  For example:
    ``document.title``, ``document.querySelectorAll('a').length``, or a
    multi-line function that extracts structured data.  The result is
    JSON-serialised for return.
    """
    if not expression:
        return "Error: expression is required."
    try:
        page = await _ensure_browser()
        import json
        result = await page.evaluate(expression)
        ctx.audit("browser_evaluate", {"expression": expression[:120]}, True, "")
        if result is None:
            return "(null / undefined)"
        if isinstance(result, (str, int, float, bool)):
            return str(result)
        return json.dumps(result, indent=2, default=str, ensure_ascii=False)
    except Exception as e:
        ctx.audit("browser_evaluate", {"expression": expression[:120]}, False, str(e))
        return f"Error evaluating expression: {type(e).__name__}: {e}"


async def browser_close(ctx: ToolContext) -> str:
    """Close the shared browser and free its resources.

    Call this when done browsing.  The browser will restart on the next call
    to any browser tool.
    """
    global _browser, _page, _playwright_api
    async with _lock:
        try:
            if _page is not None:
                await _page.close()
            if _browser is not None:
                await _browser.close()
            if _playwright_api is not None:
                await _playwright_api.stop()
        except Exception:
            pass
        _page = None
        _browser = None
        _playwright_api = None
    return "Browser closed."
