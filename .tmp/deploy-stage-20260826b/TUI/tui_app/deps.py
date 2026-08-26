"""Dependency model + probe for the Setup dashboard.

Textual-free and unit-testable. ``probe_dependencies()`` returns an ORDERED,
platform-aware list of the things WebAgent needs — from bare-device host tools
(git, Python, on Termux the Ubuntu ``proot-distro`` sandbox) all the way to a
verified install (clone → environment → config → verify). The Setup panel renders
each as a row with a status icon + an [Install] button when one applies; the
``check_dependencies`` / ``install_dependency`` agent tools read the same list, so
the checklist is one source of truth. The runners that actually install live in
``depinstall.py``.

A probe is read-only and never raises — every check is wrapped so a single failing
probe degrades to "unknown" instead of breaking the whole dashboard. The blocking
bits (socket reachability, a ``python --version`` subprocess) are cheap but should
be called off the UI thread (the app runs the probe via ``asyncio.to_thread``).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .env_probe import MachineProbe, probe_machine
from .tools import install as _install

# ── status vocabulary ────────────────────────────────────────────────────────
OK = "ok"          # satisfied — green
MISSING = "missing"  # required and absent — red, shows an install button
WARN = "warn"      # optional / soft issue — amber, shows an install button
NA = "na"          # not applicable on this platform — dim, no button
UNKNOWN = "unknown"  # the check itself failed — dim


@dataclass
class Dep:
    """One dependency row.

    ``installable`` means an [Install] button applies (a runner exists in
    ``depinstall.py`` keyed by this ``id``). ``blocked_by`` lists prerequisite ids
    that must be ``ok`` first — the panel greys a blocked row and [Install all]
    runs rows in order so prerequisites are satisfied before their dependents.
    ``manage_actions`` are extra buttons ((label, op)), used by the Ubuntu row.
    """

    id: str
    label: str
    status: str
    detail: str = ""
    installable: bool = False
    required: bool = True
    blocked_by: List[str] = field(default_factory=list)
    manage_actions: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def satisfied(self) -> bool:
        return self.status in (OK, NA)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "label": self.label, "status": self.status,
            "detail": self.detail, "installable": self.installable,
            "required": self.required, "blocked_by": list(self.blocked_by),
        }


# ── small platform helpers ───────────────────────────────────────────────────
def _termux_prefix() -> str:
    return os.environ.get("PREFIX") or "/data/data/com.termux/files/usr"


def proot_distro_present() -> bool:
    """Is the ``proot-distro`` command installed (Termux)?"""
    if shutil.which("proot-distro"):
        return True
    return os.path.exists(f"{_termux_prefix()}/bin/proot-distro")


def ubuntu_rootfs() -> Optional[Path]:
    """Path to the installed Ubuntu rootfs, or None. proot-distro has used a
    couple of layouts over versions, so check both the modern ``installed-rootfs``
    dir and the older ``containers`` one."""
    prefix = Path(_termux_prefix())
    base = prefix / "var" / "lib" / "proot-distro"
    for cand in (base / "installed-rootfs" / "ubuntu", base / "containers" / "ubuntu"):
        try:
            if cand.exists() and any(cand.iterdir()):
                return cand
        except OSError:
            continue
    return None


def ubuntu_installed() -> bool:
    return ubuntu_rootfs() is not None


def ubuntu_toolchain_ready() -> bool:
    """Is the Ubuntu sandbox prepped with python3 + a compiler? Checked by stat-ing
    the rootfs directly (fast) rather than a slow ``proot-distro login``."""
    rootfs = ubuntu_rootfs()
    if rootfs is None:
        return False
    py = (rootfs / "usr" / "bin" / "python3").exists()
    gcc = (rootfs / "usr" / "bin" / "gcc").exists() or (rootfs / "usr" / "bin" / "cc").exists()
    return py and gcc


def ubuntu_size_mb() -> Optional[int]:
    """Rough on-disk size of the Ubuntu rootfs in MB (best-effort, capped walk)."""
    rootfs = ubuntu_rootfs()
    if rootfs is None:
        return None
    total = 0
    count = 0
    try:
        for root, _dirs, files in os.walk(rootfs):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
                count += 1
                if count > 60000:  # don't walk forever on a huge tree
                    return None
        return total // (1024 * 1024)
    except OSError:
        return None


def _chromium_installed() -> bool:
    """Has Playwright's Chromium been downloaded into the per-user browser cache?"""
    home = Path.home()
    cands = [
        home / ".cache" / "ms-playwright",                       # Linux
        home / "Library" / "Caches" / "ms-playwright",           # macOS
        Path(os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local")))
        / "ms-playwright",                                       # Windows
    ]
    for d in cands:
        try:
            if d.exists() and any(p.name.startswith("chromium") for p in d.iterdir()):
                return True
        except OSError:
            continue
    return False


def _local_db_present(target: Path) -> bool:
    for cand in (target / "app" / "db" / "local.db",
                 target / "data" / "db" / "local.db",
                 target / "local.db"):
        if cand.exists():
            return True
    return False


def _repo_present(target: Path) -> bool:
    try:
        return (target / "run.py").exists() and (target / "app").is_dir()
    except OSError:
        return False


# ── the probe ─────────────────────────────────────────────────────────────────
def probe_dependencies(target: str = "",
                       machine: Optional[MachineProbe] = None) -> List[Dep]:
    """Return the ordered, platform-aware dependency checklist for ``target``.

    ``target`` is the folder WebAgent will live in (defaults to the recommended
    path). Pass a pre-computed ``machine`` probe to avoid re-running it.
    """
    f = machine or probe_machine()
    is_termux = f.is_termux
    tgt = _install._resolve_target(target) if target else _default_target(f)

    deps: List[Dep] = []

    # 1) internet — nothing installs without it.
    net = _safe(lambda: _install._reachable("github.com") and _install._reachable("pypi.org"))
    deps.append(Dep(
        "internet", "Internet (GitHub + PyPI)",
        OK if net else (MISSING if net is False else UNKNOWN),
        "github.com + pypi.org reachable" if net else
        ("cannot reach github.com / pypi.org — check the connection" if net is False
         else "couldn't test connectivity"),
        installable=False, required=True))

    # 2) git
    deps.append(Dep(
        "git", "git",
        OK if f.git_present else MISSING,
        "found on PATH" if f.git_present else "missing — needed to download WebAgent",
        installable=not f.git_present, required=True))

    # 3) host Python 3.11/3.12 — desktop only (on Termux the server's Python lives
    #    inside Ubuntu; the manager's own host Python is already running).
    if not is_termux:
        py = _safe(lambda: _install._supported_python())
        ok = bool(py)
        deps.append(Dep(
            "python", "Python 3.11 / 3.12",
            OK if ok else MISSING,
            f"found ({py})" if ok else
            f"none found (system Python is {f.system_python or 'absent'}; WebAgent needs 3.11 or 3.12)",
            installable=not ok, required=True))

    # 4) disk space at the target
    free_mb = _safe(lambda: _free_mb(tgt))
    need = _install._MIN_FREE_MB
    if free_mb is None:
        deps.append(Dep("disk", "Free disk space", UNKNOWN,
                        "couldn't check free space", installable=False, required=True))
    else:
        deps.append(Dep(
            "disk", "Free disk space",
            OK if free_mb >= need else MISSING,
            f"{free_mb} MB free at {tgt} (need ~{need})",
            installable=False, required=True))

    # ── Termux: the Ubuntu sandbox (mirrors deploy/termux-setup.sh) ──
    if is_termux:
        pd = proot_distro_present()
        deps.append(Dep(
            "proot_distro", "proot-distro",
            OK if pd else MISSING,
            "installed" if pd else "missing — provides the Ubuntu sandbox",
            installable=not pd, required=True, blocked_by=[]))

        ub = ubuntu_installed()
        size = ubuntu_size_mb() if ub else None
        deps.append(Dep(
            "ubuntu", "Ubuntu environment",
            OK if ub else MISSING,
            (f"installed{f' (~{size} MB)' if size else ''}" if ub
             else "not installed — WebAgent runs inside it on Android"),
            installable=not ub, required=True, blocked_by=["proot_distro"],
            manage_actions=([("Update", "ubuntu_update"),
                             ("Reinstall", "ubuntu_reinstall"),
                             ("Remove", "ubuntu_remove")] if ub else [])))

        tc = ubuntu_toolchain_ready()
        deps.append(Dep(
            "ubuntu_toolchain", "Ubuntu Python + build tools",
            OK if tc else (MISSING if ub else NA),
            ("python3, venv, pip and a compiler are installed" if tc
             else "not prepared yet (python3 / venv / build-essential)" if ub
             else "needs the Ubuntu environment first"),
            installable=ub and not tc, required=True, blocked_by=["ubuntu"]))

    # ── the WebAgent install itself ──
    repo = _repo_present(tgt)
    deps.append(Dep(
        "repo", "WebAgent code",
        OK if repo else MISSING,
        f"cloned at {tgt}" if repo else f"not present — will clone into {tgt}",
        installable=not repo, required=True, blocked_by=["git", "internet"]))

    venv_ok = _safe(lambda: _venv_present(tgt, is_termux)) or False
    venv_block = ["repo", "ubuntu_toolchain"] if is_termux else ["repo", "python"]
    deps.append(Dep(
        "venv", "Python environment + dependencies",
        OK if venv_ok else MISSING,
        "virtual environment built and dependencies installed" if venv_ok
        else "not built yet (the venv + app dependencies)",
        installable=not venv_ok, required=True, blocked_by=venv_block))

    # browser — desktop only, optional (WebAgent runs without it; browser tools off)
    if is_termux:
        deps.append(Dep(
            "browser", "Headless browser", NA,
            "not available on Android — browser features are off here",
            installable=False, required=False))
    else:
        chromium = _safe(lambda: _chromium_installed()) or False
        deps.append(Dep(
            "browser", "Headless browser (Chromium)",
            OK if chromium else WARN,
            "installed" if chromium else "optional — needed for browser/UI tools",
            installable=not chromium, required=False, blocked_by=["venv"]))

    # config — .env + db_connection.json
    cfg_ok = _safe(lambda: (tgt / ".env").exists() and (tgt / "db_connection.json").exists()) or False
    deps.append(Dep(
        "config", "Configuration + AI key",
        OK if cfg_ok else MISSING,
        "config seeded (.env, local SQLite)" if cfg_ok
        else "not seeded yet (.env / db config)",
        installable=not cfg_ok, required=True, blocked_by=["repo"]))

    # verify — app imports + local DB initialises
    verified = _safe(lambda: _local_db_present(tgt)) or False
    deps.append(Dep(
        "verify", "Verified (app imports + DB ready)",
        OK if verified else MISSING,
        "the app has imported cleanly and its database is initialised" if verified
        else "not verified yet",
        installable=not verified, required=True, blocked_by=["venv", "config"]))

    return deps


# ── helpers used by the panel / install-all ──────────────────────────────────
def status_index(deps: List[Dep]) -> dict:
    return {d.id: d for d in deps}


def is_blocked(dep: Dep, index: dict) -> bool:
    """True if any prerequisite isn't satisfied yet."""
    for pid in dep.blocked_by:
        prereq = index.get(pid)
        if prereq is None or not prereq.satisfied:
            return True
    return False


def pending_install_ids(deps: List[Dep]) -> List[str]:
    """Ordered ids that [Install all] should run: every installable, not-yet-ok row
    (optional rows like the browser included). Order is the list order, so
    prerequisites come before dependents."""
    return [d.id for d in deps if d.installable and d.status in (MISSING, WARN)]


def summary_line(deps: List[Dep]) -> str:
    """A one-line headline, e.g. '3 of 9 ready · 2 to install'."""
    total = len([d for d in deps if d.required])
    ready = len([d for d in deps if d.required and d.satisfied])
    todo = len(pending_install_ids(deps))
    tail = f" · {todo} to install" if todo else " · all set"
    return f"{ready} of {total} ready{tail}"


def render_text(deps: List[Dep]) -> str:
    """Plain-text checklist for the agent tool / logs."""
    mark = {OK: "[OK]", MISSING: "[ X]", WARN: "[ !]", NA: "[ -]", UNKNOWN: "[ ?]"}
    lines = [summary_line(deps), ""]
    index = status_index(deps)
    for d in deps:
        blocked = " (blocked)" if is_blocked(d, index) and not d.satisfied else ""
        btn = "  <install>" if d.installable and d.status in (MISSING, WARN) and not blocked else ""
        lines.append(f"{mark.get(d.status, '[ ?]')} {d.label}: {d.detail}{btn}")
    return "\n".join(lines)


# ── internals ─────────────────────────────────────────────────────────────────
def _default_target(f: MachineProbe) -> Path:
    return Path("C:/webagent") if f.os_label == "Windows" else Path.home() / "webagent"


def _free_mb(target: Path) -> int:
    anchor = target
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    return shutil.disk_usage(anchor).free // (1024 * 1024)


def _venv_present(target: Path, is_termux: bool) -> bool:
    # Desktop: <target>/.venv/Scripts|bin/python(.exe). Termux: the venv is built
    # inside the Ubuntu bind-mount, which is the SAME on-disk folder
    # (<target>/.venv/bin/python), so the host-side check works for both.
    p = _install._venv_python(target)
    return p.exists() or p.is_symlink()


def _safe(fn):
    """Run a probe check, swallowing any exception (returns None on failure)."""
    try:
        return fn()
    except Exception:
        return None
