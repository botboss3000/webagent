"""Generic web tools — scraper API + authenticated browser session.

Two provider-gated tool families:

  scraper          — calls a configured third-party scraping API (Apify,
                     RapidAPI, or a custom HTTP endpoint) for sites that have
                     no public API of their own.

  browser_session  — performs HTTP requests using cookies the user pasted in
                     (one session row per user, currently). Lets the agent
                     drive sites the user is already logged into.

Both are deliberately domain-agnostic. Site-specific knowledge (URL templates,
GraphQL doc ids, CSRF token names) lives in the admin-editable skill prompt
fragment, not in this file.
"""

import json
import logging
import re
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


_HTTP_TIMEOUT = 30.0


# ── Helpers ────────────────────────────────────────────────────────────────

def _err(msg: str, **extra) -> str:
    return json.dumps({"status": "error", "message": msg, **extra})


def _not_configured(provider: str) -> str:
    return json.dumps({
        "status": "not_configured",
        "provider": provider,
        "message": f"{provider} is not configured — set it up in App Config → Integrations.",
    })


async def _load_scraper_creds() -> Optional[dict]:
    try:
        from app.admin.integrations import get_scraper_creds
        creds = await get_scraper_creds()
    except Exception as e:
        logger.warning("get_scraper_creds failed: %s", e)
        return None
    if not creds or not creds.get("api_key"):
        return None
    return creds


async def _load_browser_session(user_id: str) -> Optional[dict]:
    try:
        from app.admin.integrations import get_browser_session_creds
        sess = await get_browser_session_creds(user_id)
    except Exception as e:
        logger.warning("get_browser_session_creds failed: %s", e)
        return None
    if not sess or not sess.get("cookies"):
        return None
    return sess


def _parse_cookies(blob: str) -> dict:
    """Accept either Netscape cookie-jar text or JSON {name:value}."""
    blob = (blob or "").strip()
    if not blob:
        return {}
    if blob.startswith("{"):
        try:
            parsed = json.loads(blob)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            pass
    out: dict = {}
    for line in blob.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) >= 7:
            name, value = parts[5], parts[6]
            out[name] = value
        elif "=" in line:
            name, _, value = line.partition("=")
            out[name.strip()] = value.strip().rstrip(";")
    return out


def _looks_like_login_redirect(resp: httpx.Response) -> bool:
    """Heuristic: did the upstream bounce us to a login page?"""
    if resp.status_code in (401, 403):
        return True
    if resp.status_code in (301, 302, 303, 307, 308):
        loc = resp.headers.get("location", "").lower()
        if "login" in loc or "checkpoint" in loc or "signin" in loc:
            return True
    return False


# ── Scraper tools ──────────────────────────────────────────────────────────

async def web_scrape_search(
    user_id: str,
    agent_id: str,
    query: str,
    location: str = "",
    filters: Optional[dict] = None,
    max_results: int = 20,
) -> str:
    """Search via the configured scraper. Payload is intentionally generic;
    actor-specific keys can be passed via `filters`."""
    if not query:
        return _err("query is required")
    creds = await _load_scraper_creds()
    if not creds:
        return _not_configured("scraper")

    provider = (creds.get("provider") or "").lower()
    api_key = creds["api_key"]
    endpoint = (creds.get("endpoint") or "").strip()
    extra = creds.get("extra") or {}
    payload = {
        "query": query,
        "location": location,
        "filters": filters or {},
        "maxItems": max(1, min(int(max_results or 20), 100)),
    }

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            if provider == "apify":
                actor = extra.get("actor_id") or endpoint
                if not actor:
                    return _err("apify provider requires actor_id (set it in App Config → Web Scraper)")
                url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={api_key}"
                resp = await client.post(url, json=payload)
            elif provider == "rapidapi":
                host = extra.get("host") or ""
                base = endpoint or (f"https://{host}" if host else "")
                if not base:
                    return _err("rapidapi provider requires endpoint or host (set it in App Config → Web Scraper)")
                headers = {"X-RapidAPI-Key": api_key}
                if host:
                    headers["X-RapidAPI-Host"] = host
                resp = await client.get(base, params=payload, headers=headers)
            else:
                if not endpoint:
                    return _err("custom_http provider requires endpoint (set it in App Config → Web Scraper)")
                headers = {"Authorization": f"Bearer {api_key}"}
                resp = await client.get(endpoint, params=payload, headers=headers)
    except Exception as e:
        return _err(f"upstream error: {e}")

    try:
        body: Any = resp.json()
    except Exception:
        body = resp.text
    return json.dumps({
        "status": "ok" if resp.is_success else "error",
        "http_status": resp.status_code,
        "provider": provider,
        "results": body if isinstance(body, list) else (body if isinstance(body, dict) else {"raw": body}),
    })


async def web_scrape_url(user_id: str, agent_id: str, url: str, render_js: bool = False) -> str:
    """Fetch a single URL through the configured scraper (handles JS / blocks)."""
    if not url:
        return _err("url is required")
    creds = await _load_scraper_creds()
    if not creds:
        return _not_configured("scraper")

    provider = (creds.get("provider") or "").lower()
    api_key = creds["api_key"]
    endpoint = (creds.get("endpoint") or "").strip()
    extra = creds.get("extra") or {}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            if provider == "apify":
                actor = extra.get("actor_id") or endpoint
                if not actor:
                    return _err("apify provider requires actor_id")
                api_url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?token={api_key}"
                resp = await client.post(api_url, json={"startUrls": [{"url": url}], "renderJs": bool(render_js)})
            elif provider == "rapidapi":
                host = extra.get("host") or ""
                base = endpoint or (f"https://{host}" if host else "")
                if not base:
                    return _err("rapidapi provider requires endpoint or host")
                headers = {"X-RapidAPI-Key": api_key}
                if host:
                    headers["X-RapidAPI-Host"] = host
                resp = await client.get(base, params={"url": url, "render_js": int(bool(render_js))}, headers=headers)
            else:
                if not endpoint:
                    return _err("custom_http provider requires endpoint")
                headers = {"Authorization": f"Bearer {api_key}"}
                resp = await client.get(endpoint, params={"url": url, "render_js": int(bool(render_js))}, headers=headers)
    except Exception as e:
        return _err(f"upstream error: {e}")

    try:
        body: Any = resp.json()
    except Exception:
        body = resp.text
    return json.dumps({
        "status": "ok" if resp.is_success else "error",
        "http_status": resp.status_code,
        "url": url,
        "body": body,
    })


# ── Browser session tools ──────────────────────────────────────────────────

async def web_session_status(user_id: str, agent_id: str) -> str:
    """Report whether a browser session is configured for this user."""
    sess = await _load_browser_session(user_id)
    if not sess:
        return json.dumps({"status": "not_configured", "provider": "browser_session"})
    return json.dumps({
        "status": "ok",
        "domain": sess.get("domain", ""),
        "cookie_count": len(_parse_cookies(sess.get("cookies", ""))),
        "saved_at": sess.get("saved_at", ""),
    })


async def web_session_fetch(
    user_id: str,
    agent_id: str,
    url: str,
    method: str = "GET",
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    body: Optional[Any] = None,
    form: Optional[dict] = None,
) -> str:
    """Make an HTTP call using the user's stored browser cookies + UA.

    `body` may be a string or dict (sent as JSON). `form` is sent
    url-encoded. Returns the response status, headers, and body.
    """
    if not url:
        return _err("url is required")
    sess = await _load_browser_session(user_id)
    if not sess:
        return _not_configured("browser_session")

    cookies = _parse_cookies(sess.get("cookies", ""))
    if not cookies:
        return json.dumps({"status": "session_expired", "provider": "browser_session",
                           "message": "No cookies in stored session — re-paste in App Config → Browser Cookies."})

    method = (method or "GET").upper()
    user_agent = sess.get("user_agent") or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    request_headers = {"User-Agent": user_agent, "Accept": "*/*"}
    if headers:
        request_headers.update({str(k): str(v) for k, v in headers.items()})

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, cookies=cookies, follow_redirects=False) as client:
            if form is not None:
                resp = await client.request(method, url, params=params, headers=request_headers, data=form)
            elif isinstance(body, (dict, list)):
                resp = await client.request(method, url, params=params, headers=request_headers, json=body)
            else:
                resp = await client.request(method, url, params=params, headers=request_headers, content=body)
    except Exception as e:
        return _err(f"request failed: {e}")

    if _looks_like_login_redirect(resp):
        return json.dumps({
            "status": "session_expired",
            "provider": "browser_session",
            "http_status": resp.status_code,
            "message": "Upstream bounced to login — cookies are stale, refresh them in App Config → Browser Cookies.",
        })

    try:
        parsed: Any = resp.json()
    except Exception:
        parsed = resp.text

    return json.dumps({
        "status": "ok" if resp.is_success else "error",
        "http_status": resp.status_code,
        "headers": dict(resp.headers),
        "body": parsed,
    })


_CSRF_REGEX_CACHE: dict = {}


def _compile_csrf_regex(pattern: str) -> Optional[re.Pattern]:
    if not pattern:
        return None
    if pattern in _CSRF_REGEX_CACHE:
        return _CSRF_REGEX_CACHE[pattern]
    try:
        compiled = re.compile(pattern)
    except re.error:
        return None
    _CSRF_REGEX_CACHE[pattern] = compiled
    return compiled


async def web_session_graphql(
    user_id: str,
    agent_id: str,
    url: str,
    doc_id: str = "",
    query: str = "",
    variables: Optional[dict] = None,
    extra_form: Optional[dict] = None,
    csrf_token_url: str = "",
    csrf_token_names: Optional[list] = None,
) -> str:
    """POST a GraphQL-style form to a logged-in page.

    Many SPAs (Facebook, Instagram, LinkedIn) expose an internal `/api/graphql`
    endpoint that accepts `doc_id` + variables as form data along with CSRF
    tokens scraped from the rendered page. This wrapper:

      1. Optionally GETs `csrf_token_url` and regex-scrapes the named tokens
         (`csrf_token_names`, e.g. ["fb_dtsg", "lsd"]) from the HTML.
      2. POSTs the merged form (doc_id/query/variables + scraped tokens +
         `extra_form`) to `url` with the stored cookies + UA.

    The skill prompt teaches the agent which URL/doc_id/token names to use
    for each site. Nothing here is site-specific.
    """
    if not url:
        return _err("url is required")
    if not doc_id and not query:
        return _err("either doc_id or query is required")

    sess = await _load_browser_session(user_id)
    if not sess:
        return _not_configured("browser_session")

    cookies = _parse_cookies(sess.get("cookies", ""))
    if not cookies:
        return json.dumps({"status": "session_expired", "provider": "browser_session"})

    user_agent = sess.get("user_agent") or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    headers = {"User-Agent": user_agent, "Accept": "*/*"}

    scraped: dict = {}
    if csrf_token_url and csrf_token_names:
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, cookies=cookies, follow_redirects=True) as client:
                page = await client.get(csrf_token_url, headers=headers)
            if _looks_like_login_redirect(page):
                return json.dumps({"status": "session_expired", "provider": "browser_session"})
            html = page.text or ""
            for name in csrf_token_names:
                m = re.search(rf'"{re.escape(name)}"\s*:\s*"([^"]+)"', html)
                if m:
                    scraped[name] = m.group(1)
        except Exception as e:
            return _err(f"csrf scrape failed: {e}")

    form: dict = {}
    if doc_id:
        form["doc_id"] = doc_id
    if query:
        form["query"] = query
    if variables is not None:
        form["variables"] = json.dumps(variables)
    form.update(scraped)
    if extra_form:
        form.update({str(k): str(v) for k, v in extra_form.items()})

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, cookies=cookies, follow_redirects=False) as client:
            resp = await client.post(url, data=form, headers={**headers, "X-Requested-With": "XMLHttpRequest"})
    except Exception as e:
        return _err(f"request failed: {e}")

    if _looks_like_login_redirect(resp):
        return json.dumps({"status": "session_expired", "provider": "browser_session", "http_status": resp.status_code})

    try:
        parsed: Any = resp.json()
    except Exception:
        parsed = resp.text

    return json.dumps({
        "status": "ok" if resp.is_success else "error",
        "http_status": resp.status_code,
        "scraped_tokens": list(scraped.keys()),
        "body": parsed,
    })


# ── Tool registry ──────────────────────────────────────────────────────────

TOOLS = [
    {"name": "web_scrape_search", "provider": "scraper", "handler": web_scrape_search,
     "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
        "query":       {"type": "string",  "description": "Search query — passed through to the configured scraper."},
        "location":    {"type": "string",  "description": "Optional location filter.", "default": ""},
        "filters":     {"type": "object",  "description": "Optional scraper-specific filters (passed through verbatim)."},
        "max_results": {"type": "integer", "description": "Max items to return (1-100).", "default": 20},
     }, "required": ["query"]}},

    {"name": "web_scrape_url", "provider": "scraper", "handler": web_scrape_url,
     "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {
        "url":       {"type": "string",  "description": "Full URL to scrape."},
        "render_js": {"type": "boolean", "description": "Ask the scraper to render JS (slower; needed for SPAs).", "default": False},
     }, "required": ["url"]}},

    {"name": "web_session_status", "provider": "browser_session", "handler": web_session_status,
     "stages": ["execute_tools"], "destructive": False,
     "parameters": {"type": "object", "properties": {}, "required": []}},

    {"name": "web_session_fetch", "provider": "browser_session", "handler": web_session_fetch,
     "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
        "url":     {"type": "string", "description": "Full URL of the request."},
        "method":  {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "GET"},
        "params":  {"type": "object", "description": "Optional query-string parameters."},
        "headers": {"type": "object", "description": "Optional extra headers."},
        "body":    {"description": "Optional request body (string or dict → JSON)."},
        "form":    {"type": "object", "description": "Optional url-encoded form body. Mutually exclusive with body."},
     }, "required": ["url"]}},

    {"name": "web_session_graphql", "provider": "browser_session", "handler": web_session_graphql,
     "stages": ["execute_tools"], "destructive": True, "requires_confirmation": True,
     "parameters": {"type": "object", "properties": {
        "url":              {"type": "string", "description": "GraphQL endpoint URL (e.g. https://www.facebook.com/api/graphql/)."},
        "doc_id":           {"type": "string", "description": "Persisted query id (preferred when available).", "default": ""},
        "query":            {"type": "string", "description": "GraphQL query string (only when no doc_id).", "default": ""},
        "variables":        {"type": "object", "description": "Query variables (serialized to JSON)."},
        "extra_form":       {"type": "object", "description": "Additional form fields to send (e.g. fb_api_caller_class)."},
        "csrf_token_url":   {"type": "string", "description": "URL to GET first to scrape CSRF tokens from the HTML.", "default": ""},
        "csrf_token_names": {"type": "array",  "items": {"type": "string"},
                              "description": "Names of JSON-embedded tokens to scrape (e.g. ['fb_dtsg', 'lsd'])."},
     }, "required": ["url"]}},
]
