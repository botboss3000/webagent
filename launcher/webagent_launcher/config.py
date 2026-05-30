"""Persistent launcher configuration — stored PER-EXE.

Each ``webagent.exe`` is a static pointer to one install folder. Its target (and
prefs) are stored in the Windows registry under ``HKEY_CURRENT_USER\\Software\\
webagent\\Instances``, keyed by the exe's own path — so two copies of the exe in
different folders point at different repos with no shared file. When not frozen
(dev) or off Windows, a plain ``%APPDATA%\\webagent\\launcher.json`` is used.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import winreg  # Windows only
except ImportError:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore[assignment]


def _config_dir() -> Path:
    """Resolve the per-user config directory.

    Windows:  %APPDATA%\\webagent
    macOS:    ~/Library/Application Support/webagent
    Linux:    $XDG_CONFIG_HOME/webagent  or  ~/.config/webagent
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "webagent"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "webagent"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "webagent"


# File used only when the registry isn't available (dev runs, non-Windows, or a
# rare registry-write failure). It's a single shared file, so the per-exe story
# only fully holds with the registry — which is the real (frozen, Windows) path.
CONFIG_PATH = _config_dir() / "launcher.json"

_REG_SUBKEY = r"Software\webagent\Instances"


def tools_dir() -> Path:
    """Per-user cache for tools the launcher downloads itself (e.g. ``uv``).

    Lives next to launcher.json so a portable .exe keeps one private toolbox no
    matter where the .exe sits. Created on demand by callers.
    """
    return _config_dir() / "tools"


def default_install_dir() -> Path:
    """Default destination offered on first run.

    A short, program-like path the user can accept as-is. NOT ``C:\\Program
    Files`` — that needs admin AND the app writes its own runtime data (DB,
    users, generated pages) into its folder, which a standard user can't do
    under Program Files. ``C:\\webagent`` is user-writable without elevation.
    """
    if sys.platform == "win32":
        return Path("C:/webagent")
    return Path.home() / "webagent"


def _is_project(p: Path) -> bool:
    """A folder is a webAgent install if it has run.py + an app/ folder."""
    try:
        return (p / "run.py").exists() and (p / "app").is_dir()
    except OSError:
        return False


def _instance_key() -> str | None:
    """Registry value name identifying THIS exe (its own normalized path).

    None when the registry shouldn't be used (not frozen, or non-Windows) — the
    caller then falls back to the file. Keying by the exe path is what lets each
    copy of the exe carry its own target.
    """
    if winreg is None or not getattr(sys, "frozen", False):
        return None
    try:
        return os.path.normcase(str(Path(sys.executable).resolve()))
    except OSError:
        return None


def _reg_read(name: str) -> str | None:
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_SUBKEY) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value if isinstance(value, str) else None
    except OSError:
        return None


def _reg_write(name: str, data: str) -> bool:
    if winreg is None:
        return False
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _REG_SUBKEY, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, data)
        finally:
            winreg.CloseKey(key)
        return True
    except OSError:
        return False


@dataclass
class LauncherConfig:
    project_path: str = ""
    theme_mode: str = "rainbow"  # solid | gradient | rainbow | custom
    theme_color_a: str = "#39ff14"  # phosphor green default
    theme_color_b: str = "#00bfff"
    theme_color_c: str = "#ff00ff"
    theme_speed: float = 1.0
    animation_intensity: float = 1.0
    animation_style: str = "plasma"  # plasma | flowfield | rings | static (Noise) | off
    char_ramp: str = " .:-=+*#%@"
    fps: int = 24
    auto_launch_browser: bool = True
    last_browser_url: str = "http://localhost:8080/index.html"

    # ── in-TUI chat client ─────────────────────────────────────────────
    chat_username: str = "admin"          # local server login (default admin)
    chat_password: str = "admin"
    auto_start_server: bool = True        # start the server when the app opens
    auto_restart_server: bool = True      # watchdog: relaunch the server if it exits unexpectedly
    health_check_restart: bool = True     # watchdog: relaunch if it stops answering health probes (hung)
    chat_default_view: bool = True        # vestigial: chat is now always the base screen
    default_agent_ref: str = "admin-agent"  # "template:<id>" | "agent:<id>" | bare admin template id
    last_agent_ref: str = ""              # remembered last agent selection
    last_session_id: str = ""             # remembered last session

    # ── single-screen chat UI (reference redesign) ─────────────────────
    theme_name: str = "lime"              # active Textual theme (chrome re-skin); one of THEME_ORDER
    anim_banner_visible: bool = False     # the ASCII animation banner is hidden by default
    crew: list[str] = field(default_factory=lambda: ["robot", "cat"])  # active crew sprite ids
    notes: str = ""                       # notes-sidebar scratchpad text
    todos: list[dict] = field(default_factory=list)  # [{text, done}] from the notes sidebar

    # ── persistence: registry per-exe (frozen) OR a file (dev) ─────────
    @classmethod
    def _read_raw(cls) -> str | None:
        name = _instance_key()
        if name is not None:
            # Frozen on Windows → registry is the SOLE source, keyed per exe.
            # A fresh exe copy has no entry yet (returns None → first run); it
            # never inherits another exe's or the old shared file's target.
            return _reg_read(name)
        # Dev / non-Windows → shared file.
        try:
            if CONFIG_PATH.exists():
                return CONFIG_PATH.read_text(encoding="utf-8")
        except OSError:
            pass
        return None

    @classmethod
    def load(cls) -> "LauncherConfig":
        raw = cls._read_raw()
        if raw is None:
            return cls()
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            return cls()
        # Filter unknown keys (forward-compatible)
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

    def save(self) -> None:
        blob = json.dumps(asdict(self), indent=2)
        name = _instance_key()
        if name is not None:
            _reg_write(name, blob)  # frozen → registry only (per-exe)
            return
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_PATH.with_suffix(".tmp")
            tmp.write_text(blob, encoding="utf-8")
            os.replace(tmp, CONFIG_PATH)
        except OSError:
            pass

    # ── helpers ────────────────────────────────────────────────────────
    def project_dir(self) -> Path | None:
        """The folder THIS exe points at — the saved target, if it's still a
        real install. Purely explicit: no auto-discovery, so each exe keeps its
        own destination and nothing silently re-points it."""
        if self.project_path:
            p = Path(self.project_path).expanduser()
            if _is_project(p):
                return p
        return None

    def is_valid_project(self) -> bool:
        return self.project_dir() is not None
