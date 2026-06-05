"""Wiki-control tools — let an agent manage the company-wide Wiki.

The Wiki is one shared, searchable knowledge base (company info, policies,
contacts, any reference data). These tools wrap the same store the HTTP API and
the UI use (``app/wiki/store.py``), so an article an agent writes shows up
immediately in the Wiki tab and in everyone's search.

  wiki_search  — semantic + keyword search across all articles
  wiki_list    — list every article (titles/tags/snippets)
  wiki_get     — read one full article by slug
  wiki_create  — add a new article
  wiki_update  — edit an existing article
  wiki_delete  — remove an article

Gated by the ``wiki_control`` ability (off by default — writes/deletes change
data everyone sees). Injected by app/tools/loader.py only when that ability is on.
"""

from __future__ import annotations

import json

from app.wiki import store as _wiki


def _ok(**data) -> str:
    out = {"status": "ok"}
    out.update(data)
    return json.dumps(out, default=str)


def _err(msg: str, **extra) -> str:
    out = {"status": "error", "error": msg}
    out.update(extra)
    return json.dumps(out)


def build_wiki_tools(user_id: str):
    """Return {tool_name: handler} bound to this user (for write attribution)."""

    async def wiki_search(query: str = "", limit: int = 10) -> str:
        """Search the company Wiki by meaning AND keywords. Use this first to
        find existing knowledge before answering or before creating a new
        article. Returns ranked matches with titles, slugs, tags, and snippets."""
        try:
            results = await _wiki.search_articles(query, limit=int(limit or 10))
            return _ok(results=results, count=len(results), query=query)
        except Exception as e:
            return _err(f"wiki search failed: {e}")

    async def wiki_list() -> str:
        """List every Wiki article (metadata + a short snippet), newest first.
        Use wiki_search when you have a topic; use this to browse what exists."""
        try:
            articles = await _wiki.list_articles()
            return _ok(articles=articles, count=len(articles))
        except Exception as e:
            return _err(f"wiki list failed: {e}")

    async def wiki_get(slug: str) -> str:
        """Read one full Wiki article by its slug (from search/list results)."""
        try:
            article = await _wiki.get_article(slug)
            if article is None:
                return _err(f"no article with slug '{slug}'")
            return _ok(article=article)
        except Exception as e:
            return _err(f"wiki get failed: {e}")

    async def wiki_create(
        title: str, body: str = "", tags=None, category: str = ""
    ) -> str:
        """Create a new Wiki article. The slug is derived from the title. Search
        first to avoid duplicating an existing entry."""
        try:
            article = await _wiki.create_article(
                title=title, body=body or "", tags=tags,
                category=category or "", user_id=user_id,
            )
            return _ok(article=article, created=True)
        except ValueError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"wiki create failed: {e}")

    async def wiki_update(
        slug: str, title: str = None, body: str = None,
        tags=None, category: str = None,
    ) -> str:
        """Edit an existing Wiki article. Only the fields you pass change; the
        slug (URL) is preserved. Read it first with wiki_get if you need the
        current body before rewriting it."""
        try:
            article = await _wiki.update_article(
                slug=slug, title=title, body=body, tags=tags,
                category=category, user_id=user_id,
            )
            if article is None:
                return _err(f"no article with slug '{slug}'")
            return _ok(article=article, updated=True)
        except ValueError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"wiki update failed: {e}")

    async def wiki_delete(slug: str) -> str:
        """Delete a Wiki article permanently. This removes it for everyone."""
        try:
            ok = await _wiki.delete_article(slug)
            if not ok:
                return _err(f"no article with slug '{slug}'")
            return _ok(deleted=True, slug=slug)
        except Exception as e:
            return _err(f"wiki delete failed: {e}")

    return {
        "wiki_search": wiki_search,
        "wiki_list": wiki_list,
        "wiki_get": wiki_get,
        "wiki_create": wiki_create,
        "wiki_update": wiki_update,
        "wiki_delete": wiki_delete,
    }


# JSON-Schema parameter definitions, keyed by tool name. The loader pairs these
# with the handlers above when building ToolInfo entries.
WIKI_TOOL_SCHEMAS = {
    "wiki_search": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for. Matched by meaning and keywords across titles, bodies, and tags."},
            "limit": {"type": "integer", "description": "Max results to return (default 10)."},
        },
        "required": ["query"],
    },
    "wiki_list": {"type": "object", "properties": {}, "required": []},
    "wiki_get": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "The article's slug (from a search/list result)."},
        },
        "required": ["slug"],
    },
    "wiki_create": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Article title. The URL slug is derived from this."},
            "body": {"type": "string", "description": "Article content (markdown/plain text)."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags for filtering, e.g. ['finance','2026']."},
            "category": {"type": "string", "description": "Optional grouping label, e.g. 'HR' or 'Product'."},
        },
        "required": ["title"],
    },
    "wiki_update": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "The article to edit (its slug)."},
            "title": {"type": "string", "description": "New title (optional; omit to keep current)."},
            "body": {"type": "string", "description": "New full body (optional; replaces the current body)."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "New tag list (optional; replaces current tags)."},
            "category": {"type": "string", "description": "New category (optional)."},
        },
        "required": ["slug"],
    },
    "wiki_delete": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "The article to delete (its slug)."},
        },
        "required": ["slug"],
    },
}

# Only deletes change shared data irreversibly → guardrail confirmation. Create
# and update are reversible edits, kept non-destructive to match how the visual
# page tools behave. Flip these into WIKI_DESTRUCTIVE if you want writes gated too.
WIKI_DESTRUCTIVE = {"wiki_delete"}
