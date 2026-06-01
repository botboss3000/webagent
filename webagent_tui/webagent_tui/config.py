"""Configuration + provider resolution for the webagent TUI.

Three things are resolved here, all independent of the webAgent server:

* **data dir** — a per-user directory for the TUI's OWN state (its external
  database, config). Deliberately outside any webAgent checkout so a Clear
  DB / Full Reset of the web app never touches the server manager's memory.
* **project dir** — the target webAgent checkout the agent operates on.
* **LLM provider** — api key / base url / model, resolved from (in order)
  explicit ``WEBAGENT_TUI_*`` env, generic ``LLM_*`` env, ``OPENROUTER_*`` env,
  the target project's ``.env`` file, then the saved config. OpenAI-compatible.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


# ── locations ────────────────────────────────────────────────────────────────
def data_dir() -> Path:
    """Per-user data dir for the TUI's external DB + config.

    Windows: %APPDATA%\\webagent-tui   macOS: ~/Library/Application Support/...
    Linux:   $XDG_DATA_HOME/webagent-tui or ~/.local/share/webagent-tui
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "webagent-tui"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "webagent-tui"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "webagent-tui"


def db_path() -> Path:
    """The TUI's external SQLite DB — separate from the web app's local.db."""
    override = os.environ.get("WEBAGENT_TUI_DB")
    if override:
        return Path(override).expanduser()
    return data_dir() / "webagent_tui.db"


def config_path() -> Path:
    return data_dir() / "config.json"


def _looks_like_project(p: Path) -> bool:
    try:
        return (p / "run.py").exists() and (p / "app").is_dir()
    except OSError:
        return False


def _default_project_dir() -> str:
    """Best-effort guess for the target checkout on first run."""
    env = os.environ.get("WEBAGENT_TUI_PROJECT")
    if env:
        return str(Path(env).expanduser())
    # The exe / package may sit inside or beside a checkout.
    here = Path.cwd()
    for cand in (here, *here.parents):
        if _looks_like_project(cand):
            return str(cand)
    return ""


# ── .env parsing (no python-dotenv dependency) ───────────────────────────────
def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            out[key.strip()] = val.strip().strip("'").strip('"')
    except OSError:
        pass
    return out


@dataclass
class ProviderConfig:
    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "deepseek/deepseek-v4-flash"

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and self.api_key not in (
            "your_api_key", "your_openrouter_api_key", "your_key_here",
        )


def resolve_provider(project_dir: Optional[Path], saved: "TuiConfig") -> ProviderConfig:
    """Resolve LLM provider config from env → project .env → saved config."""
    env = os.environ
    proj_env = _parse_env_file(project_dir / ".env") if project_dir else {}

    def pick(*keys: str) -> str:
        for k in keys:
            if env.get(k):
                return env[k]
        for k in keys:
            if proj_env.get(k):
                return proj_env[k]
        return ""

    api_key = pick("WEBAGENT_TUI_API_KEY", "LLM_API_KEY", "OPENROUTER_API_KEY") or saved.api_key
    base_url = (
        pick("WEBAGENT_TUI_BASE_URL", "LLM_BASE_URL", "OPENROUTER_BASE_URL")
        or saved.base_url
        or "https://openrouter.ai/api/v1"
    )
    model = (
        pick("WEBAGENT_TUI_MODEL", "LLM_MODEL", "OPENROUTER_MODEL")
        or saved.model
        or "deepseek/deepseek-v4-flash"
    )
    return ProviderConfig(api_key=api_key, base_url=base_url.rstrip("/"), model=model)


@dataclass
class TuiConfig:
    project_path: str = ""
    api_key: str = ""              # optional override saved here if user enters one
    base_url: str = ""
    model: str = ""
    autonomous: bool = False       # opt-in: act on mutating tools without per-call gating
    writes_enabled: bool = False   # interactive "armed" toggle for mutating tools
    theme_name: str = "lime"       # active Textual theme (see themes.THEME_ORDER)
    max_turns: int = 50
    temperature: float = 0.0

    @classmethod
    def load(cls) -> "TuiConfig":
        try:
            raw = config_path().read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            cfg = cls()
            if not cfg.project_path:
                cfg.project_path = _default_project_dir()
            return cfg
        valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        cfg = cls(**valid)
        if not cfg.project_path:
            cfg.project_path = _default_project_dir()
        return cfg

    def save(self) -> None:
        try:
            d = config_path()
            d.parent.mkdir(parents=True, exist_ok=True)
            tmp = d.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            os.replace(tmp, d)
        except OSError:
            pass

    def project_dir(self) -> Optional[Path]:
        if self.project_path:
            p = Path(self.project_path).expanduser()
            if _looks_like_project(p):
                return p
        return None
