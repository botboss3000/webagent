"""Image Vision ability — SELF-CONTAINED drop-in.

Two capabilities for working WITH images (as opposed to *generating* them — that's
the separate ``image_generation`` ability, so an agent can be tailored to one, the
other, or both):

  • ``process_image`` — the agent's own model often can't see images (a text-only
    or tool-only model). This tool DELEGATES one image question to a vision-capable
    model (the row ticked Image-in in App Config → Models) via a tools-free one-shot
    worker, and returns the answer. The main agent never pauses; for a sharper
    answer it simply calls again with a tighter prompt (fresh worker each time —
    cheaper and simpler than a long-lived sub-conversation).

  • ``switch_model`` — for image-heavy stretches the agent can TAKE OVER on a
    text+image model instead of delegating. This sets a per-session model override
    (sessions.metadata.llm_config) that persists until changed; it only accepts a
    model that can be the brain (supports tools). A confirmation is required by
    default (flip it to auto in the Tools panel). The bundled skill advises when to
    delegate vs. take over, and to switch back to the cheaper text model afterwards.

Drop-in contract: FEATURE descriptor + build_tools()/TOOL_SCHEMAS/DESTRUCTIVE,
discovered generically by app/tools/loader.py. See plugins/abilities/_TEMPLATE.py.

╔══════════════════════════════════════════════════════════════════════════════╗
║  SISTER-SYNC: MODEL-WORKER-DELEGATE                                           ║
║  process_image runs on the shared neutral-core worker app.agent.model_worker ║
║  .ask_model (so it works even when the Orchestration ability is OFF). If that ║
║  worker's signature/return changes, update here. Model roles (TEXT/IN/OUT)    ║
║  come from App Config → Models; vision picks the IN row via                   ║
║  app.admin.settings.pick_vision_model. (grep SISTER-SYNC: MODEL-WORKER-DELEGATE)║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from typing import Optional

FEATURE = {
    "id": "image_vision",
    "display_name": "Image Vision",
    "category": "ability",
    "status": "beta",
    "summary": "read/understand attached images (delegate to a vision model) + switch the agent's model.",
    "tools": ["process_image", "switch_model"],
    "group": "core",
    "icon": "eye",
    "color": "#7dcfff",
    "description": (
        "Lets the agent understand attached images by delegating to a vision model, "
        "and switch its own model mid-chat (e.g. to a text+image model) with permission."
    ),
    "simple": True,
    # Bundled guidance skill (body in image_vision.skill.md, found by convention).
    "skill_mode": "selectable",
    "skill_handle": "image_vision_guide_v1",
    "skill_summary": (
        "How to handle images: delegate a quick look with process_image, or take over "
        "on a vision model with switch_model — and when to switch back."
    ),
}


TOOL_SCHEMAS: dict = {}
# switch_model changes how the session runs → confirm by default (auto-able in the
# Tools panel). process_image is read-only.
DESTRUCTIVE: set = {"switch_model"}


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the image_vision ability. Heavy imports stay
    lazy (inside the handlers) so scanning FEATURE stays cheap."""

    async def process_image(question: str, attachment_id: str = "") -> str:
        """Ask a vision model a specific question about an attached image and return
        its answer. Use this when your own model can't see images."""
        import json
        from app.db import get_db
        from app.admin.settings import load_llm_capabilities_for_user, pick_vision_model
        from app.agent.model_worker import ask_model

        if not (question or "").strip():
            return json.dumps({"status": "error", "message": "question is required — say what to look for."})

        db = get_db()
        # Resolve which image to look at: explicit id, else the latest image in this session.
        att = None
        if attachment_id:
            att = await db.get_attachment(attachment_id)
            if not att:
                return json.dumps({"status": "error", "message": f"No attachment {attachment_id}."})
        else:
            try:
                atts = await db.get_session_attachments(session_id)
            except Exception:
                atts = []
            images = [a for a in atts if (a.get("mime_type") or "").lower().startswith("image/")]
            if not images:
                return json.dumps({
                    "status": "error",
                    "message": "No image is attached to this conversation. Ask the user to upload one.",
                })
            att = images[-1]  # most recent (rows come back oldest→newest)

        # Pick the configured vision (Image-in) model. Warm the catalog cache first
        # so pick_vision_model's capability guard has data to veto a mis-ticked
        # text-only model.
        try:
            from app import model_catalog
            await model_catalog.ensure_fresh()
        except Exception:
            pass
        try:
            caps = await load_llm_capabilities_for_user(user_id)
        except Exception as e:
            return json.dumps({"status": "error", "message": f"Could not read model config: {e}"})
        vision = pick_vision_model(caps)
        if not vision:
            return json.dumps({
                "status": "error",
                "code": "no_vision_model",
                "message": ("No image-input model is configured. Ask the admin to open App "
                            "Config → Models, save a vision-capable model and tick its In box."),
            })

        sys_line = (
            "You are a vision assistant. Look at the attached image and answer the "
            "question precisely and factually. Include any visible text verbatim and "
            "concrete details (objects, layout, colours, positions). Do not speculate "
            "beyond what is visible."
        )
        answer = await ask_model(vision, sys_line, question, attachments=[att], max_tokens=900)
        if not answer:
            return json.dumps({
                "status": "error",
                "message": f"The vision model ({vision.get('model','')}) could not describe the image.",
            })
        return json.dumps({
            "status": "ok",
            "model": vision.get("model", ""),
            "attachment": att.get("original_name") or att.get("id"),
            "answer": answer,
        })

    async def switch_model(model: str = "") -> str:
        """Switch THIS conversation's model. Pass a model id to take over on it
        (must support tools), or "default" / empty to revert to the agent default.
        Persists for the session until changed; takes effect on the next turn."""
        import json
        from app.db import get_db
        from app.admin.settings import load_llm_capabilities_for_user

        db = get_db()
        target = (model or "").strip()

        # Revert path.
        if target in ("", "default", "reset"):
            await db.set_session_llm_override(session_id, None)
            return json.dumps({"status": "ok", "model": "", "message": "Reverted to the agent's default model."})

        # Only allow enabled models, and only ones that can be the brain (do tools).
        try:
            caps = await load_llm_capabilities_for_user(user_id)
        except Exception as e:
            return json.dumps({"status": "error", "message": f"Could not read model config: {e}"})
        enabled = []
        d = caps.get("default") or {}
        if d.get("model"):
            enabled.append(d["model"])
        enabled += [r["model"] for r in (caps.get("racers") or [])
                    if r.get("enabled") and r.get("model")]
        if target not in enabled:
            return json.dumps({
                "status": "error",
                "message": f"'{target}' isn't an enabled model. Enabled: {', '.join(sorted(set(enabled))) or '(none)'}.",
            })
        # Reject tool-less (image-only) models as the brain — use process_image instead.
        try:
            from app import model_catalog
            await model_catalog.ensure_fresh()
            meta = model_catalog.lookup(target)
            if meta and meta.get("tool_call") is False:
                return json.dumps({
                    "status": "error",
                    "code": "no_tool_support",
                    "message": (f"'{target}' is an image-only model that can't run tools, so it "
                                "can't be the agent's model. Use process_image to delegate image "
                                "questions to it instead, or switch to a text+image model."),
                })
        except Exception:
            pass  # catalog unavailable → allow; the runtime fallback still guards us

        await db.set_session_llm_override(session_id, target)
        return json.dumps({
            "status": "ok",
            "model": target,
            "message": (f"This conversation will now run on {target} (next turn). "
                        "Call switch_model('default') to revert when the image work is done."),
        })

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "process_image": {
            "type": "object",
            "properties": {
                "question": {"type": "string",
                             "description": "What to find out about the image — be specific (e.g. 'what colours are in the top-right corner?')."},
                "attachment_id": {"type": "string",
                                  "description": "Optional. A specific attachment id; omit to use the most recent image in this chat."},
            },
            "required": ["question"],
        },
        "switch_model": {
            "type": "object",
            "properties": {
                "model": {"type": "string",
                          "description": "Model id to run this conversation on (must be enabled and support tools), or 'default' to revert."},
            },
            "required": ["model"],
        },
    })
    return {"process_image": process_image, "switch_model": switch_model}
