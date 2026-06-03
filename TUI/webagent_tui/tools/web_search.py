"""Web search tool — DuckDuckGo via HTML scraping, no API key needed.

Read-only, available in both onboarding and managed mode, so the agent can
research errors, check docs, or find solutions even when working with a fresh
install. Deliberately kept simple: one httpx GET, parse the HTML, return a
flat list of title + URL + snippet.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Optional

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

from .base import ToolContext

_DDG_URL = "https://html.duckduckgo.com/html/"
_RESULT_RE = re.compile(
    r'<a[^>]*rel="nofollow"[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>'
    r'(.*?)</a>.*?'
    r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_UDDG_RE = re.compile(r'uddg=([^&]+)')
_STRIP_TAG = re.compile(r'<[^>]+>')
_CLEAN_VISIBLE = re.compile(r'\s+')


def _unwrap_url(raw: str) -> str:
    """Extract real URL from a DDG redirect wrapper if present."""
    import urllib.parse
    m = _UDDG_RE.search(raw)
    if m:
        decoded = urllib.parse.unquote(m.group(1))
        if decoded.startswith("http") and len(decoded) < 2000:
            return decoded
    # Also handle simple relative /l/?uddg=... paths
    return raw


def _clean(text: str) -> str:
    text = _STRIP_TAG.sub(" ", text)
    text = unescape(text)
    return _CLEAN_VISIBLE.sub(" ", text).strip()


async def web_search(
    ctx: ToolContext,
    query: str,
    max_results: int = 5,
) -> str:
    """Search DuckDuckGo and return results as plain text.

    Args:
        query: The search string.
        max_results: Maximum results (1–10, default 5).
    """
    if httpx is None:
        return "Error: httpx is not installed (required for web_search)."
    if not query or not query.strip():
        return "Error: query is required."
    max_results = max(1, min(10, max_results or 5))
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            headers={
                "User-Agent": "webagent-tui/1.0 (web search agent)",
                "Accept": "text/html",
            },
            follow_redirects=True,
        ) as client:
            resp = await client.get(_DDG_URL, params={"q": query})
            if resp.status_code >= 500:
                return f"Error: DuckDuckGo returned HTTP {resp.status_code}."
            if resp.status_code >= 400:
                return f"Error: DuckDuckGo search returned HTTP {resp.status_code}. Try rephrasing."
    except httpx.TimeoutException:
        return "Error: web search timed out. Try again or rephrase the query."
    except httpx.HTTPError as e:
        return f"Error: web search request failed: {e}"

    html = resp.text
    results = _RESULT_RE.findall(html)
    if not results:
        return f"No results found for: {query}"

    lines: list[str] = []
    for url, title, snippet in results[:max_results]:
        title = _clean(title)
        snippet = _clean(snippet)
        url = _unwrap_url(url)
        if not title:
            title = "(no title)"
        if not snippet:
            snippet = "(no snippet)"
        # Truncate long snippets.
        if len(snippet) > 300:
            snippet = snippet[:297] + "..."
        lines.append(f"[{len(lines) + 1}] {title}\n    {url}\n    {snippet}")

    return "\n\n".join(lines)
