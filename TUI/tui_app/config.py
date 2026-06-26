"""Configuration + provider resolution for the webagent TUI.

Three things are resolved here, all independent of the webAgent server:

* **data dir** — a per-user directory for the TUI's OWN state (its external
  database, config). Deliberately outside any webAgent checkout so a Clear
  DB / Full Reset of the web app never touches the server manager's memory.
* **project dir** — the target webAgent checkout the agent operates on.
* **LLM provider** — api key / base url / model, resolved from (highest first):
  an explicit per-TUI ``WEBAGENT_*`` override, then the linked repo's
  ``provider.json`` (the live, *complete* credential store the web app itself
  uses — authoritative once a checkout is linked), then generic/legacy ``LLM_*``
  / ``OPENROUTER_*`` (the app-level key used during onboarding), then the saved
  config, then built-in defaults. Resolving ``provider.json`` as a coherent
  (api_key, base_url, model) triple — above both the partial ``OPENROUTER_*`` env
  and the generic ``LLM_*`` key — fixes the 401 *and* makes a linked repo's key
  win over the app key. OpenAI-compatible.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


# ── locations ────────────────────────────────────────────────────────────────
def data_dir() -> Path:
    """Where this build keeps its SQLite store + config + subagent transcripts.

    By default it lives in the ``tui-runtime/`` folder inside the TUI project
    directory, so the install is fully self-contained: the DB, config,
    monitor/alarm files, and per-subagent session history all sit in one
    subdirectory, and the TUI root stays clean. Override with
    ``WEBAGENT_DATA_DIR``.

    A packaged (frozen) ``.exe`` has no stable source tree to write into, so it
    falls back to the per-user app-data dir (``%APPDATA%/webagent`` on Windows,
    ``~/Library/Application Support/webagent`` on macOS, ``$XDG_DATA_HOME/webagent``
    or ``~/.local/share/webagent`` on Linux).
    """
    override = os.environ.get("WEBAGENT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):                 # packaged exe → app-data dir
        if sys.platform == "win32":
            base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
            return Path(base) / "webagent"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "webagent"
        xdg = os.environ.get("XDG_DATA_HOME")
        return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "webagent"
    # Running from source: keep everything in tui-runtime/ so the TUI root stays clean.
    return Path(__file__).resolve().parent.parent / "tui-runtime"


def db_path() -> Path:
    """The TUI's external SQLite DB — separate from the web app's local.db."""
    override = os.environ.get("WEBAGENT_DB")
    if override:
        return Path(override).expanduser()
    return data_dir() / "webagent.db"


def config_path() -> Path:
    return data_dir() / "config.json"


def _looks_like_project(p: Path) -> bool:
    try:
        return (p / "run.py").exists() and (p / "app").is_dir()
    except OSError:
        return False


def _default_project_dir() -> str:
    """Best-effort guess for the target checkout on first run."""
    env = os.environ.get("WEBAGENT_PROJECT")
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


def _load_provider_json(project_dir: Path) -> dict[str, str]:
    """Read the web app's ``provider.json`` and return a *coherent* provider triple.

    ``provider.json`` is the credential store the webAgent server itself uses, so
    its base_url / api_key / model always agree (unlike the project's ``.env``,
    whose ``OPENROUTER_*`` values are often partial or stale). Mirrors the
    selection in ``app/admin/settings.py``: prefer the ``admin`` profile
    (this is an operator/admin tool), then ``__anonymous__``, then the first
    profile that carries a key. Also handles the legacy flat format (config at the
    root). Returns ``{}`` when the file is absent — it's gitignored, so fresh
    checkouts won't have one — or unreadable, so resolution falls back cleanly.

    Checks both ``data/config/provider.json`` (the canonical app path) and
    ``provider.json`` at the project root (an older layout still in use).
    """
    for candidate in (
        project_dir / "data" / "config" / "provider.json",
        project_dir / "provider.json",
    ):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if "api_key" in data and "base_url" in data:      # legacy flat format
            cfg: Any = data
        else:
            cfg = data.get("admin") or data.get("__anonymous__")
            if not isinstance(cfg, dict):
                cfg = next(
                    (v for v in data.values() if isinstance(v, dict) and v.get("api_key")),
                    None,
                )
        if not isinstance(cfg, dict):
            continue
        return {
            "api_key": str(cfg.get("api_key") or "").strip(),
            "base_url": str(cfg.get("base_url") or "").strip().rstrip("/"),
            "model": str(cfg.get("model") or "").strip(),
        }
    return {}


@dataclass
class ProviderConfig:
    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "deepseek/deepseek-chat"

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and self.api_key not in (
            "your_api_key", "your_openrouter_api_key", "your_key_here",
        )


# Known OpenAI-compatible providers → (base_url, a sensible default model). The App
# panel offers these so the base URL always MATCHES the key's provider — the usual
# cause of a "missing authentication header" / 401 is a key paired with the wrong URL
# (e.g. an OpenAI key sent to the OpenRouter endpoint). "Custom" lets you type a URL.
PROVIDER_PRESETS: list[tuple[str, str, str]] = [
    ("OpenRouter", "https://openrouter.ai/api/v1", "deepseek/deepseek-chat"),
    ("OpenAI", "https://api.openai.com/v1", "gpt-4o-mini"),
    ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("Groq", "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    ("Together", "https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    ("Mistral", "https://api.mistral.ai/v1", "mistral-large-latest"),
    ("xAI (Grok)", "https://api.x.ai/v1", "grok-2-latest"),
    ("Custom", "", ""),
]


def provider_name_for_base(base_url: str) -> str:
    """Best-match provider preset name for a base URL (for pre-selecting the dropdown)."""
    b = (base_url or "").strip().rstrip("/")
    for name, url, _ in PROVIDER_PRESETS:
        if url and b == url.rstrip("/"):
            return name
    return "Custom"


def resolve_provider(project_dir: Optional[Path], saved: "TuiConfig") -> ProviderConfig:
    """Resolve LLM provider config. Order, highest priority first:

    1. explicit per-TUI override ``WEBAGENT_*`` (env or project ``.env``)
    2. the linked repo's ``provider.json`` — a *complete*, internally-consistent
       (api_key, base_url, model) triple; authoritative once a checkout is linked
    3. generic/legacy ``LLM_*`` / ``OPENROUTER_*`` (env or project ``.env``) — these
       act as the app-level key during onboarding (no repo linked)
    4. the saved TUI config
    5. built-in defaults

    Two things this ordering buys us:
    * ``provider.json`` above ``OPENROUTER_*`` fixes the 401 — those ``.env`` keys
      supply a key + model but no base URL, so base_url silently defaulted to
      OpenRouter while the key/model were really another provider's. Taking the
      whole triple from ``provider.json`` keeps the three from being mixed.
    * ``provider.json`` above the generic ``LLM_*`` means a **linked repo's key
      wins** over an app-level ``LLM_*`` key — so onboarding uses the app key and a
      linked checkout uses its own, exactly as intended.
    """
    env = os.environ
    proj_env = _parse_env_file(project_dir / ".env") if project_dir else {}
    pj = _load_provider_json(project_dir) if project_dir else {}

    def pick(*keys: str) -> str:
        for k in keys:
            if env.get(k):
                return env[k].strip()
        for k in keys:
            if proj_env.get(k):
                return proj_env[k].strip()
        return ""

    # When provider_override is ON, the TUI is fully self-reliant: it ignores
    # the web app's provider.json and project .env entirely. Only the explicit
    # WEBAGENT_* env override (for emergencies) sits above the saved config.
    # This keeps the TUI working even when the web app has no LLM configured.
    if saved.provider_override and saved.api_key:
        api_key = pick("WEBAGENT_API_KEY") or saved.api_key
        base_url = (
            pick("WEBAGENT_BASE_URL")
            or saved.base_url
            or "https://openrouter.ai/api/v1"
        )
        model = (
            pick("WEBAGENT_MODEL")
            or saved.model
            or "deepseek/deepseek-chat"
        )
        return ProviderConfig(api_key=api_key.strip(), base_url=base_url.rstrip("/"), model=model.strip())

    # Without a UI override, fall through to the web app's provider.json, then
    # legacy LLM_* / OPENROUTER_* env vars, then the saved config's fallback.
    api_key = (
        pick("WEBAGENT_API_KEY")
        or pj.get("api_key", "")
        or pick("LLM_API_KEY", "OPENROUTER_API_KEY")
        or saved.api_key
    )
    base_url = (
        pick("WEBAGENT_BASE_URL")
        or pj.get("base_url", "")
        or pick("LLM_BASE_URL", "OPENROUTER_BASE_URL")
        or saved.base_url
        or "https://openrouter.ai/api/v1"
    )
    model = (
        pick("WEBAGENT_MODEL")
        or pj.get("model", "")
        or pick("LLM_MODEL", "OPENROUTER_MODEL")
        or saved.model
        or "deepseek/deepseek-chat"
    )
    return ProviderConfig(api_key=api_key.strip(), base_url=base_url.rstrip("/"), model=model.strip())


@dataclass
class TuiConfig:
    project_path: str = ""
    api_key: str = ""              # optional override saved here if user enters one
    base_url: str = ""
    model: str = ""
    provider: str = ""             # provider preset name chosen in the App panel (display)
    provider_override: bool = False  # True once the user Saves a key/provider in the UI →
                                     # the saved triple then wins over the repo's provider.json/.env
    git_token: str = ""            # GitHub token used to authenticate fetch/pull/push (Git panel)
    autonomous: bool = False       # opt-in: act on mutating tools without per-call gating
    writes_enabled: bool = False   # interactive "armed" toggle for mutating tools
    theme_name: str = "lime"       # active Textual theme (see themes.THEME_ORDER)
    anim_enabled: bool = True      # show the animated logo banner
    anim_style: str = "plasma"     # plasma | flowfield | rings | static | off
    anim_palette: str = "theme"    # "theme" = match active theme; else a preset name
    anim_speed: float = 1.0        # 0.5 / 1.0 / 2.0
    anim_intensity: float = 1.0    # 0.6 / 1.0 / 1.5
    anim_fps: int = 20             # 12 / 20 / 30
    max_turns: int = 50
    temperature: float = 0.0
    side_expanded: bool = False    # sidebar width: narrow (default) ↔ wide, toggled in-panel
    session_name_mode: str = "summary"  # how sessions are auto-named: summary | message | off
    bridge_enabled: bool = True    # auto-start the TUI bridge server on launch
    bridge_port: int = 0           # 0 = pick a free port automatically

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
