"""Delegation tools — the main agent's interface to subagents (mk2).

These three tools let the main agent fan work out to background subagents and
keep going. They are thin: the real machinery (the sub-session, the Textual
worker, the completion callback) lives in the app via ``ctx.spawn_subagent`` and
``ctx.subagents``. Each tool returns immediately — none of them block on a
subagent finishing.

Caller-scoped tools: a subagent only ever gets the EXACT tool names passed in
``tools``. There is no inherited default set, so a fan-out worker can be made
read-only (e.g. ``["read_source", "search_source"]``) or given write/run access
explicitly — entirely the delegating call's choice.
"""

from __future__ import annotations

from .base import ToolContext


def _validate_tools(ctx: ToolContext, tools: list[str]) -> tuple[list[str], str]:
    """Keep only known, non-delegation tool names. Returns (clean, warning)."""
    available = set(ctx.available_tools or [])
    blocked = {"delegate_async", "delegate_background", "check_task"}  # no recursion (this layer)
    clean, unknown, dropped = [], [], []
    for t in tools or []:
        if t in blocked:
            dropped.append(t)
        elif available and t not in available:
            unknown.append(t)
        else:
            clean.append(t)
    notes = []
    if unknown:
        notes.append(f"unknown tools ignored: {', '.join(unknown)}")
    if dropped:
        notes.append(f"nested-delegation tools not allowed for subagents: {', '.join(dropped)}")
    return clean, ("; ".join(notes))


async def _delegate(ctx: ToolContext, task: str, tools: list[str], mode: str,
                    group_id: str = "") -> str:
    if ctx.spawn_subagent is None:
        return ("Error: subagent delegation isn't available in this context "
                "(no app worker host).")
    if not (task or "").strip():
        return "Error: 'task' is required — describe what the subagent should do."
    clean, warn = _validate_tools(ctx, tools)
    if not clean:
        return ("Error: a subagent needs at least one valid tool in 'tools'. "
                + (warn or "Pass e.g. [\"read_source\", \"search_source\"]."))
    result = await ctx.spawn_subagent(task.strip(), clean, mode, (group_id or "").strip())
    return result + (f"  (note: {warn})" if warn else "")


async def delegate_async(ctx: ToolContext, task: str, tools: list[str],
                         mode: str = "gather", group_id: str = "") -> str:
    """Launch a subagent that reports back when done (gather or notify mode)."""
    mode = mode if mode in ("gather", "notify") else "gather"
    return await _delegate(ctx, task, tools, mode, group_id)


async def delegate_background(ctx: ToolContext, task: str, tools: list[str]) -> str:
    """Launch a fire-and-forget subagent (background mode — never auto-triggers)."""
    return await _delegate(ctx, task, tools, "background", "")


async def check_task(ctx: ToolContext, task_id: str) -> str:
    """Report a delegated task's status + summary (running / completed / error)."""
    if ctx.subagents is None:
        return "Error: no subagent registry in this context."
    rec = ctx.subagents.get((task_id or "").strip())
    if rec is None:
        running = ctx.subagents.running()
        hint = (" In-flight: " + ", ".join(r.task_id for r in running)) if running else ""
        return f"No task with id '{task_id}'.{hint}"
    if rec.status == "running":
        return f"Task {rec.task_id} is still running: {rec.description}"
    tag = "completed" if rec.status == "completed" else "ended with an error"
    return (f"Task {rec.task_id} {tag}: {rec.description}\n"
            f"Result: {rec.result_summary or '(no output)'}\n"
            f"(transcript in session {rec.session_id})")
