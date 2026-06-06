"""Model-metadata catalog — context window, cost, description & capabilities.

A lightweight, self-contained library that merges two public, keyless data
sources into a single per-model metadata lookup:

  * models.dev  (https://models.dev/api.json) — the BACKBONE. Broad multi-provider
    coverage (OpenAI, Anthropic, Google/Gemini, Mistral, xAI, Meta, …) with the
    hard numbers: context window, max output, input/output/cache cost, modalities,
    and capability flags (tool_call, reasoning, attachment).
  * OpenRouter  (https://openrouter.ai/api/v1/models) — DESCRIPTIONS + extra
    coverage. Adds a written blurb per model and fills any gaps in context/price.

The merged result is keyed by model id so the model picker (and anything else)
can ask "what's the context size / cost / description of <model>?" without caring
which provider serves it.

Design notes
------------
* No API key is needed for either source (both list endpoints are public).
* Results are cached to data/config/model_catalog.json with a fetch timestamp.
  We serve from cache instantly and only re-fetch when the cache is stale
  (> CACHE_TTL_SECONDS) or a caller forces it. If the network is down we keep
  serving the last good cache — the picker still shows metadata offline.
* Matching is forgiving: every entry is indexed by both its full id
  ("openai/gpt-5") and its bare id ("gpt-5"), so a provider that returns short
  ids still resolves. enrich() tries the exact id first, then the bare tail.
* This module is intentionally NOT in app/admin/ — model metadata should keep
  working even if the admin package is removed.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# app/model_catalog.py -> app/ -> repo root -> data/config/model_catalog.json
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = PROJECT_ROOT / "data" / "config" / "model_catalog.json"

MODELSDEV_URL = "https://models.dev/api.json"
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"

CACHE_TTL_SECONDS = 24 * 60 * 60  # re-fetch sources at most once a day
FETCH_TIMEOUT = 20.0

# In-process cache so repeated lookups in one run don't re-read the file.
_catalog: Optional[Dict[str, Any]] = None  # {"fetched_at": float, "models": {...}, "index": {...}}


# ── Normalisation helpers ───────────────────────────────────────────────────

def _bare(model_id: str) -> str:
    """Last path segment of an id, lowercased ('openai/gpt-5' -> 'gpt-5')."""
    if not model_id:
        return ""
    return model_id.rsplit("/", 1)[-1].strip().lower()


def _empty_entry(model_id: str) -> Dict[str, Any]:
    return {
        "id": model_id,
        "name": model_id,
        "description": "",
        "context": None,        # max input tokens
        "max_output": None,     # max output tokens
        "cost_input": None,     # USD per 1M input tokens
        "cost_output": None,    # USD per 1M output tokens
        "cost_cache_read": None,
        "cost_cache_write": None,
        "input_modalities": [],
        "output_modalities": [],
        "tool_call": None,
        "reasoning": None,
        "attachment": None,
        "family": "",
        "release_date": "",
        "provider": "",
        "sources": [],
    }


def _from_modelsdev(provider_id: str, model_id: str, m: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise one models.dev model record into our shape."""
    full_id = f"{provider_id}/{model_id}" if provider_id else model_id
    e = _empty_entry(full_id)
    limit = m.get("limit") or {}
    cost = m.get("cost") or {}
    modalities = m.get("modalities") or {}
    e.update({
        "name": m.get("name") or model_id,
        "context": limit.get("context"),
        "max_output": limit.get("output"),
        "cost_input": cost.get("input"),
        "cost_output": cost.get("output"),
        "cost_cache_read": cost.get("cache_read"),
        "cost_cache_write": cost.get("cache_write"),
        "input_modalities": modalities.get("input") or [],
        "output_modalities": modalities.get("output") or [],
        "tool_call": m.get("tool_call"),
        "reasoning": m.get("reasoning"),
        "attachment": m.get("attachment"),
        "family": m.get("family") or "",
        "release_date": m.get("release_date") or "",
        "provider": provider_id,
        "sources": ["models.dev"],
    })
    return e


def _or_cost_per_million(raw: Any) -> Optional[float]:
    """OpenRouter prices are USD per *token* as strings; convert to per-1M."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v * 1_000_000, 6)


def _from_openrouter(m: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise one OpenRouter model record into our shape."""
    e = _empty_entry(m.get("id", ""))
    pricing = m.get("pricing") or {}
    arch = m.get("architecture") or {}
    e.update({
        "name": m.get("name") or m.get("id", ""),
        "description": m.get("description") or "",
        "context": m.get("context_length"),
        "cost_input": _or_cost_per_million(pricing.get("prompt")),
        "cost_output": _or_cost_per_million(pricing.get("completion")),
        "input_modalities": arch.get("input_modalities") or [],
        "output_modalities": arch.get("output_modalities") or [],
        "sources": ["openrouter"],
    })
    return e


def _merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """Fill empty fields of `base` from `extra`; description always prefers the
    one that is non-empty. Numeric facts keep `base` (models.dev) when present."""
    out = dict(base)
    for k, v in extra.items():
        if k == "sources":
            out["sources"] = sorted(set(out.get("sources", []) + v))
            continue
        cur = out.get(k)
        is_empty = cur is None or cur == "" or cur == []
        if is_empty and not (v is None or v == "" or v == []):
            out[k] = v
    # Description: prefer whichever source actually has prose.
    if not base.get("description") and extra.get("description"):
        out["description"] = extra["description"]
    return out


# ── Build / fetch ───────────────────────────────────────────────────────────

async def _fetch_json(client: httpx.AsyncClient, url: str) -> Any:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


def _index(models: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Map full-id and bare-id -> canonical key in `models` for fast lookup.
    Full ids win over bare on collision (bare ids are ambiguous across providers)."""
    idx: Dict[str, str] = {}
    # First pass: bare ids (may be overwritten by a later provider — acceptable).
    for key, e in models.items():
        bare = _bare(e.get("id") or key)
        if bare:
            idx.setdefault(bare, key)
    # Second pass: exact ids always take precedence.
    for key, e in models.items():
        idx[(e.get("id") or key).strip().lower()] = key
    return idx


async def refresh(force: bool = False) -> Dict[str, Any]:
    """Fetch both sources, merge, cache to disk, and return the catalog.

    On any fetch failure we fall back to (and keep) the last good cache so the
    picker keeps showing metadata. `force` bypasses the staleness check.
    """
    global _catalog
    cached = _load_cache()
    if not force and cached and (time.time() - cached.get("fetched_at", 0)) < CACHE_TTL_SECONDS:
        _catalog = cached
        return cached

    models: Dict[str, Dict[str, Any]] = {}
    fetched_any = False

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            # models.dev — backbone
            try:
                data = await _fetch_json(client, MODELSDEV_URL)
                for provider_id, pdata in (data or {}).items():
                    for model_id, m in ((pdata or {}).get("models") or {}).items():
                        entry = _from_modelsdev(provider_id, model_id, m)
                        models[entry["id"]] = entry
                fetched_any = True
            except Exception as e:
                logger.warning("model_catalog: models.dev fetch failed: %s", e)

            # OpenRouter — descriptions + extra coverage
            try:
                data = await _fetch_json(client, OPENROUTER_URL)
                for m in (data or {}).get("data", []):
                    entry = _from_openrouter(m)
                    if not entry["id"]:
                        continue
                    # Try to merge onto a models.dev entry that shares the bare id.
                    target_key = None
                    if entry["id"] in models:
                        target_key = entry["id"]
                    else:
                        bare = _bare(entry["id"])
                        for key, ex in models.items():
                            if _bare(ex["id"]) == bare:
                                target_key = key
                                break
                    if target_key:
                        models[target_key] = _merge(models[target_key], entry)
                    else:
                        models[entry["id"]] = entry
                fetched_any = True
            except Exception as e:
                logger.warning("model_catalog: OpenRouter fetch failed: %s", e)
    except Exception as e:
        logger.warning("model_catalog: HTTP client error: %s", e)

    if not fetched_any:
        # Both sources failed — keep serving whatever we had before.
        if cached:
            _catalog = cached
            return cached
        empty = {"fetched_at": 0, "models": {}, "index": {}}
        _catalog = empty
        return empty

    catalog = {
        "fetched_at": time.time(),
        "models": models,
        "index": _index(models),
    }
    _save_cache(catalog)
    _catalog = catalog
    return catalog


# ── Cache file I/O ──────────────────────────────────────────────────────────

def _load_cache() -> Optional[Dict[str, Any]]:
    global _catalog
    if _catalog is not None:
        return _catalog
    try:
        if CACHE_FILE.is_file():
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            # Rebuild the index if an older cache lacks one.
            if "index" not in data and "models" in data:
                data["index"] = _index(data["models"])
            _catalog = data
            return data
    except Exception as e:
        logger.warning("model_catalog: failed to read cache: %s", e)
    return None


def _save_cache(catalog: Dict[str, Any]) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(catalog), encoding="utf-8")
    except Exception as e:
        logger.warning("model_catalog: failed to write cache: %s", e)


# ── Public lookup API ───────────────────────────────────────────────────────

async def ensure_fresh() -> None:
    """Refresh in the background-safe way: only fetches if cache is stale/empty."""
    await refresh(force=False)


def lookup(model_id: str, provider_hint: str = "") -> Optional[Dict[str, Any]]:
    """Return the merged metadata entry for a model id, or None if unknown.

    Resolution order:
      1. exact id ('openai/gpt-5')
      2. '<provider_hint>/<bare>' when a provider is known (so a direct OpenAI
         model 'gpt-5' attaches OpenAI's pricing, not some reseller's)
      3. the bare tail alone ('gpt-5')
    """
    cat = _catalog or _load_cache()
    if not cat:
        return None
    models = cat.get("models") or {}
    index = cat.get("index") or {}
    if not model_id:
        return None
    bare = _bare(model_id)
    key = index.get(model_id.strip().lower())
    if not key and provider_hint:
        key = index.get(f"{provider_hint.strip().lower()}/{bare}")
    if not key:
        key = index.get(bare)
    if key and key in models:
        return models[key]
    return None


def enrich(model_id: str, provider_hint: str = "") -> Dict[str, Any]:
    """Like lookup() but always returns a dict (empty fields if unknown)."""
    return lookup(model_id, provider_hint) or _empty_entry(model_id)


def all_models() -> Dict[str, Any]:
    """Return the full merged {id: entry} map (may be empty before first fetch)."""
    cat = _catalog or _load_cache()
    return (cat or {}).get("models", {})


def cache_info() -> Dict[str, Any]:
    """Lightweight status for the UI / refresh button."""
    cat = _catalog or _load_cache()
    if not cat:
        return {"fetched_at": 0, "count": 0, "stale": True}
    fetched = cat.get("fetched_at", 0)
    return {
        "fetched_at": fetched,
        "count": len(cat.get("models") or {}),
        "stale": (time.time() - fetched) >= CACHE_TTL_SECONDS,
    }
