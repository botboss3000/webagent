"""
Page management for the AutoAgent multi-page workspace.

Thin async facade over the configurable PageStore (filesystem / database /
hybrid). Pick the backend from the App Config → Storage panel or via the
WEBAGENT_PAGES_STORE env var. See app/pages_store/__init__.py for details.
"""
from typing import Dict, List, Optional

from app.pages_store import get_pages_store
from app.pages_store.seed import home_page_html


async def list_pages(user_id: str) -> List[Dict]:
    """Return all pages for a user, seeding home if missing."""
    store = get_pages_store()
    await store.ensure_home_page(user_id, home_page_html())
    return await store.list_pages(user_id)


async def get_page_html(user_id: str, slug: str) -> Optional[str]:
    return await get_pages_store().get_page_html(user_id, slug)


async def save_page_html(user_id: str, slug: str, html: str) -> str:
    return await get_pages_store().save_page_html(user_id, slug, html)


async def create_page(
    user_id: str,
    slug: str,
    title: str,
    agent_context: str = "",
    initial_html: str = "",
) -> Dict:
    return await get_pages_store().create_page(
        user_id=user_id,
        slug=slug,
        title=title,
        agent_context=agent_context,
        initial_html=initial_html,
    )


async def delete_page(user_id: str, slug: str) -> bool:
    return await get_pages_store().delete_page(user_id, slug)


async def rename_page(user_id: str, slug: str, new_title: str) -> bool:
    return await get_pages_store().rename_page(user_id, slug, new_title)


async def ensure_home_page(user_id: str) -> None:
    await get_pages_store().ensure_home_page(user_id, home_page_html())
