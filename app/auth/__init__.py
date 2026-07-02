"""Auth router — login with remember-me + recall endpoint."""

import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth.jwt import create_access_token, decode_token
from app.auth.users import (
    admin_exists,
    authenticate,
    set_remember_token,
    clear_remember_token,
    resolve_remember_token,
    get_user,
    get_user_by_id,
    register_user,
    update_user,
    change_password,
    delete_user_self,
    set_session_policy,
    clamp_session_lifetime,
    DEFAULT_SESSION_LIFETIME_MINUTES,
    UpdateConflict,
)
from app.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_BOOTSTRAP_ADMIN_ID = "admin"


class LoginRequest(BaseModel):
    email: str
    password: str
    remember_me: bool = False


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    user_id: str
    display_name: str
    remember_token: str = ""


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str = ""


class RegisterResponse(BaseModel):
    access_token: str = ""
    token_type: str = "bearer"
    username: str
    user_id: str
    display_name: str
    pending_approval: bool = False


class RecallRequest(BaseModel):
    remember_token: str


class RecallResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    user_id: str
    display_name: str


class AnonymousRequest(BaseModel):
    browser_id: str | None = None


class AnonymousResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str


@router.post("/anonymous", response_model=AnonymousResponse)
async def create_anonymous_session(req: AnonymousRequest):
    """Issue a JWT for an anonymous visitor on the main page.

    Does not require an agent_id (unlike the per-agent anon-session
    endpoint), so any page visitor can get a token and start chatting.
    The identity is persisted as a channel_identity (web_anonymous) so
    sessions and interactions are properly associated.
    """
    import uuid as _uuid_mod
    from app.communications.auth import get_or_create_identity

    browser_id = req.browser_id or _uuid_mod.uuid4().hex
    identity = await get_or_create_identity(channel="web_anonymous", external_id=browser_id)

    token = create_access_token(username=identity.user_id, user_id=identity.user_id)
    return AnonymousResponse(access_token=token, user_id=identity.user_id)


def _record_auth_event(level: str, message: str, user_id: str | None = None) -> None:
    """Write a sign-in/security event into the durable diagnostics store
    (category ``auth``). Feeds the admin Dashboard's "Security & sign-ins"
    card — sign-ins, failed attempts and registrations become queryable via
    ``query_diagnostics(categories=["auth"])``. Best-effort, never raises."""
    try:
        from app.agent.diagnostics import record
        record(level, "auth", message, user_id=user_id, persist=True)
    except Exception:
        pass


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    """Authenticate with username and password.

    If remember_me is True, a persistent remember token is generated
    (invalidates any previous one). Returns it alongside the JWT.
    """
    user = authenticate(req.email, req.password)
    if user is None:
        _record_auth_event("warning", f"Failed sign-in for {req.email}")
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_approved:
        _record_auth_event("warning", f"Sign-in blocked (pending approval): {req.email}")
        raise HTTPException(status_code=403, detail="Account pending admin approval")
    _record_auth_event("info", f"Sign-in: {user.username}", user_id=user.user_id)

    # Mint the pass for the user's own chosen lifetime (Manage Account →
    # Sign-in & sessions; defaults to 30 days for accounts that haven't set one).
    token = create_access_token(
        user.username, user.user_id,
        expires_minutes=user.session_lifetime_minutes,
    )
    resp = LoginResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
    )

    # Issue a renewal ticket if the user ticked "Remember me" OR has auto-renew
    # on as a standing account preference — either way the silent refresh then
    # keeps the session alive so it never dies in place at the lifetime mark.
    if req.remember_me or user.auto_renew:
        remember = set_remember_token(req.email)
        if remember:
            resp.remember_token = remember

    # Track user login: create/update user_profiles row
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    await db.upsert_user_profile(user.user_id, last_login_at=now_iso)

    return resp


@router.post("/recall", response_model=RecallResponse)
async def recall(req: RecallRequest):
    """Exchange a valid remember token for a fresh JWT (auto-login).

    Called when user returns and the JWT has expired / doesn't exist yet.
    Returns a fresh access_token.
    """
    user = resolve_remember_token(req.remember_token)
    if user is None:
        _record_auth_event("warning", "Auto-login failed (invalid or expired remember token)")
        raise HTTPException(status_code=401, detail="Invalid or expired remember token")
    if not user.is_approved:
        raise HTTPException(status_code=403, detail="Account pending admin approval")

    token = create_access_token(
        user.username, user.user_id,
        expires_minutes=user.session_lifetime_minutes,
    )
    resp = RecallResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
    )

    # Track auto-login from remember token
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    await db.upsert_user_profile(user.user_id, last_login_at=now_iso)

    return resp


@router.post("/register", response_model=RegisterResponse)
async def register(req: RegisterRequest):
    """Register a new user account.

    Username must be unique. On success, returns a JWT (auto-login).
    Honors the app's access_mode:
      - admin_approval:   account created but unapproved; login blocked until admin approves
      - public_registered: account created and auto-approved (must still sign in)
      - open:             auto-admin localhost mode; normal registration still auto-approves
    """
    from app.admin.settings import get_access_mode as _gam
    mode = _gam()
    auto_approve = (mode != "admin_approval")
    user = register_user(req.email, req.password, req.display_name, is_approved=auto_approve)
    if user is None:
        raise HTTPException(status_code=409, detail="Email already registered")
    _record_auth_event("info", f"New account registered: {user.username}"
                       + ("" if auto_approve else " (pending approval)"), user_id=user.user_id)

    if not auto_approve:
        # Created but unapproved — no token issued, client must wait for admin
        return RegisterResponse(
            access_token="",
            username=user.username,
            user_id=user.user_id,
            display_name=user.display_name,
            pending_approval=True,
        )

    token = create_access_token(user.username, user.user_id)
    resp = RegisterResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
        pending_approval=False,
    )

    # Track new user registration (first login)
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    await db.upsert_user_profile(user.user_id, last_login_at=now_iso)

    return resp


class AccessModeResponse(BaseModel):
    access_mode: str  # admin_approval | public_registered | open


@router.get("/access-mode", response_model=AccessModeResponse)
async def access_mode():
    """Public endpoint: returns the current registration/access policy.
    Used by the sign-in modal and chat UI to gate registration and anonymous use.
    """
    from app.admin.settings import get_access_mode as _gam
    return AccessModeResponse(access_mode=_gam())


class UiConfigResponse(BaseModel):
    # Per-theme animated-background choice (background ids or "none").
    background_dark: str
    background_light: str
    # Global border + typography + full theme palette (see
    # app.admin.settings.get_appearance_config). Injected at boot as CSS-variable
    # overrides by ui/shared/js/appearance.js. The palette keys come in
    # _dark / _light pairs and mirror the design-system.css palette blocks.
    border_color_dark: str
    border_color_light: str
    border_width: str
    font_sans: str
    font_mono: str
    accent_dark: str
    accent_light: str
    secondary_dark: str
    secondary_light: str
    success_dark: str
    success_light: str
    warning_dark: str
    warning_light: str
    danger_dark: str
    danger_light: str
    surface_bg_dark: str
    surface_bg_light: str
    surface_panel_dark: str
    surface_panel_light: str
    surface_tint_dark: str
    surface_tint_light: str
    text_dark: str
    text_light: str
    text_muted_dark: str
    text_muted_light: str
    ambient_dark: str
    ambient_light: str
    # Chat bubble overrides (blank = follow theme default; #rrggbb[aa]).
    user_bubble_dark: str
    user_bubble_light: str
    agent_bubble_dark: str
    agent_bubble_light: str
    # Chat composer pill fill (blank = follow theme default; #rrggbb[aa]).
    chat_pill_bg_dark: str
    chat_pill_bg_light: str
    # Theme-independent style knobs (JSON-only; injected by appearance.js).
    radius_scale: str
    shadow_strength: str
    ui_scale: str
    reduce_motion: str
    cursor_glow: str
    # Chat-composer geometry + button knobs (CSS lengths; see app1.css
    # CHAT-PILL-VARS). Injected app-wide by ui/shared/js/appearance.js.
    chat_pill_max_width: str
    chat_pill_radius: str
    chat_pill_padding: str
    chat_pill_min_height: str
    chat_pill_max_height: str
    chat_pill_font_size: str
    chat_pill_attach_size: str
    chat_pill_attach_icon: str
    chat_pill_button_size: str
    chat_pill_button_icon: str
    # Chat panel layout (app-wide defaults; injected by appearance.js).
    #   chat_position        — "right" (default) | "left" (desktop split side)
    #   chat_default_visible — "visible" (default) | "hidden" (first-visit default)
    chat_position: str
    chat_default_visible: str
    # Master on/off for the welcome landing page (ui/splash/splash-page), app-wide.
    # app/main.py serves the landing at / when this is on; the per-device
    # "don't show again" / account preference (wa_seen_splash cookie) layers on top.
    splash_enabled: bool
    # Master on/off for the in-app tour / hint bubbles (ui/tutorials), app-wide.
    # The tutorial module reads this; the per-user "show app tour" preference
    # layers on top, and the account page hides its toggle when this is off.
    hints_enabled: bool
    # First-class genui (ui/main-panel/genui): render agent-authored genui
    # directly in the app (no sandboxed iframe) with real-page powers — live
    # webcam/mic, direct agent calls. A genui runs with the VIEWER's OWN app
    # trust (it is per-user — a person only ever sees their own genui, in their
    # own browser/session), so this is gated by the Gen UI page's VISIBILITY
    # setting, enforced server-side (app.auth.identity.caller_may_access_page):
    #   • "all"  → everyone, including anonymous guests
    #   • "auth" → any signed-in REGISTERED user (the default — registration
    #              required out of the box, NOT admin-only)
    #   • "off"  → admins only
    # Admins always pass; 'open' single-user/local mode passes everything (local
    # convenience, incl. over a Cloudflare tunnel where the browser can't mint a
    # JWT). The frontend MUST send the caller's auth token on the /ui-config fetch
    # so the gate can see who they are (see _fetchGenuiGate in autoagent.js). The
    # genui HTTP endpoints (app/api/genui.py) re-check this SAME gate + per-user
    # ownership, so a hidden tab can't be bypassed by calling the API directly.
    genui_first_class: bool
    # Whether signed-in users may set their own theme (App Settings → Appearance →
    # "Allow per-user themes"). The account page (ui/shared/js/account.js) shows
    # its "My appearance" editor only when this is True; this same endpoint also
    # layers the caller's saved per-user overrides on top of the palette/background
    # fields below when it is True. Default False (the app behaves exactly as
    # before — one global theme).
    allow_user_appearance: bool


@router.get("/ui-config", response_model=UiConfigResponse)
async def ui_config(request: Request):
    """Public endpoint: app-wide UI config every visitor needs before render.

    Carries the admin's per-theme animated-background choice (so the background
    engine ui/background/_engine/manager.js renders the right one) plus the
    global border + font appearance knobs (so ui/shared/js/appearance.js can
    recolour/resize borders and swap the UI font app-wide) — for anonymous and
    signed-in visitors alike. Also reports whether first-class genui are
    permitted (local-only safety gate; see app/visualizer + the Gen UI tab).
    """
    from app.admin.settings import (
        get_background_config,
        get_appearance_config,
        get_access_mode,
        get_splash_enabled,
        get_hints_enabled,
        get_allow_user_appearance,
        valid_background,
    )
    cfg = get_background_config()
    app = get_appearance_config()
    bg_dark, bg_light = cfg["dark"], cfg["light"]
    # Whether THIS caller may run first-class (full-trust) genui is now driven
    # by the Gen UI page's VISIBILITY setting, enforced server-side — not a
    # separate hardcoded admin-only gate. caller_may_access_page resolves the
    # caller from the token the frontend sends and applies the 3-state rule:
    #   • Gen UI "all"  → everyone, incl. anonymous guests
    #   • Gen UI "auth" → any signed-in registered user (the default — so genui
    #                     is registration-required out of the box, not admin-only)
    #   • Gen UI "off"  → admins only
    # Admins always pass, and 'open' single-user/local mode passes everything
    # (local convenience, incl. over a tunnel where the browser can't mint a JWT).
    from app.auth.identity import request_user_id, caller_may_access_page
    caller_uid = request_user_id(request)
    genui_first_class = await caller_may_access_page(request, "main", "genui")
    # Per-user theme: when the admin has enabled it, layer the signed-in caller's
    # saved overrides on top of the global appearance, so the boot-time applier
    # (ui/shared/js/appearance.js) needs NO change — it just receives an already
    # personalised theme. A blank/absent token keeps the global value (fall back
    # to global); the background is re-validated against installed plugins.
    allow_user_appearance = get_allow_user_appearance()
    if allow_user_appearance and caller_uid:
        try:
            from app.db import get_db
            db = get_db()
            overrides = {}
            if hasattr(db, "get_user_appearance"):
                overrides = await db.get_user_appearance(caller_uid) or {}
            for key, val in overrides.items():
                if key in app and isinstance(val, str) and val.strip():
                    app[key] = val.strip()
            bg_dark = valid_background(overrides.get("background_dark"), bg_dark)
            bg_light = valid_background(overrides.get("background_light"), bg_light)
        except Exception:  # noqa: BLE001 — per-user theme must never break boot config
            pass
    # `app` (get_appearance_config) carries every appearance key — the border /
    # font knobs and the full per-theme palette — each concrete (never blank).
    # Spread it so newly-added palette tokens flow through with no per-key wiring.
    return UiConfigResponse(
        background_dark=bg_dark,
        background_light=bg_light,
        genui_first_class=genui_first_class,
        splash_enabled=get_splash_enabled(),
        hints_enabled=get_hints_enabled(),
        allow_user_appearance=allow_user_appearance,
        **app,
    )


@router.get("/backgrounds")
async def list_backgrounds_public():
    """Public: the installed animated-background catalog (id, name, status).

    The per-user theme editor (account page → ui/shared/js/appearance-me.js) needs
    this to populate its background selects; the admin equivalent lives under
    /admin/settings/backgrounds. The catalog is just plugin display names, so it
    is not gated."""
    try:
        from app.ui_backgrounds import catalog as _bg_catalog
        return {"backgrounds": _bg_catalog()}
    except Exception:  # noqa: BLE001
        return {"backgrounds": []}


class SetupStatusResponse(BaseModel):
    initialized: bool


class SetupAdminRequest(BaseModel):
    password: str
    display_name: str = ""
    username: str = "admin"


class SetupAdminResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    user_id: str
    display_name: str


@router.get("/setup-status", response_model=SetupStatusResponse)
async def setup_status():
    """Public endpoint: returns whether an admin account exists."""
    return SetupStatusResponse(initialized=admin_exists())


@router.post("/setup-admin", response_model=SetupAdminResponse)
async def setup_admin(req: SetupAdminRequest, request: Request):
    """Create the first admin account.

    Open to ANY first visitor (local or remote) — the only guard is that no admin
    exists yet. The ``admin_exists()`` check below closes the door for good the
    instant the first admin is created, so this is strictly a one-time,
    first-visitor action. There is deliberately NO localhost restriction: a fresh
    install (including a freshly-deployed cloud server, whose own browser is
    remote) must always be able to reach this page to set the very first password."""
    if admin_exists():
        raise HTTPException(status_code=400, detail="Admin already exists")

    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    # The operator may choose any login name (or leave the default "admin").
    # The canonical admin user_id stays "admin" so every downstream admin gate
    # (DB is_admin seed, open-mode fallback, delete protection) keeps working.
    login_name = (req.username or "").strip() or "admin"
    user = register_user(
        login_name, req.password, req.display_name or "Admin",
        user_id=_BOOTSTRAP_ADMIN_ID,
    )
    if user is None:
        raise HTTPException(status_code=409, detail="That username is already taken")

    from app.auth.jwt import create_access_token

    token = create_access_token(user.username, user.user_id)

    # Track setup as first login
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    await db.upsert_user_profile(user.user_id, last_login_at=now_iso)

    return SetupAdminResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
    )


class SetupBundleRequest(BaseModel):
    code: str
    # The source install's admin password — decrypts the code AND becomes this
    # install's admin password (it also rides inside the bundle's admin section).
    password: str


@router.post("/setup-bundle", response_model=SetupAdminResponse)
async def setup_bundle(req: SetupBundleRequest):
    """Provision a FRESH install from a pasted setup code, on the setup page.

    Same one-time gate as ``setup_admin`` (refused once an admin exists). The
    password both decrypts the bundle and becomes the admin login — so one typed
    value unlocks the keys AND claims the box. Applies DB/vault/LLM/admin and
    returns a token so the setup page logs straight in."""
    if admin_exists():
        raise HTTPException(status_code=400, detail="Admin already exists")

    from app.admin import bootstrap_bundle as bb
    try:
        bundle = bb.decode_bundle(req.code, req.password)
    except bb.BundleError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Fresh box → apply everything and switch the DB on. Sections use overwrite so
    # the bundle wins outright on a blank install.
    sections = (bundle.get("sections") or {})
    choices = {k: bb.MERGE_OVERWRITE for k in sections}
    await bb.apply_bundle(bundle, choices, activate_db=True)

    # Guarantee an admin exists even if the bundle didn't carry one: the password
    # they typed becomes the admin password.
    if not admin_exists():
        register_user("admin", req.password, "Admin", user_id=_BOOTSTRAP_ADMIN_ID)

    user = get_user_by_id(_BOOTSTRAP_ADMIN_ID)
    if user is None:
        raise HTTPException(status_code=500, detail="Setup failed to create an admin account")

    from app.auth.jwt import create_access_token
    token = create_access_token(user.username, user.user_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    await db.upsert_user_profile(user.user_id, last_login_at=now_iso)

    return SetupAdminResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
    )


class GuestLoginRequest(BaseModel):
    browser_id: str | None = None


class GuestLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    user_id: str
    display_name: str


@router.post("/guest-login", response_model=GuestLoginResponse)
async def guest_login(request: Request, req: GuestLoginRequest | None = None):
    """Mint an anonymous guest session — only in 'public_registered' (Open
    Registration) access mode.

    Open Registration means the app is open to anonymous visitors: anyone may
    browse and chat without first creating an account, and whether a *given*
    agent will actually talk to a guest is decided per-agent (its `user_mode`),
    NOT at the app level. This endpoint is what lets that work: it hands a
    tokenless visitor a persistent anonymous identity — a `web_public` guest
    keyed to their browser id, so the same browser keeps the same guest (and its
    sessions) across reloads — plus a JWT so the WebSocket handshake and the chat
    gate accept them. It is the app-level sibling of the per-agent public-URL
    flow (`POST /api/v1/agents/{id}/anon-session`), which mints the same kind of
    `web_public` identity for a single public agent page.

    Gated to 'public_registered' only. The stricter 'admin_approval' mode
    requires a real approved account, so a guest token is refused (403) there.
    """
    import uuid as _uuid_mod
    from app.admin.settings import get_access_mode as _gam
    if _gam() != "public_registered":
        raise HTTPException(status_code=403, detail="Guest access is not enabled")

    browser_id = (req.browser_id if req else None) or _uuid_mod.uuid4().hex
    from app.communications.auth import get_or_create_identity
    identity = await get_or_create_identity(channel="web_public", external_id=browser_id)

    token = create_access_token(username=identity.user_id, user_id=identity.user_id)

    return GuestLoginResponse(
        access_token=token,
        username=identity.user_id,
        user_id=identity.user_id,
        display_name=identity.display_name or "Guest",
    )


@router.post("/logout")
async def logout(req: LoginRequest | None = None):
    """Placeholder — actual token clearing is client-side (localStorage).
    The remember token stays valid until the user logs in again.
    No server-side action needed; the client clears its storage.
    """
    return {"status": "ok", "message": "Logged out. Clear client tokens."}


# ── Self-service profile management ─────────────────────────────────────────

class MeResponse(BaseModel):
    username: str
    user_id: str
    display_name: str
    is_approved: bool


class UpdateMeRequest(BaseModel):
    email: str | None = None
    display_name: str | None = None


class UpdateMeResponse(BaseModel):
    username: str
    user_id: str
    display_name: str
    is_approved: bool
    access_token: str  # fresh JWT (sub claim may have changed)
    token_type: str = "bearer"


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ChangePasswordResponse(BaseModel):
    status: str = "ok"
    remember_token: str = ""  # rotated; client should replace any cached copy


class DeleteMeRequest(BaseModel):
    password: str


def _require_auth(request: Request) -> tuple[str, str]:
    """Decode the Bearer JWT from the Authorization header.

    AuthMiddleware is not registered in main.py, so each route resolves the
    caller from the header directly. Returns (username, user_id).
    """
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not token:
        token = request.query_params.get("token", "")
    payload = decode_token(token) if token else None
    if not payload:
        raise HTTPException(status_code=401, detail="Not authenticated")
    username = payload.get("sub") or ""
    user_id = payload.get("user_id") or ""
    if not username or not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username, user_id


@router.get("/me", response_model=MeResponse)
async def get_me(request: Request):
    """Return the currently authenticated user's profile."""
    username, _user_id = _require_auth(request)
    user = get_user(username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return MeResponse(
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
        is_approved=user.is_approved,
    )


@router.patch("/me", response_model=UpdateMeResponse)
async def patch_me(request: Request, body: UpdateMeRequest):
    """Update the current user's email (username) and/or display name.

    Returns a fresh JWT because the `sub` claim may have changed.
    """
    current_username, _user_id = _require_auth(request)
    user = get_user(current_username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    new_email = (body.email or "").strip() or None
    new_display = body.display_name if body.display_name is not None else None

    if new_email is not None and "@" not in new_email:
        raise HTTPException(status_code=400, detail="Invalid email address")

    try:
        updated = update_user(
            current_username,
            new_username=new_email,
            new_display_name=new_display,
        )
    except UpdateConflict:
        raise HTTPException(status_code=409, detail="Email already in use")

    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")

    token = create_access_token(
        updated.username, updated.user_id,
        expires_minutes=updated.session_lifetime_minutes,
    )
    return UpdateMeResponse(
        username=updated.username,
        user_id=updated.user_id,
        display_name=updated.display_name,
        is_approved=updated.is_approved,
        access_token=token,
    )


@router.post("/change-password", response_model=ChangePasswordResponse)
async def post_change_password(request: Request, body: ChangePasswordRequest):
    """Change the authenticated user's password.

    Verifies the current password, writes a new bcrypt hash, and rotates the
    remember token so any previously-issued remember credential is invalidated.
    """
    username, _user_id = _require_auth(request)

    if not body.new_password or len(body.new_password) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters")

    verified = authenticate(username, body.current_password)
    if verified is None:
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    ok = change_password(username, body.new_password)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")

    # Rotate remember token so old persistent credentials can't auto-login.
    new_remember = set_remember_token(username) or ""
    return ChangePasswordResponse(status="ok", remember_token=new_remember)


@router.delete("/me")
async def delete_me(request: Request, body: DeleteMeRequest):
    """Delete the authenticated user's account.

    Requires password confirmation. The bootstrap `admin` user
    cannot be deleted (returns 403).
    """
    username, _user_id = _require_auth(request)

    verified = authenticate(username, body.password)
    if verified is None:
        raise HTTPException(status_code=401, detail="Password is incorrect")

    ok, reason = delete_user_self(username)
    if not ok:
        if reason == "protected":
            raise HTTPException(status_code=403, detail="The bootstrap admin account cannot be deleted")
        raise HTTPException(status_code=404, detail="User not found")

    return {"status": "ok"}


# ── Per-user session policy (Manage Account → Sign-in & sessions) ────────────
# Each user controls how long their own login pass lasts and whether it silently
# auto-renews. Stored on the user record (app.auth.users). The lifetime is read
# at every token mint (login / recall / profile-edit) so a change takes effect on
# the next pass; the PUT below also re-mints the caller's current pass + issues or
# clears the renewal ticket so the change applies immediately, no re-login needed.

class SessionPolicyResponse(BaseModel):
    lifetime_minutes: int
    auto_renew: bool


class UpdateSessionPolicyRequest(BaseModel):
    lifetime_minutes: int
    auto_renew: bool


class UpdateSessionPolicyResponse(BaseModel):
    lifetime_minutes: int
    auto_renew: bool
    access_token: str           # fresh pass minted for the new lifetime
    token_type: str = "bearer"
    remember_token: str = ""     # new ticket when auto-renew on; "" when off


@router.get("/me/session-policy", response_model=SessionPolicyResponse)
async def get_my_session_policy(request: Request):
    """Return the caller's session policy (login-pass lifetime + auto-renew)."""
    username, _user_id = _require_auth(request)
    user = get_user(username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return SessionPolicyResponse(
        lifetime_minutes=user.session_lifetime_minutes,
        auto_renew=user.auto_renew,
    )


@router.put("/me/session-policy", response_model=UpdateSessionPolicyResponse)
async def put_my_session_policy(request: Request, body: UpdateSessionPolicyRequest):
    """Update the caller's session policy and apply it immediately.

    Clamps the lifetime to the allowed range, saves it, then re-mints the
    caller's current pass for the new lifetime. When auto-renew is on a fresh
    renewal ticket is issued (so the silent refresh can keep the session alive);
    when off the ticket is cleared (the session simply ends at the lifetime).
    """
    username, _user_id = _require_auth(request)
    user = get_user(username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    updated = set_session_policy(
        username,
        clamp_session_lifetime(body.lifetime_minutes),
        bool(body.auto_renew),
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")

    token = create_access_token(
        updated.username, updated.user_id,
        expires_minutes=updated.session_lifetime_minutes,
    )
    remember = ""
    if updated.auto_renew:
        remember = set_remember_token(username) or ""
    else:
        clear_remember_token(username)
    return UpdateSessionPolicyResponse(
        lifetime_minutes=updated.session_lifetime_minutes,
        auto_renew=updated.auto_renew,
        access_token=token,
        remember_token=remember,
    )


# ── Per-user appearance (self-service "My appearance" theme editor) ──────────
# The account page (ui/shared/js/account.js) reads/writes the signed-in user's
# own theme override here. Storage is the user's profile (user_profiles.appearance,
# a sparse JSON patch of the same keys as the global appearance). The override is
# applied at boot by the per-user merge in GET /api/v1/auth/ui-config above, so a
# user's theme follows them across devices. Gated by the admin's
# `allow_user_appearance` flag — when off, saving is refused (403) and the editor
# is hidden.

def _allowed_appearance_keys() -> set:
    """The set of appearance tokens a user may override: every key the global
    appearance carries, plus the two per-theme background ids."""
    from app.admin.settings import _DEFAULT_APPEARANCE
    return set(_DEFAULT_APPEARANCE.keys()) | {"background_dark", "background_light"}


@router.get("/me/appearance")
async def get_my_appearance(request: Request):
    """Return the caller's own theme overrides plus whether the feature is on.

    ``allow`` mirrors the admin's allow_user_appearance flag so the account page
    can decide whether to render its editor; ``overrides`` is the user's sparse
    patch (empty when they follow the global theme)."""
    from app.admin.settings import get_allow_user_appearance
    _username, user_id = _require_auth(request)
    from app.db import get_db
    db = get_db()
    overrides = {}
    if hasattr(db, "get_user_appearance"):
        try:
            overrides = await db.get_user_appearance(user_id) or {}
        except Exception:  # noqa: BLE001
            overrides = {}
    return {"allow": get_allow_user_appearance(), "overrides": overrides}


@router.put("/me/appearance")
async def put_my_appearance(request: Request):
    """Merge a partial theme patch into the caller's own overrides.

    Only known appearance keys are accepted (others are dropped). A key sent with
    a blank value clears that token (the user falls back to the global theme for
    it). Refused with 403 when the admin has not enabled per-user themes."""
    from app.admin.settings import get_allow_user_appearance
    _username, user_id = _require_auth(request)
    if not get_allow_user_appearance():
        raise HTTPException(status_code=403, detail="Per-user themes are disabled")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be an object of appearance keys")
    allowed = _allowed_appearance_keys()
    patch = {k: v for k, v in body.items() if k in allowed and isinstance(v, str)}
    from app.db import get_db
    db = get_db()
    if not hasattr(db, "merge_user_appearance"):
        raise HTTPException(status_code=501, detail="Per-user themes not supported by this backend")
    merged = await db.merge_user_appearance(user_id, patch)
    return {"overrides": merged}


@router.delete("/me/appearance")
async def delete_my_appearance(request: Request):
    """Clear ALL of the caller's theme overrides — they fall back to the global
    theme. Allowed regardless of the admin flag (a reset is always safe)."""
    _username, user_id = _require_auth(request)
    from app.db import get_db
    db = get_db()
    if hasattr(db, "set_user_appearance"):
        try:
            await db.set_user_appearance(user_id, {})
        except Exception:  # noqa: BLE001
            pass
    return {"overrides": {}}
