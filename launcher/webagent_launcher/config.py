"""Persistent launcher configuration.

Stored at %APPDATA%\\webagent\\launcher.json on Windows so the .exe can sit
anywhere (Desktop, Start Menu, USB stick) without losing user preferences.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


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


CONFIG_PATH = _config_dir() / "launcher.json"


def tools_dir() -> Path:
    """Per-user cache for tools the launcher downloads itself (e.g. ``uv``).

    Lives next to launcher.json so a portable .exe keeps one private toolbox no
    matter where the .exe sits. Created on demand by callers.
    """
    return _config_dir() / "tools"


def default_install_dir() -> Path:
    """Default folder offered on first run when the user installs from scratch.

    Sits in the user's home so it's easy to find and needs no elevation.
    """
    return Path.home() / "webagent"


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
    chat_default_view: bool = True        # open the chat screen on launch
    default_agent_ref: str = "admin-agent"  # "template:<id>" | "agent:<id>" | bare admin template id
    last_agent_ref: str = ""              # remembered last agent selection
    last_session_id: str = ""             # remembered last session

    # ── persistence ────────────────────────────────────────────────────
    @classmethod
    def load(cls) -> "LauncherConfig":
        if not CONFIG_PATH.exists():
            return cls()
        try:
            data: dict[str, Any] = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        # Filter unknown keys (forward-compatible)
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)

    # ── helpers ────────────────────────────────────────────────────────
    def project_dir(self) -> Path | None:
        if not self.project_path:
            return None
        p = Path(self.project_path)
        return p if p.is_dir() else None

    def is_valid_project(self) -> bool:
        p = self.project_dir()
        if not p:
            return False
        # webagent project must have run.py + app/ + pyproject.toml
        return (p / "run.py").exists() and (p / "app").is_dir()
