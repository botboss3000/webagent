"""Remote Access orchestrator.

Owns the active tunnel, computes the current public address for whichever method
is active, and runs a background watcher that re-pushes the signpost whenever a
managed tunnel's address changes (so the phone's fixed bookmark always resolves
to the live PC). Lifecycle hooks (``start_remote_access`` / ``stop_remote_access``)
are wired into app startup/shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import netinfo, signpost, store, tunnels

logger = logging.getLogger(__name__)

_MANAGED = ("ngrok", "cloudflare")
_WATCH_INTERVAL = 10  # seconds between address-change checks
_RESOLVE_TRIES = 15   # ~15s to wait for a fresh tunnel to report its address


class RemoteAccessManager:
    def __init__(self) -> None:
        self._tunnel: Optional[tunnels.ManagedTunnel] = None
        self._method: str = "same_network"
        self._last_pushed_url: str = ""
        self._watch_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ── address resolution ──
    async def _remote_url_for(self, method: str, cfg: dict) -> str:
        if method in _MANAGED and self._tunnel:
            return await self._tunnel.resolve_public_url()
        if method == "tailscale":
            ts = tunnels.tailscale_status(cfg.get("tailscale", {}).get("bin_path", ""))
            if ts.get("ip"):
                return f"http://{ts['ip']}:{netinfo.get_port()}"
            return ""
        if method == "manual":
            return (cfg.get("manual", {}).get("public_url") or "").strip()
        if method == "same_network":
            return netinfo.primary_same_network_url()
        return ""

    async def _maybe_push(self, cfg: dict, url: str) -> None:
        sp = cfg.get("signpost", {})
        if not sp.get("enabled") or sp.get("role") not in ("client", "both"):
            return
        if not url or self._method == "same_network":
            return
        if url == self._last_pushed_url:
            return
        ok = await signpost.push_pointer(
            sp.get("server_url", ""), cfg.get("rendezvous_key", ""),
            cfg.get("push_token", ""), url, sp.get("label", ""),
        )
        if ok:
            self._last_pushed_url = url
            logger.info("Remote Access: pushed address to signpost (%s)", url)

    # ── lifecycle ──
    async def start_method(self, method: str) -> dict:
        async with self._lock:
            self._stop_tunnel()
            self._last_pushed_url = ""
            self._method = method
            cfg = store.update_config({"active_method": method})

            if method in _MANAGED:
                opts = dict(cfg.get(method, {}))
                self._tunnel = tunnels.ManagedTunnel(method, netinfo.get_port(), opts)
                if not self._tunnel.start():
                    err = self._tunnel.error or "failed to start"
                    self._tunnel = None
                    return {"ok": False, "error": err}
                # Wait for the tunnel to report its address.
                url = ""
                for _ in range(_RESOLVE_TRIES):
                    if not self._tunnel.is_alive():
                        return {"ok": False, "error": self._tunnel.error or "tunnel exited",
                                "output": self._tunnel.recent_output()}
                    url = await self._tunnel.resolve_public_url()
                    if url:
                        break
                    await asyncio.sleep(1)

            url = await self._remote_url_for(method, cfg)
            await self._maybe_push(cfg, url)
            self._ensure_watcher()
            return {"ok": True, "public_url": url, "method": method}

    def _stop_tunnel(self) -> None:
        if self._tunnel:
            self._tunnel.stop()
            self._tunnel = None

    async def stop(self) -> dict:
        async with self._lock:
            self._stop_tunnel()
            self._last_pushed_url = ""
            return {"ok": True}

    # ── watcher ──
    def _ensure_watcher(self) -> None:
        if self._watch_task and not self._watch_task.done():
            return
        try:
            self._watch_task = asyncio.create_task(self._watch_loop())
        except RuntimeError:
            self._watch_task = None  # no running loop (shouldn't happen in-app)

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(_WATCH_INTERVAL)
            try:
                if self._method not in _MANAGED or not self._tunnel:
                    continue
                if not self._tunnel.is_alive():
                    continue
                cfg = store.load_config()
                url = await self._tunnel.resolve_public_url()
                if url:
                    await self._maybe_push(cfg, url)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Remote Access watcher tick failed: %s", e)

    def stop_watcher(self) -> None:
        if self._watch_task and not self._watch_task.done():
            self._watch_task.cancel()
        self._watch_task = None

    # ── status for the card ──
    async def runtime_status(self) -> dict:
        running = bool(self._tunnel and self._tunnel.is_alive())
        url = ""
        if self._tunnel:
            url = await self._tunnel.resolve_public_url()
        return {
            "active_method": self._method,
            "running": running,
            "public_url": url,
            "error": self._tunnel.error if self._tunnel else "",
            "output": self._tunnel.recent_output() if self._tunnel else "",
        }


_manager: Optional[RemoteAccessManager] = None


def get_manager() -> RemoteAccessManager:
    global _manager
    if _manager is None:
        _manager = RemoteAccessManager()
    return _manager


async def start_remote_access() -> None:
    """Startup hook — auto-start the configured managed tunnel if requested."""
    cfg = store.load_config()
    mgr = get_manager()
    mgr._method = cfg.get("active_method", "same_network")
    if cfg.get("auto_start") and cfg.get("active_method") in _MANAGED:
        try:
            await mgr.start_method(cfg["active_method"])
            logger.info("Remote Access auto-started method '%s'", cfg["active_method"])
        except Exception as e:
            logger.warning("Remote Access auto-start failed: %s", e)


async def stop_remote_access() -> None:
    """Shutdown hook — stop the watcher and any managed tunnel."""
    mgr = get_manager()
    mgr.stop_watcher()
    try:
        await mgr.stop()
    except Exception:
        pass
