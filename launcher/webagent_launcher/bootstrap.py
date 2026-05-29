"""First-run installer + updater for webagent.

Turns the portable ``webagent.exe`` into a self-bootstrapping installer: on a
clean machine it can download the public webagent repo, fetch the ``uv`` tool
(which installs its own private Python), and install every dependency — so the
end user only needs the one .exe.

Strategy (locked in by design):
  * Code   — ``git clone`` when git is on PATH (enables clean Updates later),
             otherwise a ZIP snapshot of the default branch (needs no git).
  * Python — fully self-contained: download the small ``uv`` standalone binary
             into the launcher's private tools dir; ``uv sync`` then provisions
             a matching Python automatically and installs all packages.

Everything streams progress through a ``log(str)`` callback, the same shape the
rest of the launcher uses, so it renders live in the install screen.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Optional

import httpx

from .config import tools_dir

# ── repo coordinates ────────────────────────────────────────────────────────
GITHUB_OWNER = "botboss3000"
GITHUB_REPO = "webagent"
DEFAULT_BRANCH = "main"
REPO_GIT_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}.git"
REPO_ZIP_URL = (
    f"https://codeload.github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
    f"/zip/refs/heads/{DEFAULT_BRANCH}"
)

# ── uv standalone binary (Astral's official GitHub release assets) ──────────
_UV_RELEASE_BASE = "https://github.com/astral-sh/uv/releases/latest/download"

Log = Callable[[str], None]


# ── platform / subprocess helpers ───────────────────────────────────────────
def _creationflags() -> int:
    """No flashing child console windows on Windows for our helper processes."""
    if sys.platform == "win32":
        return 0x08000000  # CREATE_NO_WINDOW
    return 0


def find_git() -> Optional[str]:
    """Absolute path to ``git`` if it's on PATH, else None (we don't install it)."""
    return shutil.which("git")


async def _run_streaming(
    cmd: list[str],
    cwd: Optional[Path],
    log: Log,
    env: Optional[dict] = None,
) -> int:
    """Run a subprocess, streaming merged stdout+stderr line-by-line to ``log``.

    Returns the process exit code, or a negative sentinel if it could not spawn.
    """
    run_env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    if env:
        run_env.update(env)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=run_env,
            creationflags=_creationflags(),
        )
    except (FileNotFoundError, OSError) as e:
        log(f"  ! could not run {cmd[0]}: {e}")
        return -1
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        log("  " + line.decode("utf-8", errors="replace").rstrip())
    return await proc.wait()


async def _download(url: str, dest: Path, log: Log, label: str) -> bool:
    """Stream a URL to ``dest`` with periodic progress logging. Follows redirects
    (GitHub release assets redirect to a CDN)."""
    log(f"[get] {label} …")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        timeout = httpx.Timeout(60.0, connect=30.0)
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or 0)
                got = 0
                next_mark = 0
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(262144):
                        f.write(chunk)
                        got += len(chunk)
                        if got >= next_mark:
                            if total:
                                pct = got * 100 // total
                                log(f"  {pct:3d}%  ({got // 1048576} / {total // 1048576} MB)")
                            else:
                                log(f"  {got // 1048576} MB")
                            next_mark = got + 8 * 1048576  # every ~8 MB
        os.replace(tmp, dest)
        log(f"  done ({got // 1048576} MB)")
        return True
    except (httpx.HTTPError, OSError) as e:
        log(f"  ! download failed: {e}")
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[call-arg]
        except OSError:
            pass
        return False


# ── uv toolchain ─────────────────────────────────────────────────────────────
def _uv_asset() -> Optional[tuple[str, bool]]:
    """(asset filename, is_zip) for the current platform, or None if unknown.

    Windows ships as a .zip (uv.exe at top level); the rest as .tar.gz.
    """
    machine = (os.environ.get("PROCESSOR_ARCHITECTURE") or "").lower()
    import platform as _pl

    arch = (_pl.machine() or machine).lower()
    is_arm = arch in ("arm64", "aarch64")
    if sys.platform == "win32":
        target = "aarch64-pc-windows-msvc" if is_arm else "x86_64-pc-windows-msvc"
        return f"uv-{target}.zip", True
    if sys.platform == "darwin":
        target = "aarch64-apple-darwin" if is_arm else "x86_64-apple-darwin"
        return f"uv-{target}.tar.gz", False
    if sys.platform.startswith("linux"):
        target = "aarch64-unknown-linux-gnu" if is_arm else "x86_64-unknown-linux-gnu"
        return f"uv-{target}.tar.gz", False
    return None


def _uv_binary_path() -> Path:
    return tools_dir() / ("uv.exe" if sys.platform == "win32" else "uv")


def _extract_uv(archive: Path, is_zip: bool, dest_dir: Path, log: Log) -> bool:
    """Pull just the uv / uvx executables out of the downloaded archive."""
    wanted = {"uv", "uv.exe", "uvx", "uvx.exe"}
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted = 0
    try:
        if is_zip:
            with zipfile.ZipFile(archive) as zf:
                for name in zf.namelist():
                    base = Path(name).name
                    if base in wanted:
                        with zf.open(name) as src, open(dest_dir / base, "wb") as out:
                            shutil.copyfileobj(src, out)
                        extracted += 1
        else:
            with tarfile.open(archive, "r:gz") as tf:
                for member in tf.getmembers():
                    base = Path(member.name).name
                    if member.isfile() and base in wanted:
                        src = tf.extractfile(member)
                        if src is None:
                            continue
                        out_path = dest_dir / base
                        with src, open(out_path, "wb") as out:
                            shutil.copyfileobj(src, out)
                        out_path.chmod(0o755)
                        extracted += 1
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as e:
        log(f"  ! could not extract uv: {e}")
        return False
    return extracted > 0


async def ensure_uv(log: Log) -> Optional[Path]:
    """Return a usable ``uv`` path, downloading it into the tools dir if needed.

    Order: uv already on PATH → cached copy in the tools dir → fresh download.
    """
    on_path = shutil.which("uv")
    if on_path:
        log(f"[uv] using uv on PATH: {on_path}")
        return Path(on_path)

    cached = _uv_binary_path()
    if cached.exists():
        log(f"[uv] using cached uv: {cached}")
        return cached

    asset = _uv_asset()
    if not asset:
        log(f"[uv] ERROR: no uv build available for this platform ({sys.platform}).")
        return None
    filename, is_zip = asset
    url = f"{_UV_RELEASE_BASE}/{filename}"

    with tempfile.TemporaryDirectory(prefix="webagent-uv-") as td:
        archive = Path(td) / filename
        if not await _download(url, archive, log, f"uv toolchain ({filename})"):
            return None
        if not _extract_uv(archive, is_zip, tools_dir(), log):
            return None

    if cached.exists():
        log(f"[uv] installed uv → {cached}")
        return cached
    log("[uv] ERROR: uv executable missing after extraction.")
    return None


# ── code acquisition ─────────────────────────────────────────────────────────
def looks_like_project(p: Path) -> bool:
    return (p / "run.py").exists() and (p / "app").is_dir()


def is_git_checkout(project_dir: Path) -> bool:
    return (project_dir / ".git").exists()


def _dir_has_content(p: Path) -> bool:
    try:
        return p.is_dir() and any(p.iterdir())
    except OSError:
        return False


async def _clone_with_git(target: Path, git: str, log: Log) -> bool:
    log(f"[code] cloning {REPO_GIT_URL} (shallow) …")
    target.parent.mkdir(parents=True, exist_ok=True)
    rc = await _run_streaming(
        [git, "clone", "--depth", "1", "--branch", DEFAULT_BRANCH, REPO_GIT_URL, str(target)],
        cwd=target.parent,
        log=log,
    )
    if rc != 0:
        log(f"[code] git clone failed (exit {rc}).")
        return False
    return True


async def _download_zip(target: Path, log: Log) -> bool:
    log("[code] downloading ZIP snapshot (no git on this machine) …")
    with tempfile.TemporaryDirectory(prefix="webagent-zip-") as td:
        tdp = Path(td)
        archive = tdp / "webagent.zip"
        if not await _download(REPO_ZIP_URL, archive, log, "webagent source ZIP"):
            return False
        try:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tdp / "x")
        except (zipfile.BadZipFile, OSError) as e:
            log(f"[code] ! could not unzip: {e}")
            return False
        # GitHub wraps everything in a single  <repo>-<branch>/  folder.
        roots = [c for c in (tdp / "x").iterdir() if c.is_dir()]
        if not roots:
            log("[code] ! ZIP had no top-level folder.")
            return False
        inner = roots[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if target.exists():
                shutil.copytree(inner, target, dirs_exist_ok=True)
            else:
                shutil.move(str(inner), str(target))
        except (OSError, shutil.Error) as e:
            log(f"[code] ! could not place files: {e}")
            return False
    return True


async def fetch_code(target: Path, log: Log, git: Optional[str]) -> bool:
    """Put the webagent source into ``target`` via git (preferred) or ZIP."""
    if looks_like_project(target):
        log(f"[code] target already holds a webagent project: {target}")
        return True
    if _dir_has_content(target) and not looks_like_project(target):
        log(f"[code] ERROR: target folder is not empty and isn't a webagent project:\n        {target}")
        return False
    if git:
        return await _clone_with_git(target, git, log)
    return await _download_zip(target, log)


async def sync_deps(target: Path, uv: Path, log: Log) -> bool:
    """``uv sync`` — provisions a matching Python (auto-download) + all packages.

    This is the long step on a fresh machine: Python + ~30 packages.
    """
    log("[deps] installing Python + dependencies via uv sync …")
    log("[deps] (first run downloads a private Python + packages — a few minutes)")
    rc = await _run_streaming([str(uv), "sync"], cwd=target, log=log)
    if rc != 0:
        log(f"[deps] uv sync failed (exit {rc}).")
        return False
    log("[deps] dependencies ready.")
    return True


def _venv_python(project_dir: Path) -> Optional[Path]:
    """Path to the project's freshly-synced interpreter, if it exists."""
    if sys.platform == "win32":
        p = project_dir / ".venv" / "Scripts" / "python.exe"
    else:
        p = project_dir / ".venv" / "bin" / "python"
    return p if p.exists() else None


async def install_playwright(target: Path, uv: Path, log: Log) -> bool:
    """Download the Chromium browser the ``browser_action`` tool drives.

    The ``playwright`` package itself comes from ``uv sync``; this fetches the
    matching browser binary (~150 MB) into Playwright's per-user cache
    (``%LOCALAPPDATA%\\ms-playwright`` on Windows) so it's shared and survives
    reinstalls. Best-effort: a failure here doesn't fail the whole install — the
    server still runs; only the browser tool needs this. Re-runnable (fast when
    the browser is already present), so Update calls it too.
    """
    log("[browser] installing Chromium for Playwright (~150 MB) …")
    py = _venv_python(target)
    if py is not None:
        cmd = [str(py), "-m", "playwright", "install", "chromium"]
    else:  # fall back to uv if the venv interpreter isn't where we expect
        cmd = [str(uv), "run", "python", "-m", "playwright", "install", "chromium"]
    rc = await _run_streaming(cmd, cwd=target, log=log)
    if rc != 0:
        log(f"[browser] WARNING: Chromium install failed (exit {rc}). The server")
        log("[browser] will still run; press Update later to retry the browser download.")
        return False
    log("[browser] Chromium ready — browser tools will work.")
    return True


# ── top-level flows ──────────────────────────────────────────────────────────
async def install(target: Path, log: Log) -> bool:
    """Full clean install into ``target``: code → uv → Python + deps.

    Returns True only when ``target`` ends up a runnable webagent project.
    """
    target = Path(target).expanduser()
    log(f"[install] installing webAgent into: {target}")

    git = find_git()
    log("[install] git: " + (git if git else "not found — using ZIP download"))

    if not await fetch_code(target, log, git):
        return False

    uv = await ensure_uv(log)
    if not uv:
        log("[install] ERROR: could not obtain the uv toolchain.")
        return False

    if not await sync_deps(target, uv, log):
        return False

    # Browser binary for the browser_action tool. Best-effort — a failed
    # Chromium download must not sink an otherwise-working install.
    await install_playwright(target, uv, log)

    if not looks_like_project(target):
        log("[install] ERROR: run.py / app/ missing after install.")
        return False

    log("[install] SUCCESS — webAgent is installed and ready to launch.")
    return True


async def _check_url(url: str) -> tuple[bool, str]:
    try:
        timeout = httpx.Timeout(8.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.head(url)
        return True, f"reachable ({url} → HTTP {r.status_code})"
    except (httpx.HTTPError, OSError) as e:
        return False, f"cannot reach {url}: {e}"


def _nearest_existing(p: Path) -> Path:
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _check_writable(target: Path) -> tuple[bool, str]:
    """Can we create the install folder and write inside it?

    Tests the REAL operation — create ``target`` (if missing) and write a probe
    file inside it — not whether a file can be made in ``C:\\`` root. Standard
    users can create a *folder* under ``C:\\`` and write inside it even though
    they can't create files directly in the root, so the naive ancestor test
    gives a false failure for ``C:\\webagent``.
    """
    target = Path(target)
    created: list[Path] = []  # folders we make here, deepest first, to undo after
    p = target
    while not p.exists() and p.parent != p:
        created.append(p)
        p = p.parent
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".webagent_write_test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True, f"can create and write {target}"
    except OSError as e:
        return False, f"cannot write to {target}: {e}"
    finally:
        for d in created:  # remove only the folders we created, and only if empty
            try:
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass


def _check_disk(target: Path) -> tuple[bool, str]:
    try:
        usage = shutil.disk_usage(str(_nearest_existing(target)))
        free_gb = usage.free / (1024 ** 3)
        return free_gb >= 2.0, f"{free_gb:.1f} GB free (need ~2 GB)"
    except OSError as e:
        return False, str(e)


async def _capture(cmd: list[str]) -> tuple[int, str]:
    """Run a short command; return (exit_code, first line of merged output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=_creationflags(),
        )
        out, _ = await proc.communicate()
    except (OSError, FileNotFoundError) as e:
        return -1, str(e)
    lines = out.decode("utf-8", errors="replace").strip().splitlines()
    return (proc.returncode if proc.returncode is not None else -1), (lines[0] if lines else "")


def _playwright_browsers() -> list[str]:
    """Chromium revisions present in Playwright's per-user cache, if any."""
    base = os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else None
    cache = Path(base) / "ms-playwright" if base else (Path.home() / ".cache" / "ms-playwright")
    if not cache.is_dir():
        return []
    try:
        return sorted(c.name for c in cache.iterdir()
                      if c.is_dir() and c.name.startswith("chromium"))
    except OSError:
        return []


def _mark(status: str) -> str:
    """Fixed-width status tag so the report columns line up."""
    return f"[{status:<9}]"


async def run_diagnostics(target: Path, log: Log) -> None:
    """A full environment report for the Doctor view — what IS and ISN'T present.

    Pure inspection: tool versions, internet, and the state of the install at
    ``target`` (code, Python env, dependencies, browser, database). Changes
    nothing.
    """
    import platform as _pl

    target = Path(target).expanduser()
    log("webAgent Health Check — environment report")
    log("=" * 46)
    log(f"OS: {_pl.system()} {_pl.release()} ({_pl.machine()})")
    log("")

    # ── Tools on this PC ────────────────────────────────────────────────
    log("Tools on this PC")
    git = find_git()
    if git:
        _, ver = await _capture([git, "--version"])
        log(f"  {_mark('installed')} git        — {ver or git}")
    else:
        log(f"  {_mark('missing')} git        — not found (a ZIP download is used instead; fine)")

    uv = shutil.which("uv") or (str(_uv_binary_path()) if _uv_binary_path().exists() else None)
    if uv:
        _, ver = await _capture([uv, "--version"])
        log(f"  {_mark('installed')} uv         — {ver or uv}")
    else:
        log(f"  {_mark('missing')} uv         — not found (downloaded automatically on install)")

    syspy = shutil.which("python") or shutil.which("py")
    if syspy:
        _, ver = await _capture([syspy, "--version"])
        log(f"  {_mark('info')} Python     — {ver or syspy} (system; optional — uv installs its own)")
    else:
        log(f"  {_mark('info')} Python     — none on PATH (optional — uv installs its own)")
    log("")

    # ── Internet ────────────────────────────────────────────────────────
    log("Internet")
    ok_gh, _ = await _check_url("https://github.com")
    log(f"  {_mark('online' if ok_gh else 'OFFLINE')} GitHub     — the source code")
    ok_pypi, _ = await _check_url("https://pypi.org")
    log(f"  {_mark('online' if ok_pypi else 'OFFLINE')} PyPI       — the Python packages")
    log("")

    # ── This install (at the target folder) ─────────────────────────────
    log(f"Install at {target}")
    if looks_like_project(target):
        log(f"  {_mark('present')} webAgent code")
        if is_git_checkout(target) and git:
            _, branch = await _capture([git, "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"])
            log(f"  {_mark('present')} git checkout — branch {branch or '?'} (Update uses git pull)")
        else:
            log(f"  {_mark('note')} not a git checkout (Update re-downloads the ZIP)")
        vpy = _venv_python(target)
        if vpy:
            _, ver = await _capture([str(vpy), "--version"])
            log(f"  {_mark('present')} Python env — .venv ({ver or 'built'})")
            _, dep = await _capture([
                str(vpy), "-c",
                "import importlib.util as u;"
                "m=['fastapi','uvicorn','openai','playwright','supabase'];"
                "x=[n for n in m if u.find_spec(n) is None];"
                "print('OK' if not x else 'MISSING:'+','.join(x))",
            ])
            if dep == "OK":
                log(f"  {_mark('present')} dependencies — fastapi, uvicorn, openai, playwright, supabase")
            else:
                log(f"  {_mark('MISSING')} dependencies — {dep} (run Reset Py / Update)")
        else:
            log(f"  {_mark('MISSING')} Python env — .venv not built (run the install)")
        db = target / "app" / "db" / "local.db"
        log(f"  {_mark('present' if db.exists() else 'not yet')} local database — app/db/local.db")
    elif target.exists():
        log(f"  {_mark('not yet')} webAgent code — folder exists but nothing installed here")
    else:
        log(f"  {_mark('not yet')} webAgent code — folder will be created on install")

    browsers = _playwright_browsers()
    if browsers:
        log(f"  {_mark('present')} browser    — Chromium ({', '.join(browsers)})")
    else:
        log(f"  {_mark('not yet')} browser    — Chromium installed during setup (~150 MB)")

    ok_w, detail_w = _check_writable(target)
    log(f"  {_mark('ok' if ok_w else 'FAIL')} writable   — {detail_w}")
    ok_d, detail_d = _check_disk(target)
    log(f"  {_mark('ok' if ok_d else 'low')} free space — {detail_d}")
    log("")

    if ok_gh and ok_w:
        log("Ready to install. Open the Install tab and press Download & Install.")
    else:
        log("Resolve the OFFLINE/FAIL line(s) above, then try the install.")


async def update(project_dir: Path, log: Log) -> bool:
    """Pull the latest code (git pull or fresh ZIP over the top) + re-sync deps.

    Runtime data (local DB, users.json, generated pages) is never in the public
    repo, so a ZIP re-extract overwrites only tracked files and leaves user data
    intact. The caller is responsible for stopping/restarting the server.
    """
    project_dir = Path(project_dir)
    if not looks_like_project(project_dir):
        log(f"[update] ERROR: not a webagent project: {project_dir}")
        return False

    git = find_git()
    if is_git_checkout(project_dir) and git:
        log("[update] pulling latest code (git) …")
        rc = await _run_streaming([git, "pull", "--ff-only"], cwd=project_dir, log=log)
        if rc != 0:
            log(f"[update] git pull failed (exit {rc}). Resolve conflicts and retry.")
            return False
    else:
        log("[update] refreshing code from ZIP snapshot …")
        if not await _download_zip(project_dir, log):
            return False

    uv = await ensure_uv(log)
    if not uv:
        log("[update] ERROR: could not obtain the uv toolchain.")
        return False
    if not await sync_deps(project_dir, uv, log):
        return False

    # Keep the browser binary in step with the (possibly updated) playwright.
    await install_playwright(project_dir, uv, log)

    log("[update] SUCCESS — code and dependencies are up to date.")
    return True
