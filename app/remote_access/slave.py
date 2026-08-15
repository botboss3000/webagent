"""Detached, visible-console tunnel slave used by the Instances page.

The app launches this module in a separate OS process.  The slave owns the
tunnel child, mirrors its output to the visible console, persists a small
freshness snapshot, and accepts token-gated stop/restart commands over loopback.
It deliberately does not import or participate in the app lifecycle.
"""
from __future__ import annotations

import argparse
import codecs
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

# ``python app/remote_access/slave.py`` puts this directory, not the repo root,
# on sys.path.  Add the root so the shared Cloudflare URL parser is reusable.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.remote_access.tunnels import _TRYCF_RE  # noqa: E402


STATUS_FRESH_SECONDS = 60.0
STATUS_REFRESH_SECONDS = 10.0
_NGROK_URL_RE = re.compile(
    r"https://[^\s\"']+\.(?:ngrok(?:-free)?\.app|ngrok\.io)", re.IGNORECASE,
)
_CLOUDFLARE_READY_RE = re.compile(
    r"(?:registered tunnel connection|connection[^\r\n]*registered)", re.IGNORECASE,
)


def status_path(port: int) -> Path:
    return Path(tempfile.gettempdir()) / f"wa_tunnel_{int(port)}.status.json"


def parse_public_url(provider: str, line: str) -> str:
    match = _TRYCF_RE.search(line) if provider == "cloudflare" else _NGROK_URL_RE.search(line)
    return match.group(0) if match else ""


def write_status(port: int, payload: Dict[str, Any]) -> None:
    """Atomically replace the slave status file."""
    path = status_path(port)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def read_status(port: int, *, max_age: float = STATUS_FRESH_SECONDS) -> Optional[Dict[str, Any]]:
    """Return a fresh status payload, or None for missing/stale/invalid data."""
    try:
        data = json.loads(status_path(port).read_text(encoding="utf-8"))
        ts = float(data.get("ts") or 0)
        age = time.time() - ts
        if ts <= 0 or age < -5.0 or age >= max_age:
            return None
        return data
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def control_request(port: int, action: str, token: str, *, timeout: float = 10.0) -> Dict[str, Any]:
    """Send a token-gated command to an already-running local slave."""
    body = json.dumps({"token": token}).encode("utf-8")
    req = urlrequest.Request(
        f"http://127.0.0.1:{int(port) + 1}/{action}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def probe_control(port: int, *, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
    try:
        with urlrequest.urlopen(f"http://127.0.0.1:{int(port) + 1}/status", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
            return data if data.get("kind") == "webagent-tunnel-slave" else None
    except (OSError, ValueError, urlerror.URLError, json.JSONDecodeError):
        return None


class TunnelSlave:
    def __init__(self, *, port: int, token: str, provider: str, quick: bool,
                 name: str = "", bin_path: str = "", public_url: str = "") -> None:
        self.port = port
        self.token = token
        self.provider = provider
        self.quick = quick
        self.name = name
        self.bin_path = bin_path
        self.configured_url = public_url.strip().rstrip("/")
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self.state = "starting"
        self.url = ""
        self.exit_code: Optional[int] = None
        self.error = ""
        self.started_at = 0.0
        self._stop_requested = False
        self._lock = threading.RLock()
        self._quitting = threading.Event()
        self._server: Optional[ThreadingHTTPServer] = None

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            data: Dict[str, Any] = {
                "kind": "webagent-tunnel-slave",
                "state": self.state,
                "url": self.url,
                "pid": os.getpid(),
                "ts": time.time(),
                "provider": self.provider,
                "started_at": self.started_at,
            }
            if self.exit_code is not None:
                data["exit_code"] = self.exit_code
            if self.error:
                data["error"] = self.error
            return data

    def persist(self, *, report: bool = False) -> None:
        data = self.snapshot()
        try:
            write_status(self.port, data)
        except OSError as exc:
            print(f"[WebAgent] Could not write status: {exc}", flush=True)
        if report:
            self._report(data)

    def _report(self, data: Dict[str, Any]) -> None:
        payload = dict(data)
        payload["token"] = self.token
        req = urlrequest.Request(
            f"http://127.0.0.1:{self.port}/admin/instances/tunnel/report",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=2.0):
                pass
        except (OSError, urlerror.URLError):
            # The whole point of the status file is to survive/report through an
            # app restart, so a temporarily absent endpoint is expected.
            pass

    def _argv(self) -> list[str]:
        binary_name = "cloudflared" if self.provider == "cloudflare" else "ngrok"
        binary = self.bin_path or shutil.which(binary_name)
        if not binary:
            raise RuntimeError(f"{binary_name} not found on PATH")
        if self.provider == "cloudflare":
            if self.quick:
                return [binary, "tunnel", "--url", f"http://localhost:{self.port}"]
            if not self.name:
                raise RuntimeError("no Cloudflare tunnel name configured")
            return [binary, "tunnel", "run", self.name]
        argv = [binary, "http", str(self.port), "--log", "stdout"]
        if self.name:
            argv += ["--domain", self.name]
        return argv

    def start_child(self) -> None:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return
            argv = self._argv()
            self.state = "starting"
            self.url = self.configured_url
            self.exit_code = None
            self.error = ""
            self._stop_requested = False
            self.started_at = time.time()
            print(f"[WebAgent] Starting {self.provider} tunnel for localhost:{self.port}", flush=True)
            print(f"[WebAgent] Command: {subprocess.list2cmdline(argv)}", flush=True)
            self.proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # Read raw chunks instead of text lines. cloudflared sometimes
                # emits progress/log records without a newline when stdout is a
                # pipe; a line iterator then leaves the visible console looking
                # frozen even though the tunnel is healthy.
                text=False,
                bufsize=0,
            )
        threading.Thread(target=self._drain_child, name="tunnel-output", daemon=True).start()
        self.persist(report=True)

    def _drain_child(self) -> None:
        proc = self.proc
        if not proc:
            return
        try:
            if proc.stdout:
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                tail = ""
                stream = proc.stdout
                while True:
                    if hasattr(stream, "read"):
                        raw = stream.read(4096)
                    else:  # small fake streams used by unit tests
                        try:
                            raw = next(stream)
                        except StopIteration:
                            raw = b""
                    if not raw:
                        break
                    chunk = decoder.decode(raw) if isinstance(raw, bytes) else str(raw)
                    if not chunk:
                        continue
                    # Mirror bytes as soon as cloudflared emits them. This keeps
                    # its connection/retry details live in the slave console.
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    tail = (tail + chunk)[-8192:]
                    public_url = parse_public_url(self.provider, tail)
                    ready = bool(public_url) or (
                        self.provider == "cloudflare" and bool(_CLOUDFLARE_READY_RE.search(tail))
                    )
                    if ready and self.state != "running":
                        with self._lock:
                            if public_url:
                                self.url = public_url.rstrip("/")
                            self.state = "running"
                        if not chunk.endswith(("\n", "\r")):
                            print(flush=True)
                        print(f"[WebAgent] Public URL: {self.url}", flush=True)
                        self.persist(report=True)
            code = proc.wait()
        except Exception as exc:  # pragma: no cover - defensive around OS pipes
            code = proc.poll()
            with self._lock:
                self.error = str(exc)
        with self._lock:
            self.exit_code = code
            if self._stop_requested or code == 0:
                self.state = "stopped"
            else:
                self.state = "error"
                if not self.error:
                    self.error = f"tunnel process exited with code {code}"
        print(f"[WebAgent] Tunnel {self.state} (exit code {code}).", flush=True)
        self.persist(report=True)

    def stop_child(self) -> None:
        with self._lock:
            proc = self.proc
            self._stop_requested = True
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        with self._lock:
            if not proc:
                self.state = "stopped"
                self.exit_code = 0
        self.persist(report=True)

    def restart_child(self) -> None:
        self.stop_child()
        self.start_child()

    def request_quit(self) -> None:
        self._quitting.set()
        server = self._server
        if server:
            threading.Thread(target=server.shutdown, daemon=True).start()

    def _refresh_loop(self) -> None:
        while not self._quitting.wait(STATUS_REFRESH_SECONDS):
            self.persist()

    def serve(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _json(self, status: int, payload: Dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                if self.path != "/status":
                    self._json(404, {"detail": "not found"})
                    return
                self._json(200, owner.snapshot())

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
                if self.path not in ("/stop", "/restart"):
                    self._json(404, {"detail": "not found"})
                    return
                try:
                    size = min(int(self.headers.get("Content-Length", "0") or 0), 8192)
                    body = json.loads(self.rfile.read(size).decode("utf-8") or "{}")
                except (ValueError, json.JSONDecodeError):
                    self._json(400, {"detail": "invalid JSON"})
                    return
                if not hmac.compare_digest(str(body.get("token") or ""), owner.token):
                    self._json(403, {"detail": "invalid token"})
                    return
                if self.path == "/restart":
                    try:
                        owner.restart_child()
                        self._json(200, {"ok": True, **owner.snapshot()})
                    except Exception as exc:
                        self._json(500, {"detail": str(exc)})
                    return
                owner.stop_child()
                self._json(200, {"ok": True, **owner.snapshot()})
                owner.request_quit()

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port + 1), Handler)
        threading.Thread(target=self._refresh_loop, name="status-refresh", daemon=True).start()
        self.start_child()
        print(f"[WebAgent] Control endpoint: http://127.0.0.1:{self.port + 1}", flush=True)
        try:
            self._server.serve_forever(poll_interval=0.25)
        finally:
            self._quitting.set()
            self._server.server_close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WebAgent detached tunnel slave")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--provider", choices=("cloudflare", "ngrok"), default="cloudflare")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--quick", action="store_true")
    mode.add_argument("--name", default="")
    parser.add_argument("--bin", dest="bin_path", default="", help=argparse.SUPPRESS)
    parser.add_argument("--public-url", default="", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not (1 <= args.port <= 65534):
        print("[WebAgent] Port must be between 1 and 65534.", file=sys.stderr)
        return 2
    slave = TunnelSlave(
        port=args.port,
        token=args.token,
        provider=args.provider,
        quick=bool(args.quick or not args.name),
        name=args.name,
        bin_path=args.bin_path,
        public_url=args.public_url,
    )
    try:
        slave.serve()
        return 0
    except KeyboardInterrupt:
        print("\n[WebAgent] Stopping tunnel…", flush=True)
        slave.stop_child()
        return 0
    except Exception as exc:
        with slave._lock:
            slave.state = "error"
            slave.error = str(exc)
        slave.persist(report=True)
        print(f"[WebAgent] Tunnel slave failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
