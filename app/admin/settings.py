"""
Settings endpoints — toggleable features for the agent.

Supports:
  - metadata_logging: stores full prompt context in interactions.metadata
  - provider: per-user AI provider config (provider, base_url, api_key, model)

Provider configs are stored per user_id in provider.json:
  {
    "__anonymous__": { ... config ... },
    "admin": { ... config ... },
    ...
  }
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from app.auth.jwt import decode_token
from app.util.config_io import safe_write_json, set_config_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/settings", tags=["admin"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
METADATA_FLAG = PROJECT_ROOT / ".metadata-enabled"
PROVIDER_FILE = PROJECT_ROOT / "data" / "config" / "provider.json"
APP_SETTINGS_FILE = PROJECT_ROOT / "data" / "config" / "app-settings.json"

ANONYMOUS_KEY = "__anonymous__"

DEFAULT_PROVIDER = {
    "provider": "openrouter",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key": "",
    "model": "",
    "providers": {},
    "multi_providers": [],
}

PROVIDER_PRESETS = {
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
    },
    "groq": {
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "together": {
        "name": "Together AI",
        "base_url": "https://api.together.xyz/v1",
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
    },
    "mistral": {
        "name": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
    },
    "fireworks": {
        "name": "Fireworks AI",
        "base_url": "https://api.fireworks.ai/inference/v1",
    },
    "xai": {
        "name": "xAI (Grok)",
        "base_url": "https://api.x.ai/v1",
    },
    "perplexity": {
        "name": "Perplexity",
        "base_url": "https://api.perplexity.ai",
    },
    "ollama": {
        "name": "Ollama (local)",
        "base_url": "http://localhost:11434/v1",
    },
    "deepinfra": {
        "name": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
    },
    "lmstudio": {
        "name": "LM Studio (local)",
        "base_url": "http://localhost:1234/v1",
    },
}


# ── User ID extraction ──────────────────────────────────────────────────────

def _resolve_user_id(authorization: str = "", token_qs: str = "") -> str:
    """Extract user_id from Authorization header or query param.
    Returns ANONYMOUS_KEY if no valid token.
    """
    raw = ""
    if authorization.startswith("Bearer "):
        raw = authorization[7:]
    if not raw and token_qs:
        raw = token_qs

    if raw:
        payload = decode_token(raw)
        if payload:
            return payload.get("user_id", ANONYMOUS_KEY)

    return ANONYMOUS_KEY


# ── Per-user provider storage ──────────────────────────────────────────────

def _load_all_providers() -> dict:
    """Load the full provider.json (map of user_id → config)."""
    try:
        if PROVIDER_FILE.exists():
            with open(PROVIDER_FILE) as f:
                data = json.load(f)
                # Migration: if old flat format (has "provider" key at root),
                # wrap under ANONYMOUS_KEY
                if "provider" in data:
                    data = {ANONYMOUS_KEY: data}
                    _save_all_providers(data)
                return data
    except Exception as e:
        logger.warning("Failed to load provider.json: %s", e)
    return {}


def _save_all_providers(data: dict) -> None:
    """Save the full provider.json (creates data/config/ on demand)."""
    try:
        safe_write_json(PROVIDER_FILE, data)
    except Exception as e:
        logger.warning("Failed to save provider.json: %s", e)


def _load_provider(user_id: str) -> dict:
    """Load provider config for a specific user. Returns DEFAULT_PROVIDER if none."""
    all_configs = _load_all_providers()
    config = all_configs.get(user_id)
    if config:
        return dict(config)
    # Fall back to admin user's config (so anonymous users get a working LLM)
    config = all_configs.get("admin")
    if config:
        return dict(config)
    # Fall back to anonymous config
    config = all_configs.get(ANONYMOUS_KEY)
    if config:
        return dict(config)
    return dict(DEFAULT_PROVIDER)


# ── LLM secret handling — keys live ONLY in the encrypted vault ─────────────
#
# An LLM config can carry several API keys: the default brain's key, one per
# entry in the ``providers`` map, and one per saved-roster model. The vault row
# (``auth_elements``) only Fernet-encrypts ``secret_ref``; its ``config`` blob is
# a plaintext column. So we pack EVERY key into a single JSON bundle stored in
# ``secret_ref`` (encrypted), and keep every other store — the ``config`` blob and
# provider.json — stripped of keys. Reads rehydrate the keys back out of the
# bundle, so callers see a complete config without any plaintext key ever landing
# on disk or in an unencrypted column.

def _has_plaintext_key(cfg: dict) -> bool:
    """True if any api_key is present anywhere in a config dict."""
    if not isinstance(cfg, dict):
        return False
    if cfg.get("api_key"):
        return True
    for v in (cfg.get("providers") or {}).values():
        if isinstance(v, dict) and v.get("api_key"):
            return True
    for e in (cfg.get("multi_providers") or []):
        if isinstance(e, dict) and e.get("api_key"):
            return True
    return False


def _strip_provider_secrets(config: dict) -> dict:
    """Return a copy of a provider config with every api_key blanked — the shape
    written to provider.json and to the vault's plaintext ``config`` blob."""
    clean = dict(config or {})
    clean["api_key"] = ""
    provs = clean.get("providers")
    if isinstance(provs, dict):
        clean["providers"] = {
            k: ({**v, "api_key": ""} if isinstance(v, dict) else v)
            for k, v in provs.items()
        }
    mp = clean.get("multi_providers")
    if isinstance(mp, list):
        clean["multi_providers"] = [
            ({**e, "api_key": ""} if isinstance(e, dict) else e) for e in mp
        ]
    return clean


def _pack_llm_secret(config: dict) -> str:
    """Collect every api_key in a config into one JSON bundle for the encrypted
    ``secret_ref``. Returns "" when there are no keys at all."""
    default = config.get("api_key", "") or ""
    providers = {}
    for name, v in (config.get("providers") or {}).items():
        if isinstance(v, dict) and v.get("api_key"):
            providers[name] = v["api_key"]
    multi = [
        (e.get("api_key", "") if isinstance(e, dict) else "")
        for e in (config.get("multi_providers") or [])
    ]
    while multi and not multi[-1]:   # trim trailing empties
        multi.pop()
    if not default and not providers and not multi:
        return ""
    bundle = {"v": 1, "default": default}
    if providers:
        bundle["providers"] = providers
    if multi:
        bundle["multi"] = multi
    return json.dumps(bundle, separators=(",", ":"))


def _unpack_llm_secret(secret_ref: str) -> dict:
    """Parse a packed bundle. A bare key string (legacy / env-seeded rows) is
    treated as just the default key.

    ``secret_ref`` should already be plaintext by the time it gets here (the
    storage layer decrypts it — see EncryptedStorageBackend in app/db/interface.py).
    If decryption failed there (e.g. no DEK available for this device/vault),
    that layer now returns "" rather than the raw ciphertext, but treat a
    still-encrypted-looking string as unavailable here too rather than smuggling
    it into a config as if it were a real key.
    """
    if not secret_ref:
        return {}
    from app.encryption.interface import is_ciphertext
    if is_ciphertext(secret_ref):
        logger.error("LLM secret_ref is still encrypted (decrypt must have failed) — ignoring it")
        return {}
    s = secret_ref.strip()
    if s.startswith("{"):
        try:
            d = json.loads(s)
            if isinstance(d, dict) and d.get("v"):
                return d
        except Exception:
            pass
    return {"default": secret_ref}   # legacy bare key


def _rehydrate_llm_keys(config: dict, secret_ref: str) -> dict:
    """Splice keys from the encrypted bundle back into a config dict (mutates and
    returns it). A key already present in the (legacy plaintext) config is kept
    only when the bundle has nothing for that slot, so this is safe both before
    and after the migration has stripped the plaintext copies."""
    bundle = _unpack_llm_secret(secret_ref)
    config["api_key"] = bundle.get("default") or config.get("api_key", "") or ""
    bp = bundle.get("providers") or {}
    provs = config.get("providers")
    if isinstance(provs, dict):
        for name, v in provs.items():
            if isinstance(v, dict):
                v["api_key"] = bp.get(name) or v.get("api_key", "") or ""
    bm = bundle.get("multi") or []
    mp = config.get("multi_providers")
    if isinstance(mp, list):
        for i, e in enumerate(mp):
            if isinstance(e, dict):
                e["api_key"] = (bm[i] if i < len(bm) else "") or e.get("api_key", "") or ""
    return config


async def _persist_llm_config(user_id: str, full: dict) -> None:
    """Single write path for a user's LLM config.

    Guarantees no API key is ever stored in plaintext: every key is packed into
    ONE encrypted vault secret (``auth_elements.secret_ref``); the DB ``config``
    blob and provider.json receive a key-stripped copy. provider.json stays as a
    non-secret fallback + boot seed; the active env (this process) still gets the
    real key.
    """
    secret = _pack_llm_secret(full)
    stripped = _strip_provider_secrets(full)
    try:
        from app.db import get_db
        db = get_db()
        await db.auth_element_set(
            user_id=user_id,
            service="llm",
            config=stripped,
            secret_ref=secret,
            label="default",
        )
    except Exception as e:
        logger.warning("Failed to save LLM config to vault: %s", e)
    # provider.json (stripped inside _save_provider) + active env (full, with key)
    _save_provider(user_id, full)


async def _load_own_llm_config(user_id: str) -> Optional[dict]:
    """Load a user's OWN LLM config (vault row, else its provider.json entry) with
    keys rehydrated — NO admin/anonymous fallback. Used by the migration so we
    never copy one user's config onto another."""
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(user_id, "llm", "default")
        if elem:
            cfg = elem.get("config") or {}
            if isinstance(cfg, str):
                cfg = json.loads(cfg or "{}")
            return _rehydrate_llm_keys(cfg, elem.get("secret_ref", ""))
    except Exception:
        pass
    fc = _load_all_providers().get(user_id)
    return dict(fc) if isinstance(fc, dict) else None


async def _llm_storage_has_plaintext(user_id: str) -> bool:
    """True if a user's provider.json entry or vault config blob still holds a
    plaintext api_key (an encrypted secret_ref does NOT count)."""
    fc = _load_all_providers().get(user_id)
    if _has_plaintext_key(fc):
        return True
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(user_id, "llm", "default")
    except Exception:
        elem = None
    if elem:
        cfg = elem.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg or "{}")
            except Exception:
                cfg = {}
        if _has_plaintext_key(cfg):
            return True
    return False


async def migrate_llm_secrets() -> None:
    """One-time scrub of historically plaintext LLM keys.

    For every user that has a provider config, fold any plaintext api_key (from
    provider.json OR the DB ``config`` blob) into the encrypted vault secret and
    rewrite both stores stripped. Idempotent: once no plaintext key remains for a
    user, re-running skips them. Safe to call on every startup.
    """
    try:
        candidates = set(_load_all_providers().keys()) | {"admin"}
    except Exception:
        candidates = {"admin"}
    secured = 0
    for uid in candidates:
        try:
            if not await _llm_storage_has_plaintext(uid):
                continue
            own = await _load_own_llm_config(uid)
            if not own:
                continue
            await _persist_llm_config(uid, own)
            secured += 1
        except Exception as e:
            logger.warning("LLM secret migration failed for %s: %s", str(uid)[:12], e)
    if secured:
        logger.info("Secured plaintext LLM key(s) for %d user(s) into the vault", secured)


def _save_provider(user_id: str, config: dict) -> None:
    """Save provider config for a specific user to provider.json — WITHOUT any
    api_key — and apply it to the active env (with the key) for this process.

    The secret never lands in provider.json; it lives only in the encrypted vault
    (see _persist_llm_config). provider.json keeps the non-secret fields
    (provider/model/base_url/capabilities/roster) as a fallback + boot seed; every
    other user's config is preserved and data/config/ is created on demand.
    """
    # Fill in base_url from preset if missing
    provider = config.get("provider", "")
    if not config.get("base_url") and provider in PROVIDER_PRESETS:
        config["base_url"] = PROVIDER_PRESETS[provider]["base_url"]

    try:
        set_config_key(PROVIDER_FILE, user_id, _strip_provider_secrets(config))
    except Exception as e:
        logger.warning("Failed to save provider.json: %s", e)

    # Apply this config as the active env vars (current user's session) — uses the
    # in-memory config WITH the key; only the on-disk copy is stripped.
    _apply_config_to_env(config)

    logger.info("Provider config saved for user %s: %s", user_id[:12], config.get("provider"))


async def _resolve_user_config(user_id: str) -> dict:
    """Resolve a user's provider config WITHOUT touching env.
    Tries auth_elements DB table first (own config, then admin fallback), then
    provider.json. Shared by the runtime applier and the chat-footer resolver so
    both see the exact same base config.
    """
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(user_id, "llm", "default")
        if not elem:
            # Fall back to admin user's config (anonymous visitors get a working LLM)
            elem = await db.auth_element_get("admin", "llm", "default")
        if elem:
            cfg = elem.get("config") or {}
            if isinstance(cfg, str):
                cfg = json.loads(cfg or "{}")
            _rehydrate_llm_keys(cfg, elem.get("secret_ref", ""))
            return cfg
    except Exception:
        pass
    return _load_provider(user_id)


def _agent_llm_override(agent_rec: Optional[dict]) -> Optional[dict]:
    """Return an agent's custom LLM config IF it overrides the default.

    Agents store ``{use_default, provider, base_url, api_key, model,
    multi_providers, …}`` in ``metadata['llm_config']``. Only an explicit
    ``use_default=False`` counts as an override, and it must carry either a single
    ``model`` (the chosen default) OR a non-empty ``multi_providers`` roster (the
    agent's own saved models); anything else means "use the app default".
    """
    if not agent_rec:
        return None
    meta = agent_rec.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    if not isinstance(meta, dict):
        return None
    cfg = meta.get("llm_config")
    if not isinstance(cfg, dict):
        return None
    if cfg.get("use_default") is not False:
        return None
    mp = cfg.get("multi_providers")
    has_roster = isinstance(mp, list) and any(
        isinstance(p, dict) and p.get("model") for p in mp
    )
    # A bare inherited-model opt-out (no own model / roster) still counts as an
    # override: the agent is narrowing which app-default models / capabilities it
    # uses, which must reach _merge_agent_override to take effect at runtime.
    inh = cfg.get("inherited_overrides")
    has_inh = isinstance(inh, dict) and len(inh) > 0
    if not cfg.get("model") and not has_roster and not has_inh:
        return None
    return cfg


def _merge_agent_override(base: dict, override: dict) -> dict:
    """Layer an agent's (or session's) non-empty override fields over the base.
    Preserves the user's api_key / base_url when the override didn't set its own
    (e.g. an agent that only swaps the model on the same provider + key).

    The override's top-level provider/base_url/api_key/model IS the chosen default
    brain (set by the model table's "Default" radio, or the per-chat switcher).
    When the override also carries its own ``multi_providers`` roster it becomes
    the UNION of inherited (app-default) + own models. That roster is NOT raced —
    parallel racing was removed; it only supplies the candidate list for the chat
    model-switcher and the vision / image-out worker picks. If the override set no
    top-level model, the first roster entry is mirrored up so there's always a
    concrete default to run / report.

    The override may also carry ``inherited_overrides`` — a per-model map (keyed
    ``provider|base_url|model``) of the agent's chosen capability subset for the
    app-default models. Each flag is clamped to the admin's stored ceiling (an agent
    can only narrow, never widen), and an inherited model the agent turned fully off
    is dropped from the union.
    """
    merged = dict(base or {})
    for k in ("provider", "base_url", "api_key", "model"):
        v = override.get(k)
        if v:
            merged[k] = v
    mp = override.get("multi_providers")
    own = [p for p in mp if isinstance(p, dict) and p.get("model")] if isinstance(mp, list) else []
    inh_ovr = override.get("inherited_overrides")
    inh_ovr = inh_ovr if isinstance(inh_ovr, dict) else {}
    # When "Extend default LLM to agents" is on, the agent's candidate models are the
    # UNION of the app-default roster (inherited) + its own — matching the per-agent
    # UI, which shows the defaults as inherited rows plus the agent's own. When off,
    # only its own. (For a session-level override `base` is the already-merged agent
    # config, so this same union simply PRESERVES the agent's candidate list while the
    # session pins its chosen model.)
    try:
        extend_on = _load_app_settings().get("extend_llm_to_agents", True) is not False
    except Exception:
        extend_on = True
    inherited_raw = [p for p in (base.get("multi_providers") or [])
                     if isinstance(p, dict) and p.get("model")] if extend_on else []
    # Apply this agent's per-model opt-outs. The admin's stored flags are the
    # CEILING; the agent's choice can only narrow them, never widen. A model the
    # agent turned fully off (no Text / In / Out / Eff left) is dropped entirely.
    inherited = []
    for p in inherited_raw:
        key = f"{p.get('provider') or ''}|{p.get('base_url') or ''}|{p.get('model') or ''}"
        ov = inh_ovr.get(key)
        if not ov:
            inherited.append(p)
            continue
        q = dict(p)
        q["enabled"] = (p.get("enabled") is not False) and (ov.get("enabled") is not False)
        q["use_for_image"] = bool(p.get("use_for_image")) and bool(ov.get("use_for_image"))
        q["use_for_image_out"] = bool(p.get("use_for_image_out")) and bool(ov.get("use_for_image_out"))
        q["high_effort_capable"] = bool(p.get("high_effort_capable")) and bool(ov.get("high_effort_capable"))
        if not (q["enabled"] or q["use_for_image"] or q["use_for_image_out"] or q["high_effort_capable"]):
            continue   # agent opted out of this inherited model completely
        inherited.append(q)
    # Build the union whenever there's anything to offer — the agent's own roster,
    # an opt-out map, OR inherited candidates (so pinning an inherited model as the
    # default still leaves the rest of the app defaults switchable).
    if own or inh_ovr or inherited:
        seen, union = set(), []
        for p in inherited + own:
            key = (p.get("provider"), p.get("model"), p.get("base_url"))
            if key in seen:
                continue
            seen.add(key)
            union.append(p)
        merged["multi_providers"] = union
        if union:
            first = union[0]
            for k in ("provider", "base_url", "api_key", "model"):
                if first.get(k) and not override.get(k):
                    merged[k] = first[k]
            # If the resolved default brain was itself opted out (no longer in the
            # union), repoint it to the first surviving candidate so the agent still
            # runs a valid model instead of a dropped one.
            union_models = {p.get("model") for p in union}
            if merged.get("model") not in union_models:
                for k in ("provider", "base_url", "api_key", "model"):
                    if first.get(k):
                        merged[k] = first[k]
    else:
        merged["multi_providers"] = []
    return merged


def _session_llm_override(cfg: Optional[dict]) -> Optional[dict]:
    """Return a session's custom LLM config IF it overrides the layer below.

    Sessions store ``{use_default, model}`` in ``metadata['llm_config']`` (set by
    the chat footer model picker). Only an explicit ``use_default=False`` carrying
    a model counts; anything else means "fall back to the agent/app default".
    """
    if not isinstance(cfg, dict):
        return None
    if cfg.get("use_default") is not False:
        return None
    if not cfg.get("model"):
        return None
    return cfg


# The reasoning-effort scale we expose in the chat footer picker and the Model
# Switcher ability. "default" = send no hint (today's behaviour). The rest map to
# the provider's normalised reasoning.effort levels; providers that don't support
# a given level (e.g. some ignore "minimal") just drop it and the call retries.
REASONING_EFFORT_LEVELS = ["default", "minimal", "low", "medium", "high"]


def _resolve_session_effort(session_override: Optional[dict], model: Optional[str]) -> Optional[str]:
    """The reasoning-effort level stored for ``model`` on this session, or None.

    Read straight from the raw session override's ``model_effort`` map — NOT gated
    on a model override, so an effort applies even when the chat runs the agent's
    default model. ``"default"`` (or unknown) resolves to None (no hint sent)."""
    if not isinstance(session_override, dict) or not model:
        return None
    effort_map = session_override.get("model_effort")
    if not isinstance(effort_map, dict):
        return None
    level = (effort_map.get(model) or "").strip().lower()
    if level in ("", "default") or level not in REASONING_EFFORT_LEVELS:
        return None
    return level


def _apply_effort_to_env(level: Optional[str]) -> None:
    """Stash the resolved reasoning-effort level for the loop to read, or clear it."""
    if level:
        os.environ["LLM_REASONING_EFFORT"] = level
    else:
        os.environ.pop("LLM_REASONING_EFFORT", None)


def _effective_config(
    base: dict,
    agent_rec: Optional[dict],
    session_override: Optional[dict] = None,
) -> dict:
    """User config with any per-agent override, then any per-session override,
    layered on top — resolution order app-default → agent → session."""
    override = _agent_llm_override(agent_rec)
    merged = _merge_agent_override(base, override) if override else dict(base or {})
    sess = _session_llm_override(session_override)
    if sess:
        merged = _merge_agent_override(merged, sess)
    return merged


async def _load_session_override(session_id: Optional[str]) -> Optional[dict]:
    """Fetch a session's stored llm_config (best-effort, None on any failure)."""
    if not session_id:
        return None
    try:
        from app.db import get_db
        return await get_db().get_session_llm_override(session_id)
    except Exception:
        return None


async def load_provider_for_user(user_id: str) -> None:
    """Load a user's provider config into env vars.
    Tries auth_elements DB table first, falls back to provider.json.
    Called at the start of each agent loop.
    """
    _apply_config_to_env(await _resolve_user_config(user_id))


async def apply_provider_for_run(
    user_id: str,
    agent_rec: Optional[dict] = None,
    session_id: Optional[str] = None,
) -> dict:
    """Apply the effective provider config for a run to env, honoring a per-agent
    LLM override (``metadata['llm_config']`` with ``use_default=False``) and then a
    per-session override (``sessions.metadata['llm_config']``) layered over the
    user's default. Returns the effective config that was applied.

    This is what makes an agent's *custom model* — or a session's picked model —
    actually take effect at runtime; without it the loop would always run on the
    user's global default.
    """
    session_override = await _load_session_override(session_id)
    effective = _effective_config(await _resolve_user_config(user_id), agent_rec, session_override)
    effective = await _ensure_tool_capable(effective, user_id)
    _apply_config_to_env(effective)
    # Per-session reasoning-effort for the resolved model (footer picker / Model
    # Switcher ability). Decoupled from the model override so an effort can apply
    # even when the chat runs on the agent's default model. The loop reads
    # LLM_REASONING_EFFORT and passes it as the provider reasoning hint.
    _apply_effort_to_env(_resolve_session_effort(session_override, effective.get("model")))
    return effective


async def resolve_active_model(
    user_id: str,
    agent_rec: Optional[dict] = None,
    session_id: Optional[str] = None,
) -> dict:
    """Return ``{model, provider, base_url}`` for the effective model of a run,
    WITHOUT mutating env. Mirrors apply_provider_for_run's resolution so the chat
    footer shows exactly the model the loop will use. Used by /current-model-info.
    """
    session_override = await _load_session_override(session_id)
    effective = _effective_config(await _resolve_user_config(user_id), agent_rec, session_override)
    effective = await _ensure_tool_capable(effective, user_id)
    provider = effective.get("provider", "") or ""
    base_url = effective.get("base_url") or (PROVIDER_PRESETS.get(provider, {}) or {}).get("base_url", "")
    effort = _resolve_session_effort(session_override, effective.get("model"))
    return {"model": effective.get("model", "") or "", "provider": provider,
            "base_url": base_url, "reasoning_effort": effort or "default"}


async def resolve_agent_models(
    user_id: str,
    agent_rec: Optional[dict] = None,
    session_id: Optional[str] = None,
) -> dict:
    """Return the Text-capable candidate models a chat user can switch between for
    this agent — the SAME union the run would consider (app default(s) + the
    agent's own roster, per the extend-to-agents setting) — plus the model
    currently active for this chat. Powers the chat footer model switcher.

    Returns ``{"models": [{"id", "provider"}], "active": "<model id>"}``.
    """
    session_override = await _load_session_override(session_id)
    effective = _effective_config(await _resolve_user_config(user_id), agent_rec, session_override)
    active = (effective.get("model") or "").strip()
    models, seen = [], set()
    for p in (effective.get("multi_providers") or []):
        mid = (p.get("model") or "").strip()
        if not mid or mid in seen:
            continue
        if p.get("text_capable") is False:   # workers (vision / image-out only) aren't brains
            continue
        seen.add(mid)
        models.append({"id": mid, "provider": p.get("provider", "") or ""})
    # The active/default model is always offered, even if it isn't in the roster.
    if active and active not in seen:
        models.insert(0, {"id": active, "provider": effective.get("provider", "") or ""})
    # Per-model reasoning-effort the chat has chosen (footer picker shows it per row).
    effort_map = (session_override or {}).get("model_effort")
    effort_map = effort_map if isinstance(effort_map, dict) else {}
    return {"models": models, "active": active, "model_effort": effort_map}


def apply_provider_config() -> None:
    """Call on startup to apply saved anonymous provider config."""
    config = _load_provider(ANONYMOUS_KEY)
    _apply_config_to_env(config)


def _apply_config_to_env(config: dict) -> None:
    """Apply provider config dict to environment variables — the single resolved
    model the run will use (provider/base_url/api_key/model).
    """
    provider = config.get("provider", "")
    base_url = config.get("base_url")
    if not base_url and provider in PROVIDER_PRESETS:
        base_url = PROVIDER_PRESETS[provider]["base_url"]

    if provider:
        os.environ["LLM_PROVIDER"] = provider
        os.environ["OPENROUTER_PROVIDER"] = provider
    if config.get("api_key"):
        os.environ["LLM_API_KEY"] = config["api_key"]
        os.environ["OPENROUTER_API_KEY"] = config["api_key"]
    else:
        os.environ.pop("LLM_API_KEY", None)
        os.environ.pop("OPENROUTER_API_KEY", None)
    if base_url:
        os.environ["LLM_BASE_URL"] = base_url
        os.environ["OPENROUTER_BASE_URL"] = base_url
    else:
        os.environ.pop("LLM_BASE_URL", None)
        os.environ.pop("OPENROUTER_BASE_URL", None)
    if config.get("model"):
        os.environ["LLM_MODEL"] = config["model"]
        os.environ["OPENROUTER_MODEL"] = config["model"]
    else:
        os.environ.pop("LLM_MODEL", None)
        os.environ.pop("OPENROUTER_MODEL", None)

    # Parallel model racing was removed — clear any stale race-engine env so an
    # old process value can never re-trigger the deleted code path.
    os.environ.pop("PARALLEL_MODE", None)
    os.environ.pop("MULTI_PROVIDERS", None)


async def load_llm_capabilities_for_user(user_id: str) -> dict:
    """Read the user's LLM config (own → admin) and return media-capability
    info WITHOUT touching env vars.

    Used by chat.py at attachment-resolution time to decide whether an attached
    image must be described by a separate vision model before the turn runs. This
    is a per-request DB read on purpose — the env vars set by
    ``_apply_config_to_env`` are process-global and shared across users.

    Shape:
      {
        "default": {model, provider, base_url, api_key, text_capable,
                    image_capable, image_out_capable, use_for_image_out,
                    high_effort_capable},
        "racers":  [{model, provider, base_url, api_key, enabled,
                     text_capable, image_capable, use_for_image,
                     image_out_capable, use_for_image_out,
                     high_effort_capable}, ...],
      }

    ("racers" is the saved-model roster — kept for the worker picks below and the
    chat model-switcher list; the models are NOT raced, parallel racing was
    removed. Exactly one model — "default" — is the brain per run.)
    """
    cfg = None
    try:
        from app.db import get_db
        db = get_db()
        for uid in (user_id, "admin"):
            elem = await db.auth_element_get(uid, "llm", "default")
            if elem:
                c = elem.get("config") or {}
                if isinstance(c, str):
                    c = json.loads(c)
                _rehydrate_llm_keys(c, elem.get("secret_ref", ""))
                cfg = c
                break
    except Exception as e:
        logger.debug("load_llm_capabilities_for_user DB read failed: %s", e)
    if cfg is None:
        cfg = _load_provider(user_id)

    default = {
        "model": cfg.get("model", ""),
        "provider": cfg.get("provider", ""),
        "base_url": cfg.get("base_url", ""),
        "api_key": cfg.get("api_key", ""),
        "text_capable": bool(cfg.get("text_capable", True)),
        "image_capable": bool(cfg.get("image_capable", False)),
        "image_out_capable": bool(cfg.get("image_out_capable", False)),
        "use_for_image_out": bool(cfg.get("use_for_image_out", False)),
        "high_effort_capable": bool(cfg.get("high_effort_capable", False)),
    }
    racers = []
    for p in (cfg.get("multi_providers") or []):
        racers.append({
            "model": p.get("model", ""),
            "provider": p.get("provider", "custom"),
            "base_url": p.get("base_url", ""),
            "api_key": p.get("api_key", ""),
            "enabled": bool(p.get("enabled", True)),
            "text_capable": bool(p.get("text_capable", True)),
            "image_capable": bool(p.get("image_capable", False)),
            "use_for_image": bool(p.get("use_for_image", False)),
            "image_out_capable": bool(p.get("image_out_capable", False)),
            "use_for_image_out": bool(p.get("use_for_image_out", False)),
            "high_effort_capable": bool(p.get("high_effort_capable", False)),
        })

    # Reconcile the top-level "default" modality flags with the matching saved row.
    # The top-level fields are a legacy summary that drifts (e.g. they can read
    # image_capable=True left over from an image model even though the active text
    # model's own row correctly says False). The per-model row in multi_providers is
    # the source of truth, so when the default model id matches a row, take that
    # row's modality flags. Prevents a stale summary from routing an image to a
    # blind model. (grep RECONCILE-DEFAULT-MODALITY)
    if default.get("model"):
        match = next((r for r in racers if r.get("model") == default["model"]), None)
        if match:
            for k in ("text_capable", "image_capable",
                      "image_out_capable", "use_for_image_out",
                      "high_effort_capable"):
                if k in match:
                    default[k] = match[k]

    return {
        "default": default,
        "racers": racers,
    }


def model_sees_images(entry: dict) -> bool:
    """Can this configured model accept image INPUT? Combines the saved flag with
    the model-catalog guard: a catalog "no image input" overrides a stale True;
    catalog unknown/True trusts the configured flag. Mirrors the _ok() guard in
    pick_vision_model so every caller agrees on what 'can see' means."""
    if not (entry and entry.get("image_capable") and entry.get("model")):
        return False
    return _catalog_modality(entry["model"], entry.get("provider", ""), "in") is not False


def model_makes_images(entry: dict) -> bool:
    """Can this configured model produce image OUTPUT? Catalog-guarded, like
    model_sees_images but for the output modality."""
    if not (entry and entry.get("image_out_capable") and entry.get("model")):
        return False
    return _catalog_modality(entry["model"], entry.get("provider", ""), "out") is not False


def _is_tool_capable(entry: dict) -> bool:
    """True unless the catalog DEFINITELY says the model can't call tools. Unknown
    or True both pass (we don't second-guess) — mirrors _ensure_tool_capable."""
    model = (entry or {}).get("model", "")
    if not model:
        return False
    try:
        from app import model_catalog
        meta = model_catalog.lookup(model, provider_hint=entry.get("provider", ""))
    except Exception:
        meta = None
    if not meta:
        return True
    return meta.get("tool_call") is not False


def turn_models_image_capable(caps: dict) -> bool:
    """Can the single model that handles this turn (the default / active model)
    see images? Catalog-guarded: a stale image_capable=True on a text-only model
    can no longer fool the describe step into skipping (the original bug). (grep
    RECONCILE-DEFAULT-MODALITY)
    """
    return model_sees_images(caps.get("default") or {})


def pick_describer(caps: dict) -> Optional[dict]:
    """Choose the image-describer model when no turn model can see images.

    Prefer the default model if it is image-capable, else the first racer flagged
    ``use_for_image`` that is image-capable. Returns {model, provider, base_url,
    api_key} or None when no image-capable describer is configured.
    """
    d = caps.get("default") or {}
    if d.get("image_capable") and d.get("model") and d.get("api_key"):
        return {"model": d["model"], "provider": d.get("provider", ""),
                "base_url": d.get("base_url", ""), "api_key": d.get("api_key", "")}
    for r in (caps.get("racers") or []):
        if r.get("image_capable") and r.get("use_for_image") and r.get("model") and r.get("api_key"):
            return {"model": r["model"], "provider": r.get("provider", ""),
                    "base_url": r.get("base_url", ""), "api_key": r.get("api_key", "")}
    return None


def _infer_api_shape(base_url: str, provider: str = "") -> str:
    """Infer the image-generation API style from a model's base URL.

    Mirrors IMAGE_PROVIDER_PRESETS in plugins/abilities/Core/image_generation.py:
      api.stability.ai                    → stability
      generativelanguage.googleapis.com   → gemini
      openrouter.ai                       → openrouter (chat-completions image out;
                                            OpenRouter has NO /images/generations)
      everything else                     → openai-compatible /images/generations
    """
    b = (base_url or "").lower()
    if "stability.ai" in b:
        return "stability"
    if "generativelanguage.google" in b:
        return "gemini"
    if "openrouter.ai" in b or (provider or "").lower() == "openrouter":
        return "openrouter"
    return "openai"


def _catalog_modality(model: str, provider: str, direction: str) -> Optional[bool]:
    """Best-effort: does the model catalog say ``model`` supports image in/out?
    direction ∈ {"in","out"}. Returns True / False / None (unknown). Sync lookup
    against the already-loaded catalog cache — never raises, never blocks on a
    network fetch (a cold cache just yields None, and the caller trusts the flag).
    """
    try:
        from app import model_catalog
        meta = model_catalog.lookup(model, provider_hint=provider)
    except Exception:
        meta = None
    if not meta:
        return None
    key = "input_modalities" if direction == "in" else "output_modalities"
    mods = meta.get(key)
    if not isinstance(mods, list):
        return None
    return "image" in [str(m).lower() for m in mods]


def pick_image_generator(caps: dict) -> Optional[dict]:
    """Choose the model that powers the ``generate_image`` tool.

    Prefer the default model when it is flagged ``use_for_image_out`` (and
    capable), else the first saved row flagged ``use_for_image_out``. A row need
    NOT be enabled for text — a user may save an image-only model used solely for
    generation. The model catalog is used as a guard: a candidate the catalog says
    canNOT output images is skipped even if mis-ticked, so a stale checkbox can't
    route generation to a text-only model. Returns
    {model, provider, base_url, api_key, api_shape} or None.
    """
    def _ok(e: dict) -> bool:
        if not (e.get("use_for_image_out") and e.get("image_out_capable")
                and e.get("model") and e.get("api_key")):
            return False
        # Catalog veto: False = definitely can't; None/True = trust the flag.
        return _catalog_modality(e["model"], e.get("provider", ""), "out") is not False

    candidates = [caps.get("default") or {}] + list(caps.get("racers") or [])
    for e in candidates:
        if _ok(e):
            return {"model": e["model"], "provider": e.get("provider", ""),
                    "base_url": e.get("base_url", ""), "api_key": e.get("api_key", ""),
                    "api_shape": _infer_api_shape(e.get("base_url", ""), e.get("provider", ""))}
    return None


def media_routing(caps: dict) -> dict:
    """The single capability-routing picture, computed once and reused by the
    attachment type-router, the image_vision tools, and their guidance messages.

    Encodes the "enforce a decision" rule: if the turn model can't see/make images
    and there is NO valid model to switch to, the only path is delegating to a
    one-shot worker (``must_delegate_*``). A *switch target* must be configured
    text + image (catalog-guarded) AND tool-capable — so an image-in/out model with
    text turned off is excluded (it can only be a worker, never the brain).

    Returns::

        {
          "sees_natively":  bool,           # the turn model can read images
          "makes_natively": bool,           # the turn model can output images
          "describer":      dict | None,    # one-shot vision worker (read)
          "generator":      dict | None,    # one-shot image-out worker (make)
          "vision_switch_targets": [str],   # enabled text+image+tools models
          "gen_switch_targets":    [str],   # enabled text+image-out+tools models
          "must_delegate_vision":  bool,    # can't see & nothing to switch to
          "must_delegate_gen":     bool,    # can't make & nothing to switch to
        }
    """
    caps = caps or {}
    sees = turn_models_image_capable(caps)
    default = caps.get("default") or {}
    makes = model_makes_images(default)

    describer = pick_describer(caps) or pick_vision_model(caps)
    generator = pick_image_generator(caps)

    enabled = [r for r in (caps.get("racers") or []) if r.get("enabled")]
    # The default model is always a candidate brain.
    brains = [default] + enabled
    vision_switch = sorted({
        b["model"] for b in brains
        if b.get("model") and b.get("text_capable")
        and model_sees_images(b) and _is_tool_capable(b)
    })
    gen_switch = sorted({
        b["model"] for b in brains
        if b.get("model") and b.get("text_capable")
        and model_makes_images(b) and _is_tool_capable(b)
    })
    return {
        "sees_natively": sees,
        "makes_natively": makes,
        "describer": describer,
        "generator": generator,
        "vision_switch_targets": vision_switch,
        "gen_switch_targets": gen_switch,
        "must_delegate_vision": (not sees) and (not vision_switch),
        "must_delegate_gen": (not makes) and (not gen_switch),
    }


def high_effort_targets(caps: dict) -> list:
    """The enabled models an admin flagged "high-effort" that can actually be the
    agent's brain (text + tool-capable). These are the models the Model Control
    ability may upgrade ONTO for a hard task. Catalog-guarded via ``_is_tool_capable``
    so a mis-ticked tool-less model is excluded. Returns a sorted list of model ids.
    """
    caps = caps or {}
    default = caps.get("default") or {}
    enabled = [r for r in (caps.get("racers") or []) if r.get("enabled")]
    brains = [default] + enabled
    return sorted({
        b["model"] for b in brains
        if b.get("model") and b.get("text_capable")
        and b.get("high_effort_capable") and _is_tool_capable(b)
    })


def is_high_effort_model(caps: dict, model: str) -> bool:
    """True when ``model`` is one of the configured high-effort brain models."""
    if not model:
        return False
    return model in high_effort_targets(caps)


def pick_vision_model(caps: dict) -> Optional[dict]:
    """Choose the model that READS images for the ``process_image`` delegate tool.

    Prefer the default model if it can see images, else the first saved row flagged
    image-input capable. A row need NOT be enabled for text — an image-only model
    (e.g. a Gemini *-image variant) is a fine vision worker even though it can't be
    the agent's tool-calling brain. The model catalog guards the pick: a candidate
    the catalog says canNOT accept image input is skipped even if mis-ticked, so a
    stale checkbox can't send an image to a blind text model. Returns
    {model, provider, base_url, api_key} or None when no image-input model fits.
    """
    def _ok(e: dict) -> bool:
        if not (e.get("image_capable") and e.get("model") and e.get("api_key")):
            return False
        return _catalog_modality(e["model"], e.get("provider", ""), "in") is not False

    def _pick(e):
        return {"model": e["model"], "provider": e.get("provider", ""),
                "base_url": e.get("base_url", ""), "api_key": e.get("api_key", "")}

    d = caps.get("default") or {}
    if _ok(d):
        return _pick(d)
    # Prefer rows the admin explicitly ticked for image input, then any capable row.
    racers = list(caps.get("racers") or [])
    for e in racers:
        if _ok(e) and e.get("use_for_image"):
            return _pick(e)
    for e in racers:
        if _ok(e):
            return _pick(e)
    return None


async def _ensure_tool_capable(effective: dict, user_id: str) -> dict:
    """Safety net for the MAIN turn: if the resolved model definitively cannot call
    tools (catalog ``tool_call`` is False — e.g. an image-only model someone set as
    the brain), swap to a tool-capable enabled text model so the agent loop doesn't
    hard-fail with OpenRouter's "No endpoints found that support tool use". Only
    acts when we KNOW the model can't do tools; unknown/True is left untouched.

    Image generation and vision are handled by their own pickers/workers — this
    only guards the conversational model the loop attaches tools to.
    """
    model = effective.get("model", "")
    if not model:
        return effective
    try:
        from app import model_catalog
        await model_catalog.ensure_fresh()
        meta = model_catalog.lookup(model, provider_hint=effective.get("provider", ""))
    except Exception:
        meta = None
    if not meta or meta.get("tool_call") is not False:
        return effective  # capable, or unknown — don't second-guess

    try:
        caps = await load_llm_capabilities_for_user(user_id)
    except Exception:
        return effective
    candidates = [caps.get("default") or {}]
    candidates += [r for r in (caps.get("racers") or [])
                   if r.get("enabled") and r.get("text_capable") and r.get("model")]
    for c in candidates:
        cm = c.get("model", "")
        if not cm or cm == model:
            continue
        try:
            cmeta = model_catalog.lookup(cm, provider_hint=c.get("provider", ""))
        except Exception:
            cmeta = None
        if cmeta and cmeta.get("tool_call") is False:
            continue  # also tool-less — keep looking
        merged = dict(effective)
        merged["model"] = cm
        for k in ("provider", "base_url", "api_key"):
            if c.get(k):
                merged[k] = c[k]
        logger.info(
            "tool-use fallback: %s cannot call tools; running this turn on %s", model, cm
        )
        return merged
    logger.warning(
        "tool-use fallback: %s cannot call tools and no tool-capable text model is "
        "enabled — the turn may fail. Tick TEXT on a tool-capable model in App Config.",
        model,
    )
    return effective


def _load_app_settings() -> dict:
    try:
        if APP_SETTINGS_FILE.exists():
            with open(APP_SETTINGS_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.warning("Failed to load app-settings.json: %s", e)
    return {}


def _save_app_settings(data: dict) -> None:
    try:
        safe_write_json(APP_SETTINGS_FILE, data)
    except Exception as e:
        logger.warning("Failed to save app-settings.json: %s", e)


def _is_metadata_enabled() -> bool:
    return METADATA_FLAG.exists()


# ── Pydantic models ────────────────────────────────────────────────────────

class ProviderConfig(BaseModel):
    provider: str
    base_url: str = ""
    api_key: str
    model: str = ""
    providers: dict = {}
    # Media-capability of the default model (detected on save, user-overridable).
    text_capable: bool = True
    image_capable: bool = False
    image_out_capable: bool = False   # can generate images
    use_for_image_out: bool = False   # this model is the image generator
    high_effort_capable: bool = False  # admin-marked "premium" tier the agent may upgrade ONTO


class MultiProviderEntry(BaseModel):
    provider: str
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    enabled: bool = True          # "use for text" — selectable as the chat/brain model
    # Media-capability (detected on save, user-overridable) + image routing roles.
    text_capable: bool = True
    image_capable: bool = False
    use_for_image: bool = False   # eligible to describe images for text-only models
    image_out_capable: bool = False  # can generate images
    use_for_image_out: bool = False  # the image generator for the generate_image tool
    high_effort_capable: bool = False  # admin-marked "premium" tier the agent may upgrade ONTO


class MultiProvidersRequest(BaseModel):
    providers: list[MultiProviderEntry] = []


class MetadataSetting(BaseModel):
    enabled: bool


class AppSettings(BaseModel):
    extend_llm_to_agents: bool = True
    access_mode: str = "admin_approval"  # admin_approval | public_registered
    # ── Startup: welcome landing page (ui/splash/splash-page) ──
    # Master on/off for the welcome landing page, app-wide. When True, app/main.py
    # serves the crawlable landing page at the front door (/) to new visitors and
    # routes the app to /app; when False, / serves the app directly and the
    # per-device "show welcome screen" preference is moot. Served to every visitor
    # via the public /api/v1/auth/ui-config endpoint.
    splash_enabled: bool = False
    # ── Startup: in-app tour / hint bubbles (ui/tutorials) ──
    # Master on/off for the numbered hint popovers, app-wide. When False the tour
    # never renders for anyone, and the per-user "show app tour" preference is
    # moot. Served via the public /api/v1/auth/ui-config endpoint; the tutorial
    # module reads it (cached, reconciled) and the account page hides its toggle.
    hints_enabled: bool = False
    # ── Appearance: allow per-user theme overrides ──
    # When True, every signed-in user gets a "My appearance" editor on their
    # account page (ui/shared/js/account.js) that writes a sparse theme override
    # to their profile (user_profiles.appearance). /api/v1/auth/ui-config layers
    # that override on top of the global appearance for the signed-in caller, so
    # anything they don't customise still follows the global theme. When False,
    # the editor is hidden and any saved per-user overrides are ignored. Served
    # via the public /api/v1/auth/ui-config endpoint so the account page can gate
    # its editor on it.
    allow_user_appearance: bool = False
    # Seconds to keep a completed turn's in-memory RunBuffer around for
    # WS-replay on reconnect. 0 = drop immediately. Default 60s gives a
    # smooth refresh-after-completion UX without holding RAM long.
    stream_buffer_retention_seconds: int = 60
    # ── Self-healing / auto-resume (app/agent/runner.py + app/agent/watchdog.py) ──
    # The liveness watchdog re-ignites runs that stopped involuntarily (server
    # restart, vanished task, frozen await) from durable history, fully backend-
    # driven (no user WebSocket). These tune detection + retry behavior.
    run_watchdog_enabled: bool = True              # master on/off for the watchdog
    run_watchdog_poll_seconds: int = 30            # how often the watchdog sweeps active runs
    # live task + no liveness heartbeat for this long ⇒ frozen. The loop beats at
    # each turn boundary and during token streams, so a healthy run beats often;
    # keep this ABOVE the loop's wall-clock cap (AGENT_MAX_WALL_SECONDS, 300s) so a
    # single legitimately-long turn is never mistaken for a hang.
    run_frozen_threshold_seconds: int = 360
    run_zombie_grace_seconds: int = 45             # row 'running' + no live task this long ⇒ zombie
    run_max_resume_attempts: int = 3               # per-run resume budget before giving up (failed)
    run_resume_backoff_seconds: int = 30           # base for exponential resume backoff
    # User feedback → GitHub issues via the WebAgent relay. Cloners can flip
    # `feedback_enabled` off to hide the form, or point `feedback_relay_url`
    # at their own relay deployment.
    feedback_enabled: bool = True
    feedback_relay_url: str = ""  # empty → use built-in default
    turnstile_site_key: str = ""  # public Cloudflare Turnstile site key
    # ── Agent-loop tunables ──
    # Maximum number of tool calls per turn. 0 = unlimited. Capped at launch by
    # each provider's max_tokens and the wall-clock cap (AGENT_MAX_WALL_SECONDS).
    max_tool_calls: int = 25
    # Maximum wall-clock seconds for a single agent turn (backstop against
    # tool-looping; the streaming chat path has no request timeout of its own).
    # 0 = unlimited. Overridable per-agent via the agent's max_wall_seconds field.
    max_wall_seconds: int = 600
    # Maximum consecutive identical tool calls before the loop halts (stall guard).
    # 0 = disabled. Overridable per-agent via max_identical_tool_calls.
    max_identical_tool_calls: int = 0
    # ── Client render recorder (app/agent/render_recorder.py + ui/js/recorder.js) ──
    # Master on/off for the browser-side flight recorder (HTML snapshots, lag,
    # JS errors, console + failed network calls), correlated to interactions /
    # diagnostics by session_seq. OFF by default — an investigation tool, flipped
    # on while diagnosing a UI/lag problem and back off when done. Finer capture
    # knobs (intervals, thresholds, per-signal toggles) live as raw
    # render_recording_* keys in app-settings.json (see render_recorder.py).
    render_recording_enabled: bool = False
    # ── App-global system prompt (app/agent/prompts.py) ──
    # A single admin-only block of CRITICAL, non-negotiable instructions injected
    # as the TOPMOST section of EVERY agent's system prompt (fleet agents and
    # spawned clones alike). This is the platform baseline — the bare minimum
    # every agent must obey regardless of its own prompt. Clones are built "from
    # scratch" on top of this (global baseline + their custom directive only), so
    # this is the only inherited identity a fire-and-forget clone carries. Keep it
    # short and rule-like; it is prepended verbatim, ahead of any agent slot.
    global_system_prompt: str = ""
    # ── Appearance: animated background (app/ui_backgrounds + ui/background/) ──
    # Which animated background renders, chosen separately per theme. Values are
    # background ids from the catalog (GET /admin/settings/backgrounds) or the
    # built-in "none" (plain themed background). Applied app-wide for every
    # visitor through the public /api/v1/auth/ui-config endpoint, so the admin's
    # pick is what everyone sees. Dark defaults to the classic starfield; light
    # to the mouse-reactive bullet grid.
    background_dark: str = "stargaze"
    background_light: str = "bullet-grid"
    # ── Appearance: global border + typography (ui/shared/js/appearance.js) ──
    # The single edit-here surface for the app's neutral border and fonts.
    # Served app-wide through the public /api/v1/auth/ui-config endpoint and
    # injected at boot as CSS-variable overrides on top of design-system.css, so
    # editing these values (then reloading) recolours / resizes every container,
    # table and divider border and swaps the UI font for every visitor — no
    # server restart, no CSS edit.
    #   border_color_*  — the neutral border hue, separate per theme (the dark
    #                     and light palettes use different border colours).
    #   border_width    — line weight shared by every neutral border; "0" turns
    #                     them ALL off (accent/status/focus borders are untouched).
    #   font_sans/_mono — the CSS font-family stacks for body text / monospace.
    # A blank value falls back to the design-system.css default for that token.
    border_color_dark: str = "#3a2c1e"
    border_color_light: str = "#cfe6f2"
    border_width: str = "1px"
    font_sans: str = "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif"
    font_mono: str = "'JetBrains Mono', 'Fira Code', Menlo, ui-monospace, monospace"
    # ── Appearance: full theme palette (ui/shared/js/appearance.js) ──────────
    # The raw palette hues + neutral surfaces + text shades, chosen separately
    # per theme, mirroring the two palette blocks in design-system.css (:root =
    # dark, body.light-mode = light). Each is a #rrggbb value; appearance.js
    # injects it as the matching CSS variable under that theme's selector and
    # also derives the `-rgb` triple (so every rgba(var(--x-rgb), a) tint
    # follows). A blank value falls back to the design-system.css default.
    # The Appearance panel (App Settings) edits these as per-theme swatches +
    # one-click presets; they can also be hand-edited here. Keys come in
    # _dark / _light pairs:
    #   accent        → --brand (+ --brand-strong, --brand-rgb)
    #   secondary     → --purple (+ --purple-strong, --purple-rgb)
    #   success/warning/danger → the status hues (+ their -rgb)
    #   surface_bg    → --bg-0   (the page base surface)
    #   surface_panel → --bg-elev (cards / panels)
    #   surface_tint  → --bg-tint (tinted insets)
    #   text          → --fg-1   (primary text)
    #   text_muted    → --fg-3   (muted text)
    #   ambient       → --ambient-2-rgb (the page-background glow, stored as hex)
    accent_dark: str = "#e0a35e"
    accent_light: str = "#1f8fbf"
    secondary_dark: str = "#c8915a"
    secondary_light: str = "#3b6ea5"
    success_dark: str = "#9ece6a"
    success_light: str = "#5a8a4a"
    warning_dark: str = "#e0af68"
    warning_light: str = "#d4873a"
    danger_dark: str = "#f7768e"
    danger_light: str = "#d44848"
    surface_bg_dark: str = "#16100b"
    surface_bg_light: str = "#f5fafd"
    surface_panel_dark: str = "#241a12"
    surface_panel_light: str = "#e9f3fa"
    surface_tint_dark: str = "#36281c"
    surface_tint_light: str = "#dbeaf5"
    text_dark: str = "#ece0d2"
    text_light: str = "#163040"
    text_muted_dark: str = "#9a8266"
    text_muted_light: str = "#5a7589"
    ambient_dark: str = "#7a5636"
    ambient_light: str = "#9fd6ec"
    # Chat bubbles — optional per-theme overrides for the user / agent message
    # bubble fill. Blank = follow the theme-derived default in design-system.css.
    # Stored as #rrggbb or #rrggbbaa (trailing aa = opacity; 00 = transparent).
    user_bubble_dark: str = ""
    user_bubble_light: str = ""
    agent_bubble_dark: str = ""
    agent_bubble_light: str = ""
    # Chat composer pill fill — same shape as the bubbles. Blank = follow the
    # theme-derived default (the shared panel tint, so it matches the bubbles).
    chat_pill_bg_dark: str = ""
    chat_pill_bg_light: str = ""
    # ── Appearance: theme-independent style knobs (JSON-only; no UI control) ──
    # Edit these in app-settings.json (or via a POST) and reload — ui/shared/js/
    # appearance.js injects them app-wide. Each default is "no change" so they're
    # inert until edited. Stored as strings (the whole appearance pipeline is
    # string-valued). Shared across both themes.
    #   radius_scale    — corner roundness multiplier (0 = square … 2 = round)
    #   shadow_strength — drop-shadow depth multiplier (0 = flat … 2 = heavy)
    #   ui_scale        — overall UI zoom (1 = normal; e.g. 1.1 = 110%)
    #   reduce_motion   — "on" collapses animations/transitions (accessibility)
    #   cursor_glow     — "off" hides the pointer glow (ui/shared/js/cursor-effects.js)
    # ── TALL chat-composer (#chat-input-row) geometry + button knobs. Each is a
    #    CSS length injected app-wide by appearance.js as the matching
    #    --chat-pill-* variable (see app1.css CHAT-PILL-VARS); the compact 1-line
    #    bars keep their own fixed sizing.
    #   chat_pill_max_width   — composer width cap (e.g. "500px")
    #   chat_pill_radius      — corner roundness
    #   chat_pill_padding     — outer padding (CSS shorthand)
    #   chat_pill_min_height  — resting text-area height
    #   chat_pill_max_height  — grow cap before scrolling
    #   chat_pill_font_size   — input + placeholder text size
    #   chat_pill_attach_size — attach (+) button box
    #   chat_pill_attach_icon — attach (+) glyph
    #   chat_pill_button_size — mic / send button box
    #   chat_pill_button_icon — mic / send glyph
    radius_scale: str = "1"
    shadow_strength: str = "1"
    ui_scale: str = "1"
    reduce_motion: str = "off"
    cursor_glow: str = "on"
    # ── RAM speed layer (ui/shared/js/media-cache.js) — app-wide client knob ──
    #   media_cache_enabled   — "on" (default) | "off": the in-memory
    #                           attachment/thumbnail cache in the browser.
    #   media_cache_budget_mb — memory ceiling (MB) for cached bytes/thumbnails;
    #                           it LRU-evicts above this, so it can't exceed it.
    media_cache_enabled: str = "on"
    media_cache_budget_mb: str = "128"
    chat_pill_max_width: str = "500px"
    chat_pill_radius: str = "20px"
    chat_pill_padding: str = "4px 4px 4px 14px"
    chat_pill_min_height: str = "96px"
    chat_pill_max_height: str = "160px"
    chat_pill_font_size: str = "14px"
    chat_pill_attach_size: str = "38px"
    chat_pill_attach_icon: str = "24px"
    chat_pill_button_size: str = "62px"
    chat_pill_button_icon: str = "44px"
    # ── Chat panel layout (app-wide defaults; ui/shared/js/appearance.js) ──
    #   chat_position         — which side the chat side-panel sits on the desktop
    #                           split: "right" (default) or "left". Injected as a
    #                           #stage flex-direction rule; the resize handle
    #                           detects the side automatically.
    #   chat_default_visible  — whether the chat panel is shown for a FIRST-TIME
    #                           visitor on desktop: "visible" (default) or
    #                           "hidden". Once a visitor toggles the chat their own
    #                           per-browser choice wins; this only seeds the default.
    chat_position: str = "right"
    chat_default_visible: str = "visible"
    # User-saved custom themes — a JSON array string of
    # [{id, name, dark:{<token>:val,…}, light:{…}}], managed by the Appearance
    # panel's "Add theme" button. Admin-only editor; not served via ui-config
    # (visitors only need the resolved palette, not the preset list).
    custom_themes: str = "[]"


# The two canonical access levels surfaced in User Management:
#   admin_approval   — Private: register, then wait for an admin to approve
#   public_registered — Open Registration: anyone joins, must sign in
VALID_ACCESS_MODES = {"admin_approval", "public_registered"}

# Legacy stored values are migrated on read/write so old configs keep working:
#   open             → public_registered (the no-sign-in auto-admin mode was
#                       retired; that convenience is now covered by "Remember me".
#                       Dormant `== "open"` branches survive but never match.)
#   public_anonymous → public_registered (the old anonymous-chat mode)
#   private          → admin_approval (fully-disabled registration was removed)
_LEGACY_ACCESS_MODES = {
    "open": "public_registered",
    "public_anonymous": "public_registered",
    "private": "admin_approval",
}


def normalize_access_mode(raw: str | None) -> str:
    """Map any stored/posted access_mode to one of the two canonical modes.

    Unknown or empty values fall back to the default (admin_approval), so a
    corrupted setting can never leave the app in an undefined access state.
    """
    val = (raw or "").strip()
    val = _LEGACY_ACCESS_MODES.get(val, val)
    return val if val in VALID_ACCESS_MODES else "admin_approval"

_DEFAULT_BACKGROUNDS = {"dark": "stargaze", "light": "bullet-grid"}


def get_background_config() -> dict:
    """Read the per-theme background choice, validated against what's installed.

    Returns ``{"dark": <id>, "light": <id>}``. A stored id whose folder was
    deleted falls back to the default (or "none" if even that is gone), so a
    removed background never leaves the page pointing at a missing plugin.
    """
    data = _load_app_settings()
    try:
        from app.ui_backgrounds import valid_ids
        allowed = valid_ids() | {"none"}
    except Exception:  # noqa: BLE001
        allowed = None

    def pick(key: str, default: str) -> str:
        val = data.get(key) or default
        if allowed is not None and val not in allowed:
            val = default if default in allowed else "none"
        return val

    return {
        "dark": pick("background_dark", _DEFAULT_BACKGROUNDS["dark"]),
        "light": pick("background_light", _DEFAULT_BACKGROUNDS["light"]),
    }


# Defaults mirror the design-system.css palette (dark :root + body.light-mode).
# These are the values shipped in design-system.css; the appearance config
# overrides them app-wide when the admin edits app-settings.json.
_DEFAULT_APPEARANCE = {
    "border_color_dark": "#3a2c1e",
    "border_color_light": "#cfe6f2",
    "border_width": "1px",
    "font_sans": "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
    "font_mono": "'JetBrains Mono', 'Fira Code', Menlo, ui-monospace, monospace",
    # Full theme palette (per theme). Mirror of the design-system.css palette
    # blocks; kept in sync with the AppSettings fields + ui/shared/js/appearance.js
    # DEFAULTS. get_appearance_config() iterates this dict, so adding a key here
    # automatically carries it through the public /api/v1/auth/ui-config endpoint.
    "accent_dark": "#e0a35e",
    "accent_light": "#1f8fbf",
    "secondary_dark": "#c8915a",
    "secondary_light": "#3b6ea5",
    "success_dark": "#9ece6a",
    "success_light": "#5a8a4a",
    "warning_dark": "#e0af68",
    "warning_light": "#d4873a",
    "danger_dark": "#f7768e",
    "danger_light": "#d44848",
    "surface_bg_dark": "#16100b",
    "surface_bg_light": "#f5fafd",
    "surface_panel_dark": "#241a12",
    "surface_panel_light": "#e9f3fa",
    "surface_tint_dark": "#36281c",
    "surface_tint_light": "#dbeaf5",
    "text_dark": "#ece0d2",
    "text_light": "#163040",
    "text_muted_dark": "#9a8266",
    "text_muted_light": "#5a7589",
    "ambient_dark": "#7a5636",
    "ambient_light": "#9fd6ec",
    # Chat bubble overrides (blank = follow the theme default). #rrggbb[aa].
    "user_bubble_dark": "",
    "user_bubble_light": "",
    "agent_bubble_dark": "",
    "agent_bubble_light": "",
    # Chat composer pill fill (blank = follow the theme default; #rrggbb[aa]).
    "chat_pill_bg_dark": "",
    "chat_pill_bg_light": "",
    # Theme-independent style knobs (JSON-only; see the AppSettings fields).
    "radius_scale": "1",
    "shadow_strength": "1",
    "ui_scale": "1",
    "reduce_motion": "off",
    "cursor_glow": "on",
    # RAM speed layer (media-cache.js) — see the AppSettings fields.
    "media_cache_enabled": "on",
    "media_cache_budget_mb": "128",
    # Chat-composer geometry + button knobs (see the AppSettings fields).
    "chat_pill_max_width": "500px",
    "chat_pill_radius": "20px",
    "chat_pill_padding": "4px 4px 4px 14px",
    "chat_pill_min_height": "96px",
    "chat_pill_max_height": "160px",
    "chat_pill_font_size": "14px",
    "chat_pill_attach_size": "38px",
    "chat_pill_attach_icon": "24px",
    "chat_pill_button_size": "62px",
    "chat_pill_button_icon": "44px",
    # Chat panel layout (see the AppSettings fields). Served via ui-config and
    # applied app-wide by ui/shared/js/appearance.js.
    "chat_position": "right",
    "chat_default_visible": "visible",
}


def get_appearance_config() -> dict:
    """Read the global border + font knobs from app-settings.json, falling back
    to the design-system.css defaults for any missing/blank value.

    Returns ``{border_color_dark, border_color_light, border_width, font_sans,
    font_mono}`` — every value concrete (never blank), so the public
    ``/api/v1/auth/ui-config`` endpoint always carries a usable token and the
    boot-time applier (ui/shared/js/appearance.js) can inject it directly. This
    is the server side of the "edit the JSON, reload, the whole app follows"
    flow; the file is re-read on every call, so no server restart is needed."""
    data = _load_app_settings()
    out = {}
    for key, default in _DEFAULT_APPEARANCE.items():
        val = data.get(key)
        out[key] = val.strip() if isinstance(val, str) and val.strip() else default
    return out


def get_splash_enabled() -> bool:
    """Master on/off for the welcome landing page (app-wide), read live from
    app-settings.json. When True, app/main.py serves the landing at the front door
    (/); served via /api/v1/auth/ui-config too. Ships OFF: defaults to False when
    unset/blank so a fresh install opens straight into the app (no landing page)."""
    return _load_app_settings().get("splash_enabled", False) is True


def get_hints_enabled() -> bool:
    """Master on/off for the in-app tour / hint bubbles (app-wide), read live from
    app-settings.json. Served via /api/v1/auth/ui-config so the tutorial module
    can gate on it. Ships OFF: defaults to False (hidden) when unset/blank."""
    return _load_app_settings().get("hints_enabled", False) is True


def get_allow_user_appearance() -> bool:
    """Whether signed-in users may set their own theme (per-user appearance),
    read live from app-settings.json. Served via /api/v1/auth/ui-config so the
    account page can show/hide its "My appearance" editor and the same endpoint
    knows whether to layer a caller's saved overrides on top of the global
    theme. Defaults to False (off) when unset/blank."""
    return _load_app_settings().get("allow_user_appearance", False) is True


def valid_background(val: str | None, fallback: str) -> str:
    """Return ``val`` if it names an installed background (or "none"), else
    ``fallback``. Used to sanitise a per-user background choice the same way
    get_background_config validates the global one, so a stale/removed id can
    never leave the page pointing at a missing plugin."""
    v = (val or "").strip()
    try:
        from app.ui_backgrounds import valid_ids
        allowed = valid_ids() | {"none"}
    except Exception:  # noqa: BLE001
        return v or fallback
    if v and v in allowed:
        return v
    return fallback if fallback in allowed else "none"


def get_access_mode() -> str:
    """Read just the access_mode flag from app-settings.json.

    Legacy stored values (public_anonymous, private) are normalized to the
    current three-mode vocabulary so an upgraded config keeps working.
    """
    return normalize_access_mode(_load_app_settings().get("access_mode"))


def get_global_system_prompt() -> str:
    """Read the app-global system prompt (the platform baseline injected at the
    top of every agent's prompt). Sync — called from build_system_prompt each
    turn. Returns '' when unset, so callers can prepend unconditionally."""
    try:
        return (str(_load_app_settings().get("global_system_prompt") or "")).strip()
    except Exception:  # noqa: BLE001
        return ""


def get_self_heal_config() -> dict:
    """Read the self-healing / auto-resume tunables from app-settings.json
    (sync — used by the watchdog each tick and by the runner for defaults).
    Falls back to the AppSettings defaults for any missing/invalid value."""
    s = AppSettings(**_load_app_settings())
    # data/config/debug-config.json can force the watchdog on/off, overriding the
    # App Settings value.
    _watchdog_enabled = bool(s.run_watchdog_enabled)
    try:
        from app.admin.debug_config import debug_overrides
        _ov = debug_overrides()
        if "run_watchdog_enabled" in _ov:
            _watchdog_enabled = bool(_ov["run_watchdog_enabled"])
    except Exception:
        pass
    return {
        "watchdog_enabled": _watchdog_enabled,
        "poll_seconds": max(5, int(s.run_watchdog_poll_seconds)),
        "frozen_threshold_seconds": max(15, int(s.run_frozen_threshold_seconds)),
        "zombie_grace_seconds": max(10, int(s.run_zombie_grace_seconds)),
        "max_resume_attempts": max(0, int(s.run_max_resume_attempts)),
        "backoff_seconds": max(1, int(s.run_resume_backoff_seconds)),
    }


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/providers")
async def get_providers():
    """Return known provider presets (id → name + base_url)."""
    return PROVIDER_PRESETS


@router.get("/provider", response_model=ProviderConfig)
async def get_provider(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Get current provider configuration for the requesting user.
    Reads from the auth_elements table in the DB (per-user), falls back to provider.json.
    API key is returned as plaintext.
    """
    user_id = _resolve_user_id(authorization or "", token or "")

    # Resolve own config → admin fallback → provider.json (shared resolver).
    config = await _resolve_user_config(user_id)

    # Ensure providers dict exists
    if "providers" not in config:
        config["providers"] = {}

    # Ensure base_url is present
    if not config.get("base_url"):
        prov = config.get("provider", "")
        if prov in PROVIDER_PRESETS:
            config["base_url"] = PROVIDER_PRESETS[prov]["base_url"]
    return ProviderConfig(**config)


@router.post("/provider", response_model=dict)
async def set_provider(
    config: ProviderConfig,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Set provider configuration for the requesting user.
    API keys are packed into the ENCRYPTED vault secret (auth_elements.secret_ref);
    the DB config blob + provider.json fallback are stored key-stripped. Never
    shared between users.
    """
    user_id = _resolve_user_id(authorization or "", token or "")
    # Resolve the CURRENT config from the vault (keys rehydrated) so a partial
    # save — e.g. changing the model with the key field left blank to "keep" — does
    # not wipe the stored key now that provider.json no longer holds it.
    existing = await _resolve_user_config(user_id)
    existing_providers = existing.get("providers", {})
    request_providers = config.providers or {}

    # Merge incoming providers map over existing
    merged_providers = dict(existing_providers)
    for pk, pv in request_providers.items():
        merged_providers[pk] = pv

    # Ensure current provider's key+model+url live in root too
    current_key = config.api_key or existing.get("api_key", "")
    current_model = config.model or existing.get("model", "")
    current_url = config.base_url or existing.get("base_url", "")

    # Sync the active provider in the map
    merged_providers[config.provider] = {
        "api_key": current_key,
        "model": current_model,
        "base_url": current_url,
    }

    merged = {
        "provider": config.provider,
        "base_url": current_url,
        "api_key": current_key,
        "model": current_model,
        "providers": merged_providers,
        "text_capable": config.text_capable,
        "image_capable": config.image_capable,
        "image_out_capable": config.image_out_capable,
        "use_for_image_out": config.use_for_image_out,
        "high_effort_capable": config.high_effort_capable,
        # Preserve the saved roster — saving the default model must not wipe the
        # Models grid (auth_element_set fully replaces).
        "multi_providers": existing.get("multi_providers", []),
    }

    # Persist through the single secret-safe path: keys packed into the encrypted
    # vault secret, plaintext stores (DB config blob + provider.json) stripped.
    await _persist_llm_config(user_id, merged)

    logger.info("Provider config set for user %s: %s", user_id[:12], config.provider)
    return {"status": "ok", "message": f"Provider set to {config.provider}", "user": user_id}


@router.post("/provider/clear", response_model=dict)
async def clear_provider(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Clear provider configuration for the requesting user."""
    user_id = _resolve_user_id(authorization or "", token or "")
    # Clear the vault (now authoritative) as well as provider.json + env, so a
    # cleared key cannot be resurrected from the vault on the next read.
    await _persist_llm_config(user_id, dict(DEFAULT_PROVIDER))
    logger.info("Provider config cleared for user %s", user_id[:12])
    return {"status": "ok", "message": "Provider settings cleared", "user": user_id}


@router.get("/multi-providers")
async def get_multi_providers(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Get the saved-model roster for the requesting user (the candidate models
    for the chat switcher + vision/image workers). Returns a list of entries.
    """
    user_id = _resolve_user_id(authorization or "", token or "")

    # Resolve own config → admin fallback → provider.json (shared resolver).
    config = await _resolve_user_config(user_id)

    return {
        "providers": config.get("multi_providers", []),
    }


@router.post("/multi-providers", response_model=dict)
async def set_multi_providers(
    body: MultiProvidersRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Save the user's saved-model roster (the candidate models for the chat
    switcher + vision/image workers) to both DB auth_elements and provider.json.
    The default brain model is owned separately by POST /provider (the "Default"
    radio) and is NOT changed here unless none is set yet.
    """
    user_id = _resolve_user_id(authorization or "", token or "")
    # Vault-first (keys rehydrated) so the default brain's key + providers map are
    # preserved across a roster-only edit.
    existing = await _resolve_user_config(user_id)

    merged = dict(existing)
    merged["multi_providers"] = [p.model_dump() for p in body.providers]

    # Mirror the first provider up to the root default slots ONLY when no default
    # model is set yet. The explicit "Default" radio (POST /provider) owns the
    # default — re-saving the roster (e.g. a capability toggle) must not clobber it.
    if body.providers and not merged.get("model"):
        first = body.providers[0]
        merged["provider"] = first.provider
        if first.base_url:
            merged["base_url"] = first.base_url
        if first.api_key:
            merged["api_key"] = first.api_key
        if first.model:
            merged["model"] = first.model

    # Persist through the single secret-safe path: every roster key + the default
    # key packed into the encrypted vault secret; plaintext stores stripped.
    await _persist_llm_config(user_id, merged)

    count = len(body.providers)
    logger.info("Saved-model roster saved for user %s: count=%d", user_id[:12], count)
    return {
        "status": "ok",
        "count": count,
        "message": f"Saved-model roster saved. {count} model(s).",
    }


# Model-id substrings that imply image GENERATION. Dedicated image hosts
# (OpenAI images, Stability, Gemini/Imagen, FLUX/SDXL on Together, Fireworks,
# DeepInfra) don't advertise modalities on /models, so we fall back to the id.
_IMAGE_OUT_NAME_HINTS = (
    "dall-e", "dalle", "gpt-image", "flux", "sdxl", "stable-diffusion",
    "stable-image", "sd3", "imagen", "playground", "diffus",
)


def _name_suggests_image_out(model_id: str) -> bool:
    mid = (model_id or "").lower()
    return any(h in mid for h in _IMAGE_OUT_NAME_HINTS)


def _detect_model_modalities(m: dict) -> tuple:
    """Best-effort detection of a model's media capabilities from a provider's
    /models entry. Returns
    ``(text_capable, image_capable, image_out_capable, modality_known)``.

      image_capable      = accepts image INPUT (vision).
      image_out_capable  = can GENERATE images — from the provider's
        ``output_modalities`` when present, plus a model-id name heuristic
        (dedicated image hosts don't report modalities on /models).
      modality_known     = the provider reported structured INPUT modality info.

    Handles OpenRouter's ``architecture.input_modalities`` list and the legacy
    ``modality`` string (e.g. "text+image->text"); text input is assumed for
    every chat model — image in/out are the meaningful signals.
    """
    arch = m.get("architecture") or {}

    # Image OUTPUT — structured output_modalities, else id name heuristic.
    out_mods = arch.get("output_modalities") or m.get("output_modalities")
    img_out = False
    if isinstance(out_mods, list) and out_mods:
        img_out = "image" in [str(x).lower() for x in out_mods]
    if not img_out:
        img_out = _name_suggests_image_out(m.get("id", ""))

    # Image INPUT (vision).
    mods = arch.get("input_modalities") or m.get("input_modalities")
    if isinstance(mods, list) and mods:
        low = [str(x).lower() for x in mods]
        return (True, "image" in low, img_out, True)
    modality = arch.get("modality") or m.get("modality")
    if isinstance(modality, str) and modality:
        inp = modality.split("->")[0].lower()
        return (True, ("image" in inp or "vision" in inp), img_out, True)
    return (True, False, img_out, False)


@router.get("/models")
async def get_models(
    provider: str = "",
    api_key: str = Query("", alias="api_key"),
    base_url: str = Query("", alias="base_url"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Fetch available models from the configured provider's API.
    Uses the requesting user's saved config, or the explicit api_key/base_url
    params passed by the frontend (for per-row model fetching in parallel providers UI).
    """
    user_id = _resolve_user_id(authorization or "", token or "")

    # Explicit params override saved config (used by parallel provider rows)
    if api_key and base_url:
        # Use the explicitly provided key and URL directly
        pass
    else:
        # Fall back to saved provider config
        config = _load_provider(user_id)
        api_key = config.get("api_key", "")
        if not api_key:
            return {"error": "No API key configured", "models": []}

        prov = provider or config.get("provider", "openrouter")
        base_url = config.get("base_url", "")

        # If no saved base_url, get from preset
        if not base_url and prov in PROVIDER_PRESETS:
            base_url = PROVIDER_PRESETS[prov]["base_url"]

    if not base_url:
        return {"error": "No base URL configured for this provider", "models": []}

    # Make sure the metadata catalog (models.dev + OpenRouter) is loaded so we
    # can attach context size / cost / description to each model. Cheap after the
    # first fetch — served from the on-disk cache and only re-fetched when stale.
    try:
        from app import model_catalog
        await model_catalog.ensure_fresh()
    except Exception as e:
        logger.warning("model_catalog enrich unavailable: %s", e)
        model_catalog = None

    # Build models endpoint: <base_url>/models (handle trailing slash)
    models_url = base_url.rstrip("/") + "/models"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            resp = await client.get(models_url, headers=headers)
            if resp.status_code == 401:
                return {"error": "Invalid API key", "models": []}
            resp.raise_for_status()
            data = resp.json()
            models = []
            for m in data.get("data", []):
                tcap, icap, iocap, known = _detect_model_modalities(m)
                entry = {
                    "id": m["id"],
                    "name": m.get("name", m["id"]),
                    "text_capable": tcap,
                    "image_capable": icap,
                    "image_out_capable": iocap,
                    "modality_known": known,
                    # Metadata fields (populated below from the catalog when known).
                    "context": None,
                    "max_output": None,
                    "cost_input": None,
                    "cost_output": None,
                    "description": "",
                }
                if model_catalog is not None:
                    meta = model_catalog.lookup(m["id"], provider_hint=provider or "")
                    if meta:
                        entry["context"] = meta.get("context")
                        entry["max_output"] = meta.get("max_output")
                        entry["cost_input"] = meta.get("cost_input")
                        entry["cost_output"] = meta.get("cost_output")
                        entry["description"] = meta.get("description") or ""
                models.append(entry)
            models.sort(key=lambda x: x["id"])
            return {"error": None, "models": models}
    except httpx.RequestError as e:
        logger.warning(f"Failed to fetch models from {models_url}: %s", e)
        return {"error": f"Network error: {e}", "models": []}
    except Exception as e:
        logger.warning(f"Failed to fetch models from {models_url}: %s", e)
        return {"error": str(e), "models": []}


@router.get("/model-catalog")
async def get_model_catalog_status():
    """Status of the merged model-metadata catalog (models.dev + OpenRouter)."""
    try:
        from app import model_catalog
        return {"error": None, **model_catalog.cache_info()}
    except Exception as e:
        return {"error": str(e), "fetched_at": 0, "count": 0, "stale": True}


@router.post("/model-catalog/refresh")
async def refresh_model_catalog():
    """Force a re-fetch of the model-metadata catalog from both sources."""
    try:
        from app import model_catalog
        await model_catalog.refresh(force=True)
        return {"error": None, **model_catalog.cache_info()}
    except Exception as e:
        logger.warning("model_catalog refresh failed: %s", e)
        return {"error": str(e), "fetched_at": 0, "count": 0, "stale": True}


async def _is_admin(db, user_id: str) -> bool:
    """True when this user may see cross-user (global) usage totals."""
    if not user_id:
        return False
    if user_id == "admin":
        return True
    try:
        prof = await db.get_user_profile(user_id)
        return bool(prof and prof.get("is_admin"))
    except Exception:
        return False


@router.get("/model-usage")
async def get_model_usage(
    model: str = Query(""),
    provider: str = Query(""),
    scope: str = Query("user"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Aggregate real token usage and cost for one model+provider combo.

    scope='user' (default) sums only the current user's calls. scope='global'
    (admin only) sums every agent, every user, and background tasks (git commit
    messages, placeholder/suggestion text, compaction, embeddings) for a true
    app-wide model total. cost_usd is the canonical published-price figure;
    total_cost_cents stays as the secondary provider-billed figure."""
    user_id = _resolve_user_id(authorization or "", token or "")
    if not user_id:
        return {"error": "not_authenticated", "total_input_tokens": 0,
                "total_output_tokens": 0, "total_cost_cents": 0, "total_cost_usd": 0.0}

    try:
        from app.db import get_db
        db = get_db()
        want_global = (scope == "global") and await _is_admin(db, user_id)
        total_in = 0
        total_out = 0
        total_cost_cents = 0
        total_cost_usd = 0.0

        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                # Global sums every provider for this model (the same model can be
                # billed via different provider strings, and background rows carry
                # their own); user-scope keeps the provider filter for precision,
                # but a blank provider means "any provider".
                cols = ("SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                        "COALESCE(SUM(provider_cost_cents),0), COALESCE(SUM(cost_usd),0) "
                        "FROM usage_events WHERE ")
                if want_global:
                    sql = cols + "model=?"
                    params = [model]
                else:
                    sql = cols + "user_id=? AND model=?"
                    params = [user_id, model]
                if provider:
                    sql += " AND provider=?"
                    params.append(provider)
                rows = conn.execute(sql, tuple(params)).fetchall()
                if rows:
                    total_in, total_out = int(rows[0][0]), int(rows[0][1])
                    total_cost_cents = int(rows[0][2])
                    total_cost_usd = float(rows[0][3] or 0)
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            q = db.get_raw_client().table("usage_events") \
                .select("input_tokens, output_tokens, provider_cost_cents, cost_usd") \
                .eq("model", model)
            if provider:
                q = q.eq("provider", provider)
            if not want_global:
                q = q.eq("user_id", user_id)
            resp = q.execute()
            for row in resp.data:
                total_in += row.get("input_tokens", 0)
                total_out += row.get("output_tokens", 0)
                total_cost_cents += row.get("provider_cost_cents", 0)
                total_cost_usd += row.get("cost_usd", 0) or 0

        return {
            "error": None,
            "scope": "global" if want_global else "user",
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_cost_cents": total_cost_cents,
            "total_cost_usd": round(total_cost_usd, 6),
        }
    except Exception as e:
        return {"error": str(e), "total_input_tokens": 0, "total_output_tokens": 0,
                "total_cost_cents": 0, "total_cost_usd": 0.0}


@router.get("/session-cost")
async def get_session_cost(
    session_id: str = Query(""),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Authoritative session cost: sum of the locked-in per-call cost_usd for one
    session, plus a per-model breakdown. Stays correct across model switches
    because each call already carries its own model's price. The chat footer
    reconciles its live running total against this on session load."""
    user_id = _resolve_user_id(authorization or "", token or "")
    if not user_id:
        return {"error": "not_authenticated", "total_cost_usd": 0.0, "by_model": {}}
    if not session_id:
        return {"error": None, "total_cost_usd": 0.0, "by_model": {}}

    try:
        from app.db import get_db
        db = get_db()
        by_model: dict = {}
        total = 0.0

        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                rows = conn.execute(
                    "SELECT model, COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                    "COALESCE(SUM(cost_usd),0) FROM usage_events "
                    "WHERE user_id=? AND session_id=? GROUP BY model",
                    (user_id, session_id),
                ).fetchall()
                for r in rows or []:
                    m = r[0] or ""
                    inp, out, cost = int(r[1] or 0), int(r[2] or 0), float(r[3] or 0)
                    if m:
                        by_model[m] = {"input": inp, "output": out,
                                       "total": inp + out, "cost_usd": round(cost, 6)}
                    total += cost
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            resp = db.get_raw_client().table("usage_events") \
                .select("model, input_tokens, output_tokens, cost_usd") \
                .eq("user_id", user_id).eq("session_id", session_id).execute()
            for row in resp.data or []:
                m = row.get("model") or ""
                inp = row.get("input_tokens", 0) or 0
                out = row.get("output_tokens", 0) or 0
                cost = row.get("cost_usd", 0) or 0
                if m:
                    cur = by_model.setdefault(m, {"input": 0, "output": 0, "total": 0, "cost_usd": 0.0})
                    cur["input"] += inp
                    cur["output"] += out
                    cur["total"] += inp + out
                    cur["cost_usd"] = round(cur["cost_usd"] + cost, 6)
                total += cost

        return {"error": None, "total_cost_usd": round(total, 6), "by_model": by_model}
    except Exception as e:
        return {"error": str(e), "total_cost_usd": 0.0, "by_model": {}}


@router.get("/agent-usage")
async def get_agent_usage(
    agent_id: str = Query(""),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Usage the CURRENT user has accrued with one agent: a grand total
    (tokens + cost_usd) plus a per-model breakdown, scoped to this (user, agent)
    pair only — not the agent's all-users total, not the user's all-agents total.
    Powers the cumulative cost figure in the agent's configuration tab, so each
    viewer sees what they personally spent with this agent."""
    user_id = _resolve_user_id(authorization or "", token or "")
    if not user_id:
        return {"error": "not_authenticated", "total_cost_usd": 0.0,
                "total_input_tokens": 0, "total_output_tokens": 0, "by_model": {}}
    if not agent_id:
        return {"error": None, "total_cost_usd": 0.0,
                "total_input_tokens": 0, "total_output_tokens": 0, "by_model": {}}

    try:
        from app.db import get_db
        db = get_db()
        by_model: dict = {}
        total_cost = 0.0
        total_in = 0
        total_out = 0

        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                rows = conn.execute(
                    "SELECT model, COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                    "COALESCE(SUM(cost_usd),0) FROM usage_events "
                    "WHERE agent_id=? AND user_id=? GROUP BY model",
                    (agent_id, user_id),
                ).fetchall()
                for r in rows or []:
                    m = r[0] or ""
                    inp, out, cost = int(r[1] or 0), int(r[2] or 0), float(r[3] or 0)
                    total_in += inp
                    total_out += out
                    total_cost += cost
                    if m:
                        by_model[m] = {"input": inp, "output": out,
                                       "total": inp + out, "cost_usd": round(cost, 6)}
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            resp = db.get_raw_client().table("usage_events") \
                .select("model, input_tokens, output_tokens, cost_usd") \
                .eq("agent_id", agent_id).eq("user_id", user_id).execute()
            for row in resp.data or []:
                m = row.get("model") or ""
                inp = row.get("input_tokens", 0) or 0
                out = row.get("output_tokens", 0) or 0
                cost = row.get("cost_usd", 0) or 0
                total_in += inp
                total_out += out
                total_cost += cost
                if m:
                    cur = by_model.setdefault(m, {"input": 0, "output": 0, "total": 0, "cost_usd": 0.0})
                    cur["input"] += inp
                    cur["output"] += out
                    cur["total"] += inp + out
                    cur["cost_usd"] = round(cur["cost_usd"] + cost, 6)

        return {
            "error": None,
            "total_cost_usd": round(total_cost, 6),
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "by_model": by_model,
        }
    except Exception as e:
        return {"error": str(e), "total_cost_usd": 0.0,
                "total_input_tokens": 0, "total_output_tokens": 0, "by_model": {}}


@router.post("/agent-usage/reset")
async def reset_agent_usage(
    agent_id: str = Query(""),
    scope: str = Query("all"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Reset the CURRENT user's usage counters for one agent. scope = 'all'
    (delete every row), 'input' / 'output' / 'cost' (zero only that column).
    Scoped to this (user, agent) pair to match the user-scoped figure shown in
    the agent's configuration tab — it never touches other users' usage rows."""
    user_id = _resolve_user_id(authorization or "", token or "")
    if not user_id:
        return {"error": "not_authenticated"}
    if not agent_id:
        return {"error": "agent_id required"}

    try:
        from app.db import get_db
        db = get_db()
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                if scope == "all":
                    conn.execute("DELETE FROM usage_events WHERE agent_id=? AND user_id=?", (agent_id, user_id))
                elif scope == "input":
                    conn.execute("UPDATE usage_events SET input_tokens=0 WHERE agent_id=? AND user_id=?", (agent_id, user_id))
                elif scope == "output":
                    conn.execute("UPDATE usage_events SET output_tokens=0 WHERE agent_id=? AND user_id=?", (agent_id, user_id))
                elif scope == "cost":
                    conn.execute("UPDATE usage_events SET cost_usd=0 WHERE agent_id=? AND user_id=?", (agent_id, user_id))
                else:
                    return {"error": f"invalid scope: {scope}"}
                conn.commit()
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            client = db.get_raw_client()
            if scope == "all":
                client.table("usage_events").delete().eq("agent_id", agent_id).eq("user_id", user_id).execute()
            elif scope == "input":
                client.table("usage_events").update({"input_tokens": 0}).eq("agent_id", agent_id).eq("user_id", user_id).execute()
            elif scope == "output":
                client.table("usage_events").update({"output_tokens": 0}).eq("agent_id", agent_id).eq("user_id", user_id).execute()
            elif scope == "cost":
                client.table("usage_events").update({"cost_usd": 0}).eq("agent_id", agent_id).eq("user_id", user_id).execute()
            else:
                return {"error": f"invalid scope: {scope}"}
        else:
            return {"error": "unsupported database"}
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


@router.get("/session-model-usage")
async def get_session_model_usage(
    session_id: str = Query(""),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Per-model token totals for one session, scoped to the current user.

    usage_events has no session_id column; it links to a session through
    interaction_id -> interactions.session_id, so we join on that. Returns a
    map keyed by model id: {input, output, total}. Used by the footer model
    picker to show each model's session usage next to its name."""
    user_id = _resolve_user_id(authorization or "", token or "")
    if not user_id:
        return {"error": "not_authenticated", "usage": {}}
    if not session_id:
        return {"error": None, "usage": {}}

    try:
        from app.db import get_db
        db = get_db()
        usage: dict = {}

        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                rows = conn.execute(
                    "SELECT ue.model, COALESCE(SUM(ue.input_tokens),0), "
                    "COALESCE(SUM(ue.output_tokens),0), COALESCE(SUM(ue.cost_usd),0) "
                    "FROM usage_events ue "
                    "JOIN interactions i ON ue.interaction_id = i.id "
                    "WHERE ue.user_id=? AND i.session_id=? "
                    "GROUP BY ue.model",
                    (user_id, session_id),
                ).fetchall()
                for r in rows or []:
                    model = r[0] or ""
                    if not model:
                        continue
                    inp, out = int(r[1] or 0), int(r[2] or 0)
                    usage[model] = {"input": inp, "output": out, "total": inp + out,
                                    "cost_usd": round(float(r[3] or 0), 6)}
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            client = db.get_raw_client()
            # Two-step (no server-side join): which interactions belong to the
            # session, then sum usage for those interaction ids.
            irows = client.table("interactions").select("id") \
                .eq("session_id", session_id).execute()
            iids = [row["id"] for row in (irows.data or []) if row.get("id")]
            if iids:
                resp = client.table("usage_events") \
                    .select("model, input_tokens, output_tokens, cost_usd, interaction_id") \
                    .eq("user_id", user_id).in_("interaction_id", iids).execute()
                for row in resp.data or []:
                    model = row.get("model") or ""
                    if not model:
                        continue
                    inp = row.get("input_tokens", 0) or 0
                    out = row.get("output_tokens", 0) or 0
                    cost = row.get("cost_usd", 0) or 0
                    cur = usage.setdefault(model, {"input": 0, "output": 0, "total": 0, "cost_usd": 0.0})
                    cur["input"] += inp
                    cur["output"] += out
                    cur["total"] += inp + out
                    cur["cost_usd"] = round(cur["cost_usd"] + cost, 6)

        return {"error": None, "usage": usage}
    except Exception as e:
        return {"error": str(e), "usage": {}}


@router.get("/model-info")
async def get_model_info(model: str = Query("", alias="model")):
    """Full merged metadata for a single model id (context, cost, description…)."""
    try:
        from app import model_catalog
        await model_catalog.ensure_fresh()
        meta = model_catalog.lookup(model)
        return {"error": None, "found": meta is not None, "info": meta or model_catalog.enrich(model)}
    except Exception as e:
        return {"error": str(e), "found": False, "info": None}


def _claude_model_window(model: str):
    """Best-effort context window (in tokens) for a Claude Code model id.

    The local `claude` CLI isn't served through the app's OpenRouter catalog, so
    we can't look its window up there (and OpenRouter under-reports Anthropic at
    200K anyway). Map by family instead: current Opus/Sonnet run a 1M window;
    Haiku is 200K. Returns None for a blank/unknown id (e.g. "Claude's default"),
    which the footer fills in live once the run reports its real model.
    """
    m = (model or "").lower()
    if not m:
        return None
    if "haiku" in m:
        return 200_000
    if "opus" in m or "sonnet" in m or "fable" in m or "mythos" in m:
        return 1_000_000
    return None


@router.get("/current-model-info")
async def get_current_model_info(
    agent_id: str = Query("", alias="agent_id"),
    session_id: str = Query("", alias="session_id"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Resolve the *active* model for the current chat and return its catalog
    metadata (context window, max output, cost) in a single call. Used by the
    chat footer to show context/max next to the in/out token counters.

    The model is resolved exactly as the agent loop resolves it for a run: the
    user's default with any per-agent LLM override (the agent's custom model)
    and then any per-session override (the session's picked model) layered on
    top — so the footer shows the model the run will actually use, not just the
    global default.
    """
    user_id = _resolve_user_id(authorization or "", token or "")

    agent_rec = None
    if agent_id:
        try:
            from app.db import get_db
            agent_rec = await get_db().get_agent_by_id(agent_id)
        except Exception:
            agent_rec = None

    active = await resolve_active_model(user_id, agent_rec, session_id or None)
    model = active.get("model", "")
    provider = active.get("provider", "")

    # Alternate-engine agents (e.g. Local Claude Code) don't run on the app's own
    # model, so the resolved model/context/cost above describe a model they never
    # use. Surface the engine id so the footer can drop the inapplicable price
    # while keeping the (real) token counters. Mirrors loop.py's engine lookup.
    engine = ""
    cc_meta: dict = {}
    try:
        _eng_meta = agent_rec.get("metadata") if agent_rec else None
        if isinstance(_eng_meta, str):
            import json as _json
            _eng_meta = _json.loads(_eng_meta or "{}")
        if isinstance(_eng_meta, dict):
            engine = str(_eng_meta.get("engine") or "").strip()
            _cc = _eng_meta.get("claude_code")
            cc_meta = _cc if isinstance(_cc, dict) else {}
    except Exception:
        engine = ""

    # Local Claude Code: the footer must describe the model the *local* claude
    # program runs, not the app's default LLM. Use the agent's configured model
    # (blank ⇒ "Claude's default", which the engine fills in live from the model
    # the run actually reports), and the real Claude context window for it — never
    # the app default's price (which doesn't apply here). See _claude_model_window.
    if engine == "claude_code":
        cc_model = str(cc_meta.get("model") or "").strip()
        return {
            "error": None, "model": cc_model, "provider": "anthropic",
            "engine": engine, "found": bool(cc_model),
            "context": _claude_model_window(cc_model), "max_output": None,
            "cost_input": None, "cost_output": None,
            "reasoning_effort": "default", "reasoning": None,
        }

    result = {
        "error": None, "model": model, "provider": provider, "found": False,
        "engine": engine,
        "context": None, "max_output": None, "cost_input": None, "cost_output": None,
        # The reasoning-effort level this chat is running the active model at, and
        # whether the model supports reasoning at all (gates the footer effort UI).
        "reasoning_effort": active.get("reasoning_effort", "default"), "reasoning": None,
    }
    if not model:
        return result
    try:
        from app import model_catalog
        await model_catalog.ensure_fresh()
        meta = model_catalog.lookup(model, provider_hint=provider)
        if meta:
            result.update({
                "found": True,
                "context": meta.get("context"),
                "max_output": meta.get("max_output"),
                "cost_input": meta.get("cost_input"),
                "cost_output": meta.get("cost_output"),
                "reasoning": meta.get("reasoning"),
            })
    except Exception as e:
        result["error"] = str(e)
    return result


@router.get("/agent-models")
async def get_agent_models(
    agent_id: str = Query("", alias="agent_id"),
    session_id: str = Query("", alias="session_id"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """List the Text-capable models a chat user can switch between for this agent
    (app default(s) + the agent's own roster — the exact union the run considers),
    plus the model currently active for this chat. Used by the chat footer model
    switcher so it shows ALL of the agent's models, not just the admin defaults.
    """
    user_id = _resolve_user_id(authorization or "", token or "")
    agent_rec = None
    if agent_id:
        try:
            from app.db import get_db
            agent_rec = await get_db().get_agent_by_id(agent_id)
        except Exception:
            agent_rec = None
    try:
        return await resolve_agent_models(user_id, agent_rec, session_id or None)
    except Exception as e:
        logger.warning("get_agent_models failed: %s", e)
        return {"models": [], "active": "", "error": str(e)}


@router.get("/metadata", response_model=MetadataSetting)
async def get_metadata_setting():
    return MetadataSetting(enabled=_is_metadata_enabled())


@router.post("/metadata", response_model=MetadataSetting)
async def set_metadata_setting(setting: MetadataSetting):
    if setting.enabled:
        METADATA_FLAG.touch()
    else:
        METADATA_FLAG.unlink() if METADATA_FLAG.exists() else None
    return MetadataSetting(enabled=_is_metadata_enabled())


@router.get("/backgrounds")
async def list_backgrounds():
    """Drop-in animated-background catalog for the Appearance selector.

    Scans ui/background/<id>/<id>.json fresh each call so a folder dropped in
    (or removed) shows up without a server restart. The built-in "none" option
    is added by the selector UI, not listed here."""
    from app.ui_backgrounds import catalog as _bg_catalog, reload as _bg_reload
    _bg_reload()
    return {"backgrounds": _bg_catalog()}


@router.get("/app", response_model=AppSettings)
async def get_app_settings():
    """Return app-wide feature flags."""
    return AppSettings(**_load_app_settings())


@router.post("/app", response_model=AppSettings)
async def set_app_settings(request: Request):
    """Save app-wide feature flags.

    MERGES the posted fields onto the existing settings rather than replacing the
    whole object, so a form that submits just one or two flags can't silently reset
    every other setting (e.g. the global_system_prompt) back to its default. Unknown
    raw keys already in the file (e.g. render_recording_* capture knobs) are
    preserved too."""
    # Admin-only. These are app-WIDE controls (access mode, global system
    # prompt, watchdog tunables, …) so a non-admin must never be able to flip
    # them. There is no global auth middleware, so the gate lives here: resolve
    # the caller from the JWT and require is_admin. The matching GET stays open
    # because non-admin clients read these flags at boot.
    from app.db import get_db
    caller_id = _resolve_user_id(
        request.headers.get("Authorization", "") or "",
        request.query_params.get("token", "") or "",
    )
    if not await get_db().is_user_admin(caller_id):
        raise HTTPException(status_code=403, detail="Admin access required")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    existing = _load_app_settings()
    settings = AppSettings(**{**existing, **body})
    settings.access_mode = normalize_access_mode(settings.access_mode)
    # Clamp stream buffer retention to a sane range so a bad value can't
    # exhaust RAM (huge) or break replay-after-reconnect entirely (negative).
    try:
        sb = int(settings.stream_buffer_retention_seconds)
    except (TypeError, ValueError):
        sb = 60
    if sb < 0:
        sb = 0
    if sb > 3600:
        sb = 3600
    settings.stream_buffer_retention_seconds = sb
    # Preserve any unknown raw keys already in the file while writing the merged
    # validated settings on top.
    _save_app_settings({**existing, **settings.model_dump()})
    return settings
