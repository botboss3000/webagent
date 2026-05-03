"""
Seed the tools table with starter tools.

Run once to populate the database with built-in tools.
Usage: python scripts/seed_tools.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

# Add parent dir to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import get_db
from app.tools.registry import create_tool


# ── Tool definitions ──────────────────────────────────────────────────────────

WEB_SEARCH_CODE = r'''async def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web using DuckDuckGo and return summarized results.
    Uses DuckDuckGo's HTML search endpoint - no API key required.

    Args:
        query: The search query
        max_results: Maximum number of results to return (default 5, max 10)

    Returns:
        Formatted string with search results including titles, snippets, and URLs
    """
    import httpx
    import re
    from urllib.parse import quote_plus

    max_results = min(max_results, 10)
    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        response = await client.post(url, data={"q": query}, headers=headers)
        response.raise_for_status()
        html = response.text

    # Parse results using regex (lightweight, no extra deps)
    results = []
    # DuckDuckGo HTML results look like:
    # <a rel="nofollow" href="URL" class="result__a">TITLE</a>
    # <a class="result__snippet">SNIPPET</a>
    link_pattern = re.compile(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
    snippet_pattern = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)

    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    for i in range(min(len(links), max_results)):
        href, title_html = links[i]
        # Clean HTML tags from title
        title = re.sub(r'<[^>]+>', '', title_html).strip()
        snippet = ""
        if i < len(snippets):
            snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()
        # Clean up the URL (sometimes DuckDuckGo wraps redirect URLs)
        if href.startswith("//"):
            href = "https:" + href
        results.append(f"{i+1}. {title}\n   {snippet}\n   {href}")

    if not results:
        return f"No results found for '{query}'."

    return f"Search results for '{query}':\n\n" + "\n\n".join(results)
'''


CREATE_TOOL_CODE = r'''async def create_tool(name: str, description: str, parameters: dict, code: str) -> dict:
    """
    Create or update a tool in the tools table.
    The tool code is stored and compiled dynamically on next agent load.

    Args:
        name: Tool identifier (e.g. 'check_email', 'web_search')
        description: What the tool does (shown to the model to describe when to use it)
        parameters: JSON Schema describing the tool's input parameters
        code: Full Python async function code. Must contain an async function with the same name as the tool.

    Returns:
        dict with status, tool_name, tool_id, and message
    """
    import json
    from app.db import get_db

    # This is a stub - the actual implementation is injected by the tool loader.
    # This entry exists in the DB so the tool is discoverable.
    raise RuntimeError("create_tool is handled by the built-in loader. This DB entry is for schema/description only.")
'''


# Stub code for built-in tools (actual handler injected by loader)
BUILTIN_STUB = '''async def builtin_stub(**kwargs):
    raise RuntimeError("This tool is handled by the built-in loader. The DB entry is for schema/description only.")
'''


BROWSER_ACTION_CODE = r'''async def browser_action(
    action: str,
    selector: str | None = None,
    text: str | None = None,
    url: str | None = None,
    js: str | None = None,
    timeout_ms: int = 5000,
    full_page: bool = True,
    user_id: str = "",
) -> dict:
    """
    Control a persistent headless Chromium browser.

    Maintains one browser per user. Consecutive calls within the same
    session share the same page — navigate, then click, type, scrape, etc.
    Call action="close" to free resources when done.

    Actions:
      navigate   → url (required). Go to a URL.
      click      → selector (required). Click an element.
      type       → selector (required), text (required). Fill an input field.
      get_text   → selector (required). Get inner text of an element.
      get_html   → selector (optional, defaults to full page HTML).
      screenshot → full_page (optional). Saves to disk, returns URI e.g. /screenshots/<uuid>.png.
      wait       → timeout_ms (optional, default 5000). Wait in ms.
      evaluate   → js (required). Run JavaScript in the page.
      title      → Get current page title.
      url        → Get current page URL.
      close      → Close the browser for this user.

    Args:
        action: One of navigate/click/type/get_text/get_html/screenshot/wait/evaluate/title/url/close
        selector: CSS selector (for click, type, get_text)
        text: Text to type (for type action)
        url: URL to navigate to (for navigate action)
        js: JavaScript code (for evaluate action)
        timeout_ms: Wait timeout in ms (for wait action, default 5000)
        full_page: Capture full scrollable page (for screenshot, default true)
        user_id: Injected automatically by tool loader

    Returns:
        dict with success, result (text or URI), error, url, title
    """
    from app.tools.browser import browser_action as _ba
    return await _ba(
        user_id=user_id,
        session_id=user_id,  # one browser per user is fine
        action=action,
        selector=selector,
        text=text,
        url=url,
        js=js,
        timeout_ms=timeout_ms,
        full_page=full_page,
    )
'''


TAKE_SCREENSHOT_CODE = r'''async def take_screenshot(
    output_path: str | None = None,
    region: dict | None = None,
    monitor: int = 0,
) -> dict:
    """
    Capture a screenshot of the screen.

    Captures the primary monitor by default. Supports multi-monitor
    setups on Windows via win32api (fallback gracefully if unavailable).
    When no output_path given, saves to screenshots/ and returns URL
    that is served at /screenshots/<uuid>.png.

    Args:
        output_path: Optional path to save screenshot (e.g. 'screenshot.png').
                     If omitted, saves to screenshots/ dir and returns URL + base64.
        region: Optional region dict with keys x, y, width, height to capture.
        monitor: Monitor index (0=primary, 1=secondary, ...). -1 = all monitors.

    Returns:
        dict with status, file_path, url (if no output_path), dimensions, optional base64
    """
    import asyncio
    import base64
    import io
    import os
    import uuid
    from pathlib import Path

    loop = asyncio.get_event_loop()

    def _capture():
        from PIL import ImageGrab

        if region:
            bbox = (
                region['x'], region['y'],
                region['x'] + region['width'],
                region['y'] + region['height'],
            )
            return ImageGrab.grab(bbox=bbox)

        if monitor == 0:
            return ImageGrab.grab()
        elif monitor < 0:
            return ImageGrab.grab(all_screens=True)
        else:
            bbox = _get_monitor_bbox(monitor)
            if bbox:
                return ImageGrab.grab(bbox=bbox)
            return ImageGrab.grab(all_screens=True)

    def _get_monitor_bbox(idx: int) -> tuple | None:
        """Get bounding box (l,t,r,b) for monitor idx on Windows."""
        try:
            import win32api
            monitors = win32api.EnumDisplayMonitors()
            if 0 < idx <= len(monitors):
                return monitors[idx - 1][2]
            return None
        except ImportError:
            return None

    try:
        screenshot = await loop.run_in_executor(None, _capture)

        if output_path:
            out_dir = os.path.dirname(os.path.abspath(output_path))
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            screenshot.save(output_path)
            return {
                'status': 'success',
                'message': f'Screenshot saved to {output_path}',
                'file_path': str(Path(output_path).resolve()),
                'dimensions': screenshot.size,
            }
        else:
            screenshots_dir = Path('screenshots')
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            filename = f'{uuid.uuid4().hex}.png'
            filepath = screenshots_dir / filename
            screenshot.save(str(filepath))

            buffered = io.BytesIO()
            screenshot.save(buffered, format='PNG')
            img_b64 = base64.b64encode(buffered.getvalue()).decode()

            return {
                'status': 'success',
                'message': f'Screenshot saved to {filepath}',
                'file_path': str(filepath),
                'url': f'/screenshots/{filename}',
                'base64_image': img_b64,
                'dimensions': screenshot.size,
            }

    except Exception as e:
        return {
            'status': 'error',
            'message': f'Failed to capture screenshot: {str(e)}',
            'error': str(e),
        }
'''


STARTER_TOOLS = [
    {
        "name": "create_tool",
        "description": "Create or update a tool in the tools database. Stores executable Python code that the agent can call on subsequent turns. Use this when the user asks you to build a new capability or automate a task.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Tool identifier (e.g. 'check_email', 'summarize_pdf')"
                },
                "description": {
                    "type": "string",
                    "description": "What the tool does (shown to model to describe when to call it)"
                },
                "parameters": {
                    "type": "object",
                    "description": "JSON Schema describing the tool's input parameters"
                },
                "code": {
                    "type": "string",
                    "description": "Full Python async function code. Must contain an async function with the same name as the tool."
                }
            },
            "required": ["name", "description", "parameters", "code"]
        },
        "code": CREATE_TOOL_CODE,
        "status": "active",
    },
    {
        "name": "delete_source",
        "description": "Delete a file or directory from the filesystem. Use recursive=true to delete directories. WARNING: deletions are permanent (not sent to trash).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file or directory to delete"
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Set true to delete directories",
                    "default": False
                }
            },
            "required": ["path"]
        },
        "code": BUILTIN_STUB,
        "status": "active",
    },
    {
        "name": "run_command",
        "description": "Execute any shell command on the server. Returns stdout, stderr, and exit code. Use for system administration, git operations, package management, etc.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                    "default": 30
                }
            },
            "required": ["command"]
        },
        "code": BUILTIN_STUB,
        "status": "active",
    },
    {
        "name": "write_source",
        "description": "Create a new file or overwrite an existing one with the given content. Validates Python syntax for .py files. Backs up existing files automatically. Use this to create new files (docs, configs, scripts, HTML pages, etc.). After writing .py files, call restart_server() to apply changes.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file, relative to project root (e.g. 'docs/api.md', 'scripts/deploy.py')"
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write to the file"
                }
            },
            "required": ["path", "content"]
        },
        "code": BUILTIN_STUB,
        "status": "active",
    },
    {
        "name": "read_source",
        "description": "Read any project source file. Path can be relative (e.g. 'app/agent/loop.py') or absolute. Use to inspect current code before making changes.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file, relative to project root (e.g. 'app/agent/loop.py')"
                }
            },
            "required": ["path"]
        },
        "code": BUILTIN_STUB,
        "status": "active",
    },
    {
        "name": "edit_source",
        "description": "Edit a project source file by replacing exact old_text with new_text. Creates a backup automatically. Validates Python syntax before saving. After editing .py files, call restart_server() to apply changes.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file, relative to project root (e.g. 'app/agent/loop.py')"
                },
                "old_text": {
                    "type": "string",
                    "description": "Exact text to find and replace (must match exactly including whitespace)"
                },
                "new_text": {
                    "type": "string",
                    "description": "Replacement text"
                }
            },
            "required": ["path", "old_text", "new_text"]
        },
        "code": BUILTIN_STUB,
        "status": "active",
    },
    {
        "name": "restart_server",
        "description": "Restart the Python agent server. Call this after editing .py files to apply changes. The server will be down for ~1-2 seconds while webAgent.bat restarts it.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        },
        "code": BUILTIN_STUB,
        "status": "active",
    },
    {
        "name": "web_search",
        "description": "Search the web using DuckDuckGo. Returns titled search results with snippets and URLs. Use for finding current information, documentation, news, or any web content. No API key required.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query (what to search for)"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default 5, max 10)",
                    "default": 5
                }
            },
            "required": ["query"]
        },
        "code": WEB_SEARCH_CODE,
        "status": "active",
    },
    {
        "name": "browser_action",
        "description": "Control a persistent headless Chromium browser. Supports: navigate, click, type, get_text, get_html, screenshot, wait, evaluate, title, url, close. One browser per user+session — consecutive calls share the same page.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["navigate", "click", "type", "get_text", "get_html", "screenshot", "wait", "evaluate", "title", "url", "close"],
                    "description": "The browser action to perform"
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector (for click, type, get_text)"
                },
                "text": {
                    "type": "string",
                    "description": "Text to type (for type action)"
                },
                "url": {
                    "type": "string",
                    "description": "URL to navigate to (for navigate action)"
                },
                "js": {
                    "type": "string",
                    "description": "JavaScript code (for evaluate action)"
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "Wait timeout in ms (for wait action)",
                    "default": 5000
                },
                "full_page": {
                    "type": "boolean",
                    "description": "Capture full scrollable page (for screenshot)",
                    "default": True
                }
            },
            "required": ["action"]
        },
        "code": BROWSER_ACTION_CODE,
        "status": "active",
    },
    {
        "name": "take_screenshot",
        "description": "Capture a screenshot of the screen. Supports primary monitor (default), specific monitor index, or all monitors. Optionally capture a region or save to a custom file path. Returns file path, dimensions, optional base64 encoded image, and URL if saved to the default screenshots/ directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "output_path": {
                    "type": "string",
                    "description": "Optional path to save screenshot (e.g. 'screenshot.png'). If omitted, saved to screenshots/ dir and returns URL + base64."
                },
                "region": {
                    "type": "object",
                    "description": "Optional region dict with {x, y, width, height} to capture"
                },
                "monitor": {
                    "type": "integer",
                    "description": "Monitor index (0=primary, 1=secondary, ...). Use -1 for all monitors.",
                    "default": 0
                }
            },
            "required": []
        },
        "code": TAKE_SCREENSHOT_CODE,
        "status": "active",
    },
]


# ── Seed logic ────────────────────────────────────────────────────────────────

async def seed():
    db = get_db()
    client = db.get_raw_client()

    print("Seeding starter tools...\n")

    for tool in STARTER_TOOLS:
        name = tool["name"]

        # Check if tool already exists
        existing = (
            client.table("tools")
            .select("id")
            .eq("name", name)
            .eq("status", "active")
            .limit(1)
            .execute()
        )

        if existing.data:
            # Update in place
            tool_id = existing.data[0]["id"]
            client.table("tools").update({
                "code": tool["code"],
                "description": tool["description"],
                "parameters": json.dumps(tool["parameters"]),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", tool_id).execute()
            print(f"  Updated: {name}")
        else:
            # Insert new
            row = {
                "name": name,
                "code": tool["code"],
                "description": tool["description"],
                "parameters": json.dumps(tool["parameters"]),
                "language": "python",
                "status": tool.get("status", "active"),
                "created_by": "__system__",
            }
            client.table("tools").insert(row).execute()
            print(f"  Created: {name}")

    print("\nDone! Starter tools are ready.")
    print("  - create_tool    : create new tools dynamically")
    print("  - web_search     : search the web via DuckDuckGo")
    print("  - read_source    : read any file")
    print("  - write_source   : create/overwrite any file")
    print("  - edit_source    : find-replace in any file")
    print("  - delete_source  : delete any file/directory")
    print("  - run_command    : execute any shell command")
    print("  - restart_server : restart the agent server")
    print("  - take_screenshot : capture screen (monitor, region, or full)")


if __name__ == "__main__":
    asyncio.run(seed())
