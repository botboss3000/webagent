"""Host-ability catalog — the drop-in registry for agent abilities.

An **ability** is a host-side capability bundle the agent admin can grant to an
agent (Codebase Admin, Web Access, Terminal Control, Wiki Control, …). It is the
last subsystem to join the drop-in model used by integrations, channels, events,
secrets, encryption, billing, and scheduler.

╔══════════════════════════════════════════════════════════════════════════╗
║  THIS FILE IS CORE.  Abilities themselves are NOT defined here.           ║
║  Each ability is ONE self-describing file in  ``plugins/abilities/`` —    ║
║  drop a file in to add an ability, delete it to remove one. You do NOT    ║
║  edit this manager, app/api/agents.py, app/tools/loader.py,               ║
║  ui/js/app-config.js, or ui/js/agents.js when adding an ability. They all ║
║  read whatever this manager discovers. See CLAUDE.md ("Core vs. plugins") ║
║  and docs/claude/production-editions.md.                                  ║
╚══════════════════════════════════════════════════════════════════════════╝

Ability file contract (``plugins/abilities/<id>.py``)
-----------------------------------------------------
A module-level ``FEATURE`` dict. The standard self-description fields (consumed
by the feature catalog + edition gating) PLUS ability-specific fields:

    FEATURE = {
        "id": "web_access",                 # stable id; defaults to file stem
        "display_name": "Web Access",
        "category": "ability",
        "status": "stable",                 # stable | beta | experimental
        "summary": "web_search, weather, maps geocoding.",
        # --- ability runtime + UI (read here) ---
        "tools": ["web_search", "get_weather", "maps_geocode"],  # tool NAMES it gates
        "group": "web",                     # UI group id — reuse one or invent a new one
        "icon": "globe",                    # lucide icon name
        "color": "#7aa2f7",                 # accent colour (used in both panels)
        "description": "Lets the agent search the web, …",        # row description
        "simple": True,                     # True = toggles directly; False = needs config
        # optional — only when you INVENT a new group; style it from here:
        # "group_label": "Communication", "group_icon": "message-circle",
        # "group_color": "#7aa2f7", "group_desc": "Talk to the outside world.",
        # optional bundled skill (same scheme as integrations) — minted once:
        # "skill": "…how-to…", "skill_handle": "web_access_a1b2c3d4",
    }

Tool handlers can live EITHER in core (the classic ability just declares which
tool *names* it unlocks, and the handlers sit in app/tools/core_tools.py and
friends) OR in the ability file itself — a SELF-CONTAINED ability ships its own
handlers via an optional ``build_tools()`` hook (and an optional
``start_background()`` service), exactly like an integration carries its TOOLS.
Both are auto-discovered; see "Self-contained abilities" lower in this file.

Groups are EMERGENT — there is no master list to edit. The ``group`` id you give
an ability decides its bucket: name an existing id to JOIN that group, or a new
id to CREATE one (see the group-resolution rules below ``_KNOWN_GROUPS``). A new
group can be styled right here via the optional ``group_*`` fields, or it just
gets a sensible default look. Abilities AND groups are both the drop-in unit.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ABILITIES_DIR = Path(__file__).resolve().parents[2] / "plugins" / "abilities"


# ── UI groups — EMERGENT from the abilities, NOT a fixed list ────────────────
# An ability says which group it belongs to via FEATURE["group"] (an id like
# "administrator" or "communication"). The set of groups is then DERIVED from
# whatever the ability files declare — there is no master group list to edit:
#   • Two abilities that name the SAME group id  → they share that one group.
#   • An ability that names a BRAND-NEW group id → a new group is created for it.
# So adding a group is itself drop-in: just name one in an ability file.
#
# Each group needs a look (label, icon, colour, blurb). It is resolved, in order:
#   1. _KNOWN_GROUPS below — the curated built-in groups (stable name/icon/order).
#   2. Optional hints ON the ability file — FEATURE["group_label" / "group_icon" /
#      "group_color" / "group_desc"] (first ability that sets each wins). This lets
#      a NEW group be fully styled from a drop-in file, with no edit here.
#   3. Sensible defaults — Title-cased id + a neutral icon/colour.
# Known groups render first (in _KNOWN_GROUPS order); emergent groups follow,
# alphabetically. Members inside a group are sorted by display name.
_KNOWN_GROUPS: Dict[str, Dict[str, Any]] = {
    "administrator": {"order": 0, "name": "Administrator", "icon": "shield-alert", "color": "#f7768e",
                      "desc": "Full host control — files & shell, version control, terminals, and diagnostics. Grant with care."},
    "core":          {"order": 1, "name": "Core", "icon": "wrench", "color": "#7dcfff",
                      "desc": "Everyday, non-destructive capabilities — UI edits, tool creation, automation, pages, image generation, orchestration, agent management, and app view control."},
    "productivity":  {"order": 2, "name": "Productivity", "icon": "briefcase", "color": "#4285F4",
                      "desc": "Read, search, create, edit, and delete entries in the company-wide Wiki."},
    "web":           {"order": 3, "name": "Web", "icon": "globe", "color": "#7aa2f7",
                      "desc": "Reach the open web — search, a headless browser, third-party scraping, and authenticated cookie sessions."},
}
# An ability that forgets to declare a group lands here, so it's never silently
# dropped from the UI.
_DEFAULT_GROUP = "core"
# Look for an emergent (unknown) group when the ability gives no styling hints.
_EMERGENT_GROUP_ICON = "layers"
_EMERGENT_GROUP_COLOR = "#9aa5ce"
_EMERGENT_GROUP_ORDER = 100


def _titleize(gid: str) -> str:
    """'communication' / 'data-ops' → 'Communication' / 'Data Ops'."""
    return (gid or "").replace("_", " ").replace("-", " ").strip().title() or gid


def _group_of(feat: Dict[str, Any]) -> str:
    return feat.get("group") or _DEFAULT_GROUP


def _resolve_group_meta(gid: str, hints: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Group display metadata: curated → ability hints → defaults. Carries a
    private ``_order`` used only for sorting (stripped before returning)."""
    if gid in _KNOWN_GROUPS:
        m = _KNOWN_GROUPS[gid]
        return {"id": gid, "name": m["name"], "icon": m["icon"], "color": m["color"],
                "desc": m["desc"], "_order": m["order"]}
    h = hints.get(gid, {})
    return {"id": gid,
            "name": h.get("label") or _titleize(gid),
            "icon": h.get("icon") or _EMERGENT_GROUP_ICON,
            "color": h.get("color") or _EMERGENT_GROUP_COLOR,
            "desc": h.get("desc") or "",
            "_order": _EMERGENT_GROUP_ORDER}

# Credential providers that are NOT abilities (no tool bundle of their own) but
# render inside an ability group as their own config cards. Their connection
# rows are still produced by app/api/agents.py; here we only tell the UI which
# group they sit in so the panels can place them. (display_name, group).
_CREDENTIAL_MEMBERS: Dict[str, Dict[str, str]] = {
    "scraper":         {"display_name": "Web Scraper",    "group": "web"},
    "browser_session": {"display_name": "Browser Cookies", "group": "web"},
}


_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
# {ability_id: loaded module} — kept alongside _CACHE so an ability that ships
# its OWN tool handlers / background service (see "Self-contained abilities"
# below) can be reached without re-executing the file.
_MODULE_CACHE: Dict[str, Any] = {}


def _load(force: bool = False) -> Dict[str, Dict[str, Any]]:
    """Path-scan ``plugins/abilities/*.py`` and return {id: FEATURE dict}.

    Path-based loading (not import_module) so the top-level ``plugins/`` tree
    need not be on ``sys.path``. Mirrors the communications / events managers.
    The executed module objects are cached in ``_MODULE_CACHE`` so a
    self-contained ability's ``build_tools`` / ``start_background`` hooks can be
    invoked later without re-importing.
    """
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE

    out: Dict[str, Dict[str, Any]] = {}
    mods: Dict[str, Any] = {}
    if not _ABILITIES_DIR.is_dir():
        logger.warning("Abilities dir not found: %s", _ABILITIES_DIR)
        _CACHE = out
        _MODULE_CACHE.clear()
        return out

    for fpath in sorted(_ABILITIES_DIR.iterdir()):
        if fpath.suffix != ".py" or fpath.name.startswith("_"):
            continue
        stem = fpath.stem
        try:
            spec = importlib.util.spec_from_file_location(f"plugins.abilities.{stem}", fpath)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            feature = getattr(mod, "FEATURE", None)
            if not isinstance(feature, dict):
                logger.debug("Ability %s has no FEATURE dict, skipping", stem)
                continue
            feat = dict(feature)
            feat.setdefault("id", stem)
            feat.setdefault("category", "ability")
            aid = str(feat["id"])
            out[aid] = feat
            mods[aid] = mod
        except Exception as e:
            logger.error("Failed to load ability %s: %s", stem, e)

    _CACHE = out
    _MODULE_CACHE.clear()
    _MODULE_CACHE.update(mods)
    return out


def reload() -> None:
    """Drop the cache so a newly-dropped ability file is picked up."""
    _load(force=True)


# ── Self-contained abilities — abilities that ship their own tools / runtime ──
# By default an ability only DECLARES tool names (handlers live in core). But an
# ability MAY instead carry its own implementation in its plugin file, exactly
# like an integration does (app/integrations/*). It does so with two optional
# module-level hooks, both discovered generically — no core edit per ability:
#
#   build_tools(*, user_id, session_id, agent_id, agent_template_id, **ctx)
#       → {tool_name: async_handler}.  Called by app/tools/loader.py for every
#         ENABLED ability that defines it; the returned handlers are injected as
#         normal tools. The module may also expose TOOL_SCHEMAS ({name: schema})
#         and DESTRUCTIVE (a set of names needing confirmation). The ability owns
#         any gating of its own (e.g. refusing to arm inside certain sessions) —
#         return {} to inject nothing this call.
#
#   start_background() / stop_background()  (async)
#       → a long-lived service (e.g. a poller) the ability needs running while
#         the app is up. Started/stopped generically by app/main.py.
#
# This keeps a tool-bearing ability fully drop-in: its FEATURE, skill, handlers,
# storage, and background service all live in plugins/abilities/, and deleting
# the file removes the whole capability. Heavy imports should be done lazily
# INSIDE these hooks (or in a sibling ``_<id>_impl.py``, skipped by the scanner)
# so the FEATURE scan stays cheap.


def ability_module(ability_id: str) -> Optional[Any]:
    """Return the executed plugin module for an ability id (or None). Used to
    reach optional ``build_tools`` / ``start_background`` hooks."""
    _load()
    return _MODULE_CACHE.get(ability_id)


def background_service_hooks() -> List[Dict[str, Any]]:
    """Discover abilities that expose a background service. Returns a list of
    ``{"id", "start", "stop"}`` for every ability module defining an async
    ``start_background`` (``stop`` may be None). app/main.py runs these at
    startup / shutdown so no ability wires itself into the lifespan by name."""
    _load()
    hooks: List[Dict[str, Any]] = []
    for aid, mod in _MODULE_CACHE.items():
        start = getattr(mod, "start_background", None)
        if callable(start):
            hooks.append({
                "id": aid,
                "start": start,
                "stop": getattr(mod, "stop_background", None),
            })
    return hooks


# ── Ability-bundled skills ───────────────────────────────────────────────────
# An ability can carry a skill — a knowledge pack folded into the agent's
# `# [SKILLS]` catalog whenever the ability is enabled (load-on-demand by
# default, or always-on). The body can live INLINE in FEATURE["skill"], or in a
# separate file so long bodies stay out of the .py. Resolution order for a file:
#   1. FEATURE["skill_file"]        — explicit path, relative to plugins/abilities/
#   2. plugins/abilities/<id>.skill.md
#   3. plugins/abilities/<id>/SKILL.md   (a per-ability folder)
# Mode + handle come from FEATURE (skill_mode / skill_handle); the agent layer
# (app/agent/ability_skills.py) shapes the final skill dict and mints a stable
# handle when none is given. The catalog "when to use it" line is
# FEATURE["skill_summary"] if set (so a skill can prompt loading in its own
# words), else it falls back to the ability's tool FEATURE["summary"].

def _resolve_skill_body(feat: Dict[str, Any], ability_id: str) -> str:
    """Return the skill body for an ability — inline FEATURE['skill'] or a file."""
    inline = (feat.get("skill") or "").strip()
    if inline:
        return inline
    candidates = []
    if feat.get("skill_file"):
        candidates.append(_ABILITIES_DIR / str(feat["skill_file"]))
    candidates.append(_ABILITIES_DIR / f"{ability_id}.skill.md")
    candidates.append(_ABILITIES_DIR / ability_id / "SKILL.md")
    for path in candidates:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Could not read skill file %s: %s", path, e)
    return ""


def ability_feature_with_skill(ability_id: str) -> Optional[Dict[str, Any]]:
    """If the ability declares a skill (inline or file), return a copy of its
    FEATURE with the resolved body inlined under ``skill``; else None. The agent
    layer turns this into a `# [SKILLS]` entry."""
    feat = _load().get(ability_id)
    if not feat:
        return None
    body = _resolve_skill_body(feat, ability_id)
    if not body:
        return None
    return {**feat, "skill": body}


# ── Accessors ───────────────────────────────────────────────────────────────

def all_raw() -> Dict[str, Dict[str, Any]]:
    """Every discovered ability's raw FEATURE dict, keyed by id."""
    return dict(_load())


def tools_map() -> Dict[str, List[str]]:
    """{ability_id: [tool names it gates]} — feeds app/tools/loader.ABILITY_TOOLS."""
    return {aid: list(feat.get("tools") or []) for aid, feat in _load().items()}


def feature_descriptors() -> List[Any]:
    """FeatureDescriptor objects for the feature catalog / edition gating."""
    from app.features.descriptor import normalize_feature
    out = []
    for aid, feat in _load().items():
        out.append(normalize_feature(
            feat, category="ability", default_id=aid,
            module=f"plugins.abilities.{aid}", drop_in=True,
        ))
    return out


def _surfaced() -> Dict[str, Dict[str, Any]]:
    """Every discovered ability file surfaces as a UI toggle — drop a file in
    and it shows up. One that omits FEATURE["group"] lands in the default group
    rather than vanishing."""
    return dict(_load())


def connection_rows() -> List[Dict[str, Any]]:
    """The ``section='ability'`` connection rows app/api/agents.py serves.

    Carries the UI metadata (description/icon/color/group/simple) so the two
    ability panels can render generically without per-ability constants.
    """
    rows: List[Dict[str, Any]] = []
    for aid, feat in _surfaced().items():
        rows.append({
            "connection_type": aid,
            "section": "ability",
            "display_name": feat.get("display_name") or aid,
            "status": "available",
            "description": feat.get("description") or feat.get("summary") or "",
            "icon": feat.get("icon") or "plug",
            "color": feat.get("color") or "#7aa2f7",
            "group": feat.get("group") or _DEFAULT_GROUP,
            "simple": bool(feat.get("simple", True)),
            "maturity": feat.get("status") or "unknown",
        })
    return rows


def ui_catalog() -> Dict[str, Any]:
    """The payload both ability panels fetch to render generically.

    {
      groups: [{id, name, icon, color, desc, members:[ability_id…]}],
      abilities: {id: {display_name, description, icon, color, simple, status, group, tools}},
      credential_members: [id…],
    }
    Members are sorted alphabetically by display name and include the credential
    providers that live in the group (scraper / browser_session).
    """
    raw = _surfaced()

    abilities: Dict[str, Any] = {}
    group_hints: Dict[str, Dict[str, Any]] = {}  # optional per-group styling from ability files
    for aid, feat in raw.items():
        gid = _group_of(feat)
        abilities[aid] = {
            "id": aid,
            "display_name": feat.get("display_name") or aid,
            "description": feat.get("description") or feat.get("summary") or "",
            "icon": feat.get("icon") or "plug",
            "color": feat.get("color") or "#7aa2f7",
            "simple": bool(feat.get("simple", True)),
            "status": feat.get("status") or "unknown",
            "group": gid,
            "tools": list(feat.get("tools") or []),
        }
        # First ability to style an emergent group wins (known groups ignore hints).
        if gid not in _KNOWN_GROUPS and gid not in group_hints:
            h = {}
            for src, dst in (("group_label", "label"), ("group_icon", "icon"),
                             ("group_color", "color"), ("group_desc", "desc")):
                if feat.get(src):
                    h[dst] = feat[src]
            if h:
                group_hints[gid] = h

    # Display names for member sorting (abilities + credential providers).
    name_of: Dict[str, str] = {aid: a["display_name"] for aid, a in abilities.items()}
    for cid, meta in _CREDENTIAL_MEMBERS.items():
        name_of[cid] = meta["display_name"]

    # Bucket members by group — group ids are whatever the abilities declared.
    by_group: Dict[str, List[str]] = {}
    for aid, a in abilities.items():
        by_group.setdefault(a["group"], []).append(aid)
    for cid, meta in _CREDENTIAL_MEMBERS.items():
        by_group.setdefault(meta["group"], []).append(cid)

    # Resolve each group's look, attach sorted members, order known-first.
    groups = []
    for gid, members in by_group.items():
        if not members:
            continue
        g = _resolve_group_meta(gid, group_hints)
        g["members"] = sorted(members, key=lambda m: name_of.get(m, m).lower())
        groups.append(g)
    groups.sort(key=lambda g: (g["_order"], g["name"].lower()))
    for g in groups:
        g.pop("_order", None)

    return {
        "groups": groups,
        "abilities": abilities,
        "credential_members": list(_CREDENTIAL_MEMBERS.keys()),
    }
