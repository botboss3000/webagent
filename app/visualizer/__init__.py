"""
Visualizer module -- Canvas workspace.

Injects the following tools into the agent's tool registry:
  render_visual   -- write HTML to a named canvas
  list_canvases   -- list all canvases for the current user
  create_canvas   -- create a new named canvas
  delete_canvas   -- delete a canvas (home is protected)
  get_canvas      -- read the current HTML content of a canvas
  rename_canvas   -- rename a canvas's display title
  get_canvas_data -- read a canvas's data bag (content kept separate from markup)
  set_canvas_data -- update a canvas's data bag WITHOUT rewriting its page markup
  get_canvas_logs -- read a canvas's OWN console output (page-scoped; no logs.db needed)
  check_credential -- is an ability connected (vault) + what a login form needs (no secrets)
  request_credential -- ask the user for a NEW secret (secure card → vault), get a key id back
  list_vault_keys -- list the user's vault keys (id/name/service/filled) — never any value

Call register_tools(tools, user_id) from app/tools/loader.py.
"""
import json
from typing import Dict
from app.tools.loader import ToolInfo


def register_tools(tools: Dict[str, ToolInfo], user_id: str, agent_id: str = "") -> None:
    """Inject visualizer tools into the tools dict.

    ``agent_id`` is the agent these tools are being built for; render_visual /
    create_canvas record it as the canvas's owning agent (so the Canvas footer
    can name + chat to the agent that made each canvas)."""

    from app.visualizer.tool import render_visual as _render_visual
    from app.visualizer.edit import edit_canvas as _edit_canvas
    from app.visualizer.canvas import (
        list_canvases as _list_canvases,
        create_canvas as _create_canvas,
        delete_canvas as _delete_canvas,
        get_canvas_html as _get_canvas_html,
        rename_canvas as _rename_canvas,
        get_canvas_data as _get_canvas_data,
        save_canvas_data as _save_canvas_data,
        read_canvas_logs as _read_canvas_logs,
    )

    # -- render_visual ---------------------------------------------------------
    # render_visual is the always-available FALLBACK (a full-page make) that weaker
    # models can do even when surgical edit_canvas matching is beyond them — so it
    # must never hard-crash on the arg quirks those models produce. Several hidden
    # aliases map to ``slug`` (``canvas_name``/``page_name``/``name``/``canvas``),
    # and ``**_extra`` swallows any other stray keyword a model invents instead of
    # raising "unexpected keyword argument" (which is exactly what aborted a real
    # v6 build). None of these are advertised in the schema, so the model is still
    # steered to ``html`` + ``slug``; this is purely a crash-proofing net.
    async def _render_visual_wrapper(html: str, title: str = "", slug: str = "home",
                                     canvas_name: str = None, page_name: str = None,
                                     name: str = None, canvas: str = None, **_extra):
        alias = canvas_name or page_name or name or canvas
        return await _render_visual(
            html=html,
            title=title,
            slug=slug,
            canvas_name=alias,
            user_id=user_id,
            agent_id=agent_id,
        )

    tools["render_visual"] = ToolInfo(
        name="render_visual",
        handler=_render_visual_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "html": {
                    "type": "string",
                    "description": (
                        "Full HTML document string to render. "
                        "For p5.js sketches, include the p5.js CDN script tag. "
                        "For regular canvases, standard HTML/CSS/JS is fine."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable title for the canvas (shown in the UI).",
                },
                "slug": {
                    "type": "string",
                    "description": (
                        "Slug of the canvas to write to (e.g. 'home', 'dashboard', 'notes'). "
                        "Pass the slug from the `Canvas: \"<slug>\"` hand-off tag — it MUST "
                        "match the canvas the user is viewing, or your work lands on the wrong "
                        "canvas. Only omit it for the 'home' canvas (the default)."
                    ),
                },
            },
            "required": ["html"],
        },
    )

    # -- edit_canvas -----------------------------------------------------------
    # Surgical find/replace edits to an existing canvas — the alternative to
    # re-rendering the whole page for a small change. Routes through the same
    # save_canvas_html path as render_visual (so the live refresh fires) and
    # returns the same path/slug shape the frontend reload handler reads.
    async def _edit_canvas_wrapper(slug: str, edits=None, find: str = None,
                                   replace: str = None, replace_all: bool = False):
        return await _edit_canvas(
            slug=slug, edits=edits, find=find, replace=replace,
            replace_all=replace_all, user_id=user_id, agent_id=agent_id,
        )

    tools["edit_canvas"] = ToolInfo(
        name="edit_canvas",
        handler=_edit_canvas_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the canvas to edit. Read it first with get_canvas('<slug>').",
                },
                "edits": {
                    "type": "array",
                    "description": (
                        "One or more find/replace edits, applied in order. PREFER this over "
                        "re-rendering the whole page when changing an existing canvas. Each "
                        "`find` must be copied EXACTLY from the current canvas (read it with "
                        "get_canvas first — whitespace and tags must match) and must be unique "
                        "unless replace_all is true. If any edit's `find` is missing or "
                        "ambiguous, NOTHING is saved and the canvas is left unchanged."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "find": {
                                "type": "string",
                                "description": "Exact existing text to locate. Include enough surrounding context to be unique.",
                            },
                            "replace": {
                                "type": "string",
                                "description": "Text to put in its place. Use an empty string to delete the found text.",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": "Replace every occurrence instead of requiring a single unique match. Default false.",
                            },
                        },
                        "required": ["find", "replace"],
                    },
                },
            },
            "required": ["slug", "edits"],
        },
    )

    # -- list_canvases ---------------------------------------------------------
    async def _list_canvases_wrapper():
        canvases = await _list_canvases(user_id)
        return json.dumps({"status": "ok", "canvases": canvases, "count": len(canvases)})

    tools["list_canvases"] = ToolInfo(
        name="list_canvases",
        handler=_list_canvases_wrapper,
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    )

    # -- get_canvas ------------------------------------------------------------
    async def _get_canvas_wrapper(slug: str):
        html = await _get_canvas_html(user_id=user_id, slug=slug)
        if html is None:
            return json.dumps({"status": "error", "message": "Canvas '{}' not found.".format(slug)})
        return json.dumps({
            "status": "ok",
            "slug": slug,
            "html": html,
            "size_bytes": len(html),
        })

    tools["get_canvas"] = ToolInfo(
        name="get_canvas",
        handler=_get_canvas_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the canvas to read (e.g. 'home', 'dashboard', 'notes').",
                },
            },
            "required": ["slug"],
        },
    )

    # -- create_canvas ---------------------------------------------------------
    async def _create_canvas_wrapper(
        slug: str,
        title: str,
        agent_context: str = "",
        initial_html: str = "",
    ):
        try:
            entry = await _create_canvas(
                user_id=user_id,
                slug=slug,
                title=title,
                agent_context=agent_context,
                initial_html=initial_html,
                agent_id=agent_id,
            )
            return json.dumps({"status": "ok", "canvas": entry})
        except ValueError as e:
            return json.dumps({"status": "error", "message": str(e)})

    tools["create_canvas"] = ToolInfo(
        name="create_canvas",
        handler=_create_canvas_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "URL-safe identifier for the canvas (e.g. 'dashboard', 'notes'). Lowercase, no spaces.",
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable display name for the canvas (e.g. 'My Dashboard').",
                },
                "agent_context": {
                    "type": "string",
                    "description": (
                        "Optional system prompt / persona for this canvas's agent. "
                        "Describes the agent's role and the canvas's purpose. "
                        "If omitted, a default context is generated from the title."
                    ),
                },
                "initial_html": {
                    "type": "string",
                    "description": "Optional initial HTML to seed the canvas with. If omitted, a blank placeholder is used.",
                },
            },
            "required": ["slug", "title"],
        },
    )

    # -- delete_canvas ---------------------------------------------------------
    async def _delete_canvas_wrapper(slug: str):
        ok = await _delete_canvas(user_id=user_id, slug=slug)
        if ok:
            return json.dumps({"status": "ok", "message": "Canvas '{}' deleted.".format(slug)})
        if slug == "home":
            return json.dumps({"status": "error", "message": "The home canvas cannot be deleted."})
        return json.dumps({"status": "error", "message": "Canvas '{}' not found.".format(slug)})

    tools["delete_canvas"] = ToolInfo(
        name="delete_canvas",
        handler=_delete_canvas_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the canvas to delete. The 'home' canvas cannot be deleted.",
                },
            },
            "required": ["slug"],
        },
    )

    # -- rename_canvas ---------------------------------------------------------
    async def _rename_canvas_wrapper(slug: str, title: str):
        ok = await _rename_canvas(user_id=user_id, slug=slug, new_title=title)
        if ok:
            return json.dumps({"status": "ok", "message": "Canvas '{}' renamed to '{}'.".format(slug, title)})
        return json.dumps({"status": "error", "message": "Canvas '{}' not found.".format(slug)})

    tools["rename_canvas"] = ToolInfo(
        name="rename_canvas",
        handler=_rename_canvas_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the canvas to rename (e.g. 'dashboard', 'notes').",
                },
                "title": {
                    "type": "string",
                    "description": "New human-readable display title for the canvas.",
                },
            },
            "required": ["slug", "title"],
        },
    )

    # -- get_canvas_data -------------------------------------------------------
    # Read a canvas's DATA bag (its content — the records the page renders),
    # which lives in data.json separately from the page markup. Read this before
    # set_canvas_data when you only want to change part of the content.
    async def _get_canvas_data_wrapper(slug: str):
        data = await _get_canvas_data(user_id=user_id, slug=slug)
        return json.dumps({"status": "ok", "slug": slug, "data": data or {}})

    tools["get_canvas_data"] = ToolInfo(
        name="get_canvas_data",
        handler=_get_canvas_data_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the canvas whose data bag to read (e.g. 'home', 'dashboard').",
                },
            },
            "required": ["slug"],
        },
    )

    # -- set_canvas_data -------------------------------------------------------
    # Write a canvas's DATA bag WITHOUT touching its page markup — this is how you
    # update a dashboard's content (add a student, move a lesson, change a row).
    # The page reads it via api.getData(); the change shows on next load/refresh.
    # `merge=true` shallow-merges your top-level keys into the existing data
    # (change one section, leave the rest); `merge=false` (default) replaces the
    # whole bag. PREFER this over render_visual/edit_canvas for data-only changes.
    async def _set_canvas_data_wrapper(slug: str, data=None, merge: bool = False, **_extra):
        if not isinstance(data, dict):
            return json.dumps({"status": "error", "message": "`data` must be a JSON object."})
        if merge:
            existing = await _get_canvas_data(user_id=user_id, slug=slug)
            base = dict(existing) if isinstance(existing, dict) else {}
            base.update(data)
            data = base
        await _save_canvas_data(user_id=user_id, slug=slug, data=data)
        return json.dumps({"status": "ok", "slug": slug, "merged": bool(merge), "keys": list(data.keys())})

    tools["set_canvas_data"] = ToolInfo(
        name="set_canvas_data",
        handler=_set_canvas_data_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the canvas whose data to write (e.g. 'home', 'dashboard').",
                },
                "data": {
                    "type": "object",
                    "description": (
                        "The canvas's content as a JSON object (e.g. "
                        "{\"students\": [...], \"lessons\": {...}}). The page reads "
                        "these via api.getData(). Use the SAME key names the page expects."
                    ),
                },
                "merge": {
                    "type": "boolean",
                    "description": (
                        "If true, shallow-merge these top-level keys into the existing "
                        "data (change one section, keep the rest). If false (default), "
                        "replace the entire data bag."
                    ),
                },
            },
            "required": ["slug", "data"],
        },
    )

    # -- get_canvas_logs -------------------------------------------------------
    # Read the page's OWN console output — the console.log/info/warn/error and
    # uncaught script errors the canvas produced while running, captured per-page in a
    # file beside the canvas (NOT the global logs.db). A page-scoped debug log the agent
    # reads with no codebase-admin access: build a canvas, then read its errors and fix
    # them. Auto-cleared on every re-render, so it reflects the version now running.
    # TWO sources fill it: (a) a LIVE user session in the Canvas tab (entries arrive a
    # moment after the page mounts; includes errors from the user's own clicks), and
    # (b) screenshot_canvas's HEADLESS render (tagged source:'headless') — so calling
    # screenshot_canvas after a build populates these logs immediately, no live session
    # needed. Each entry carries `source` ('headless' or absent = live).
    async def _get_canvas_logs_wrapper(slug: str, level: str = None, limit: int = 100):
        try:
            lim = int(limit) if limit else 100
        except (TypeError, ValueError):
            lim = 100
        lim = max(1, min(lim, 500))
        lvl = (level or "").strip().lower() or None
        logs = _read_canvas_logs(user_id=user_id, slug=slug, limit=lim, level=lvl)
        n_err = sum(1 for r in logs if str(r.get("level")) == "error")
        n_warn = sum(1 for r in logs if str(r.get("level")) == "warn")
        note = (
            "Empty means nothing has run+logged for this version yet. Either render it "
            "with screenshot_canvas (populates these logs headlessly, source:'headless') "
            "or have the user open it in the Canvas tab, then read again." if not logs else
            "Newest entries last. Each: {ts, level, text, stack?, source?}. source:'headless' "
            "is from a screenshot_canvas render; no source = a live user session. A 'Cannot "
            "read properties of null' is almost always the shadow-scope bug — query through "
            "the mount `root`, not document.*."
        )
        return json.dumps({
            "status": "ok",
            "slug": slug,
            "count": len(logs),
            "errors": n_err,
            "warnings": n_warn,
            "logs": logs,
            "note": note,
        })

    tools["get_canvas_logs"] = ToolInfo(
        name="get_canvas_logs",
        handler=_get_canvas_logs_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the canvas whose console output to read (e.g. 'home', 'dashboard').",
                },
                "level": {
                    "type": "string",
                    "description": (
                        "Optional. Filter to one level: 'error', 'warn', 'log', 'info', or "
                        "'debug'. Omit to get all levels. Use 'error' to see only failures."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of most-recent entries to return (default 100, max 500).",
                },
            },
            "required": ["slug"],
        },
    )

    # -- check_credential ------------------------------------------------------
    # Lets a canvas be "linked to the vault": the design agent asks whether the
    # user has already connected an ability (e.g. browser_control / a login) and
    # what fields a connect form should collect — so it can render "Connected ✓"
    # vs a login form, WITHOUT ever seeing any secret value. Reads the same
    # encrypted vault the Abilities → Credentials panel writes to.
    async def _check_credential_wrapper(ability: str = "browser_control"):
        from app.abilities import credentials as _creds
        view = await _creds.public_view(ability, user_id=user_id)
        if view is None:
            return json.dumps({
                "status": "error",
                "message": "Ability '{}' has no credentials block to link to.".format(ability),
            })
        # public_view already excludes secret VALUES — pass through only the
        # safe shape (configured flag, field schema, which secrets are set).
        return json.dumps({
            "status": "ok",
            "ability": ability,
            "configured": view.get("configured", False),
            "fields": [
                {
                    "key": f.get("key"),
                    "label": f.get("label", ""),
                    "type": f.get("type", "text"),
                    "secret": bool(f.get("secret")),
                    "placeholder": f.get("placeholder", ""),
                }
                for f in (view.get("fields") or [])
            ],
            "secrets_set": view.get("secrets_set", {}),
        })

    tools["check_credential"] = ToolInfo(
        name="check_credential",
        handler=_check_credential_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "ability": {
                    "type": "string",
                    "description": (
                        "Ability whose vault credential the canvas links to "
                        "(e.g. 'browser_control'). Returns whether the user has "
                        "connected it and the fields a login/connect form should "
                        "collect — never any secret value."
                    ),
                },
            },
            "required": [],
        },
    )

    # -- request_credential ----------------------------------------------------
    # Ask the USER for a secret (an API key, a token) WITHOUT ever seeing it. This
    # reserves a vault slot under the signed-in user and surfaces a secure card in
    # the chat: the user types the value there and it saves STRAIGHT to the vault —
    # it never comes back to you. You only get a stable `key_id` to wire into the
    # dashboard you're building. The dashboard then uses the secret by id through
    # the server-side proxy (api.callWithKey), so the plaintext never reaches the
    # page either. Re-calling with the same key_id just re-describes/re-prompts;
    # it never wipes an already-saved secret.
    async def _request_credential_wrapper(name: str, service_url: str = "",
                                          attach: str = "bearer", fields=None,
                                          key_id: str = None, **_extra):
        from app.abilities import vault_store
        meta = await vault_store.reserve_key(
            user_id,
            name=name,
            binding={"base_url": service_url, "attach": attach},
            fields=fields,
            key_id=key_id,
        )
        if not meta:
            return json.dumps({
                "status": "error",
                "message": "Could not reserve the vault key (is a user signed in?).",
            })
        bound = bool(str(meta.get("service") or "").strip())
        message = (
            "A secure entry card is now showing in the chat. The user types the "
            "secret there and it saves straight to the vault — you will NEVER see "
            "the value. Use key id '{kid}' in the dashboard; make its calls with "
            "api.callWithKey('{kid}', {{ path, method, ... }}) so the secret is added "
            "server-side. Re-check whether it's filled with list_vault_keys."
        ).format(kid=meta["key_id"])
        out = {
            "status": "ok",
            "ui": "vault_credential_form",   # the chat panel renders a secure entry card
            "key_id": meta["key_id"],
            "name": meta["name"],
            "fields": meta["fields"],
            "service": meta["service"],
            "filled": meta["filled"],
        }
        if not bound:
            # No service_url means the key is NOT usable yet: api.callWithKey fails
            # closed ("no service URL bound") until you bind it. This is the #1 way a
            # built dashboard ends up dead. Make it loud and tell the agent the fix.
            out["warning"] = (
                "This key has NO service_url, so the dashboard CANNOT use it yet — "
                "api.callWithKey will fail until you bind it. Once you know the exact "
                "service (e.g. Gmail → 'https://gmail.googleapis.com'), call "
                "request_credential AGAIN with the same name/key_id PLUS service_url. "
                "Re-calling keeps any value the user already typed; it only adds the "
                "binding. Do NOT wire api.callWithKey to an unbound key."
            )
            message = (
                "Card is showing — the user's secret saves straight to the vault (you "
                "never see it). BUT this key has no service_url yet, so it can't be "
                "used. " + out["warning"]
            )
        out["message"] = message
        return json.dumps(out)

    tools["request_credential"] = ToolInfo(
        name="request_credential",
        handler=_request_credential_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Human label for the secret, shown to the user on the card "
                        "(e.g. 'Gmail API Key', 'Stripe Secret Key'). Also the basis "
                        "for the key id if you don't pass one."
                    ),
                },
                "service_url": {
                    "type": "string",
                    "description": (
                        "The base URL of the service this key calls (e.g. "
                        "'https://www.googleapis.com'). The key is LOCKED to this "
                        "destination — the dashboard can only ever use it to call "
                        "URLs under this base, so the secret can't leak elsewhere. "
                        "Required for the dashboard to actually USE the key via the "
                        "proxy."
                    ),
                },
                "attach": {
                    "type": "string",
                    "description": (
                        "How the secret attaches to outbound calls: 'bearer' "
                        "(Authorization: Bearer <secret>, the default), 'basic' "
                        "(Authorization: Basic <secret>), 'header:<Name>' (e.g. "
                        "'header:X-Api-Key'), or 'query:<param>' (e.g. 'query:key')."
                    ),
                },
                "fields": {
                    "type": "array",
                    "description": (
                        "Optional. The field(s) the card collects. Default is a "
                        "single secret field. Use several for multi-part credentials "
                        "(e.g. a username + password). Each: {key, label, secret}."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "Field id (e.g. 'api_key')."},
                            "label": {"type": "string", "description": "Label shown on the card."},
                            "secret": {"type": "boolean", "description": "True = masked + never returned (default true)."},
                        },
                        "required": ["key"],
                    },
                },
                "key_id": {
                    "type": "string",
                    "description": (
                        "Optional explicit key id (slug). Omit to derive one from the "
                        "name. Pass an existing id to re-prompt for / update that key "
                        "without wiping the stored secret."
                    ),
                },
            },
            "required": ["name"],
        },
    )

    # -- list_vault_keys -------------------------------------------------------
    # See what the user has in the vault — id, name, bound service, and whether
    # it's been filled in yet — so you can REUSE an existing key instead of asking
    # again, or confirm a requested key is now filled before relying on it. Never
    # returns any secret value.
    async def _list_vault_keys_wrapper():
        from app.abilities import vault_store
        keys = await vault_store.list_keys(user_id)
        return json.dumps({"status": "ok", "keys": keys, "count": len(keys)})

    tools["list_vault_keys"] = ToolInfo(
        name="list_vault_keys",
        handler=_list_vault_keys_wrapper,
        parameters={"type": "object", "properties": {}, "required": []},
    )