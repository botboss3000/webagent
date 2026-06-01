"""Manager-state tools — things that change the *manager's* situation rather
than the target codebase. Available in onboarding mode (no project required).

v1: ``link_project`` points the manager at an existing webAgent checkout, which
flips it from onboarding into managed mode and re-picks the AI key (the linked
repo's credentials take over). The heavy install flow (cloning a fresh copy,
building the environment) lands in a later phase.
"""

from __future__ import annotations

from .base import ToolContext


async def link_project(ctx: ToolContext, path: str) -> str:
    """Link the manager to an existing webAgent checkout at ``path``."""
    if ctx.set_project is None:
        return "Error: linking is unavailable in this context."
    path = (path or "").strip().strip('"').strip("'")
    if not path:
        return "Error: a folder path is required."
    return await ctx.set_project(path)
