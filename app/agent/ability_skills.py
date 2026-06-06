"""Ability-bundled skills.

A feature/ability can ship a skill alongside its tools by declaring ``skill``
(and optional ``skill_mode`` / ``skill_handle``) in its module-level ``FEATURE``
header. When that ability is enabled for an agent, its skill is folded into the
agent's ``# [SKILLS]`` catalog automatically — the agent always sees the
"when to use it" line and pulls the full body on demand with ``load_skill``.

Identity: ability skills are keyed by a **minted-once-frozen handle** (e.g.
``email_a1b2c3d4``) so they can never collide with agent-authored skills (which
stay name-keyed) and so a loaded ability-skill still matches itself across
restarts / conversation replay. The author sets ``FEATURE['skill_handle']``;
if absent we derive a stable handle from the feature id (deterministic, never
regenerated).

Phase 2 wiring. Fully guarded — any failure yields no ability skills rather than
breaking the prompt build.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


def derive_handle(feature_id: str) -> str:
    """Stable, random-looking handle for a feature with no explicit one."""
    h = hashlib.md5(feature_id.encode("utf-8")).hexdigest()[:8]
    return f"{feature_id}_{h}"


def _skill_from_feature(feature: dict, module_name: str) -> Optional[dict]:
    if not isinstance(feature, dict):
        return None
    body = (feature.get("skill") or "").strip()
    if not body:
        return None
    fid = str(feature.get("id") or module_name)
    handle = str(feature.get("skill_handle") or derive_handle(fid))
    display = feature.get("display_name") or fid
    return {
        # `name` IS the load/active-tracking key — the handle for ability skills.
        "name": handle,
        "handle": handle,
        "display_name": display,
        # The catalog "when to use it" line. Prefer a skill-specific summary so a
        # bundled skill can prompt loading in its own words; fall back to the
        # ability's tool summary when none is given.
        "description": (feature.get("skill_summary") or feature.get("summary") or "").strip(),
        "body": body,
        "mode": (feature.get("skill_mode") or "selectable"),
        "enabled": True,
        "source": display,
        "_ability": True,
    }


async def collect_ability_skills(agent_id: str, user_id: Optional[str] = None) -> List[dict]:
    """Skill dicts contributed by the abilities enabled on this agent.

    Two sources, both keyed off the agent's *enabled* set (``gather_enabled_providers``
    returns enabled OAuth providers AND enabled host abilities, already
    admin-gated):

      1. **Integration abilities** — each enabled OAuth provider maps to the
         integration module(s) that serve it; pull each module's ``FEATURE.skill``.
      2. **Host abilities** — each enabled ability id that matches a drop-in file
         in ``plugins/abilities/`` declaring a skill (inline or a sibling file).

    Deduped by handle. Fully guarded — any failure yields fewer skills, never an
    error in the prompt build.
    """
    if not agent_id:
        return []
    out: List[dict] = []
    seen: set = set()
    try:
        from app.integrations import gather_enabled_providers, _discover_tool_specs
        providers = await gather_enabled_providers(agent_id, user_id)
        if not providers:
            return []

        # ── 1. Integration-module skills (keyed by served provider) ──
        mod_providers: Dict[str, set] = {}
        for spec in _discover_tool_specs():
            p = spec.get("provider")
            m = spec.get("_module")
            if p and m:
                mod_providers.setdefault(m, set()).add(p)
        for mod_name, provs in mod_providers.items():
            if not (provs & providers):
                continue
            try:
                mod = importlib.import_module(f"app.integrations.{mod_name}")
            except Exception:
                continue
            skill = _skill_from_feature(getattr(mod, "FEATURE", None), mod_name)
            if skill and skill["handle"] not in seen:
                seen.add(skill["handle"])
                out.append(skill)

        # ── 2. Host-ability skills (plugins/abilities/*) ──
        try:
            from app.abilities import ability_feature_with_skill
            for ability_id in providers:
                feat = ability_feature_with_skill(ability_id)
                if not feat:
                    continue
                skill = _skill_from_feature(feat, ability_id)
                if skill and skill["handle"] not in seen:
                    seen.add(skill["handle"])
                    out.append(skill)
        except Exception as e:
            logger.debug("host-ability skill collection failed: %s", e)

        return out
    except Exception as e:
        logger.debug("collect_ability_skills failed: %s", e)
        return out
