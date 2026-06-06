"""
Domain-restricted web search connector.

Config:
  domain: str   — e.g. "support.acme.com" or "wikipedia.org"
  hint:   str   — optional extra terms always appended to every query

Wraps the existing web-search tool in `app.tools.browser` (or `core_tools`),
scoping every query to the configured domain. Enforced server-side: the
LLM cannot escape to the wider web through this tool.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, List, Optional

from app.connectors.base import Connector, GeneratedTool

FEATURE = {
    "id": "web_search_domain",
    "display_name": "Domain-restricted web search",
    "category": "connector",
    "status": "beta",
    "summary": "Search forced through site:<domain> so the agent can't escape to the wider web.",
}

logger = logging.getLogger(__name__)


_DOMAIN_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})+$")


def _normalize_domain(d: str) -> str:
    if not d:
        return ""
    s = d.strip().lower()
    if s.startswith("http://"):
        s = s[7:]
    if s.startswith("https://"):
        s = s[8:]
    s = s.split("/", 1)[0]
    return s


async def _provider_web_search(query: str, limit: int = 5) -> List[dict]:
    """Best-effort call into whichever web-search tool exists.

    Tries, in order:
      1. app.tools.core_tools.web_search
      2. app.tools.browser.web_search
    Returns a list of {title, url, snippet} dicts.
    """
    try:
        from app.tools.core_tools import web_search as _ws  # type: ignore

        res = await _ws(query, limit=limit)  # type: ignore[arg-type]
        return _coerce(res)
    except Exception:
        pass
    try:
        from app.tools.browser import web_search as _ws2  # type: ignore

        res = await _ws2(query, limit=limit)  # type: ignore[arg-type]
        return _coerce(res)
    except Exception as e:
        logger.warning("web_search_domain: no provider available: %s", e)
        return []


def _coerce(raw) -> List[dict]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, dict):
        return raw.get("results") or []
    if isinstance(raw, list):
        return raw
    return []


class WebSearchDomainConnector(Connector):
    type_name = "web_search_domain"
    display_name = "Website search (domain-restricted)"
    requires_credential = False

    async def test_connection(self, data_source, auth_resolver):
        cfg = data_source.get("config") or {}
        domain = _normalize_domain(cfg.get("domain") or "")
        if not domain:
            return {"ok": False, "message": "domain is required", "details": {}}
        if not _DOMAIN_RE.match(domain):
            return {"ok": False, "message": f"invalid domain: {domain!r}", "details": {}}
        try:
            results = await _provider_web_search(f"site:{domain} test", limit=1)
            return {
                "ok": True,
                "message": "search provider reachable" if results else "search returned no results",
                "details": {"domain": domain, "sample_count": len(results)},
            }
        except Exception as e:
            return {"ok": False, "message": f"search provider failed: {e}", "details": {}}

    async def introspect(self, data_source, auth_resolver):
        cfg = data_source.get("config") or {}
        domain = _normalize_domain(cfg.get("domain") or "")
        return {"domain": domain, "hint": cfg.get("hint") or ""}

    def generated_tools(self, data_source, attachment, auth_resolver):
        cfg = data_source.get("config") or {}
        domain = _normalize_domain(cfg.get("domain") or "")
        hint = (cfg.get("hint") or "").strip()
        tool_name = self.default_tool_name(data_source, attachment)
        if not tool_name.startswith("web_search_"):
            tool_name = f"web_search_{tool_name}"

        async def _handler(query: str, limit: int = 5):
            if not domain:
                return json.dumps({"status": "error", "message": "domain not configured"})
            # Server-side domain enforcement; never trust LLM to add site:
            scoped = f"site:{domain} {query}".strip()
            if hint:
                scoped = f"{scoped} {hint}"
            try:
                results = await _provider_web_search(scoped, limit=int(limit))
            except Exception as e:
                return json.dumps({"status": "error", "message": str(e)})
            # Hard filter: drop any result whose URL is not on the domain.
            filtered = []
            for r in results:
                url = (r.get("url") or r.get("link") or "").lower()
                if not url:
                    continue
                host = url.split("//", 1)[-1].split("/", 1)[0]
                if host == domain or host.endswith("." + domain):
                    filtered.append(r)
            return json.dumps({
                "status": "ok",
                "domain": domain,
                "query": query,
                "result_count": len(filtered),
                "results": filtered[: int(limit)],
            }, default=str)

        return [
            GeneratedTool(
                name=tool_name,
                description=(
                    f"Search the web restricted to `{domain or '???'}`. "
                    f"Every query is automatically scoped to this domain; the "
                    f"agent cannot escape to the wider web through this tool."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                    },
                    "required": ["query"],
                },
                handler=_handler,
                destructive=False,
                metadata={"connector": self.type_name, "data_source_id": data_source.get("id"), "domain": domain},
            )
        ]

    def prompt_snippet(self, data_source, attachment):
        cfg = data_source.get("config") or {}
        domain = _normalize_domain(cfg.get("domain") or "") or "<unset>"
        name = data_source.get("name", "site_search")
        return (
            f"Website search source `{name}` is scoped to **{domain}**. "
            f"Use the search tool to look things up on that site; the wider web is not reachable here."
        )
