"""Agent-facing tools for the self-healing Playbook (issue knowledge base).

These read/write the manager's OWN store (``webagent.db``) — the watchdog's
learned issues, the remedies tried against each, and the incident log. Like the
monitor tools they edit manager-owned state (never the linked repo) and run a
shell command for nobody, so they are not gated behind "Allow writes" and work in
onboarding mode. The watchdog picks up new/edited remedies on its next reaction.
"""

from __future__ import annotations

from .. import playbook
from .base import ToolContext

CATALOG_KINDS = playbook.CATALOG_KINDS  # re-export for the registry's schema enum


def _pct(remedy: dict) -> str:
    tried = int(remedy.get("times_tried") or 0)
    if not tried:
        return "untried"
    return (f"{int(round(playbook.confidence(remedy) * 100))}% "
            f"({remedy.get('times_helped', 0)}✓/{remedy.get('times_didnt_help', 0)}✗)")


def _remedy_line(r: dict) -> str:
    human = playbook.REMEDY_HUMAN.get(r.get("kind"), r.get("kind"))
    extra = f" — {r['payload'][:80]}" if r.get("payload") else ""
    return (f"  {r.get('id')}: {human}{extra} [{r.get('status')}] "
            f"confidence {_pct(r)}")


async def playbook_list(ctx: ToolContext) -> str:
    """List every issue the watchdog has learned, with its best-known remedy."""
    if ctx.store is None:
        return "[playbook] knowledge store unavailable."
    issues = ctx.store.pb_list_issues()
    if not issues:
        return "[playbook] no issues recorded yet — the watchdog hasn't seen any."
    out = [f"[playbook] {len(issues)} known issue(s):"]
    for iss in issues:
        remedies = ctx.store.pb_remedies_for(iss["key"])
        best = playbook.best_remedy(remedies)
        best_txt = (f"{playbook.REMEDY_HUMAN.get(best['kind'], best['kind'])} "
                    f"(confidence {_pct(best)})") if best else "no remedy yet"
        flags = []
        if iss.get("programmed"):
            flags.append("trigger-programmed")
        flag_txt = f" [{', '.join(flags)}]" if flags else ""
        out.append(f"  {iss['key']}: {iss.get('label')} — seen {iss.get('occurrences')}× "
                   f"· status {iss.get('status')}{flag_txt} → {best_txt}")
    return "\n".join(out)


async def playbook_show(ctx: ToolContext, key: str) -> str:
    """Show one issue in full: its remedies (with helped/didn't stats) and the most
    recent incidents (how it was triggered and how each was resolved)."""
    if ctx.store is None:
        return "[playbook] knowledge store unavailable."
    iss = ctx.store.pb_get_issue(key)
    if not iss:
        return f"[playbook] no issue with key '{key}'. Use playbook_list to see keys."
    out = [f"[playbook] {iss.get('label')}  ({key})",
           f"  kind: {iss.get('kind')} · seen {iss.get('occurrences')}× · "
           f"status {iss.get('status')} · programmed: {bool(iss.get('programmed'))}"]
    remedies = playbook.rank_remedies(ctx.store.pb_remedies_for(key))
    out.append("  remedies (best first):" if remedies else "  remedies: none yet")
    out += [_remedy_line(r) for r in remedies]
    incidents = ctx.store.pb_recent_incidents(key, limit=8)
    if incidents:
        out.append("  recent incidents:")
        for inc in incidents:
            out.append(f"    {inc.get('outcome')} — {(inc.get('trigger') or '')[:90]}")
    return "\n".join(out)


async def playbook_record_remedy(ctx: ToolContext, issue_key: str, kind: str,
                                 payload: str = "", approved: bool = False) -> str:
    """Record a remedy for an issue after you've diagnosed it. ``kind`` is one of the
    catalog actions (restart_server, clear_port, escalate_to_agent, notify_only) or
    'command' (a shell command in ``payload``) or 'note' (a written instruction).
    Custom 'command' remedies start as 'suggested' and must be approved before they
    can auto-run; pass approved=true only when the user is happy for it to run
    unattended. The watchdog will rank this against the issue's other remedies."""
    if ctx.store is None:
        return "[playbook] knowledge store unavailable."
    if kind not in playbook.CATALOG_KINDS:
        return f"[playbook] unknown remedy kind '{kind}'. Use one of: {', '.join(playbook.CATALOG_KINDS)}."
    if not ctx.store.pb_get_issue(issue_key):
        return f"[playbook] no issue with key '{issue_key}'. Use playbook_list to see keys."
    # Safe built-in actions can be approved on sight; commands/notes default to suggested.
    status = "approved" if (approved or kind in playbook.SAFE_KINDS) else "suggested"
    r = ctx.store.pb_add_remedy(issue_key, kind, payload=payload, status=status)
    ctx.audit("playbook_record_remedy", {"issue": issue_key, "kind": kind}, True, status)
    return (f"[playbook] remedy {r['id']} ({kind}) recorded for '{issue_key}' as {status}. "
            + ("It can auto-run within the remediation policy." if status == "approved"
               else "Approve it (playbook_set_remedy status=approved) to let it auto-run."))


async def playbook_set_remedy(ctx: ToolContext, remedy_id: str, status: str = "",
                              priority: int = None) -> str:
    """Approve, disable, or re-prioritise a remedy. status ∈ approved|suggested|disabled.
    Higher priority breaks confidence ties."""
    if ctx.store is None:
        return "[playbook] knowledge store unavailable."
    if status and status not in ("approved", "suggested", "disabled"):
        return "[playbook] status must be approved, suggested, or disabled."
    ok = ctx.store.pb_set_remedy(remedy_id, status=status or None, priority=priority)
    ctx.audit("playbook_set_remedy", {"id": remedy_id, "status": status, "priority": priority},
              ok, "updated" if ok else "not found")
    return (f"[playbook] remedy {remedy_id} updated." if ok
            else f"[playbook] no remedy with id {remedy_id}.")


async def playbook_forget(ctx: ToolContext, key: str = "", remedy_id: str = "") -> str:
    """Delete a whole issue (key=...) — its remedies + incident history — or a single
    remedy (remedy_id=...). Use when something was learned wrong."""
    if ctx.store is None:
        return "[playbook] knowledge store unavailable."
    if remedy_id:
        ok = ctx.store.pb_remove_remedy(remedy_id)
        ctx.audit("playbook_forget", {"remedy_id": remedy_id}, ok, "removed" if ok else "missing")
        return f"[playbook] remedy {remedy_id} removed." if ok else f"[playbook] no remedy {remedy_id}."
    if key:
        ok = ctx.store.pb_forget(key)
        ctx.audit("playbook_forget", {"key": key}, ok, "removed" if ok else "missing")
        return f"[playbook] issue '{key}' and its history removed." if ok else f"[playbook] no issue '{key}'."
    return "[playbook] pass either key=... or remedy_id=... to forget."
