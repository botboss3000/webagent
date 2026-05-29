"""Dependency doctor — inspects the proot environment + project.

Each check returns a Check dataclass with severity:
  ok       — green; everything healthy
  warn     — yellow; non-critical (e.g. Playwright Chromium missing — webagent
             still runs without browser tools)
  fail     — red; webagent will not start until fixed

The doctor never modifies anything. Fixes live in fixes.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Packages we treat as required for the server to boot at all.
# (Playwright is intentionally NOT in this list — it loads lazily.)
REQUIRED_IMPORTS = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("pydantic", "pydantic"),
    ("openai", "openai"),
    ("httpx", "httpx"),
    ("dotenv", "python-dotenv"),
    ("multipart", "python-multipart"),
    ("jose", "python-jose"),
    ("bcrypt", "bcrypt"),
    ("tiktoken", "tiktoken"),
    ("PIL", "Pillow"),
    ("wsproto", "wsproto"),
    ("croniter", "croniter"),
]

# Optional packages — webagent uses them but can run without (the relevant
# feature gracefully degrades). Surfaced as warn rather than fail.
OPTIONAL_IMPORTS = [
    ("playwright", "playwright"),
    ("supabase", "supabase"),
    ("telegram", "python-telegram-bot"),
    ("twilio", "twilio"),
    ("asyncpg", "asyncpg"),
    ("keyring", "keyring"),
    ("aiomysql", "aiomysql"),
    ("sqlglot", "sqlglot"),
]

# apt packages needed for the Python deps that compile from source on
# Android-Ubuntu (bcrypt → libffi, Pillow → libjpeg/zlib, etc.).
# rustc + cargo are included as insurance — when a PyPI wheel is missing
# for the phone's arch/Python combo, pip falls back to source build and
# packages like pydantic-core / jiter / tiktoken / bcrypt need Rust to
# compile. Costs ~200 MB; saves a remote-debug session.
APT_BUILD_DEPS = [
    "build-essential",
    "gcc",
    "g++",
    "python3-dev",
    "libffi-dev",
    "libssl-dev",
    "libjpeg-dev",
    "zlib1g-dev",
    "libxml2-dev",
    "libxslt1-dev",
    "rustc",
    "cargo",
]


@dataclass
class Check:
    key: str               # short id for the row
    label: str             # human-readable
    severity: str          # ok | warn | fail | unknown
    detail: str = ""       # one-line explanation
    fix_action: Optional[str] = None   # key into fixes.FIXES, or None
    skip_action: Optional[str] = None  # key for "mark as skip / acknowledge"


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for c in self.checks if c.severity == "ok")

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if c.severity == "fail")

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if c.severity == "warn")

    @property
    def overall(self) -> str:
        if self.fail_count:
            return "fail"
        if self.warn_count:
            return "warn"
        return "ok"


def _venv_python(project_dir: Path) -> Optional[Path]:
    for name in ("venv", ".venv"):
        p = project_dir / name / "bin" / "python"
        if p.exists():
            return p
    return None


def _check_venv(project_dir: Path) -> Check:
    py = _venv_python(project_dir)
    if py:
        return Check(
            key="venv",
            label="Python venv",
            severity="ok",
            detail=str(py.relative_to(project_dir)),
        )
    return Check(
        key="venv",
        label="Python venv",
        severity="fail",
        detail="missing — needs `python3 -m venv venv` in project root",
        fix_action="create_venv",
    )


def _check_env_file(project_dir: Path) -> Check:
    env = project_dir / ".env"
    example = project_dir / ".env.example"
    if not env.exists():
        if example.exists():
            return Check(
                key="env",
                label=".env file",
                severity="fail",
                detail="missing — copy from .env.example and add LLM_API_KEY",
                fix_action="create_env",
            )
        return Check(
            key="env",
            label=".env file",
            severity="fail",
            detail=".env and .env.example both missing",
        )

    text = env.read_text(errors="replace")
    has_key = any(
        any(line.startswith(prefix) and "=" in line and line.split("=", 1)[1].strip() not in ("", "your_api_key", "your_openrouter_api_key")
            for prefix in ("LLM_API_KEY", "OPENROUTER_API_KEY"))
        for line in text.splitlines()
    )
    if not has_key:
        return Check(
            key="env",
            label=".env file",
            severity="warn",
            detail="present but LLM_API_KEY / OPENROUTER_API_KEY not set",
        )
    return Check(key="env", label=".env file", severity="ok", detail="present")


def _check_imports(
    py: Path,
    items: list[tuple[str, str]],
    *,
    fatal: bool,
) -> list[Check]:
    """Probe a list of (module, pip_name) imports inside the project venv."""
    if not py.exists():
        return [
            Check(
                key=f"py-{pkg}",
                label=pkg,
                severity="unknown",
                detail="(venv missing — install deps first)",
            )
            for _mod, pkg in items
        ]

    script = (
        "import importlib, json, sys\n"
        "mods = " + repr([m for m, _ in items]) + "\n"
        "out = {}\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m); out[m] = True\n"
        "    except Exception as e:\n"
        "        out[m] = f'ERR: {e!s}'[:120]\n"
        "print(json.dumps(out))\n"
    )
    try:
        proc = subprocess.run(
            [str(py), "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        import json
        results = json.loads(proc.stdout.strip() or "{}")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        results = {}

    out: list[Check] = []
    for mod, pkg in items:
        r = results.get(mod)
        if r is True:
            out.append(Check(
                key=f"py-{pkg}",
                label=pkg,
                severity="ok",
                detail="importable",
            ))
        else:
            severity = "fail" if fatal else "warn"
            detail = "not installed" if not r else r
            out.append(Check(
                key=f"py-{pkg}",
                label=pkg,
                severity=severity,
                detail=detail,
                fix_action=f"pip_install:{pkg}",
                skip_action=None if fatal else f"skip:{pkg}",
            ))
    return out


def _check_apt_pkg(name: str) -> bool:
    try:
        proc = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "install ok installed" in proc.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def _check_apt() -> Check:
    if shutil.which("apt-get") is None:
        return Check(
            key="apt",
            label="apt build deps",
            severity="warn",
            detail="apt-get not on PATH — not an apt-based distro?",
        )
    missing = [p for p in APT_BUILD_DEPS if not _check_apt_pkg(p)]
    if not missing:
        return Check(
            key="apt",
            label="apt build deps",
            severity="ok",
            detail="all build packages installed",
        )
    return Check(
        key="apt",
        label="apt build deps",
        severity="warn",
        detail=f"{len(missing)} missing ({', '.join(missing[:3])}{'...' if len(missing) > 3 else ''})",
        fix_action="apt_install",
    )


def _check_playwright_browser(py: Optional[Path]) -> Check:
    """Playwright Python pkg may be installed but the Chromium binary may not.

    On Android-ARM the Chromium download from playwright's CDN fails.
    The launcher's preferred posture is: skip the browser download,
    let webagent's browser tools no-op gracefully.
    """
    skip_env = os.environ.get("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD")
    if skip_env in ("1", "true", "TRUE"):
        return Check(
            key="playwright-browser",
            label="Playwright browser",
            severity="ok",
            detail="skipped via PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1",
        )

    return Check(
        key="playwright-browser",
        label="Playwright browser",
        severity="warn",
        detail="Chromium download fails on Android — recommend skipping",
        fix_action="set_playwright_skip",
    )


def _check_termux_bridge() -> Check:
    """Termux:API helper is outside the proot; we expose a shim file path the
    Termux side can poll. Just report whether the bridge path is writable."""
    bridge = Path(os.environ.get("WEBAGENT_BROWSER_BRIDGE", "/tmp/webagent_open.url"))
    try:
        bridge.parent.mkdir(parents=True, exist_ok=True)
        return Check(
            key="bridge",
            label="Browser bridge",
            severity="ok",
            detail=f"writes to {bridge}",
        )
    except OSError as e:
        return Check(
            key="bridge",
            label="Browser bridge",
            severity="warn",
            detail=f"cannot write bridge file: {e}",
        )


def run_doctor(project_dir: Path) -> DoctorReport:
    """Run all checks and return a single report."""
    report = DoctorReport()
    report.checks.append(_check_venv(project_dir))
    report.checks.append(_check_env_file(project_dir))
    report.checks.append(_check_apt())
    report.checks.append(_check_playwright_browser(_venv_python(project_dir)))
    report.checks.append(_check_termux_bridge())

    py = _venv_python(project_dir)
    if py:
        report.checks.extend(_check_imports(py, REQUIRED_IMPORTS, fatal=True))
        report.checks.extend(_check_imports(py, OPTIONAL_IMPORTS, fatal=False))
    else:
        report.checks.extend(
            Check(
                key=f"py-{pkg}",
                label=pkg,
                severity="unknown",
                detail="(create venv first)",
            )
            for _m, pkg in (REQUIRED_IMPORTS + OPTIONAL_IMPORTS)
        )

    return report
