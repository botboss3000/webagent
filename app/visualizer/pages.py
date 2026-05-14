"""
Page management for the AutoAgent multi-page workspace.

Each user has their own page directory:
  visuals/users/<user_id>/
    pages.json          ← manifest: [{slug, title, agent_context, created_at, updated_at}]
    home.html           ← default intro/onboarding page
    <slug>.html         ← user-created pages

Pages are stored per-user so they persist across sessions.
The home page is auto-seeded on first access and cannot be deleted.
"""
import json
import os
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

VISUALS_DIR = "visuals"
USERS_DIR = os.path.join(VISUALS_DIR, "users")


# ── Path helpers ──────────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    """Sanitize a string for safe use as a directory / file name segment."""
    return re.sub(r"[^\w\-]", "_", name)


def _user_dir(user_id: str) -> str:
    return os.path.join(USERS_DIR, _safe(user_id))


def _manifest_path(user_id: str) -> str:
    return os.path.join(_user_dir(user_id), "pages.json")


def _page_path(user_id: str, slug: str) -> str:
    return os.path.join(_user_dir(user_id), f"{_safe(slug)}.html")


def _page_url(user_id: str, slug: str) -> str:
    return f"/visuals/users/{_safe(user_id)}/{_safe(slug)}.html"


def _ensure_user_dir(user_id: str) -> str:
    d = _user_dir(user_id)
    os.makedirs(d, exist_ok=True)
    return d


# ── Manifest R/W ──────────────────────────────────────────────────────────────

def load_manifest(user_id: str) -> List[Dict]:
    path = _manifest_path(user_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_manifest(user_id: str, pages: List[Dict]) -> None:
    _ensure_user_dir(user_id)
    with open(_manifest_path(user_id), "w", encoding="utf-8") as f:
        json.dump(pages, f, indent=2)


# ── Public API ────────────────────────────────────────────────────────────────

def list_pages(user_id: str) -> List[Dict]:
    """Return all pages for a user, seeding home if missing."""
    ensure_home_page(user_id)
    pages = load_manifest(user_id)
    # Attach URL to each entry for the frontend
    return [dict(p, url=_page_url(user_id, p["slug"])) for p in pages]


def get_page_html(user_id: str, slug: str) -> Optional[str]:
    path = _page_path(user_id, slug)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def save_page_html(user_id: str, slug: str, html: str) -> str:
    """Write HTML for a page and bump its updated_at. Returns the URL path."""
    _ensure_user_dir(user_id)
    with open(_page_path(user_id, slug), "w", encoding="utf-8") as f:
        f.write(html)
    # Bump updated_at in manifest
    pages = load_manifest(user_id)
    now = datetime.now(timezone.utc).isoformat()
    for page in pages:
        if page["slug"] == slug:
            page["updated_at"] = now
            break
    save_manifest(user_id, pages)
    return _page_url(user_id, slug)


def create_page(
    user_id: str,
    slug: str,
    title: str,
    agent_context: str = "",
    initial_html: str = "",
) -> Dict:
    """Create a new page entry and seed its HTML file. Returns the manifest entry."""
    pages = load_manifest(user_id)
    safe_slug = _safe(slug)
    if any(p["slug"] == safe_slug for p in pages):
        raise ValueError(f"Page '{safe_slug}' already exists")
    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "slug": safe_slug,
        "title": title,
        "agent_context": agent_context or _default_agent_context(title),
        "created_at": now,
        "updated_at": now,
    }
    pages.append(entry)
    save_manifest(user_id, pages)
    html = initial_html or _blank_page_html(title)
    save_page_html(user_id, safe_slug, html)
    return dict(entry, url=_page_url(user_id, safe_slug))


def delete_page(user_id: str, slug: str) -> bool:
    """Delete a page. Returns False if slug is 'home' or not found."""
    safe_slug = _safe(slug)
    if safe_slug == "home":
        return False
    pages = load_manifest(user_id)
    new_pages = [p for p in pages if p["slug"] != safe_slug]
    if len(new_pages) == len(pages):
        return False
    save_manifest(user_id, new_pages)
    path = _page_path(user_id, safe_slug)
    if os.path.exists(path):
        os.remove(path)
    return True


def ensure_home_page(user_id: str) -> None:
    """Seed the home page if it doesn't exist yet."""
    pages = load_manifest(user_id)
    if any(p["slug"] == "home" for p in pages):
        return
    now = datetime.now(timezone.utc).isoformat()
    entry = {
        "slug": "home",
        "title": "Home",
        "agent_context": (
            "You are the webAgent home page agent. Your role is to maintain this "
            "informational page about webAgent — its features, getting started guide, "
            "and use cases. When asked to update or modify this page, produce clean, "
            "well-structured HTML that matches the dark webAgent aesthetic. The page "
            "serves as the main welcome and onboarding resource for users of the app."
        ),
        "created_at": now,
        "updated_at": now,
    }
    # Home page always goes first
    save_manifest(user_id, [entry] + [p for p in pages if p["slug"] != "home"])
    if not os.path.exists(_page_path(user_id, "home")):
        _ensure_user_dir(user_id)
        with open(_page_path(user_id, "home"), "w", encoding="utf-8") as f:
            f.write(_home_page_html())


# ── HTML templates ─────────────────────────────────────────────────────────────

def _default_agent_context(title: str) -> str:
    return (
        f"You are the {title} page agent. Your role is to build and maintain this "
        f"page called '{title}'. When asked to create or update content, produce clean, "
        f"well-designed HTML that matches the dark webAgent aesthetic "
        f"(#0a0a0f background, #c0caf5 text, #7aa2f7 accents). Render functional, "
        f"interactive pages tailored to the purpose of '{title}'."
    )


def _blank_page_html(title: str) -> str:
    escaped = title.replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escaped}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; height: 100%; background: #0a0a0f; color: #c0caf5;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
  .empty-state {{ display: flex; flex-direction: column; align-items: center;
    justify-content: center; height: 100vh; gap: 12px; color: #565f89; }}
  .empty-state h2 {{ color: #7aa2f7; font-size: 22px; margin: 0; }}
  .empty-state p {{ font-size: 14px; margin: 0; }}
</style>
</head>
<body>
<div class="empty-state">
  <h2>{escaped}</h2>
  <p>This page is empty — send a prompt to build it.</p>
</div>
</body>
</html>"""


def _home_page_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>webAgent — Home</title>
<style>
*, *::before, *::after { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { margin: 0; padding: 0; background: #0a0a0f; color: #c0caf5;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  font-size: 15px; line-height: 1.6; }
a { color: #7dcfff; text-decoration: none; }
a:hover { text-decoration: underline; }
.container { max-width: 860px; margin: 0 auto; padding: 0 24px; }

/* Header */
header { background: linear-gradient(135deg, #0d0d1e 0%, #160a28 100%);
  border-bottom: 1px solid #1e1e3a; padding: 44px 0 36px; text-align: center; }
.header-badge { display: inline-block; background: rgba(122,162,247,0.1);
  border: 1px solid rgba(122,162,247,0.22); border-radius: 20px; padding: 4px 14px;
  font-size: 11px; color: #7aa2f7; letter-spacing: 0.08em; text-transform: uppercase;
  margin-bottom: 16px; }
header h1 { margin: 0 0 12px; font-size: 44px; font-weight: 700;
  background: linear-gradient(135deg, #7dcfff 0%, #7aa2f7 50%, #bb9af7 100%);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; }
header p { margin: 0 auto; font-size: 16px; color: #a9b1d6; max-width: 500px; }

/* Sections */
section { padding: 40px 0; border-bottom: 1px solid #1e1e3a; }
section:last-of-type { border-bottom: none; }
h2 { font-size: 20px; font-weight: 600; color: #7aa2f7; margin: 0 0 20px;
  display: flex; align-items: center; gap: 10px; }

/* Feature grid */
.feature-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 14px; }
.feature-card { background: #0d0d1e; border: 1px solid #1e1e3a; border-radius: 10px;
  padding: 18px 16px; transition: border-color 0.2s, transform 0.15s; }
.feature-card:hover { border-color: #2d2d5a; transform: translateY(-2px); }
.feature-card .fc-icon { font-size: 22px; margin-bottom: 10px; }
.feature-card h3 { margin: 0 0 5px; font-size: 13px; font-weight: 600; color: #e0def4; }
.feature-card p { margin: 0; font-size: 12px; color: #6272a4; line-height: 1.5; }

/* Steps */
.steps { display: flex; flex-direction: column; gap: 14px; }
.step { display: flex; gap: 14px; align-items: flex-start; }
.step-num { width: 28px; height: 28px; border-radius: 50%;
  background: linear-gradient(135deg, #7aa2f7, #bb9af7); color: #0a0a0f;
  font-size: 12px; font-weight: 700; display: flex; align-items: center;
  justify-content: center; flex-shrink: 0; margin-top: 2px; }
.step-body h4 { margin: 0 0 3px; font-size: 14px; color: #e0def4; }
.step-body p { margin: 0; font-size: 13px; color: #6272a4; }
code { background: #1a1a2e; border: 1px solid #2a2a4a; border-radius: 4px;
  padding: 1px 6px; font-size: 11.5px; font-family: 'Fira Code', monospace; color: #7dcfff; }

/* Use cases */
.use-cases { display: grid; grid-template-columns: repeat(auto-fill, minmax(175px, 1fr)); gap: 10px; }
.use-case { background: rgba(122,162,247,0.05); border: 1px solid rgba(122,162,247,0.13);
  border-radius: 8px; padding: 12px 14px; font-size: 12px; color: #a9b1d6; }
.use-case strong { display: block; color: #e0def4; margin-bottom: 3px; font-size: 13px; }

/* Tips */
.tip-list { display: flex; flex-direction: column; gap: 8px; }
.tip { display: flex; gap: 10px; font-size: 13px; color: #a9b1d6; align-items: baseline; }
.tip-arrow { color: #7dcfff; flex-shrink: 0; }

/* Footer */
footer { text-align: center; padding: 24px 0; font-size: 12px; color: #3b3f66;
  border-top: 1px solid #1e1e3a; }
</style>
</head>
<body>

<header>
  <div class="container">
    <div class="header-badge">AI Agent Platform</div>
    <h1>webAgent</h1>
    <p>A tool-calling AI agent with memory, persistent sessions, and a live page workspace — running locally or on the cloud.</p>
  </div>
</header>

<main class="container">

  <section>
    <h2>⚡ Core Features</h2>
    <div class="feature-grid">
      <div class="feature-card">
        <div class="fc-icon">💬</div>
        <h3>Persistent Chat</h3>
        <p>Conversations persist across sessions. The agent remembers context from prior turns automatically.</p>
      </div>
      <div class="feature-card">
        <div class="fc-icon">🧠</div>
        <h3>Hybrid Memory</h3>
        <p>Keyword + vector search surfaces relevant past knowledge before every response.</p>
      </div>
      <div class="feature-card">
        <div class="fc-icon">🔧</div>
        <h3>Dynamic Tools</h3>
        <p>Tools are created on demand and stored in the DB. Bootstrap discovery runs from turn 1.</p>
      </div>
      <div class="feature-card">
        <div class="fc-icon">🎨</div>
        <h3>AutoAgent Pages</h3>
        <p>Build and maintain custom HTML pages — dashboards, notes, visuals — right in this tab.</p>
      </div>
      <div class="feature-card">
        <div class="fc-icon">📎</div>
        <h3>File Attachments</h3>
        <p>Upload images, audio, video, and documents. The agent reads and processes them inline.</p>
      </div>
      <div class="feature-card">
        <div class="fc-icon">🗄️</div>
        <h3>Dual Storage</h3>
        <p>Switch between local SQLite and Supabase cloud from the settings toggle at any time.</p>
      </div>
    </div>
  </section>

  <section>
    <h2>🚀 Getting Started</h2>
    <div class="steps">
      <div class="step">
        <div class="step-num">1</div>
        <div class="step-body">
          <h4>Install dependencies</h4>
          <p>Create a virtual environment and run <code>pip install -r requirements.txt</code></p>
        </div>
      </div>
      <div class="step">
        <div class="step-num">2</div>
        <div class="step-body">
          <h4>Configure your API key</h4>
          <p>Copy <code>.env.example</code> to <code>.env</code> and set <code>OPENROUTER_API_KEY</code>. Or use the ⚙️ Settings modal in the UI.</p>
        </div>
      </div>
      <div class="step">
        <div class="step-num">3</div>
        <div class="step-body">
          <h4>Start the server</h4>
          <p>Run <code>uvicorn app.main:app --reload --port 8080</code> or double-click <code>webAgent.bat</code> on Windows.</p>
        </div>
      </div>
      <div class="step">
        <div class="step-num">4</div>
        <div class="step-body">
          <h4>Open the UI &amp; chat</h4>
          <p>Go to <code>http://localhost:8080</code>. Your first message seeds your agent context automatically.</p>
        </div>
      </div>
    </div>
  </section>

  <section>
    <h2>💡 Use Cases</h2>
    <div class="use-cases">
      <div class="use-case"><strong>Research Assistant</strong>Web search + memory to build persistent knowledge bases.</div>
      <div class="use-case"><strong>Personal Dashboard</strong>Ask the agent to build a live data dashboard on a custom page.</div>
      <div class="use-case"><strong>Automation Hub</strong>Register webhooks, create tools, and automate external workflows.</div>
      <div class="use-case"><strong>Code Assistant</strong>Use the admin agent for privileged file editing and shell access.</div>
      <div class="use-case"><strong>Notes &amp; Docs</strong>Build a notes page — the agent maintains and searches your notes.</div>
      <div class="use-case"><strong>Creative Coding</strong>Generate p5.js generative art, animations, and interactive sketches.</div>
    </div>
  </section>

  <section>
    <h2>✨ Tips</h2>
    <div class="tip-list">
      <div class="tip"><span class="tip-arrow">→</span><span>Use <strong>AutoAgent pages</strong> (this tab) to build persistent custom interfaces. Click <strong>+</strong> to add a new page.</span></div>
      <div class="tip"><span class="tip-arrow">→</span><span>Each page has its own dedicated agent — the home agent knows about webAgent, a dashboard agent knows about your dashboards.</span></div>
      <div class="tip"><span class="tip-arrow">→</span><span>The agent discovers tools automatically via <code>list_tools</code> — no need to tell it what's available.</span></div>
      <div class="tip"><span class="tip-arrow">→</span><span>Toggle <strong>Local / Cloud</strong> storage in the top bar to switch SQLite ↔ Supabase without losing data.</span></div>
      <div class="tip"><span class="tip-arrow">→</span><span>Say <em>"add a section about X"</em> or <em>"redesign the layout"</em> and the agent will update this page directly.</span></div>
    </div>
  </section>

</main>

<footer>
  <div class="container">
    webAgent &nbsp;·&nbsp; Built with FastAPI &amp; OpenRouter &nbsp;·&nbsp;
    <a href="/docs">API Docs</a> &nbsp;·&nbsp; <a href="/test">Test Interface</a>
  </div>
</footer>

</body>
</html>"""
