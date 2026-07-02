"""Agent-facing tools for the watchdog: inspect and retune monitoring/alarms by
talking to the manager.

These edit the manager's OWN configuration (the data-dir JSON the watchdog reads)
or just report / send a test notification — they never touch the linked repo or
run commands, so they aren't gated behind the write toggle and work in onboarding
mode too. The running watchdog re-reads the files each tick, so changes apply live.
"""

from __future__ import annotations

from .. import monstate, sysmetrics
from ..notify import Notifier
from ..watchdog import active_watchdog
from .base import ToolContext


def _fmt_resources(snap: dict) -> list[str]:
    """Human lines for a sysmetrics snapshot (skipping anything unavailable)."""
    lines: list[str] = []
    cpu = snap.get("cpu_percent")
    if cpu is not None:
        lines.append(f"  host CPU: {cpu}%")
    mem = snap.get("memory") or {}
    if mem.get("percent") is not None:
        lines.append(f"  host memory: {mem['percent']}% used "
                     f"({sysmetrics.fmt_bytes(mem.get('available'))} free)")
    disk = snap.get("disk") or {}
    if disk.get("percent") is not None:
        lines.append(f"  disk: {disk['percent']}% used "
                     f"({sysmetrics.fmt_bytes(disk.get('free'))} free)")
    rss = snap.get("process_rss")
    if rss:
        lines.append(f"  server process memory: {sysmetrics.fmt_bytes(rss)}")
    return lines


async def monitor_status(ctx: ToolContext) -> str:
    wd = active_watchdog()
    snap = wd.snapshot() if wd is not None else None
    cfg = monstate.load_monitor_config()
    lines = ["[monitor] watchdog status:"]
    running = snap["running"] if snap else False
    lines.append(f"  running: {running}; enabled: {cfg.get('enabled')}; "
                 f"interval: {cfg.get('interval_seconds')}s")
    lines.append(f"  autonomy: {cfg.get('autonomy')}; auto_restart: {cfg.get('auto_restart')}; "
                 f"channels: {', '.join(cfg.get('channels') or []) or 'none'}")
    lines.append(f"  thresholds: error_rate≥{cfg.get('error_rate_threshold')} / interval, "
                 f"max {cfg.get('max_restarts_per_hour')} restarts/hour")
    lines.append(f"  alarms configured: {len(monstate.load_alarms())}")
    if snap:
        lines.append(f"  last server health: {snap['last_health']}; "
                     f"restarts in last hour: {snap['restarts_last_hour']}")
        res = snap.get("resources") or {}
        res_lines = _fmt_resources(res) if res else []
        if res_lines:
            lines.append("  last resource sample:")
            lines += res_lines
        if snap["recent_reactions"]:
            lines.append("  recent reactions:")
            lines += [f"    {r}" for r in snap["recent_reactions"][-8:]]
    else:
        lines.append("  (watchdog not active — managed mode + enabled monitoring needed)")
    return "\n".join(lines)


async def server_resources(ctx: ToolContext) -> str:
    if ctx.project_root is None:
        return "[monitor] no checkout linked — link one first."
    from .server import _pid_alive, _read_pidinfo  # lazy: avoid import cycle
    info = _read_pidinfo()
    pid = int(info["pid"]) if (info and info.get("pid")
                               and _pid_alive(int(info["pid"]))) else 0
    # Host CPU is a delta between two samples; for a one-off manual call, prime a
    # baseline and pause briefly so gather()'s reading reflects a real interval
    # (the watchdog's periodic ticks are already spaced out, so it skips this).
    import asyncio
    sysmetrics.host_cpu_percent()
    await asyncio.sleep(0.3)
    snap = sysmetrics.gather(str(ctx.project_root), pid)
    body = _fmt_resources(snap)
    if not body:
        return "[monitor] no resource metrics available on this platform."
    note = "" if pid else "  (server process not tracked — memory shown for host only)"
    return "[monitor] resources now:\n" + "\n".join(body) + (("\n" + note) if note else "")


async def list_alarms(ctx: ToolContext) -> str:
    alarms = monstate.load_alarms()
    if not alarms:
        return "[monitor] no alarm rules yet. Add one with add_alarm."
    out = [f"[monitor] {len(alarms)} alarm rule(s):"]
    for a in alarms:
        crit = " ".join(filter(None, [
            f'contains="{a.get("contains")}"' if a.get("contains") else "",
            f'level={a.get("level")}' if a.get("level") else "",
            f'category={a.get("category")}' if a.get("category") else "",
        ])) or "(no criteria)"
        ch = ", ".join(a.get("channels") or []) or "default"
        out.append(f"  {a.get('id')}: {a.get('label')} — {crit} → {a.get('action')} "
                   f"[{a.get('loudness')}] via {ch}")
    return "\n".join(out)


async def add_alarm(ctx: ToolContext, contains: str = "", level: str = "", category: str = "",
                    action: str = "notify", loudness: str = "every",
                    channels: list[str] | None = None, label: str = "") -> str:
    try:
        rule = monstate.add_alarm(contains=contains, level=level, category=category,
                                  action=action, loudness=loudness, channels=channels,
                                  label=label)
    except ValueError as e:
        return f"[monitor] {e}"
    ctx.audit("add_alarm", {"id": rule["id"], "label": rule["label"]}, True, "added")
    return (f"[monitor] alarm '{rule['label']}' added (id {rule['id']}): "
            f"action {rule['action']}, loudness {rule['loudness']}. "
            "The watchdog will evaluate it on its next tick.")


async def remove_alarm(ctx: ToolContext, id: str) -> str:
    ok = monstate.remove_alarm(id)
    ctx.audit("remove_alarm", {"id": id}, ok, "removed" if ok else "not found")
    return f"[monitor] alarm {id} removed." if ok else f"[monitor] no alarm with id {id}."


async def set_monitor_config(ctx: ToolContext, **changes) -> str:
    if not changes:
        return "[monitor] nothing to change. Pass any of: enabled, interval_seconds, autonomy, channels, auto_restart, error_rate_threshold, max_restarts_per_hour."
    cfg = monstate.update_monitor_config(**changes)
    ctx.audit("set_monitor_config", changes, True, "updated")
    return ("[monitor] config updated. Now: "
            f"enabled={cfg.get('enabled')}, interval={cfg.get('interval_seconds')}s, "
            f"autonomy={cfg.get('autonomy')}, auto_restart={cfg.get('auto_restart')}, "
            f"channels={', '.join(cfg.get('channels') or []) or 'none'}. Applied live.")


async def notify_test(ctx: ToolContext, message: str = "") -> str:
    cfg = monstate.load_monitor_config()
    channels = cfg.get("channels") or ["desktop"]
    notifier = Notifier(log=ctx.log)
    await notifier.notify("WebAgent Server Manager",
                          message or "Test notification — alerts are reaching you.",
                          channels)
    return f"[monitor] test notification sent via: {', '.join(channels)}."
