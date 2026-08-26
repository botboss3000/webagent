#!/usr/bin/env python3
"""Preview any remote commit as a live WebAgent on its own port (8082).

Browse origin/<branch> history with the arrow keys; Enter runs the highlighted
commit in a detached git worktree (../app-preview); Esc stops the running
preview (or exits when nothing is running); Q quits and leaves it running;
R re-fetches.

Why a worktree: the preview shares this repo's .git object store, so it is a
full checkout of any historical commit at near-zero storage cost, and it can
never touch main (detached HEAD). It is launched with WEBAGENT_PORT, which the
port-aware run.py reads, so it can never fight the hub on 8080.

Companion scripts (Windows):
  preview-commit.bat   this browser (pass --run <sha> to start headless)
  preview-stop.bat     stop the preview (kills whatever serves :8082)
  preview-clean.bat    stop + delete the worktree + registry entry

Modes:
  (none)         interactive commit browser
  --run <sha>    start <sha> headless (no TUI)
  --stop         stop the running preview
  --clean        stop, remove worktree + registry entry
  --status       print preview state (running/busy/stopped)
  --list         print the commit list and exit
"""
from __future__ import annotations

import json
import msvcrt
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WT = ROOT.parent / "app-preview"
PORT = int(os.environ.get("PREVIEW_PORT", "8082"))  # 8081 is used by the hub's Cloudflare tunnel
REGISTRY = ROOT / "data" / "config" / "local-instances.json"
STATEDIR = ROOT / "data" / "local-instances"
PID_FILE = STATEDIR / "preview.pid"
LOG_FILE = STATEDIR / "preview.log"
BOOT_WAIT = 120.0
IS_WIN = sys.platform == "win32"
_CREATE_FLAGS = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


def out(s: str = "") -> None:
    print(s, flush=True)


def git(*args, cwd=None, timeout=60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd or ROOT), capture_output=True,
                          text=True, timeout=timeout)


# ── repo / remote ────────────────────────────────────────────────────────────
def remote_ref() -> str:
    for b in ("main", "master"):
        if git("rev-parse", "--verify", f"origin/{b}").returncode == 0:
            return f"origin/{b}"
    out("[preview] no origin/main or origin/master — check your remotes.")
    sys.exit(1)


def fetch() -> None:
    out("[preview] fetching origin ...")
    try:
        r = git("fetch", "origin", "--prune", timeout=180)
        if r.returncode != 0:
            out(f"[preview] WARNING: fetch failed ({r.stderr.strip()[:200]}) — using local refs.")
    except subprocess.TimeoutExpired:
        out("[preview] WARNING: fetch timed out — using local refs.")


def commit_rows(ref: str) -> list:
    r = git("log", ref, "--pretty=format:%H %ad %s", "--date=format:%Y-%m-%d %H:%M")
    if r.returncode != 0:
        out(f"[preview] git log failed: {r.stderr.strip()}")
        sys.exit(1)
    rows = [(ln.split()[0], ln) for ln in r.stdout.splitlines() if ln.strip()]
    if not rows:
        out("[preview] no commits found.")
        sys.exit(1)
    return rows


# ── preview state ────────────────────────────────────────────────────────────
def health_ok() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
            return r.status < 500 and b"status" in r.read(2000)
    except Exception:
        return False


def port_listeners(port: int) -> list:
    pids = []
    try:
        txt = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=10).stdout
        for line in txt.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if parts and parts[-1].isdigit():
                    pids.append(int(parts[-1]))
    except Exception:
        pass
    return pids


def state() -> str:
    """'running' (WebAgent answering) | 'busy' (port taken by something else) | 'stopped'."""
    if health_ok():
        return "running"
    return "busy" if port_listeners(PORT) else "stopped"


def stop_preview(quiet: bool = False) -> bool:
    st = state()
    if st == "stopped":
        if not quiet:
            out(f"[preview] nothing running on :{PORT}.")
        return False
    if st == "busy":
        out(f"[preview] WARNING: port {PORT} is held by a non-WebAgent process — not touching it.")
        return False
    for pid in set(port_listeners(PORT)):
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
        out(f"[preview] stopped pid {pid}.")
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline and port_listeners(PORT):
        time.sleep(0.5)
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    out(f"[preview] port {PORT} is clear.")
    return True


# ── worktree ─────────────────────────────────────────────────────────────────
def ensure_worktree(sha: str) -> None:
    if (WT / ".git").exists():
        # Already a preview worktree of this repo — re-point it (detached, forced:
        # the preview is disposable, so local dirt is discarded).
        r = git("checkout", "--detach", "-f", sha, cwd=WT)
        if r.returncode != 0:
            out(f"[preview] re-point failed: {r.stderr.strip()}")
            sys.exit(1)
        git("clean", "-fd", cwd=WT)  # drop stale untracked files (keeps ignored .venv/data)
        out(f"[preview] worktree re-pointed to {sha[:12]}.")
        return
    if WT.exists():
        out(f"[preview] {WT} exists but is not a worktree of this repo.")
        out("[preview] run preview-clean.bat (or delete the folder) and retry.")
        sys.exit(1)
    git("worktree", "prune")
    r = git("worktree", "add", "--detach", str(WT), sha)
    if r.returncode != 0:
        out(f"[preview] worktree add failed: {r.stderr.strip()}")
        sys.exit(1)
    out(f"[preview] worktree ready at {WT}.")


def remove_worktree() -> None:
    if not (WT / ".git").exists():
        return
    git("worktree", "remove", "--force", str(WT))
    if WT.exists():
        shutil.rmtree(WT, ignore_errors=True)


def sanitize_worktree() -> None:
    """Some historical commits contain a non-UTF8 byte in .gitignore (a CP1252
    em-dash saved as raw 0x97). hatchling reads .gitignore as UTF-8 and the whole
    `uv sync` dies with UnicodeDecodeError. The worktree is disposable, so fix the
    file in place (decode as cp1252 — every byte maps — then re-encode as UTF-8).
    This is why the preview works on ANY commit, old or new."""
    for rel in (".gitignore",):
        p = WT / rel
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        try:
            raw.decode("utf-8")
            continue  # fine already
        except UnicodeDecodeError:
            pass
        try:
            fixed = raw.decode("cp1252").encode("utf-8")
        except UnicodeDecodeError:
            continue  # give up on this file
        p.write_bytes(fixed)
        out(f"[preview] sanitized {rel} (invalid UTF-8 bytes -> UTF-8).")


def is_port_aware() -> bool:
    try:
        return "WEBAGENT_PORT" in (WT / "run.py").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


# ── registry (lets the hub's Instances view see / stop the preview) ──────────
def write_registry() -> None:
    data = {}
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    insts = [i for i in (data.get("instances") or []) if i.get("id") != "preview"]
    insts.append({"id": "preview",
                  "label": "Preview commit (:8082)",
                  "folder": str(WT).replace("\\", "/"),
                  "port": PORT})
    out_data = {"instances": insts}
    if data.get("hub_port"):
        out_data["hub_port"] = data["hub_port"]
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(out_data, indent=2), encoding="utf-8")
    out("[preview] registered in the Instances view (id=preview).")


def remove_registry() -> None:
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        return
    insts = [i for i in (data.get("instances") or []) if i.get("id") != "preview"]
    out_data = {"instances": insts}
    if data.get("hub_port"):
        out_data["hub_port"] = data["hub_port"]
    REGISTRY.write_text(json.dumps(out_data, indent=2), encoding="utf-8")


# ── start ────────────────────────────────────────────────────────────────────
def start_preview(sha: str, subject: str) -> None:
    if state() == "busy":
        out(f"[preview] port {PORT} is taken by another process — stop it first.")
        return
    stop_preview(quiet=True)  # one preview at a time
    ensure_worktree(sha)
    sanitize_worktree()
    if not (WT / "run.py").is_file():
        out(f"[preview] {sha[:12]} has no run.py — not a usable checkout.")
        return
    if not is_port_aware():
        out("[preview] REFUSING: that commit's run.py predates WEBAGENT_PORT support —")
        out("[preview] it would try to take over port 8080 and kill this app. Pick a newer commit.")
        return
    write_registry()
    STATEDIR.mkdir(parents=True, exist_ok=True)
    logf = open(LOG_FILE, "ab")
    env = dict(os.environ)
    env["WEBAGENT_PORT"] = str(PORT)
    env["WEBAGENT_ENABLE_BROWSER_SESSION_CACHE"] = "1"
    uv = shutil.which("uv")
    cmd = [uv, "run", "--extra", "encryption", "python", "run.py"] if uv else [sys.executable, "run.py"]
    p = subprocess.Popen(cmd, cwd=str(WT), env=env, stdout=logf, stderr=logf,
                         creationflags=_CREATE_FLAGS)
    PID_FILE.write_text(str(p.pid))
    out(f"[preview] launching {sha[:12]} on :{PORT} (pid {p.pid}) — first boot may uv-sync deps...")
    deadline = time.monotonic() + BOOT_WAIT
    while time.monotonic() < deadline:
        if health_ok():
            break
        if p.poll() is not None:
            out(f"[preview] server exited early (code {p.returncode}). Log tail:")
            try:
                for t in LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]:
                    out("    " + t)
            except OSError:
                pass
            return
        time.sleep(1.0)
    if health_ok():
        out(f"[preview] RUNNING: http://localhost:{PORT}/  ({sha[:12]} {subject[:60]})")
        if IS_WIN:
            try:
                os.startfile(f"http://localhost:{PORT}/")
            except Exception:
                pass
    else:
        out(f"[preview] still booting after {int(BOOT_WAIT)}s — log: {LOG_FILE}")


# ── TUI ──────────────────────────────────────────────────────────────────────
def _detail(sha: str, cache: dict) -> list:
    if sha not in cache:
        r = git("show", "--stat", "--format=fuller", sha, timeout=15)
        cache[sha] = r.stdout.splitlines() if r.returncode == 0 else [f"(no detail for {sha})"]
    return cache[sha]


def tui(rows: list, running_sha: str) -> tuple:
    idx = 0
    detail_cache = {}
    os.system("")  # enable VT processing on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    while True:
        cols, lines = shutil.get_terminal_size((110, 35))
        list_h = max(8, lines // 2)
        det_h = max(3, lines - list_h - 5)
        status = (f"RUNNING {running_sha[:12]} on :{PORT}" if running_sha
                  else "stopped (Esc exits)")
        header = (f" WebAgent commit preview | origin/main | {status} | "
                  f"arrows/j/k move  Enter=run  Esc=stop/exit  Q=quit(keep running)  R=refresh")
        buf = ["\x1b[2J\x1b[H", header, "-" * cols]
        top = max(0, idx - (list_h - 2) // 2)
        for i in range(list_h - 2):
            ri = top + i
            if ri >= len(rows):
                break
            line = rows[ri][1]
            if len(line) > cols - 4:
                line = line[: cols - 7] + "..."
            if ri == idx:
                buf.append("\x1b[7m > " + line.ljust(cols - 4) + "\x1b[0m")
            else:
                buf.append("   " + line.ljust(cols - 4))
        buf.append("-" * cols)
        det = _detail(rows[idx][0], detail_cache)
        shown = det[-(det_h - 1):] if det_h > 1 else det[:1]
        buf.append(" " + rows[idx][1][: cols - 2])
        for d in shown:
            buf.append(" " + d[: cols - 2])
        sys.stdout.write("\n".join(buf))
        sys.stdout.flush()
        ch = msvcrt.getwch()
        if ch == "\x1b":
            return ("esc", rows[idx][0])
        if ch in ("q", "Q"):
            return ("quit", rows[idx][0])
        if ch == "\r":
            return ("run", rows[idx][0])
        if ch in ("\xe0", "\x00"):
            k = msvcrt.getwch()
            if k == "H":
                idx = max(0, idx - 1)
            elif k == "P":
                idx = min(len(rows) - 1, idx + 1)
        elif ch == "k":
            idx = max(0, idx - 1)
        elif ch == "j":
            idx = min(len(rows) - 1, idx + 1)
        elif ch == "r":
            return ("refresh", "")


# ── modes ────────────────────────────────────────────────────────────────────
def main() -> None:
    args = [a for a in sys.argv[1:]]
    mode = "browse"
    for a in args:
        if a == "--stop":
            mode = "stop"
        elif a == "--clean":
            mode = "clean"
        elif a == "--status":
            mode = "status"
        elif a == "--list":
            mode = "list"
        elif a == "--run":
            mode = "run"

    if mode == "stop":
        stop_preview()
        return
    if mode == "clean":
        stop_preview(quiet=True)
        remove_worktree()
        remove_registry()
        out("[preview] clean: worktree + registry entry removed.")
        return
    if mode == "status":
        st = state()
        sha = ""
        if st == "running":
            r = git("rev-parse", "--short", "HEAD", cwd=WT)
            sha = f" ({r.stdout.strip()})" if r.returncode == 0 else ""
        out(f"[preview] {st}{sha} on :{PORT}")
        return
    if mode == "list":
        fetch()
        for _, ln in commit_rows(remote_ref()):
            out(ln)
        return
    if mode == "run":
        sha = next((a for a in args if not a.startswith("--")), "")
        if not sha:
            out("[preview] usage: preview-commit.bat --run <sha>")
            sys.exit(1)
        fetch()
        if git("rev-parse", "--verify", sha).returncode != 0:
            out(f"[preview] unknown commit {sha} — run without --run to browse.")
            sys.exit(1)
        subject = git("log", "-1", "--pretty=%s", sha).stdout.strip()
        start_preview(sha, subject)
        return

    # interactive browse
    fetch()
    rows = commit_rows(remote_ref())
    running_sha = ""
    if state() == "running":
        r = git("rev-parse", "--short", "HEAD", cwd=WT)
        running_sha = r.stdout.strip() if r.returncode == 0 else ""
    while True:
        action, sha = tui(rows, running_sha)
        if action == "quit":
            out(f"[preview] left running on http://localhost:{PORT}/ — "
                f"stop with preview-stop.bat (or reopen and press Esc).")
            return
        if action == "esc":
            if state() == "running":
                stop_preview()
                running_sha = ""
            else:
                out("[preview] bye.")
                return
        if action == "run":
            subject = next((s for h, s in rows if h == sha), "")
            start_preview(sha, subject)
            r = git("rev-parse", "--short", "HEAD", cwd=WT)
            running_sha = r.stdout.strip() if r.returncode == 0 else ""
        if action == "refresh":
            fetch()
            rows = commit_rows(remote_ref())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        out("\n[preview] interrupted — preview keeps running (stop with preview-stop.bat).")
        sys.exit(130)
