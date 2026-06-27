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
    ``python -m tui_app`` as a fallback. Returns the next steps for the user
    (install the Termux:Widget add-on, add the widget, tap it). Mutating.
    """
    if not _is_termux():
        return ("Home-screen shortcuts are an Android/Termux feature, and this device "
                "isn't Termux — nothing to set up here. On desktop, launch the manager "
                "from its launcher/.exe or with 'python -m tui_app'.")
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    home = Path(os.environ.get("HOME") or Path.home())
    shortcuts = home / ".shortcuts"
    launcher = Path(prefix) / "bin" / "webagent"
    if launcher.exists():
        body = f"#!{prefix}/bin/bash\nexec {launcher}\n"
    else:
        # No installed launcher to defer to — run the package straight from the
        # checkout. There is no ``webagent`` module, so we run ``-m tui_app`` and
        # put the TUI project dir on PYTHONPATH (mirrors install-termux.sh) so the
        # ``tui_app`` package is importable regardless of cwd.
        from ..selfinfo import tui_project_dir
        tui_dir = tui_project_dir()
        pp = (f'export PYTHONPATH="{tui_dir}:${{PYTHONPATH:-}}"\n' if tui_dir else "")
        body = f"#!{prefix}/bin/bash\n{pp}exec python -m tui_app\n"
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


async def uninstall(ctx: ToolContext) -> str:
    """Remove webAgent from this device (Termux only): the launcher, the home-screen
    shortcut, the cloned repo + venv, the manager's data dir, and the pip package.
    Irreversible. Mutating. The caller should close the manager afterward (the code
    being removed is the one running — on Linux/Android the inodes survive until exit).
    """
    if not _is_termux():
        return ("Automatic uninstall is Android/Termux only. On desktop, remove the manager's "
                "install by hand: its launcher/.exe, the cloned repo, and its data folder.")
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    import shutil
    import sys

    from ..config import data_dir
    from ..selfinfo import gather
    from . import install as _install

    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    home = Path(os.environ.get("HOME") or Path.home())
    info = gather()
    repo = Path(info.repo_root) if info.repo_root else (home / "webagent")
    targets = [
        ("launcher", Path(prefix) / "bin" / "webagent"),
        ("home-screen shortcut", home / ".shortcuts" / "webagent.sh"),
        ("repo + virtualenv", repo),
        ("manager data", data_dir()),
    ]
    removed: list[str] = []
    failed: list[str] = []
    for label, p in targets:
        try:
            if p.is_dir():
                shutil.rmtree(p)
                removed.append(f"{label} ({p})")
            elif p.exists():
                p.unlink()
                removed.append(f"{label} ({p})")
        except OSError as e:
            failed.append(f"{label} ({p}): {e}")
    # Best-effort: drop the pip-installed package too.
    try:
        _out, code = await _install._run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "webagent"], None, 90)
        if code == 0:
            removed.append("python package (webagent)")
    except Exception:
        pass
    msg = "[uninstall] removed: " + ("; ".join(removed) if removed else "(nothing found)")
    if failed:
        msg += "\n[uninstall] could NOT remove: " + "; ".join(failed)
    msg += "\nwebAgent has been removed from this device. Closing the manager now."
    return msg
