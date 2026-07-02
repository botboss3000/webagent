"""Admin Dashboard — drop-in BACKEND for /admin/dashboard/*

Serves the movable/resizable metric grid on the Admin Tools → Dashboard view.
Three concerns, all admin-only (via ``resolve_admin_uid``, honours open mode):

  • metrics  — ONE composite snapshot the grid polls. The LIVE parts (CPU, DB
    latency, loop throughput) come from the in-memory recorder (app/metrics.py)
    with ZERO database round-trips — reading DB slowness from the DB would add
    the very load we're diagnosing. The heavier parts (token totals, cost,
    failures, devices) are cheap aggregates cached for a few seconds so a
    dashboard left open doesn't hammer the database. `/chart` feeds the
    full-width "Metrics over time" hero card: a shared time axis with per-model
    cost (stacked bars) + token/CPU/RAM lines, over a window OR a custom date
    range (durable series from usage_events; CPU/RAM live-only via metrics ring).
  • layout   — each admin's card arrangement (PER-USER), persisted to a
    gitignored JSON store so it survives reloads and restarts.
  • ai-card  — turn a plain-English request into a custom card spec, reusing the
    App Settings default-LLM resolver (app/agent/suggestions._resolve_default_llm).

Discovered + mounted by the page catalog (app/ui_pages discover_routers via this
folder's page.json `router` field) — the API comes and goes with the folder, NO
edit to app/main.py. `.py` files under ui/ are never served to the browser.
REMOVE-WHEN: the Dashboard view is dropped from the admin page catalog.

NOTE: no ``from __future__ import annotations`` here — this module is imported
under a synthetic name by the page-catalog loader, and stringised annotations
would leave the Pydantic request models unresolvable at request time (500).
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/dashboard", tags=["admin-dashboard"])

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LAYOUT_FILE = _PROJECT_ROOT / "data" / "config" / "dashboard-layouts.json"

# Composite-snapshot cache: the DB-derived parts are recomputed at most every
# _CACHE_TTL seconds no matter how many admins/tabs poll, so an open dashboard
# adds negligible DB load. The live (in-memory) parts are always fresh.
_CACHE_TTL = 8.0
_cache: Dict[str, Any] = {"at": 0.0, "window": None, "data": None}


async def _require_admin(uid: str) -> None:
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(uid):
        raise HTTPException(status_code=403, detail="Admin required")


# ── time helpers ────────────────────────────────────────────────────────────
def _to_epoch(val: Any) -> float:
    """Parse a stored timestamp (ISO string or epoch number) to epoch seconds.
    Returns 0 on anything unparseable so it falls outside every window."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        pass
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _iso_since(window_s: float) -> str:
    return datetime.fromtimestamp(time.time() - window_s, tz=timezone.utc).isoformat()


def _iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _sql_ts(epoch: float) -> str:
    """A lower-bound string that matches how ``created_at`` is STORED — space
    separated UTC, no ``T`` and no offset (e.g. ``2026-07-01 18:11:25``). This
    matters for the coarse DB-side ``.gte`` pre-filter: stored timestamps look
    like ``2026-07-01 19:02:28`` and comparisons are lexical on SQLite (text) and
    string-cast on Postgres. An ISO bound with a ``T`` (0x54) sorts AFTER a
    space (0x20), so a ``T``-formatted bound silently drops every row on the same
    calendar day — which is exactly why a 5m/1h window came back empty. The
    caller still re-filters precisely by epoch, so this bound only needs to sort
    correctly, not be exact."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── DB-derived aggregate sections (each best-effort, degrade to safe defaults) ─
# The usage_events rows for the window are fetched ONCE per snapshot rebuild
# (_fetch_usage_rows — hybrid-aware) and shared by the tokens / agents /
# sessions / models sections, so adding cards did not add extra 20k-row scans.
def _tokens_section(rows: List[Dict[str, Any]], window_s: float) -> Dict[str, Any]:
    """Token in/out + cost over the window, plus a small per-agent breakdown,
    aggregated from the shared usage_events rows."""
    out = {"in": 0, "out": 0, "cost_usd": 0.0, "by_agent": [], "calls": 0}
    try:
        cutoff = time.time() - window_s
        by_agent: Dict[str, Dict[str, float]] = {}
        for r in rows:
            if _to_epoch(r.get("created_at")) < cutoff:
                continue
            ti = int(r.get("input_tokens") or 0)
            to = int(r.get("output_tokens") or 0)
            cost = float(r.get("cost_usd") or 0.0)
            out["in"] += ti
            out["out"] += to
            out["cost_usd"] += cost
            out["calls"] += 1
            aid = r.get("agent_id") or "—"
            a = by_agent.setdefault(aid, {"in": 0, "out": 0, "cost": 0.0})
            a["in"] += ti
            a["out"] += to
            a["cost"] += cost
        top = sorted(by_agent.items(), key=lambda kv: kv[1]["in"] + kv[1]["out"], reverse=True)[:6]
        out["by_agent"] = [
            {"agent": k, "in": v["in"], "out": v["out"], "cost_usd": round(v["cost"], 4)}
            for k, v in top
        ]
        out["cost_usd"] = round(out["cost_usd"], 4)
    except Exception as e:
        logger.debug("dashboard tokens section failed: %s", e)
    return out


async def _db_health_section() -> Dict[str, Any]:
    """Which backend is live and whether it silently degraded to local SQLite."""
    from app.db import get_connection_health, get_mode, is_remote_db
    from app.db.hybrid import hybrid_enabled
    from app.db.connection_config import load_config
    try:
        health = get_connection_health()
        cfg = load_config()
        prov = cfg.provider
        host = cfg.host or None
    except Exception:
        health = {}
        prov = "unknown"
        host = None
    try:
        hybrid = bool(hybrid_enabled())
    except Exception:
        hybrid = False
    return {
        "provider": prov,
        "host": host,
        "hybrid": hybrid,
        "mode": get_mode(),
        "actual": health.get("actual") or get_mode(),
        "intended": health.get("intended"),
        "degraded": bool(health.get("degraded")),
        "remote": bool(is_remote_db()),
        "ok": bool(health.get("ok", True)),
        "message": health.get("message"),
    }


async def _active_run_rows() -> List[Dict[str, Any]]:
    """All currently-running run-state rows (all users) — the count feeds the
    Active Runs card; the rows feed the Sessions & runs monitor."""
    try:
        from app.db import get_db
        from app.db.offload import db_offload
        db = get_db()
        return await db_offload(lambda: db.run_state_list_active_all()) or []
    except Exception as e:
        logger.debug("dashboard active-runs failed: %s", e)
        return []


async def _devices_section() -> List[Dict[str, Any]]:
    try:
        from app.db import get_db
        from app.db.offload import db_offload
        db = get_db()
        rows = await db_offload(lambda: db.list_devices(120)) or []
        out = []
        for r in rows:
            caps = r.get("capabilities")
            if isinstance(caps, str):
                try:
                    caps = json.loads(caps)
                except Exception:
                    caps = {}
            caps = caps or {}
            last = _to_epoch(r.get("last_seen"))
            out.append({
                "label": r.get("label") or r.get("instance_id") or "device",
                "online": (time.time() - last) < 90 if last else False,
                "platform": caps.get("platform") or caps.get("os") or "—",
                "last_seen_s": int(time.time() - last) if last else None,
            })
        return out
    except Exception as e:
        logger.debug("dashboard devices failed: %s", e)
        return []


async def _failures_section(window_s: float) -> Dict[str, Any]:
    try:
        from app.db import get_db
        from app.db.offload import db_offload
        db = get_db()
        rows = await db_offload(lambda: db.query_diagnostics(
            levels=["error", "critical"], since=_iso_since(window_s), limit=200)) or []
        recent = [{
            "ts": r.get("created_at"),
            "level": r.get("level"),
            "category": r.get("category"),
            "agent": r.get("agent_id"),
            "session": r.get("session_id"),
            "message": (r.get("message") or "")[:160],
        } for r in rows[:12]]
        return {"count": len(rows), "recent": recent}
    except Exception as e:
        logger.debug("dashboard failures failed: %s", e)
        return {"count": 0, "recent": []}


async def _context_section(uid: str) -> Dict[str, Any]:
    """Context window of the app's default model + the default agent's output cap."""
    out = {"model": None, "max_input": None, "max_output": None}
    try:
        from app.agent.suggestions import _resolve_default_llm
        llm = await _resolve_default_llm(uid)
        model = llm.get("model")
        out["model"] = model
        try:
            from app import model_catalog
            info = model_catalog.enrich(model) if model else None
            if info:
                out["max_input"] = info.get("context")
                out["max_output"] = info.get("max_output")
        except Exception:
            pass
    except Exception as e:
        logger.debug("dashboard context failed: %s", e)
    return out


def _storage_section() -> Dict[str, Any]:
    out: Dict[str, Any] = {"files": []}
    data_dir = _PROJECT_ROOT / "data"
    for name in ("local.db", "logs.db", "vault.db", "recordings.db"):
        p = data_dir / name
        try:
            if p.is_file():
                out["files"].append({"name": name, "mb": round(p.stat().st_size / 1e6, 1)})
        except Exception:
            pass
    try:
        import shutil
        usage = shutil.disk_usage(str(_PROJECT_ROOT))
        out["disk_free_gb"] = round(usage.free / 1e9, 1)
        out["disk_total_gb"] = round(usage.total / 1e9, 1)
    except Exception:
        pass
    return out


def _memory_mb() -> Optional[float]:
    try:
        from app.agent.loop import _process_memory_mb
        v = _process_memory_mb()
        return round(v, 1) if v else None
    except Exception:
        return None


# ── operational card sections (agents / users / sessions / health / tools / security) ─
def _raw_rows(table: str, cols: str, limit: int, order: Optional[str] = None) -> List[Dict[str, Any]]:
    """One raw-client table read (worker-thread caller wraps this)."""
    from app.db import get_db
    q = get_db().get_raw_client().table(table).select(cols)
    if order:
        q = q.order(order, desc=True)
    res = q.limit(limit).execute()
    return getattr(res, "data", None) or []


async def _agents_section(usage_rows: List[Dict[str, Any]], window_s: float,
                          run_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Quick-access agent list: every non-hidden active agent with its live
    run count and window usage, busiest first."""
    out: Dict[str, Any] = {"total": 0, "list": []}
    try:
        rows = await asyncio.to_thread(_raw_rows, "agents", "id,name,status,model,metadata,updated_at", 300)
        cutoff = time.time() - window_s
        usage: Dict[str, Dict[str, float]] = {}
        for r in usage_rows:
            if _to_epoch(r.get("created_at")) < cutoff:
                continue
            a = usage.setdefault(r.get("agent_id") or "", {"cost": 0.0, "calls": 0, "tok": 0})
            a["cost"] += float(r.get("cost_usd") or 0.0)
            a["calls"] += 1
            a["tok"] += int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0)
        running: Dict[str, int] = {}
        for rr in run_rows:
            aid = rr.get("agent_id") or ""
            running[aid] = running.get(aid, 0) + 1
        agents = []
        for r in rows:
            if (r.get("status") or "active") != "active":
                continue
            meta = r.get("metadata")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            meta = meta or {}
            if meta.get("hidden_from_user") or meta.get("system_agent"):
                continue
            aid = r.get("id")
            u = usage.get(aid, {})
            agents.append({
                "id": aid,
                "name": r.get("name") or aid,
                "icon": meta.get("icon") or "bot",
                "model": r.get("model"),
                "engine": meta.get("engine"),
                "running": running.get(aid, 0),
                "cost_usd": round(u.get("cost", 0.0), 4),
                "calls": int(u.get("calls", 0)),
                "tokens": int(u.get("tok", 0)),
            })
        out["total"] = len(agents)
        agents.sort(key=lambda a: (-a["running"], -a["tokens"], a["name"].lower()))
        out["list"] = agents[:10]
    except Exception as e:
        logger.debug("dashboard agents section failed: %s", e)
    return out


async def _users_section(window_s: float) -> Dict[str, Any]:
    """User-management summary: counts + the most recently active accounts.
    Joins the credential store (app/auth/users.py) with user_profiles
    (is_admin / created_at / last_login_at)."""
    out: Dict[str, Any] = {"total": 0, "admins": 0, "pending": 0,
                           "new_in_window": 0, "recent": []}
    try:
        from app.auth.users import list_users
        accounts = list_users() or []
        profiles = await asyncio.to_thread(
            _raw_rows, "user_profiles", "user_id,is_admin,created_at,last_login_at", 2000)
        prof = {p.get("user_id"): p for p in profiles}
        cutoff = time.time() - window_s
        now = time.time()
        rows = []
        for u in accounts:
            p = prof.get(u.user_id) or {}
            is_admin = bool(p.get("is_admin")) or u.user_id == "admin"
            approved = bool(getattr(u, "is_approved", True))
            last = _to_epoch(p.get("last_login_at"))
            out["total"] += 1
            out["admins"] += 1 if is_admin else 0
            out["pending"] += 0 if approved else 1
            if _to_epoch(p.get("created_at")) >= cutoff:
                out["new_in_window"] += 1
            rows.append({
                "user_id": u.user_id,
                "name": getattr(u, "display_name", None) or u.username,
                "username": u.username,
                "admin": is_admin,
                "approved": approved,
                "last_login_s": int(now - last) if last else None,
            })
        rows.sort(key=lambda r: (r["last_login_s"] is None, r["last_login_s"] or 0))
        out["recent"] = rows[:8]
    except Exception as e:
        logger.debug("dashboard users section failed: %s", e)
    return out


async def _sessions_section(usage_rows: List[Dict[str, Any]],
                            run_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Sessions & runs monitor: running sessions first (with live duration),
    then the most recently touched, each with its window cost."""
    out: Dict[str, Any] = {"active": len(run_rows), "list": []}
    try:
        rows = await asyncio.to_thread(
            _raw_rows, "sessions", "id,user_id,title,agent_id,status,updated_at", 40, "updated_at")
        cost: Dict[str, Dict[str, float]] = {}
        for r in usage_rows:
            sid = r.get("session_id")
            if not sid:
                continue
            c = cost.setdefault(sid, {"cost": 0.0, "calls": 0})
            c["cost"] += float(r.get("cost_usd") or 0.0)
            c["calls"] += 1
        runs = {rr.get("session_id"): rr for rr in run_rows}
        now = time.time()
        seen = set()
        items = []

        def _item(sid: str, title, user_id, agent_id, updated) -> Dict[str, Any]:
            rr = runs.get(sid) or {}
            started = _to_epoch(rr.get("started_at"))
            hb = _to_epoch(rr.get("heartbeat_at") or rr.get("updated_at"))
            running = bool(rr)
            c = cost.get(sid, {})
            return {
                "id": sid,
                "title": (title or "Untitled session")[:60],
                "user_id": user_id,
                "agent_id": agent_id or rr.get("agent_id"),
                "running": running,
                "running_s": int(now - started) if running and started else None,
                # No heartbeat for 2+ min while "running" → probably stuck.
                "stale": bool(running and hb and (now - hb) > 120),
                "updated_s": int(now - _to_epoch(updated)) if updated else None,
                "cost_usd": round(c.get("cost", 0.0), 4),
                "calls": int(c.get("calls", 0)),
            }

        # Running sessions first — even ones whose sessions row didn't make the
        # recent-40 page.
        by_recent = {r.get("id"): r for r in rows}
        for sid, rr in runs.items():
            r = by_recent.get(sid) or {}
            items.append(_item(sid, r.get("title"), r.get("user_id") or rr.get("user_id"),
                               r.get("agent_id"), r.get("updated_at")))
            seen.add(sid)
        for r in rows:
            sid = r.get("id")
            if sid in seen or (r.get("status") or "active") != "active":
                continue
            items.append(_item(sid, r.get("title"), r.get("user_id"), r.get("agent_id"),
                               r.get("updated_at")))
            if len(items) >= 9:
                break
        out["list"] = items[:9]
    except Exception as e:
        logger.debug("dashboard sessions section failed: %s", e)
    return out


def _sw_version() -> Optional[str]:
    """The de-facto release marker: the CACHE name in sw.js (webagent-vNNN)."""
    try:
        head = (_PROJECT_ROOT / "sw.js").read_text(encoding="utf-8", errors="ignore")[:2000]
        import re
        m = re.search(r'CACHE\s*=\s*"([^"]+)"', head)
        return m.group(1) if m else None
    except Exception:
        return None


async def _health_section(db_health: Dict[str, Any], devices: List[Dict[str, Any]],
                          storage: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The status-light board: one row per subsystem, each ok/warn/err/off."""
    checks: List[Dict[str, Any]] = []

    def add(cid, label, state, value, detail=None):
        checks.append({"id": cid, "label": label, "state": state,
                       "value": value, "detail": detail})

    # Database
    st = "err" if db_health.get("degraded") else ("ok" if db_health.get("ok", True) else "warn")
    val = (db_health.get("actual") or "—") + (" · hybrid" if db_health.get("hybrid") else "")
    add("db", "Database", st, val, db_health.get("host") or "local file")

    # Secrets vault
    try:
        from app.secrets import get_secrets_status
        s = await asyncio.to_thread(get_secrets_status)
        add("vault", "Secrets vault", "warn" if s.get("restart_recommended") else "ok",
            s.get("provider") or "—",
            "restart recommended" if s.get("restart_recommended") else None)
    except Exception as e:
        add("vault", "Secrets vault", "err", "unavailable", str(e)[:80])

    # Scheduler / automations engine
    try:
        from app.scheduler import get_scheduler
        st_ = await get_scheduler().get_status()
        run = bool(st_.get("running"))
        add("scheduler", "Automations", "ok" if run else "off",
            f"{st_.get('tasks_enabled', 0)}/{st_.get('tasks_total', 0)} enabled",
            (st_.get("last_error") or None) if run else "scheduler idle")
    except Exception as e:
        add("scheduler", "Automations", "err", "unavailable", str(e)[:80])

    # Tunnel link (remote access relay)
    try:
        from app.tunnel_link.store import load_config as _tl
        cfg = _tl()
        url = cfg.get("tunnel_url")
        add("tunnel", "Tunnel", "ok" if (cfg.get("registered_relay") and url) else "off",
            (url or "not linked").replace("https://", ""), None)
    except Exception:
        add("tunnel", "Tunnel", "off", "not installed", None)

    # Devices
    online = sum(1 for d in devices if d.get("online"))
    add("devices", "Devices", "ok" if online else "off",
        f"{online}/{len(devices)} online" if devices else "none linked", None)

    # Disk
    free = storage.get("disk_free_gb")
    if free is not None:
        add("disk", "Disk space", "err" if free < 2 else ("warn" if free < 10 else "ok"),
            f"{free} GB free", f"of {storage.get('disk_total_gb')} GB")

    # Build / release marker
    ver = _sw_version()
    if ver:
        add("version", "Build", "ok", ver, None)
    return checks


async def _tools_section(usage_rows: List[Dict[str, Any]], window_s: float) -> Dict[str, Any]:
    """Tool & model usage: top tools by executions (with failure rate + avg
    duration, from logs.db tool_executions) and per-model calls/tokens/cost
    (from the shared usage rows)."""
    out: Dict[str, Any] = {"tools": [], "models": [], "tool_calls": 0, "tool_failures": 0}
    start = time.time() - window_s
    try:
        from app.db.logs_store import get_log_store
        rows = await get_log_store().query_tool_executions(
            since=_iso_from_epoch(start), limit=2000)
        agg: Dict[str, Dict[str, float]] = {}
        for r in rows:
            name = r.get("tool_name") or "?"
            a = agg.setdefault(name, {"n": 0, "fail": 0, "ms": 0.0})
            a["n"] += 1
            a["fail"] += 0 if r.get("success") else 1
            a["ms"] += float(r.get("duration_ms") or 0.0)
        out["tool_calls"] = sum(int(a["n"]) for a in agg.values())
        out["tool_failures"] = sum(int(a["fail"]) for a in agg.values())
        top = sorted(agg.items(), key=lambda kv: kv[1]["n"], reverse=True)[:8]
        out["tools"] = [{
            "name": k, "calls": int(v["n"]), "failures": int(v["fail"]),
            "fail_pct": round(v["fail"] / v["n"] * 100, 1) if v["n"] else 0.0,
            "avg_ms": round(v["ms"] / v["n"], 0) if v["n"] else 0.0,
        } for k, v in top]
    except Exception as e:
        logger.debug("dashboard tools section failed: %s", e)
    try:
        cutoff = time.time() - window_s
        models: Dict[str, Dict[str, float]] = {}
        for r in usage_rows:
            if _to_epoch(r.get("created_at")) < cutoff:
                continue
            m = models.setdefault(r.get("model") or "unknown", {"n": 0, "tok": 0, "cost": 0.0})
            m["n"] += 1
            m["tok"] += int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0)
            m["cost"] += float(r.get("cost_usd") or 0.0)
        top = sorted(models.items(), key=lambda kv: kv[1]["n"], reverse=True)[:6]
        out["models"] = [{
            "model": k, "calls": int(v["n"]), "tokens": int(v["tok"]),
            "cost_usd": round(v["cost"], 4),
        } for k, v in top]
    except Exception as e:
        logger.debug("dashboard models aggregate failed: %s", e)
    return out


async def _security_section(window_s: float) -> Dict[str, Any]:
    """Security & sign-ins: auth events recorded by app/auth (category ``auth``
    in the diagnostics store) — sign-ins, failed attempts, registrations."""
    out: Dict[str, Any] = {"signins": 0, "failed": 0, "recent": []}
    try:
        from app.db.logs_store import get_log_store
        rows = await get_log_store().query_diagnostics(
            categories=["auth"], since=_iso_from_epoch(time.time() - window_s), limit=500)
        for r in rows:
            msg = (r.get("message") or "")
            lvl = (r.get("level") or "info").lower()
            if lvl in ("warning", "error", "critical"):
                out["failed"] += 1
            elif msg.lower().startswith(("sign-in", "social sign-in")):
                out["signins"] += 1
        out["recent"] = [{
            "ts": r.get("created_at") or r.get("ts"),
            "level": (r.get("level") or "info").lower(),
            "message": (r.get("message") or "")[:120],
        } for r in rows[:8]]
    except Exception as e:
        logger.debug("dashboard security section failed: %s", e)
    return out


async def _live_snapshot(window_s: float) -> Dict[str, Any]:
    """The FAST first fill for the dashboard: only the sections that need NO
    database scan — the in-memory live gauges (app/metrics.py), process memory,
    the connection-health read (in-memory + file config), and the filesystem
    storage stat. Returns in milliseconds even against a cold/slow remote DB, so
    the grid's live/db-health/storage cards populate immediately while the heavier
    DB-backed cards keep spinning until the full ``/metrics`` poll lands. Marked
    ``partial`` so callers know the DB-derived sections are absent (not empty)."""
    from app import metrics
    live = metrics.snapshot(window_s=min(window_s, 300.0))
    db_health = await _db_health_section()
    memory_mb = _memory_mb()
    # Feed the live ring so the over-time chart keeps trending between full polls.
    metrics.record_system((live or {}).get("cpu_percent"), memory_mb, 0)
    return {
        "generated_at": int(time.time()),
        "window_s": window_s,
        "partial": True,
        "live": live,
        "memory_mb": memory_mb,
        "db_health": db_health,
        "storage": _storage_section(),
    }


async def _build_snapshot(uid: str, window_s: float) -> Dict[str, Any]:
    from app import metrics
    # Live section is always fresh (in-memory, DB-free). Clamp the live window to
    # something sensible (5 min) so a "7 days" aggregate window doesn't dilute the
    # live gauges — aggregates honour the full window; live gauges stay recent.
    live = metrics.snapshot(window_s=min(window_s, 300.0))
    # ONE usage_events fetch (hybrid-aware) + one active-runs read feed the
    # tokens / agents / sessions / models sections below.
    usage_rows, run_rows = await asyncio.gather(
        _usage_rows(time.time() - window_s),
        _active_run_rows(),
    )
    db_health, devices, failures, context, agents, users, sessions, tools, security = \
        await asyncio.gather(
            _db_health_section(),
            _devices_section(),
            _failures_section(window_s),
            _context_section(uid),
            _agents_section(usage_rows, window_s, run_rows),
            _users_section(window_s),
            _sessions_section(usage_rows, run_rows),
            _tools_section(usage_rows, window_s),
            _security_section(window_s),
        )
    storage = _storage_section()
    health = await _health_section(db_health, devices, storage)
    return {
        "generated_at": int(time.time()),
        "window_s": window_s,
        "live": live,
        "memory_mb": _memory_mb(),
        "active_runs": len(run_rows),
        "tokens": _tokens_section(usage_rows, window_s),
        "db_health": db_health,
        "devices": devices,
        "failures": failures,
        "context": context,
        "storage": storage,
        "agents": agents,
        "users": users,
        "sessions": sessions,
        "health": health,
        "tool_usage": tools,
        "security": security,
    }


# ── endpoints ───────────────────────────────────────────────────────────────
@router.get("/metrics")
async def metrics_endpoint(
    requesting_user_id: str = Query(""),
    window: float = Query(3600.0),
    live: int = Query(0),
):
    await _require_admin(requesting_user_id)
    window = max(60.0, min(float(window), 7 * 86400.0))
    # Fast, DB-free partial for the grid's first paint (see _live_snapshot). Does
    # NOT touch the composite cache — it's cheap enough to always recompute.
    if live:
        return await _live_snapshot(window)
    now = time.time()
    if _cache["data"] is not None and _cache["window"] == window and (now - _cache["at"]) < _CACHE_TTL:
        # Refresh only the always-cheap live section on a cache hit so CPU/DB
        # gauges keep ticking between the heavier aggregate refreshes.
        from app import metrics
        cached = dict(_cache["data"])
        cached["live"] = metrics.snapshot(window_s=min(window, 300.0))
        cached["memory_mb"] = _memory_mb()
        cached["cached"] = True
        # Feed the live CPU/RAM/active-run history ring so the over-time chart can
        # trend them (live-only, going forward — none of the three has a durable store).
        metrics.record_system((cached["live"] or {}).get("cpu_percent"), cached["memory_mb"],
                              cached.get("active_runs") or 0)
        return cached
    data = await _build_snapshot(requesting_user_id, window)
    _cache.update(at=now, window=window, data=data)
    from app import metrics
    metrics.record_system((data.get("live") or {}).get("cpu_percent"), data.get("memory_mb"),
                          data.get("active_runs") or 0)
    return data


@router.get("/metrics/timeseries")
async def timeseries_endpoint(
    requesting_user_id: str = Query(""),
    kind: str = Query("db"),
    window: float = Query(1800.0),
    buckets: int = Query(30),
):
    await _require_admin(requesting_user_id)
    from app import metrics
    kind = "db" if kind not in ("db", "llm") else kind
    buckets = max(6, min(int(buckets), 120))
    window = max(300.0, min(float(window), 7 * 86400.0))
    return {"kind": kind, "points": metrics.timeseries(kind, buckets=buckets, window_s=window)}


# ── the full-width "Metrics over time" chart ─────────────────────────────────
# ONE endpoint feeding the hero chart card: a shared time X axis with several
# selectable series. Durable series (tokens in/out, per-model cost) come from
# usage_events and support any window OR a custom date range. In hybrid mode the
# rows are a UNION of the remote authority (history) + the local hot store (the
# freshest, not-yet-synced tail), deduped by id — see _fetch_usage_rows — so a
# frequent poll reflects live activity immediately. Live series (CPU, RAM) come
# from the in-memory ring (app/metrics.py) and only trend forward.
_CHART_LINE_META = {
    # Durable (usage_events / diagnostics) — full history, any window or range.
    "tokens_in":  {"label": "Tokens in",   "unit": "tok",   "src": "usage"},
    "tokens_out": {"label": "Tokens out",  "unit": "tok",   "src": "usage"},
    "llm_calls":  {"label": "LLM calls",   "unit": "calls", "src": "usage"},
    "errors":     {"label": "Errors",      "unit": "calls", "src": "diag"},
    # Live (in-memory rings, app/metrics.py) — trend forward from boot only.
    "cpu":         {"label": "CPU",              "unit": "%",    "src": "live", "kind": "cpu"},
    "ram":         {"label": "RAM",              "unit": "MB",   "src": "live", "kind": "ram"},
    "active_runs": {"label": "Active runs",      "unit": "runs", "src": "live", "kind": "runs"},
    "db_avg_ms":   {"label": "DB latency (avg)", "unit": "ms",   "src": "live", "kind": "db"},
    "db_p95_ms":   {"label": "DB latency (p95)", "unit": "ms",   "src": "live", "kind": "db_p95"},
}
_CHART_MAX_MODELS = 8  # top-N models by cost get their own bar; the rest fold into "Other"
# Shared by the chart AND the snapshot sections (tokens / agents / sessions /
# models) — agent_id + session_id ride along so one fetch serves them all.
_USAGE_COLS = "id,input_tokens,output_tokens,cost_usd,model,agent_id,session_id,created_at"


def _select_usage(raw, start: float) -> List[Dict[str, Any]]:
    """Run the usage_events query on ONE raw client since ``start``. The lower
    bound uses the stored timestamp format (see ``_sql_ts``) so the coarse
    DB-side pre-filter is correct; the caller re-filters precisely by epoch."""
    q = (raw.table("usage_events").select(_USAGE_COLS)
         .gte("created_at", _sql_ts(start)).order("created_at", desc=True).limit(20000))
    return getattr(q.execute(), "data", None) or []


async def _fetch_usage_rows(start: float) -> List[Dict[str, Any]]:
    """Usage rows for the chart since ``start``.

    In HYBRID mode the remote Postgres is the authority (full history) but the
    background sync means its newest rows can be a minute-to-hours behind; the
    local SQLite hot store holds those freshest rows first. So we UNION both,
    deduped by row ``id`` — remote supplies history, local overlays the live
    tail — which is what lets a frequent poll show activity the instant it
    happens instead of waiting for the push to remote. Non-hybrid installs just
    query the single active backend. Either side failing degrades to the other
    (never hard-fails the chart)."""
    from app.db import get_db
    db = get_db()
    local = getattr(db, "local", None)
    remote = getattr(db, "remote", None)
    if local is not None and remote is not None:
        merged: Dict[Any, Dict[str, Any]] = {}
        # Remote first (authority/history); local second so its fresher copy of a
        # shared id wins and its not-yet-synced tail is added on top.
        for who, backend in (("remote", remote), ("local", local)):
            try:
                rows = await asyncio.to_thread(_select_usage, backend.get_raw_client(), start)
                for r in rows:
                    merged[r.get("id") or id(r)] = r
            except Exception as e:
                logger.debug("dashboard chart usage fetch (%s) failed: %s", who, e)
        return list(merged.values())
    try:
        return await asyncio.to_thread(_select_usage, db.get_raw_client(), start)
    except Exception as e:
        logger.debug("dashboard chart usage fetch failed: %s", e)
        return []


# ── shared usage-rows fetch cache (de-dupes the biggest cost on page open) ────
# Opening the dashboard fires BOTH /metrics (→ _build_snapshot) and /chart at the
# same instant, and each one independently scans up to 20k usage_events rows —
# in hybrid mode that is a remote AND a local query per call, so up to FOUR big
# scans against the slow remote Postgres for a single page open. Every consumer
# already re-filters the rows precisely by epoch, so a slightly-wider/older row
# set is always safe to reuse. This wrapper therefore (1) serves a fresh cached
# set for a few seconds, and (2) coalesces concurrent callers onto ONE in-flight
# fetch — so the two page-open requests share a single scan instead of racing.
# Windows are bucketed to 10s so /metrics' and /chart's near-identical start
# times collide; distinct windows (5m vs 7d) key separately and never share.
_USAGE_TTL = 6.0
_usage_cache: Dict[int, Dict[str, Any]] = {}      # bucket -> {"at", "rows"}
_usage_inflight: Dict[int, "asyncio.Task"] = {}   # bucket -> in-flight fetch


def _usage_bucket(start: float) -> int:
    return int(start // 10)


async def _usage_rows(start: float) -> List[Dict[str, Any]]:
    """Cached, concurrency-coalesced wrapper over ``_fetch_usage_rows``."""
    bucket = _usage_bucket(start)
    now = time.time()
    hit = _usage_cache.get(bucket)
    if hit is not None and (now - hit["at"]) < _USAGE_TTL:
        return hit["rows"]
    task = _usage_inflight.get(bucket)
    if task is not None:
        return await task            # a sibling request is already fetching this window
    task = asyncio.ensure_future(_fetch_usage_rows(start))
    _usage_inflight[bucket] = task
    try:
        rows = await task
    finally:
        _usage_inflight.pop(bucket, None)
    _usage_cache[bucket] = {"at": time.time(), "rows": rows}
    # Prune stale buckets so a long-lived process doesn't accrete windows.
    for b in [b for b, v in _usage_cache.items() if (time.time() - v["at"]) > _USAGE_TTL * 3]:
        _usage_cache.pop(b, None)
    return rows


def _resolve_range(window: float, frm: Optional[str], to: Optional[str]) -> tuple:
    """Return (start_epoch, end_epoch) from either a preset window (seconds ending
    now) or an explicit from/to. Date-only strings (YYYY-MM-DD) span whole days:
    `from` = start of that day, `to` = end of that day."""
    now = time.time()
    if frm or to:
        start = _to_epoch(frm) if frm else (now - window)
        if to:
            end = _to_epoch(to)
            if end and len(str(to).strip()) == 10 and "T" not in str(to):
                end += 86400.0  # date-only upper bound → include the whole day
        else:
            end = now
    else:
        window = max(60.0, min(float(window), 30 * 86400.0))
        start, end = now - window, now
    if not start:
        start = now - 3600.0
    if not end or end <= start:
        end = start + 60.0
    return start, end


@router.get("/chart")
async def chart_endpoint(
    requesting_user_id: str = Query(""),
    window: float = Query(3600.0),
    frm: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None, alias="to"),
    buckets: int = Query(48),
    series: str = Query("tokens_in,tokens_out,cost"),
):
    await _require_admin(requesting_user_id)
    from app import metrics
    buckets = max(6, min(int(buckets), 200))
    start, end = _resolve_range(window, frm, to)
    span = (end - start) / buckets
    want = {s.strip() for s in (series or "").split(",") if s.strip()}
    now = time.time()

    def _bucket_idx(ts: float) -> int:
        return int((ts - start) / span) if span > 0 else 0

    lines: List[Dict[str, Any]] = []
    cost_models: List[Dict[str, Any]] = []

    # Durable token/cost series from usage_events (bucketed in Python, like
    # _tokens_section — the raw builder has no .lt(), so the upper bound is a
    # Python filter). Fetch once, only when a durable series is requested.
    need_usage = bool(want & {"tokens_in", "tokens_out", "cost", "llm_calls"})
    if need_usage:
        tin = [0.0] * buckets
        tout = [0.0] * buckets
        lcalls = [0.0] * buckets
        per_model: Dict[str, List[float]] = {}
        model_totals: Dict[str, float] = {}
        try:
            rows = await _usage_rows(start)
            for r in rows:
                ts = _to_epoch(r.get("created_at"))
                if ts < start or ts >= end:
                    continue
                bi = _bucket_idx(ts)
                if bi < 0 or bi >= buckets:
                    continue
                tin[bi] += int(r.get("input_tokens") or 0)
                tout[bi] += int(r.get("output_tokens") or 0)
                lcalls[bi] += 1
                cost = float(r.get("cost_usd") or 0.0)
                if cost:
                    mdl = r.get("model") or "unknown"
                    per_model.setdefault(mdl, [0.0] * buckets)[bi] += cost
                    model_totals[mdl] = model_totals.get(mdl, 0.0) + cost
        except Exception as e:
            logger.debug("dashboard chart usage fetch failed: %s", e)

        if "tokens_in" in want:
            lines.append({"id": "tokens_in", "label": "Tokens in", "unit": "tok", "values": [round(v) for v in tin]})
        if "tokens_out" in want:
            lines.append({"id": "tokens_out", "label": "Tokens out", "unit": "tok", "values": [round(v) for v in tout]})
        if "llm_calls" in want:
            lines.append({"id": "llm_calls", "label": "LLM calls", "unit": "calls", "values": [round(v) for v in lcalls]})
        if "cost" in want:
            top = sorted(model_totals.items(), key=lambda kv: kv[1], reverse=True)
            keep = {m for m, _ in top[:_CHART_MAX_MODELS]}
            other = [0.0] * buckets
            for mdl, vals in per_model.items():
                if mdl in keep:
                    cost_models.append({"model": mdl, "values": [round(v, 4) for v in vals]})
                else:
                    for i, v in enumerate(vals):
                        other[i] += v
            if any(other):
                cost_models.append({"model": "Other", "values": [round(v, 4) for v in other]})
            # Preserve cost order (largest total first) for stable stacking/colours.
            order = {m: i for i, (m, _) in enumerate(top)}
            cost_models.sort(key=lambda cm: order.get(cm["model"], 10_000))

    # Durable error series from the diagnostics store (count per bucket).
    if "errors" in want:
        errs = [0.0] * buckets
        try:
            from app.db import get_db
            db = get_db()
            from app.db.offload import db_offload
            rows = await db_offload(lambda: db.query_diagnostics(
                levels=["error", "critical"], since=_iso_from_epoch(start), limit=2000)) or []
            for r in rows:
                ts = _to_epoch(r.get("created_at") or r.get("ts"))
                bi = _bucket_idx(ts)
                if start <= ts < end and 0 <= bi < buckets:
                    errs[bi] += 1
        except Exception as e:
            logger.debug("dashboard chart errors fetch failed: %s", e)
        lines.append({"id": "errors", "label": "Errors", "unit": "calls", "values": [round(v) for v in errs]})

    # Live series (CPU / RAM / active runs / DB latency) from the in-memory rings.
    # Only meaningful up to "now", so for a past-ending custom range they come
    # back empty (live-only, by design).
    for sid, meta in _CHART_LINE_META.items():
        if meta.get("src") != "live" or sid not in want:
            continue
        if end >= now - span:
            pts = metrics.timeseries(meta["kind"], buckets=buckets, window_s=max(now - start, span))
            values = [p.get("avg_ms") or 0.0 for p in pts][:buckets]
            values += [0.0] * (buckets - len(values))
        else:
            values = [0.0] * buckets
        lines.append({"id": sid, "label": meta["label"], "unit": meta["unit"], "values": values})

    return {
        "start": int(start),
        "end": int(end),
        "buckets": [int(start + (b + 1) * span) for b in range(buckets)],
        "lines": lines,
        "cost_models": cost_models,
    }


# ── admin actions (the operational cards' buttons) ──────────────────────────
class StopRunBody(BaseModel):
    requesting_user_id: str = ""
    session_id: str = ""


@router.post("/runs/stop")
async def stop_run(body: StopRunBody):
    """Gracefully interrupt a running agent session from the Sessions & runs
    monitor card (admin Stop button). Same mechanism as the chat Stop button
    (run_manager.interrupt), but admin-gated so an admin can stop ANY user's
    stuck or runaway run."""
    await _require_admin(body.requesting_user_id)
    sid = (body.session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id required")
    try:
        from app.agent.run_manager import get_run_manager
        from app.db import get_db
        was_running = await get_run_manager().interrupt(sid, get_db())
        return {"ok": True, "was_running": bool(was_running)}
    except Exception as e:
        logger.warning("dashboard stop-run failed: %s", e)
        raise HTTPException(status_code=500, detail="Could not stop the run")


# ── per-user layout ─────────────────────────────────────────────────────────
class LayoutBody(BaseModel):
    requesting_user_id: str = ""
    cards: List[Dict[str, Any]] = []


def _load_layouts() -> Dict[str, Any]:
    from app.util.config_io import read_json
    data = read_json(_LAYOUT_FILE, {})
    return data if isinstance(data, dict) else {}


@router.get("/layout")
async def get_layout(requesting_user_id: str = Query("")):
    await _require_admin(requesting_user_id)
    layouts = _load_layouts()
    entry = layouts.get(requesting_user_id or "_")
    if isinstance(entry, dict) and isinstance(entry.get("cards"), list):
        return {"cards": entry["cards"], "default": False}
    return {"cards": _default_cards(), "default": True}


@router.put("/layout")
async def put_layout(body: LayoutBody):
    await _require_admin(body.requesting_user_id)
    from app.util.config_io import read_json, safe_write_json
    layouts = read_json(_LAYOUT_FILE, {})
    if not isinstance(layouts, dict):
        layouts = {}
    layouts[body.requesting_user_id or "_"] = {"cards": body.cards, "saved_at": int(time.time())}
    safe_write_json(_LAYOUT_FILE, layouts)
    return {"ok": True, "count": len(body.cards)}


class ResetBody(BaseModel):
    requesting_user_id: str = ""


@router.post("/layout/reset")
async def reset_layout(body: ResetBody):
    """Drop this admin's saved arrangement so the grid returns to the seed layout.
    Returns the default cards so the caller can render them without a second fetch."""
    await _require_admin(body.requesting_user_id)
    from app.util.config_io import read_json, safe_write_json
    layouts = read_json(_LAYOUT_FILE, {})
    if isinstance(layouts, dict) and (body.requesting_user_id or "_") in layouts:
        layouts.pop(body.requesting_user_id or "_", None)
        safe_write_json(_LAYOUT_FILE, layouts)
    return {"cards": _default_cards(), "default": True}


@router.post("/layout/default")
async def save_as_default(body: LayoutBody):
    """Make the posted arrangement the NEW default for everyone — the cards a fresh
    admin, or anyone who hits "reset to default", will get. Writes the runtime
    override (data/config/dashboard-default.json) so the bundled factory seed
    (app/defaults/dashboard.json) stays pristine."""
    await _require_admin(body.requesting_user_id)
    from app.util.config_io import safe_write_json
    from app.util.paths import dashboard_default_override_path
    cards = body.cards if isinstance(body.cards, list) else []
    safe_write_json(dashboard_default_override_path(), {"version": 1, "cards": cards})
    return {"ok": True, "count": len(cards)}


# The seed layout a fresh admin sees comes from a JSON file (like the default
# agent template) — the bundled app/defaults/dashboard.json, or a runtime override
# an admin saved via "Save as default" (data/config/dashboard-default.json), which
# wins. Resolved through app.util.paths so relocation is a one-file edit. A tiny
# in-code list is kept ONLY as a last-ditch fallback if the JSON is missing/corrupt
# so the dashboard never comes up empty.
_FALLBACK_CARDS: List[Dict[str, Any]] = [
    {"id": "c-chart", "type": "metric_chart", "x": 0, "y": 0, "w": 12, "h": 5,
     "series": ["tokens_in", "tokens_out", "cost"], "range": {"window": 3600}},
    {"id": "c-dbmode", "type": "db_mode", "x": 0, "y": 5, "w": 3, "h": 2},
    {"id": "c-dblat", "type": "db_latency", "x": 3, "y": 5, "w": 6, "h": 3},
    {"id": "c-add", "type": "add_card", "x": 0, "y": 8, "w": 4, "h": 3},
]


def _default_cards() -> List[Dict[str, Any]]:
    """Load the current default layout (override-or-bundled JSON). Read fresh each
    call so a just-saved "Save as default" takes effect immediately, with no server
    restart. Falls back to the in-code list if the file is unreadable."""
    try:
        from app.util.config_io import read_json
        from app.util.paths import dashboard_default_path
        data = read_json(dashboard_default_path(), {})
        cards = data.get("cards") if isinstance(data, dict) else None
        if isinstance(cards, list) and cards:
            return cards
    except Exception as e:
        logger.debug("dashboard default-cards load failed, using fallback: %s", e)
    return _FALLBACK_CARDS


# ── AI card generation ──────────────────────────────────────────────────────
class AiCardBody(BaseModel):
    requesting_user_id: str = ""
    prompt: str = ""


# The metric fields an AI-generated custom card may reference — the language the
# LLM is allowed to speak. Keep in sync with the frontend renderer's FIELD map.
_AI_FIELDS = [
    "live.cpu_percent", "memory_mb", "active_runs",
    "live.db.avg_ms", "live.db.p95_ms", "live.db.rate_per_min", "live.db.calls_total",
    "live.llm.rate_per_min", "live.llm.tokens_per_min", "live.llm.calls_total",
    "live.loop_split.llm_pct", "live.loop_split.db_pct", "live.uptime_s",
    "tokens.in", "tokens.out", "tokens.cost_usd", "tokens.calls",
    "db_health.actual", "db_health.degraded", "db_health.provider", "db_health.host", "db_health.hybrid", "failures.count",
    "devices", "storage.disk_free_gb", "context.max_input",
    "agents.total", "users.total", "users.admins", "users.pending", "users.new_in_window",
    "sessions.active", "tool_usage.tool_calls", "tool_usage.tool_failures",
    "security.signins", "security.failed",
]


@router.post("/ai-card")
async def ai_card(body: AiCardBody):
    await _require_admin(body.requesting_user_id)
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Describe the card you want")
    try:
        from app.agent.suggestions import _resolve_default_llm
        from openai import AsyncOpenAI
    except Exception:
        raise HTTPException(status_code=503, detail="AI is not configured")

    llm = await _resolve_default_llm(body.requesting_user_id)
    if not llm.get("api_key"):
        raise HTTPException(status_code=503, detail="No LLM key configured")

    sys = (
        "You design a single metric card for a self-hosted AI-agent admin dashboard. "
        "Return ONLY compact JSON, no prose. Shape: "
        '{"title": str, "icon": <lucide-icon-name>, "viz": "stat"|"list"|"bars", '
        '"unit": str, "fields": [ {"label": str, "path": <one of the allowed paths>, '
        '"unit": str} ] }. '
        "viz 'stat' shows 1-2 big numbers; 'list'/'bars' show several field rows. "
        "Choose ONLY from these allowed data paths: " + ", ".join(_AI_FIELDS) + ". "
        "Pick paths that genuinely match the user's request; keep to 1-4 fields."
    )
    try:
        client = AsyncOpenAI(base_url=llm["base_url"], api_key=llm["api_key"], timeout=45.0)
        resp = await client.chat.completions.create(
            model=llm["model"],
            messages=[{"role": "system", "content": sys},
                      {"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=400,
        )
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):]
        spec = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception as e:
        logger.warning("dashboard ai-card failed: %s", e)
        raise HTTPException(status_code=502, detail="Could not generate a card")

    # Validate: keep only allowed paths.
    fields = []
    for f in (spec.get("fields") or []):
        if isinstance(f, dict) and f.get("path") in _AI_FIELDS:
            fields.append({"label": str(f.get("label") or f["path"]),
                           "path": f["path"], "unit": str(f.get("unit") or "")})
    if not fields:
        raise HTTPException(status_code=422, detail="AI picked no valid metrics — try rephrasing")
    return {
        "type": "custom",
        "title": str(spec.get("title") or prompt[:40]),
        "icon": str(spec.get("icon") or "sparkles"),
        "viz": spec.get("viz") if spec.get("viz") in ("stat", "list", "bars") else "list",
        "fields": fields,
    }


# ── Local instances (the Dashboard's instance-switcher header) ───────────────
# The full-width instance header at the top of the Dashboard shows the CURRENT
# instance (this app) with its DB connection + health, and a chevron dropdown to
# start a new instance or switch to another. The hub can run OTHER WebAgent repo
# checkouts, each on its own port; the registry + launch/stop logic lives in
# app/local_instances.py (mirrors app/git_repos.py). These endpoints are the thin
# admin-gated surface, folded into the Dashboard backend because the header lives
# ON the Dashboard — there is no separate Local Instances page. Start/stop stream
# NDJSON, same shape as the Deploy / Server-Manager cards.
class InstFolderBody(BaseModel):
    requesting_user_id: str = ""
    folder: str = ""


class InstAddBody(BaseModel):
    requesting_user_id: str = ""
    label: str = ""
    folder: str = ""
    port: int = 0


class InstUpdateBody(BaseModel):
    requesting_user_id: str = ""
    id: str = ""
    label: str = ""
    folder: str = ""
    port: int = 0


class InstIdBody(BaseModel):
    requesting_user_id: str = ""
    id: str = ""


@router.get("/instances")
async def instances_list(requesting_user_id: str = Query("")):
    await _require_admin(requesting_user_id)
    from app import local_instances as li
    return {"instances": await li.list_instances(), "hub_port": li.hub_port()}


@router.post("/instances/validate")
async def instances_validate(body: InstFolderBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    return li.validate_folder(body.folder)


@router.post("/instances/add")
async def instances_add(body: InstAddBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    try:
        return {"ok": True, "instance": li.add_instance(body.label, body.folder, body.port)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/instances/update")
async def instances_update(body: InstUpdateBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    try:
        return {"ok": True, "instance": li.update_instance(
            body.id, label=body.label, folder=body.folder, port=body.port)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/instances/remove")
async def instances_remove(body: InstIdBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    try:
        li.remove_instance(body.id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _instance_stream(agen):
    async def _gen():
        try:
            async for evt in agen:
                yield json.dumps(evt) + "\n"
        except Exception as e:  # never leave the client's reader hanging
            logger.exception("dashboard instance stream crashed")
            yield json.dumps({"phase": "done", "result": {"ok": False, "message": str(e) or "failed"}}) + "\n"
    return StreamingResponse(
        _gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/instances/start")
async def instances_start(body: InstIdBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    return _instance_stream(li.start_instance(body.id))


@router.post("/instances/stop")
async def instances_stop(body: InstIdBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    return _instance_stream(li.stop_instance(body.id))
