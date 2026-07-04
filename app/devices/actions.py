"""Device job ACTIONS — non-agent commands one device runs on another.

A normal device job means "run this ``prompt`` through the agent loop on the
target device" (app/devices/worker.py ``_execute``). An ACTION job instead
carries ``payload = {"action": "<name>", ...}`` and runs a small local handler
with NO agent — fleet control the user triggers from the Instances page, e.g.
starting the Cloudflare tunnel on a remote instance that has it set up but off.

Why a registry (not an if/elif in the worker): actions are meant to grow
(start/stop tunnel today; other providers, restart, etc. later) WITHOUT touching
the core claim/execute loop. The worker calls ``run_device_action`` for any job
whose payload names an action; new capabilities ``register_action`` here.

Each handler is ``async def h(*, job, db, payload) -> str`` and returns a short
human-readable result (stored on the job's ``result_excerpt``). Raise to mark the
job errored — the worker records the message.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

logger = logging.getLogger(__name__)

ActionHandler = Callable[..., Awaitable[str]]
_REGISTRY: Dict[str, ActionHandler] = {}


def register_action(name: str, handler: ActionHandler) -> None:
    """Register (or replace) the handler for an action name."""
    _REGISTRY[name] = handler


def has_action(name: str) -> bool:
    return name in _REGISTRY


async def run_device_action(name: str, *, job: dict, db: Any, payload: dict) -> str:
    """Dispatch a named action to its handler. Raises on an unknown action."""
    handler = _REGISTRY.get(name)
    if not handler:
        raise RuntimeError(f"unknown device action {name!r}")
    return await handler(job=job, db=db, payload=payload)


# ── Built-in actions ─────────────────────────────────────────────────────────
# Remote Access tunnel control. These reuse the SAME RemoteAccessManager
# singleton the local admin card / agent tool drive, so a remote Start can't
# spawn a second cloudflared fighting the local one (see app/remote_access).

async def _start_tunnel(*, job: dict, db: Any, payload: dict) -> str:
    from app.remote_access import store
    from app.remote_access.manager import get_manager

    cfg = store.load_config()
    # Prefer the sender's explicit provider hint; else start whatever managed
    # method this device has configured (mirrors the boot auto-start).
    method = (payload.get("provider") or cfg.get("active_method") or "").strip()
    if method not in ("cloudflare", "ngrok"):
        raise RuntimeError("no tunnel is set up on this device")
    res = await get_manager().start_method(method)
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "tunnel failed to start")
    url = res.get("public_url") or "(resolving address)"
    logger.info("Device action: started %s tunnel → %s", method, url)
    return f"{method} tunnel started: {url}"


async def _stop_tunnel(*, job: dict, db: Any, payload: dict) -> str:
    from app.remote_access.manager import get_manager
    await get_manager().stop()
    logger.info("Device action: stopped tunnel")
    return "tunnel stopped"


register_action("start_tunnel", _start_tunnel)
register_action("stop_tunnel", _stop_tunnel)


# ── Server / repo fleet control ───────────────────────────────────────────────
# Restart this device's server, or bring its OWN app repo up to date / commit +
# push it — the "run it on that box, not here" versions of the local Server Reset
# and the Source-Control ⭐ button. Each runs on the TARGET device's machine when
# its worker claims the job, reusing the same helpers the local UI routes use so
# behaviour is identical. All git actions are PINNED to the running app's own repo
# (project root) — never whatever repo that device's admin last selected — so a
# fleet "pull + restart" is deterministic across every instance.

async def _restart_server(*, job: dict, db: Any, payload: dict) -> str:
    from app.relauncher import trigger_restart
    # Schedule the exit with a few seconds' grace so the worker can mark THIS job
    # 'done' before the process dies. Without the grace the job would stay
    # 'claimed', get reclaimed after its lease expires, and restart the box again
    # in a loop. Returns immediately; the daemon thread exits the process after.
    res = trigger_restart(delay=3.0)
    if not res.get("auto_restart"):
        raise RuntimeError(res.get("reason") or "this device can't restart itself")
    logger.info("Device action: restarting server on this device")
    return res.get("message") or "server is restarting"


async def _git_pull(*, job: dict, db: Any, payload: dict) -> str:
    from app.api.github import _pin_to_project_root, pull_repo
    _pin_to_project_root()
    res = await pull_repo()
    if res.get("status") == "error":
        raise RuntimeError(res.get("message") or "git pull failed")
    msg = res.get("message") or "pulled"
    if res.get("backend_changed"):
        msg += " (backend files changed — a restart is recommended)"
    logger.info("Device action: git pull → %s", msg)
    return msg


async def _git_commit_push(*, job: dict, db: Any, payload: dict) -> str:
    from app.api.github import _pin_to_project_root, commit_and_push_repo
    _pin_to_project_root()
    res = await commit_and_push_repo(payload.get("message") or "")
    status = res.get("status")
    message = res.get("message") or status or "done"
    if status in ("error", "blocked"):
        raise RuntimeError(message)
    logger.info("Device action: commit+push → %s: %s", status, message)
    # Prefix the status (committed / nothing_to_commit) so the Instances page can
    # show a clear one-line outcome.
    return f"{status}: {message}" if status else message


register_action("restart_server", _restart_server)
register_action("git_pull", _git_pull)
register_action("git_commit_push", _git_commit_push)
