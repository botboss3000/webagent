"""Fetch the onboarding guide from the public repo at runtime.

The guide lives in the repo (``onboarding-guide.md``) and is fetched
live, so editing it there improves onboarding for every installed manager with no
app update or reinstall — the local TUI never has to change. The last good copy is
cached in the data dir, so an offline phone still gets the most recent guidance we
ever fetched; if there is no cache either, we return ``""`` and the agent falls
back to the onboarding instructions baked into its system prompt.
"""

from __future__ import annotations

from .config import data_dir

# Raw URL of the guide on the public repo's main branch. Editing the file there
# (and pushing to main) updates onboarding everywhere on the next launch.
GUIDE_URL = (
    "https://raw.githubusercontent.com/botboss3000/webagent/main/"
    "TUI/tui-data/onboarding-guide.md"
)
_CACHE_NAME = "onboarding-guide.md"


def _cache_path():
    return data_dir() / _CACHE_NAME


async def fetch_onboarding_guide(timeout: float = 6.0) -> str:
    """Return the latest onboarding guide text.

    Order: live fetch from the repo → last good cached copy → ``""``. A live fetch
    that succeeds also refreshes the cache. Never raises — onboarding must work
    even with no network.
    """
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(GUIDE_URL)
        if resp.status_code < 400 and resp.text.strip():
            text = resp.text
            try:
                p = _cache_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
            except OSError:
                pass
            return text
    except Exception:
        pass
    # Network failed or returned nothing useful — fall back to the last good copy.
    try:
        return _cache_path().read_text(encoding="utf-8")
    except OSError:
        return ""
