"""Model Switcher ability — SELF-CONTAINED drop-in.

The single home for an agent changing THIS conversation's model at runtime. (The
image_vision ability deliberately does NOT switch models — it only reads images;
taking over on a vision/image-output model is done here via ``set_model``.)

Five tools — a read-only menu plus the switches:

  • ``list_models`` — the MENU the agent reads on demand: every enabled, runnable
    model with capability badges (sees images / makes images / premium tier), which
    one is running now, which is the default, and the reasoning-effort levels. This
    is what lets the agent switch by *capability* instead of memorising ids — it
    only needs a real model name to honour a user's explicit request. Read-only.

  • ``set_model`` — switch onto any *enabled, tool-capable* model: the user asked
    for a specific model, the agent wants a vision/image-output model for sustained
    image work, or it wants to drop back to the cheap default. Pass a model id to
    take over on it, or "default"/empty to revert.

  • ``use_premium_model`` — for a genuinely hard task the agent can UPGRADE
    itself onto the configured premium / "high-effort" tier (the rows an admin
    ticked *Eff* in App Config → Models). If it's already on a premium model, this
    is a no-op. Pair it with ``set_model('default')`` to fall back to the cheap
    model when the hard task is done.

  • ``set_effort`` / ``reset_to_default`` — tune the reasoning depth for this chat,
    and drop both model + effort back to the agent's defaults in one call.

The switches write a per-session model override (sessions.metadata.llm_config) that
the loop resolves on the NEXT turn (app-default → agent → session). The spending
switches are DESTRUCTIVE → a confirmation is required by default (flip to auto in
the Tools panel) so an upgrade never spends on a pricier model without a yes.
``list_models`` and ``reset_to_default`` are not gated.

Drop-in contract: descriptor .json + build_tools()/TOOL_SCHEMAS/DESTRUCTIVE,
discovered generically by app/tools/loader.py. See plugins/abilities/_TEMPLATE.py.

╔══════════════════════════════════════════════════════════════════════════════╗
║  SISTER-SYNC: SESSION-MODEL-OVERRIDE                                          ║
║  The session override + capability ladder (text+tools = a valid brain) is the ║
║  SAME mechanism the chat footer model picker uses (and that image_vision's     ║
║  describe/route guidance now points back to here). The high-effort tier is via ║
║  app.admin.settings.high_effort_targets (the *Eff* tick). If that resolver or ║
║  set_session_llm_override changes, update here. (grep SISTER-SYNC: SESSION-MODEL-OVERRIDE)║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from typing import Optional

TOOL_SCHEMAS: dict = {}
# Anything that can INCREASE spend (a model swap, an upgrade, or raising reasoning
# effort) confirms by default (auto-able in the Tools panel) so the agent never
# quietly costs more without a yes. The plain revert (reset_to_default) is NOT
# gated — dropping back to the cheap default should be frictionless.
#   set_effort is listed here, but the guardrail applies a per-arg exemption
#   (loop._effort_raises_spend): it ONLY asks when the new level is HIGHER than the
#   current one. Lowering effort or clearing it runs without confirmation.
DESTRUCTIVE: set = {"set_model", "use_premium_model", "set_effort"}

# The reasoning-effort scale (mirrors REASONING_EFFORT_LEVELS in
# app/admin/settings.py and the footer picker). "default" clears the hint.
_EFFORT_LEVELS = ("default", "minimal", "low", "medium", "high")


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the model_control ability. Heavy imports stay
    lazy (inside the handlers) so scanning the descriptor stays cheap."""

    async def _load_caps():
        """Warm the catalog (so tool-capability guards have data) and read the
        user's model config. Returns (caps, error_str_or_None)."""
        try:
            from app import model_catalog
            await model_catalog.ensure_fresh()
        except Exception:
            pass
        try:
            from app.admin.settings import load_llm_capabilities_for_user
            return (await load_llm_capabilities_for_user(user_id)), None
        except Exception as e:  # noqa: BLE001
            return None, f"Could not read model config: {e}"

    def _enabled_brains(caps: dict) -> list:
        """Model ids that are enabled AND can be the brain (text + tool-capable)."""
        from app.admin.settings import _is_tool_capable
        d = caps.get("default") or {}
        entries = ([d] if d.get("model") else []) + [
            r for r in (caps.get("racers") or []) if r.get("enabled") and r.get("model")
        ]
        return sorted({
            e["model"] for e in entries
            if e.get("text_capable") and _is_tool_capable(e)
        })

    async def _current_model(caps: dict) -> str:
        """The model this conversation is running on right now: the session
        override if set, else the agent/app default."""
        from app.db import get_db
        db = get_db()
        try:
            override = await db.get_session_llm_override(session_id)
        except Exception:
            override = None
        return ((override or {}).get("model")
                or (caps.get("default") or {}).get("model") or "")

    async def set_model(model: str = "") -> str:
        """Switch THIS conversation's model. Pass a model id to run on it (must be
        an enabled, tool-capable model), or "default"/empty to revert to the
        agent's default. Persists for the session; takes effect next turn."""
        import json
        from app.db import get_db

        db = get_db()
        target = (model or "").strip()

        # Revert path.
        if target in ("", "default", "reset"):
            await db.set_session_llm_override(session_id, None)
            return json.dumps({"status": "ok", "model": "",
                               "message": "Reverted to the agent's default model (next turn)."})

        caps, err = await _load_caps()
        if err:
            return json.dumps({"status": "error", "message": err})
        brains = _enabled_brains(caps)
        if target not in brains:
            return json.dumps({
                "status": "error",
                "code": "not_a_brain_model",
                "message": (f"'{target}' isn't an enabled model that can run the agent "
                            f"(it must be enabled and support tools). You can switch to: "
                            f"{', '.join(brains) or '(none configured)'}."),
            })

        await db.set_session_llm_override(session_id, target)
        return json.dumps({
            "status": "ok",
            "model": target,
            "message": (f"This conversation will now run on {target} (next turn). "
                        "Call set_model('default') to revert when you're done."),
        })

    async def use_premium_model() -> str:
        """Upgrade THIS conversation onto a configured premium (high-effort) model
        for a hard task. No-op if already on one. Revert later with
        set_model('default'). Ask the user before calling unless told otherwise."""
        import json
        from app.db import get_db
        from app.admin.settings import high_effort_targets

        caps, err = await _load_caps()
        if err:
            return json.dumps({"status": "error", "message": err})

        targets = high_effort_targets(caps)
        if not targets:
            return json.dumps({
                "status": "error",
                "code": "no_high_effort_model",
                "message": ("No high-effort model is configured, so there's nothing stronger "
                            "to upgrade onto. Tell the user an admin can mark one in App Config "
                            "→ Models by ticking the 'Eff' box on a capable model."),
            })

        current = await _current_model(caps)
        if current in targets:
            return json.dumps({
                "status": "ok",
                "model": current,
                "changed": False,
                "message": f"Already running on a high-effort model ({current}); no change needed.",
            })

        # Prefer a target that isn't the current model (it won't be, given the
        # guard above, but stay defensive), else the first configured one.
        target = next((t for t in targets if t != current), targets[0])
        db = get_db()
        await db.set_session_llm_override(session_id, target)
        return json.dumps({
            "status": "ok",
            "model": target,
            "changed": True,
            "message": (f"Upgraded this conversation to the high-effort model {target} (next turn). "
                        "Call set_model('default') to drop back to the cheaper model when the hard "
                        "task is done."),
        })

    async def set_effort(level: str = "default", model: str = "") -> str:
        """Set how hard the model THINKS on this conversation — the reasoning
        effort. ``level`` is one of default / minimal / low / medium / high
        ('default' clears the hint = the model's own default). ``model`` is the
        model id to set it for; leave empty to set it for the model the chat is
        running on right now. Each model remembers its own level. Takes effect next
        turn. Raising effort costs more, so propose it before calling unless told
        otherwise."""
        import json
        from app.db import get_db

        lvl = (level or "default").strip().lower()
        if lvl not in _EFFORT_LEVELS:
            return json.dumps({
                "status": "error", "code": "bad_level",
                "message": (f"'{level}' isn't a valid effort level. Choose one of: "
                            f"{', '.join(_EFFORT_LEVELS)} (use 'default' to clear it)."),
            })

        caps, err = await _load_caps()
        if err:
            return json.dumps({"status": "error", "message": err})
        target_model = (model or "").strip() or await _current_model(caps)
        if not target_model:
            return json.dumps({
                "status": "error", "code": "no_model",
                "message": "Couldn't resolve which model to set the effort for. Name a model id.",
            })

        db = get_db()
        await db.set_session_model_effort(session_id, target_model, lvl)
        if lvl == "default":
            msg = (f"Cleared the reasoning-effort hint for {target_model} (next turn) — "
                   "it'll use its own default thinking depth.")
        else:
            msg = (f"{target_model} will now think at '{lvl}' reasoning effort (next turn). "
                   "Call set_effort('default') or reset_to_default() to drop it back when the "
                   "task is done.")
        return json.dumps({"status": "ok", "model": target_model, "reasoning_effort": lvl,
                           "message": msg})

    async def reset_to_default() -> str:
        """Return this conversation to the agent's DEFAULT model at DEFAULT effort —
        clears both the picked-model override and every per-model reasoning-effort
        level in one call. The clean "the hard task is done" revert. Takes effect
        next turn."""
        import json
        from app.db import get_db

        db = get_db()
        await db.clear_session_llm_override(session_id)
        return json.dumps({
            "status": "ok", "model": "", "reasoning_effort": "default",
            "message": "Reverted this conversation to the agent's default model and default "
                       "reasoning effort (next turn).",
        })

    async def list_models() -> str:
        """List the models this conversation can switch onto and what each can do —
        call this BEFORE switching so you pick by capability, not by guessing an id.
        Returns every enabled, runnable (text + tool-capable) model with badges —
        whether it can see images, generate images, and whether it's the premium
        (high-effort) tier — plus which model is running now, which is the default,
        and the reasoning-effort levels you can set. Read-only; changes nothing.

        Use it like a menu: to honour a user's explicit model request pass that id to
        set_model; to right-size on your own, switch by *capability* — use_premium_model()
        for the premium tier, or set_model onto a model whose makes_images/sees_images
        is true for image work — without memorising any id."""
        import json
        from app.admin.settings import (
            model_sees_images, model_makes_images, high_effort_targets,
        )

        caps, err = await _load_caps()
        if err:
            return json.dumps({"status": "error", "message": err})

        # Index every configured entry by model id so we can read its capability
        # flags. A same-id racer is filled first; the default row then wins.
        entries: dict = {}
        for r in (caps.get("racers") or []):
            if r.get("model"):
                entries.setdefault(r["model"], r)
        d = caps.get("default") or {}
        if d.get("model"):
            entries[d["model"]] = d

        brains = _enabled_brains(caps)            # the valid set_model targets
        premium = set(high_effort_targets(caps))  # the *Eff* (high-effort) tier
        current = await _current_model(caps)
        default_id = (caps.get("default") or {}).get("model") or ""

        models = []
        for mid in brains:
            e = entries.get(mid, {"model": mid})
            models.append({
                "id": mid,
                "current": mid == current,
                "default": mid == default_id,
                "sees_images": model_sees_images(e),
                "makes_images": model_makes_images(e),
                "premium": mid in premium,
            })

        actionable_effort = [lvl for lvl in _EFFORT_LEVELS if lvl != "default"]
        return json.dumps({
            "status": "ok",
            "current": current,
            "default": default_id,
            "models": models,
            "premium_available": bool(premium),
            "image_out_available": any(m["makes_images"] for m in models),
            "vision_available": any(m["sees_images"] for m in models),
            "effort_levels": actionable_effort,
            "message": (
                "These are the models you can switch onto with set_model('<id>'). For a "
                "user's explicit model request, pass that id. To right-size on your own, "
                "switch by capability: use_premium_model() for the premium tier, or set_model "
                "onto a model whose makes_images/sees_images is true for image work. Tune "
                "thinking depth with set_effort (levels above; 'default' clears it), and revert "
                "with reset_to_default() when the task is done. Some models ignore reasoning "
                "effort — it's applied if the model supports it, harmlessly dropped if not."
            ),
        })

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "set_model": {
            "type": "object",
            "properties": {
                "model": {"type": "string",
                          "description": "Model id to run this conversation on (must be enabled and support tools), or 'default' to revert."},
            },
            "required": ["model"],
        },
        "use_premium_model": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "set_effort": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": list(_EFFORT_LEVELS),
                          "description": "Reasoning effort: default (clear), minimal, low, medium, or high."},
                "model": {"type": "string",
                          "description": "Model id to set the effort for; empty = the model this chat is running on now."},
            },
            "required": ["level"],
        },
        "reset_to_default": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "list_models": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    })
    return {"set_model": set_model, "use_premium_model": use_premium_model,
            "set_effort": set_effort, "reset_to_default": reset_to_default,
            "list_models": list_models}
