"""webagent TUI — a standalone, server-independent operator agent.

This is a separate project from the web app and from ``launcher/``: a single
self-contained Textual TUI whose agent talks **directly to the LLM API** (never
through the webAgent server), so it can install, diagnose, repair, and manage a
webAgent checkout even when the server is down.

v1 scope: the serverless agent with **Codebase Admin** + **Source Control**
abilities operating on a target project directory, with its own external
database (kept separate from the web app's ``app/db/local.db``).
"""

__version__ = "0.1.0"
