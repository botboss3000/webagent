"""Manager-state tools — things that change the *manager's* situation rather
than the target codebase. Available in onboarding mode (no project required).

v1: ``link_project`` points the manager at an existing webAgent checkout, which
flips it from onboarding into managed mode and re-picks the AI key (the linked
repo's credentials take over). The heavy install flow (cloning a fresh copy,
building the environment) lands in a later phase.
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import WRITES_DISABLED_MSG, ToolContext


async def link_project(ctx: ToolContext, path: str) -> str:
    """Link the manager to an existing webAgent checkout at ``path``."""
    if ctx.set_project is None:
        return "Error: linking is unavailable in this context."
    path = (path or "").strip().strip('"').strip("'")
    if not path:
        return "Error: a folder path is required."
    return await ctx.set_project(path)


def _is_termux() -> bool:
    prefix = os.environ.get("PREFIX", "")
    return bool(os.environ.get("TERMUX_VERSION")) or "com.termux" in prefix \
        or Path("/data/data/com.termux").exists()


async def setup_launch_shortcut(ctx: ToolContext) -> str:
    """Write a Termux:Widget home-screen shortcut that launches the manager.

    Android/Termux only. Creates ``~/.shortcuts/webagent.sh`` (the directory the
    Termux:Widget add-on reads) pointing at the installed ``webagent`` command, or
    ``python -m webagent_tui`` as a fallback. Returns the next steps for the user
    (install the Termux:Widget add-on, add the widget, tap it). Mutating.
    """
    if not _is_termux():
        return ("Home-screen shortcuts are an Android/Termux feature, and this device "
                "isn't Termux — nothing to set up here. On desktop, launch the manager "
                "from its launcher/.exe or with 'python -m webagent_tui'.")
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    home = Path(os.environ.get("HOME") or Path.home())
    shortcuts = home / ".shortcuts"
    launcher = Path(prefix) / "bin" / "webagent"
    if launcher.exists():
        body = f"#!{prefix}/bin/bash\nexec {launcher}\n"
    else:
        body = f"#!{prefix}/bin/bash\nexec python -m webagent_tui\n"
    try:
        shortcuts.mkdir(parents=True, exist_ok=True)
        sc = shortcuts / "webagent.sh"
        sc.write_text(body, encoding="utf-8")
        sc.chmod(0o755)
    except OSError as e:
        ctx.audit("setup_launch_shortcut", {}, False, str(e))
        return f"Couldn't write the shortcut: {e}"
    ctx.audit("setup_launch_shortcut", {"path": str(sc)}, True, "")
    return (
        f"Created the home-screen shortcut at {sc}. To add it to your home screen: "
        "1) install the **Termux:Widget** add-on from F-Droid — it's a separate small "
        "companion app, not part of Termux; 2) on your Android home screen, long-press "
        "an empty spot → Widgets → Termux:Widget; 3) pick **webagent**. Tapping it opens "
        "the manager."
    )
