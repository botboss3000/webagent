"""Helpers shared by all CanvasStore implementations."""

import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app import user_workspace as _ws

logger = logging.getLogger(__name__)

# Per-process guard so the one-time legacy migrations run at most once per user.
_migrated: set = set()
_manifest_migrated: set = set()

# Per-canvas descriptor file, mirroring the main-panel page convention
# (ui/<page>/page.json — see app/ui_pages/__init__.py). Optional: a folder with
# an index.html is already a valid canvas; the descriptor only adds ordering and
# metadata (title, agent_context, description, timestamps).
CANVAS_META_FILE = "page.json"

# Per-canvas DATA file — the canvas's content (records the page renders: rosters,
# schedules, rows…) kept SEPARATE from index.html so the agent updates data
# without rewriting the page markup, and the page reads it instead of hardcoding
# it. Optional: a canvas with no data.json just has no injected data. Sibling to
# index.html / page.json in the canvas folder. (Shared cross-canvas datastores
# are a later phase; this is the per-canvas file.)
CANVAS_DATA_FILE = "data.json"

# A canvas with no `order` in its descriptor sorts after all ordered canvases.
DEFAULT_ORDER = 100


def safe(name: str) -> str:
    """Sanitize a string for safe use as a directory / file / slug segment."""
    return re.sub(r"[^\w\-]", "_", name)


def user_canvases_dir(user_id: str) -> str:
    """On-disk dir holding a user's canvases.

    Lives in the user's data home: ``data/user_data/<user_id>/canvas/`` (see
    app/user_workspace.py). Created if missing. Any legacy ``pages/`` layout is
    migrated into the new ``canvas/`` folder-per-canvas layout on first access.
    A second one-time migration converts any old root ``canvases.json`` manifest
    into per-canvas ``page.json`` descriptors."""
    _migrate_legacy_pages(user_id)
    _migrate_manifest_to_meta(user_id)
    return str(_ws.user_dir(user_id, "canvas"))


def canvas_dir(user_id: str, slug: str) -> str:
    """Per-canvas folder: ``data/user_data/<user_id>/canvas/<slug>/``.

    Each canvas owns its own directory so it can grow to hold assets/versions
    later without colliding with siblings."""
    return os.path.join(user_canvases_dir(user_id), safe(slug))


def canvas_body_path(user_id: str, slug: str) -> str:
    """The HTML body file for one canvas: ``<canvas dir>/<slug>/index.html``."""
    return os.path.join(canvas_dir(user_id, slug), "index.html")


def canvas_meta_path(user_id: str, slug: str) -> str:
    """The optional descriptor file for one canvas: ``<canvas dir>/<slug>/page.json``.

    Holds the canvas's title, agent_context, description, display ``order`` and
    timestamps. Entirely optional — a folder with just an index.html is a valid
    canvas (title defaults to the slug, no description, sorted alphabetically
    after the explicitly-ordered canvases)."""
    return os.path.join(canvas_dir(user_id, slug), CANVAS_META_FILE)


def read_canvas_meta(user_id: str, slug: str) -> Dict:
    """Read a canvas's page.json descriptor, or ``{}`` when absent/unreadable."""
    path = canvas_meta_path(user_id, slug)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_canvas_meta(user_id: str, slug: str, meta: Dict) -> None:
    """Write (create or overwrite) a canvas's page.json descriptor."""
    os.makedirs(canvas_dir(user_id, slug), exist_ok=True)
    with open(canvas_meta_path(user_id, slug), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def canvas_data_path(user_id: str, slug: str) -> str:
    """The optional data file for one canvas: ``<canvas dir>/<slug>/data.json``.

    Holds the canvas's CONTENT (the records the page displays) as a JSON object,
    kept out of index.html so the agent edits data without touching the page."""
    return os.path.join(canvas_dir(user_id, slug), CANVAS_DATA_FILE)


def read_canvas_data(user_id: str, slug: str) -> Optional[Dict]:
    """Read a canvas's data.json, or ``None`` when absent/unreadable/not an object."""
    path = canvas_data_path(user_id, slug)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_canvas_data(user_id: str, slug: str, data: Dict) -> None:
    """Write (create or overwrite) a canvas's data.json content file."""
    os.makedirs(canvas_dir(user_id, slug), exist_ok=True)
    with open(canvas_data_path(user_id, slug), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Per-canvas CONSOLE LOG ─────────────────────────────────────────────────────
# A canvas's own console output (console.log/warn/error + uncaught script errors),
# captured in the browser as the page runs and POSTed back here, kept in a plain
# file BESIDE index.html — NOT in the global logs.db. This gives the design agent a
# page-scoped debug log it can read with no codebase-admin / logs.db access: just
# the logs of the one canvas it built. Stored as JSON-lines (one record per line:
# {ts, level, text, stack?}) so appends are cheap and the file stays human-readable.
# Filesystem-local on purpose (canvas is a local-single-user feature; see the
# canvas.js local-only gate) — it lives in the same canvas folder as data.json.
CANVAS_LOG_FILE = "console.log"

# Hard cap on retained lines so a chatty canvas (an interval logging every tick)
# can't grow the file without bound — we keep only the most recent N.
CANVAS_LOG_MAX_LINES = 500


def canvas_log_path(user_id: str, slug: str) -> str:
    """The per-canvas console-log file: ``<canvas dir>/<slug>/console.log`` (JSONL)."""
    return os.path.join(canvas_dir(user_id, slug), CANVAS_LOG_FILE)


def append_canvas_logs(user_id: str, slug: str, entries: List[Dict],
                       replace_source: Optional[str] = None) -> int:
    """Append console entries to a canvas's log file, trimming to the most recent
    ``CANVAS_LOG_MAX_LINES``. Each entry is normalised + size-capped so a runaway
    canvas can't write a huge file. Returns the number of lines kept.

    Entries may carry a ``source`` tag (e.g. 'headless' for the screenshot_canvas
    render vs the default live-browser capture). ``replace_source`` first DROPS any
    existing lines with that source before appending — so a headless re-verify
    refreshes its own entries without wiping the live-session ones (and a clean
    render with no output still clears stale headless errors). Best-effort: bad
    entries are skipped and the whole call is wrapped so logging never bubbles up."""
    if not isinstance(entries, list):
        entries = []
    # Nothing to write and nothing to clear → no-op.
    if not entries and not replace_source:
        return 0
    new_lines: List[str] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        rec = {
            "ts": e.get("ts") or now_iso(),
            "level": str(e.get("level") or "log")[:12],
            "text": str(e.get("text") or "")[:4000],
        }
        stack = e.get("stack")
        if stack:
            rec["stack"] = str(stack)[:8000]
        src = e.get("source")
        if src:
            rec["source"] = str(src)[:40]
        new_lines.append(json.dumps(rec, ensure_ascii=False))
    try:
        os.makedirs(canvas_dir(user_id, slug), exist_ok=True)
        path = canvas_log_path(user_id, slug)
        existing: List[str] = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    existing = [ln for ln in f.read().splitlines() if ln.strip()]
            except Exception:
                existing = []
        if replace_source:
            kept_existing: List[str] = []
            for ln in existing:
                try:
                    r = json.loads(ln)
                    if isinstance(r, dict) and r.get("source") == replace_source:
                        continue  # drop the prior entries from this source
                except Exception:
                    pass  # keep unparseable lines as-is
                kept_existing.append(ln)
            existing = kept_existing
        kept = (existing + new_lines)[-CANVAS_LOG_MAX_LINES:]
        with open(path, "w", encoding="utf-8") as f:
            f.write(("\n".join(kept) + "\n") if kept else "")
        return len(kept)
    except Exception as ex:
        logger.warning("append_canvas_logs failed for %s/%s: %s", user_id, slug, ex)
        return 0


def read_canvas_logs(user_id: str, slug: str, limit: int = 100,
                     level: Optional[str] = None) -> List[Dict]:
    """Return a canvas's most recent console entries (newest last), optionally
    filtered to one level ('error'/'warn'/'log'/'info'/'debug'). Empty list when
    the canvas has no log file yet."""
    path = canvas_log_path(user_id, slug)
    if not os.path.exists(path):
        return []
    out: List[Dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                if level and str(rec.get("level") or "").lower() != level.lower():
                    continue
                out.append(rec)
    except Exception as ex:
        logger.warning("read_canvas_logs failed for %s/%s: %s", user_id, slug, ex)
        return []
    if limit and limit > 0:
        out = out[-limit:]
    return out


def clear_canvas_logs(user_id: str, slug: str) -> None:
    """Delete a canvas's console-log file (best-effort). Called when the page is
    re-rendered so the log only ever reflects the version now running."""
    path = canvas_log_path(user_id, slug)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as ex:
        logger.warning("clear_canvas_logs failed for %s/%s: %s", user_id, slug, ex)


def discover_canvas_slugs(user_id: str) -> List[str]:
    """Folder names under canvas/ that contain an index.html — i.e. every canvas,
    descriptor or not. This is the source of truth for which canvases exist."""
    root = user_canvases_dir(user_id)
    slugs: List[str] = []
    try:
        for name in os.listdir(root):
            folder = os.path.join(root, name)
            if not os.path.isdir(folder) or name.startswith((".", "_")):
                continue
            if os.path.exists(os.path.join(folder, "index.html")):
                slugs.append(name)
    except FileNotFoundError:
        pass
    return slugs


def canvas_entry(user_id: str, slug: str) -> Dict:
    """Build a catalog entry for one canvas from its (optional) descriptor.

    Defaults when no/partial page.json: title → slug, description → "",
    agent_context → generated default, order → DEFAULT_ORDER (sorts last,
    alphabetically), timestamps → None."""
    meta = read_canvas_meta(user_id, slug)
    title = meta.get("title") or slug
    return {
        "slug": slug,
        "title": title,
        "description": meta.get("description") or "",
        "agent_context": meta.get("agent_context") or default_agent_context(title),
        # Owning agent (the agent that created/manages this canvas), or "" when the
        # canvas hasn't been rendered by an agent yet — the footer then falls back
        # to the default webAgent.
        "agent_id": meta.get("agent_id") or "",
        "order": meta.get("order", DEFAULT_ORDER),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "url": canvas_url(user_id, slug),
    }


def sort_canvas_entries(entries: List[Dict]) -> List[Dict]:
    """Home always first; then explicitly-ordered canvases by ``order``; then
    the rest alphabetically by title. Mirrors the main-panel page sort."""
    def key(e: Dict):
        is_home = 0 if e.get("slug") == "home" else 1
        order = e.get("order")
        order = order if isinstance(order, (int, float)) else DEFAULT_ORDER
        return (is_home, order, (e.get("title") or e.get("slug") or "").lower())
    return sorted(entries, key=key)


def _migrate_manifest_to_meta(user_id: str) -> None:
    """One-time, non-destructive migration from the old root ``canvases.json``
    manifest to a per-canvas ``page.json`` descriptor in each canvas folder.

    For each manifest entry whose folder lacks a page.json, write one carrying
    its title/agent_context/timestamps and an ``order`` equal to its position in
    the manifest (so the existing dropdown arrangement is preserved). The old
    manifest is then removed. Runs at most once per user per process; a no-op
    when there is no manifest (already migrated, or a non-filesystem backend)."""
    if user_id in _manifest_migrated:
        return
    _manifest_migrated.add(user_id)
    try:
        root = _ws.user_dir(user_id, "canvas")
        manifest = os.path.join(str(root), "canvases.json")
        if not os.path.exists(manifest):
            return
        with open(manifest, "r", encoding="utf-8") as f:
            pages = json.load(f)
        if not isinstance(pages, list):
            os.remove(manifest)
            return
        for idx, page in enumerate(pages):
            slug = safe(str(page.get("slug") or "")) if isinstance(page, dict) else ""
            if not slug:
                continue
            folder = os.path.join(str(root), slug)
            meta_path = os.path.join(folder, CANVAS_META_FILE)
            # Only seed folders that exist and aren't already described.
            if not os.path.isdir(folder) or os.path.exists(meta_path):
                continue
            meta = {
                "title": page.get("title") or slug,
                "agent_context": page.get("agent_context") or "",
                "order": idx,
                "created_at": page.get("created_at"),
                "updated_at": page.get("updated_at"),
            }
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        os.remove(manifest)
        logger.info("Migrated canvas manifest for user %s: canvases.json -> per-folder page.json", user_id)
    except Exception as e:
        logger.warning("Canvas manifest migration skipped for user %s: %s", user_id, e)


def _migrate_legacy_pages(user_id: str) -> None:
    """Move a user's old flat ``pages/<slug>.html`` layout into the new
    folder-per-canvas ``canvas/<slug>/index.html`` layout.

    Idempotent and non-destructive: only moves a body when its new home does
    not already exist; runs at most once per user per process. Database-mode
    users have no ``pages/`` dir, so this is a no-op for them."""
    if user_id in _migrated:
        return
    _migrated.add(user_id)
    try:
        home = _ws.user_home(user_id)
        old = home / "pages"
        if not old.is_dir():
            return
        new = home / "canvas"
        new.mkdir(parents=True, exist_ok=True)
        # Carry any legacy manifest across; _migrate_manifest_to_meta (run right
        # after this) converts it into per-folder page.json descriptors.
        old_manifest = old / "canvases.json"
        new_manifest = new / "canvases.json"
        if old_manifest.exists() and not new_manifest.exists():
            shutil.move(str(old_manifest), str(new_manifest))
        # Each canvas body: pages/<slug>.html -> canvas/<slug>/index.html
        for html in old.glob("*.html"):
            dest_dir = new / safe(html.stem)
            dest = dest_dir / "index.html"
            if dest.exists():
                continue
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(html), str(dest))
        logger.info("Migrated canvas data for user %s: pages/ -> canvas/", user_id)
    except Exception as e:
        # Don't let a migration hiccup break canvas reads/writes.
        logger.warning("Canvas data migration skipped for user %s: %s", user_id, e)


def canvas_url(user_id: str, slug: str) -> str:
    """The URL the iframe loads to render a page.

    Backend-agnostic: served by app/api/canvases.py:GET /api/v1/canvases/{uid}/{slug}/html
    which dispatches through the active CanvasStore."""
    return f"/api/v1/canvases/{safe(user_id)}/{safe(slug)}/html"


def default_agent_context(title: str) -> str:
    return (
        f"You are the {title} canvas agent. Your role is to build and maintain this "
        f"canvas called '{title}'. When asked to create or update content, produce "
        f"clean, well-designed HTML that matches the webAgent look in both dark and "
        f"light themes and on desktop and mobile (follow the Visualizer ability "
        f"skill). Render functional, interactive canvases tailored to the purpose "
        f"of '{title}'."
    )


def home_agent_context() -> str:
    return (
        "You are the webAgent home canvas agent. Your role is to maintain this "
        "informational canvas about webAgent — its features, getting started guide, "
        "and use cases. When asked to update or modify this canvas, produce clean, "
        "well-structured HTML that matches the webAgent look in both dark and light "
        "themes (follow the Visualizer ability skill). The canvas serves as the "
        "main welcome and onboarding resource for users of the app."
    )


def blank_canvas_html(title: str) -> str:
    """The BASE canvas — the minimal, correct skeleton every new/empty canvas starts
    from (and the structure an agent extends). It deliberately models the four rules
    of the first-class-canvas contract so the common bugs can't be copied from it:
    consume app tokens (no own palette), one uniquely-named ~20px wrapper (never a
    second ``canvas-root``), query the shadow ``root`` (never ``document.*``), and do
    all setup INSIDE ``WebagentCanvas.register`` (not at script top-level)."""
    escaped = title.replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escaped}</title>
<!--
  BASE CANVAS — minimal correct skeleton. Follow these and the usual bugs can't happen:
   1. CONSUME the app's design tokens (var(--fg-1), var(--bg-elev), var(--accent),
      var(--border), var(--shadow-rest), var(--font-sans)…). Do NOT define your own
      :host/:root palette — the app's tokens inherit in and flip light/dark for free.
   2. ONE uniquely-named wrapper (#cv-root) with ~20px padding. Never give your own
      wrapper class="canvas-root" — the app already wraps you in one, so duplicating it
      DOUBLES padding/margins.
   3. Query your DOM through the shadow `root` the mount hands you — NEVER document.*
      (your markup lives in a shadow root; document.getElementById returns null).
   4. Do ALL setup INSIDE WebagentCanvas.register(...) using that root — not at the top
      of the script (the shadow root doesn't exist yet when top-level code runs).
-->
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  /* #cv-root is OUR wrapper (uniquely named, not .canvas-root). 20px = the app's gutter. */
  #cv-root {{ padding: 20px; min-height: 100vh; font-family: var(--font-sans, system-ui);
    color: var(--fg-1, #c0caf5); background: transparent; }}
  .cv-empty {{ display: flex; flex-direction: column; align-items: center; justify-content: center;
    min-height: calc(100vh - 40px); gap: 10px; text-align: center; padding: 40px;
    border: var(--border-width, 1px) solid var(--border, rgba(255,255,255,.08));
    border-radius: 16px; background: var(--bg-elev, rgba(255,255,255,.03));
    box-shadow: var(--shadow-rest, 0 2px 10px rgba(0,0,0,.2)); }}
  .cv-empty h2 {{ margin: 0; font-size: 20px; color: var(--accent, #7dcfff); }}
  .cv-empty p {{ margin: 0; font-size: 14px; color: var(--fg-3, #565f89); }}
</style>
</head>
<body>
<div id="cv-root">
  <div class="cv-empty">
    <h2>{escaped}</h2>
    <p>This canvas is empty — send a prompt to build it.</p>
  </div>
</div>
<script>
(function () {{
  // Rule 4: all wiring goes INSIDE register, using the shadow `root` it hands you.
  function mount(root, api) {{
    // const el = root.querySelector('#cv-root');   // Rule 3: query via root, never document.*
    return function cleanup() {{ /* stop any cameras/timers you started */ }};
  }}
  if (window.WebagentCanvas) window.WebagentCanvas.register(mount);
}})();
</script>
</body>
</html>"""
