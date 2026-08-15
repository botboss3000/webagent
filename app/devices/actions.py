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
    if await _stop_slave_if_present():
        logger.info("Device action: stopped detached tunnel slave")
        return "slave tunnel stopped"
    from app.remote_access.manager import get_manager
    await get_manager().stop()
    logger.info("Device action: stopped tunnel")
    return "tunnel stopped"


register_action("start_tunnel", _start_tunnel)


# ── Detached tunnel slave ────────────────────────────────────────────────────
# The Instances page launches an independent Python controller. On Windows it
# receives a visible console; on every platform it survives app restarts and is
# driven through a token-gated loopback endpoint. The old managed action remains
# available as the launch-failure fallback and for the Remote Access card.

async def _stop_slave_if_present() -> bool:
    import asyncio

    from app.remote_access import netinfo, store
    from app.remote_access.slave import control_request, probe_control

    port = netinfo.get_port()
    status = await asyncio.to_thread(probe_control, port)
    if not status:
        return False
    token = str((store.load_config().get("slave_tokens") or {}).get(str(port)) or "")
    if not token:
        raise RuntimeError("a tunnel slave is running but its control token is unavailable")
    await asyncio.to_thread(control_request, port, "stop", token)
    store.update_slave_state(port, running=False, url="", clear_token=True)
    return True


async def _start_slave_tunnel(*, job: dict, db: Any, payload: dict) -> str:
    import asyncio
    from pathlib import Path
    import secrets
    import shutil
    import socket
    import subprocess
    import sys
    import time

    from app.remote_access import netinfo, store
    from app.remote_access.slave import control_request, probe_control, read_status

    port = netinfo.get_port()
    cfg = store.load_config()
    provider = (payload.get("provider") or cfg.get("active_method") or "").strip()
    if provider not in ("cloudflare", "ngrok"):
        raise RuntimeError("no Cloudflare or ngrok tunnel is set up on this device")

    tokens = cfg.get("slave_tokens") or {}
    existing_token = str(tokens.get(str(port)) or "")
    existing = await asyncio.to_thread(probe_control, port)
    if existing:
        if existing.get("state") in ("starting", "running"):
            url = str(existing.get("url") or "")
            return f"Slave tunnel already running: {url or '(resolving address)'}"
        if not existing_token:
            raise RuntimeError("a tunnel slave owns the control port but its token is unavailable")
        restarted = await asyncio.to_thread(control_request, port, "restart", existing_token)
        return f"Slave tunnel restarted: {restarted.get('url') or '(resolving address)'}"

    opts = cfg.get(provider, {}) or {}
    binary_name = "cloudflared" if provider == "cloudflare" else "ngrok"
    configured_bin = str(opts.get("bin_path") or "").strip()
    binary = (configured_bin if configured_bin and Path(configured_bin).is_file()
              else shutil.which(configured_bin or binary_name))

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port + 1))
    except OSError as exc:
        # Do not start a managed tunnel when an unresponsive slave (or another
        # service) may already own the control port: that could double-launch.
        raise RuntimeError(f"tunnel slave control port {port + 1} is already in use") from exc

    proc = None
    try:
        if not binary:
            raise RuntimeError(f"{binary_name} not found on PATH — install it first")
        token = secrets.token_urlsafe(32)
        slave_path = Path(__file__).resolve().parents[1] / "remote_access" / "slave.py"
        argv = [sys.executable, str(slave_path), "--port", str(port), "--token", token,
                "--provider", provider, "--bin", str(binary)]
        if provider == "cloudflare":
            if opts.get("quick"):
                argv.append("--quick")
            elif opts.get("tunnel"):
                argv += ["--name", str(opts["tunnel"])]
            else:
                raise RuntimeError("no Cloudflare quick or named tunnel is configured")
        elif opts.get("domain"):
            argv += ["--name", str(opts["domain"])]
        else:
            argv.append("--quick")

        store.update_slave_state(port, token=token, running=True, url="")
        popen_kwargs: Dict[str, Any] = {
            "cwd": str(Path(__file__).resolve().parents[2]), "close_fds": True,
        }
        if sys.platform.startswith("win"):
            popen_kwargs["creationflags"] = 0x00000010  # CREATE_NEW_CONSOLE
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(argv, **popen_kwargs)

        deadline = time.monotonic() + 3.0
        status = None
        while time.monotonic() < deadline:
            status = await asyncio.to_thread(probe_control, port, timeout=0.3)
            if status or proc.poll() is not None:
                break
            await asyncio.sleep(0.1)
        if not status:
            detail = read_status(port) or {}
            raise RuntimeError(str(detail.get("error") or "tunnel slave did not open its control port"))
        logger.info("Device action: detached %s tunnel slave launched (pid=%s)", provider, proc.pid)
        return f"Slave tunnel launched: {status.get('url') or '(resolving address)'}"
    except Exception as slave_error:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        store.update_slave_state(port, running=False, url="", clear_token=True)
        logger.warning("Tunnel slave launch failed; trying managed fallback: %s", slave_error)
        try:
            managed = await _start_tunnel(job=job, db=db, payload=payload)
        except Exception as managed_error:
            raise RuntimeError(str(slave_error)) from managed_error
        return f"Managed fallback after slave launch failed ({slave_error}): {managed}"


async def _stop_slave_tunnel(*, job: dict, db: Any, payload: dict) -> str:
    if await _stop_slave_if_present():
        logger.info("Device action: stopped tunnel slave")
        return "slave tunnel stopped"
    return await _stop_tunnel(job=job, db=db, payload=payload)


register_action("stop_tunnel", _stop_tunnel)
register_action("slave_tunnel", _start_slave_tunnel)
register_action("slave_stop", _stop_slave_tunnel)


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
