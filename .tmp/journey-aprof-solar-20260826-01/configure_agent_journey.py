import json
import os
import sys
import time
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright


sys.stdout.reconfigure(encoding="utf-8")
BASE_URL = "http://127.0.0.1:18099"
RUN_ID = "APROF-SOLAR-20260826-01"
DESCRIPTION = (
    "Solara Piano information assistant for prospective visitors, registered "
    "students and event clients, and the agent's own administrators."
)


def iso_now():
    return datetime.now(timezone.utc).isoformat()


def record(steps, step_id, action, expected, actual, result, started, evidence=None, issues=None):
    steps.append({
        "id": step_id,
        "action": action,
        "expected": expected,
        "actual": actual,
        "result": result,
        "timing": {"valueMs": round((time.perf_counter() - started) * 1000), "boundary": "action-to-authoritative-response"},
        "evidence": evidence or [],
        "issues": issues or [],
    })


def request_json(api, method, path, token="", data=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = api.fetch(
        f"{BASE_URL}{path}",
        method=method,
        headers=headers,
        data=json.dumps(data) if data is not None else None,
    )
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text()[:1000]}
    return response.status, body


started_at = iso_now()
steps = []
agent_id = ""
client_username = f"client-{RUN_ID.lower()}@solarapiano.test"
client_password = "Journey-client-123456"

with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
    page = context.new_page()
    page.on("dialog", lambda dialog: dialog.dismiss())
    page_errors = []
    bad_responses = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("response", lambda response: bad_responses.append(
        f"{response.status} {response.url}"
    ) if response.status >= 400 else None)
    login_capture = {}
    login_status = {"value": 0}

    def capture_login(route):
        response = route.fetch()
        login_status["value"] = response.status
        login_capture.update(response.json())
        route.fulfill(response=response)

    page.route("**/api/v1/auth/login", capture_login)

    t0 = time.perf_counter()
    page.goto(f"{BASE_URL}/login.html", wait_until="domcontentloaded")
    page.get_by_label("Email").fill("admin")
    page.get_by_label("Password").fill(os.environ["WA_JOURNEY_ADMIN_PASSWORD"])
    page.get_by_role("button", name="Sign In").click()
    page.wait_for_url("**/", timeout=15_000)
    login_data = login_capture
    token = login_data.get("access_token", "")
    admin_user_id = login_data.get("user_id", "")
    page.screenshot(path=f".tmp/journey-aprof-solar-20260826-01/{RUN_ID}-admin-signed-in.png", full_page=True)
    record(
        steps, "J01-S1", "Sign in through the visible admin login form.",
        "The admin account authenticates and the app returns to its home route.",
        f"HTTP {login_status['value']}; authenticated user {admin_user_id}; URL {page.url}",
        "pass" if login_status["value"] < 300 and token and admin_user_id else "fail", t0,
        [f"{RUN_ID}-admin-signed-in.png"],
    )

    api = context.request
    t0 = time.perf_counter()
    status, existing = request_json(api, "GET", f"/api/v1/agents?user_id={admin_user_id}", token)
    matches = [row for row in existing.get("agents", []) if row.get("name") == "Solara Piano Assistant" and row.get("description") == DESCRIPTION]
    if matches:
        agent = matches[-1]
        created_actual = f"Reused this run's existing agent {agent.get('id')} after idempotency lookup."
    else:
        status, created = request_json(api, "POST", "/api/v1/agents", token, {
            "user_id": admin_user_id,
            "name": "Solara Piano Assistant",
            "description": DESCRIPTION,
            "template_id": "default",
            "user_mode": "anonymous",
            "capability_profile": "advanced",
            "embed": {"enabled": False},
        })
        agent = created.get("agent") or {}
        created_actual = f"HTTP {status}; created agent {agent.get('id')}."
    agent_id = agent.get("id", "")
    record(
        steps, "J01-S2", "Create the Solara Piano Assistant under the signed-in admin.",
        "One persisted agent is created and the creator becomes its agent administrator.",
        created_actual, "pass" if status < 300 and agent_id else "fail", t0,
    )
    if not agent_id:
        raise RuntimeError(f"Agent creation failed: {existing}")

    profile_specs = [
        (
            "visitor", "Visitor", "Anonymous Solara Piano website visitors.",
            {
                "abilities": ["wiki_context"],
                "tools": ["get_time", "get_date", "calculate", "wiki_search", "request_agent_login"],
                "features": ["chat"],
                "limits": {"daily_turns": 10, "monthly_turns": 50, "max_message_chars": 2000},
                "funding": {"mode": "owner_wallet"},
                "chat_ui": {},
                "prompt_overlay": "Explain Solara Piano, lessons, student services, and event services. Invite the visitor to register as a Client for personalized help. Do not perform administrative or write operations.",
            },
        ),
        (
            "member", "Clients", "Agent-registered students and event clients; read-mostly except for their own profile.",
            {
                "abilities": ["wiki_context", "web_access"],
                "tools": ["get_time", "get_date", "calculate", "wiki_search", "web_search", "read_attachment", "request_agent_login"],
                "features": ["chat", "own_profile"],
                "limits": {"daily_turns": 100, "monthly_turns": 2000, "max_message_chars": 8000},
                "funding": {"mode": "member"},
                "chat_ui": {},
                "prompt_overlay": "Serve the signed-in Client with student or event information. Treat other clients' records and all shared configuration as read-only. The Client may change only their own personal profile.",
            },
        ),
        (
            "agent-administrator", "Admin", "Administrators of this Solara Piano agent.",
            {
                "abilities": ["*"], "tools": ["*"], "features": ["*"],
                "limits": {}, "funding": {"mode": "member"}, "chat_ui": {},
                "prompt_overlay": "You are assisting an administrator of this agent. Administrative and write operations are available subject to normal confirmation and safety controls.",
            },
        ),
    ]
    for index, (slug, name, description, policy) in enumerate(profile_specs, start=1):
        t0 = time.perf_counter()
        status, body = request_json(api, "PUT", f"/api/v1/agents/{agent_id}/profiles/{slug}", token, {
            "slug": slug, "name": name, "description": description, "policy": policy,
        })
        actual = body.get("profile") or body
        record(
            steps, f"J02-S{index}", f"Configure the {name} profile.",
            f"The {slug} profile persists with the intended display name and capability policy.",
            f"HTTP {status}; slug={actual.get('slug')}; name={actual.get('name')}",
            "pass" if status < 300 and actual.get("name") == name else "fail", t0,
        )

    t0 = time.perf_counter()
    status, policy_body = request_json(api, "PUT", f"/api/v1/agents/{agent_id}/auth/policy", token, {
        "app_login_enabled": False,
        "app_login_enrollment": "disabled",
        "local_signup_mode": "open",
        "transcript_review": False,
    })
    persisted_policy = policy_body.get("auth_policy") or {}
    record(
        steps, "J02-S4", "Make Client registration agent-native and open; disable app-account enrollment.",
        "New local registrations enter the renamed Clients/member profile without requiring an app account.",
        f"HTTP {status}; local={persisted_policy.get('local_signup_mode')}; app_login={persisted_policy.get('app_login_enabled')}",
        "pass" if status < 300 and persisted_policy.get("local_signup_mode") == "open" and not persisted_policy.get("app_login_enabled") else "fail", t0,
    )

    t0 = time.perf_counter()
    status, profiles_body = request_json(api, "GET", f"/api/v1/agents/{agent_id}/profiles", token)
    names = [item.get("name") for item in profiles_body.get("profiles", [])]
    record(
        steps, "J02-S5", "Reload profiles from the authoritative agent database.",
        "Visitor, Clients, and Admin are returned with no extra custom profile.",
        f"HTTP {status}; profile names={names}",
        "pass" if status < 300 and names == ["Visitor", "Clients", "Admin"] else "fail", t0,
    )

    t0 = time.perf_counter()
    status, register_body = request_json(api, "POST", f"/api/v1/agents/{agent_id}/auth/register", data={
        "username": client_username,
        "password": client_password,
        "display_name": "Journey Client",
    })
    if status == 400 and "already registered" in str(register_body):
        status, register_body = request_json(api, "POST", f"/api/v1/agents/{agent_id}/auth/login", data={
            "username": client_username,
            "password": client_password,
        })
    client_token = register_body.get("token", "")
    client_principal = register_body.get("principal") or {}
    record(
        steps, "J04-S1", "Register a Client directly with the agent credential system.",
        "The identity is stored by the agent and receives the Clients profile without app registration.",
        f"HTTP {status}; profile={((client_principal.get('profile') or {}).get('name'))}; subject is agent-scoped={str(register_body.get('user_id', '')).startswith('agentmember--')}",
        "pass" if status < 300 and client_token and (client_principal.get("profile") or {}).get("name") == "Clients" else "fail", t0,
    )

    t0 = time.perf_counter()
    status, denied = request_json(api, "GET", f"/api/v1/agents/{agent_id}/profiles", client_token)
    record(
        steps, "J04-S2", "Attempt profile administration with the Client token.",
        "The request is denied because Clients are not agent administrators.",
        f"HTTP {status}; detail={denied.get('detail')}",
        "pass" if status == 403 else "fail", t0,
    )

    t0 = time.perf_counter()
    public_access = {
        "enabled": True,
        "funding": {"mode": "owner_wallet", "owner_user_id": admin_user_id},
        "capabilities": {
            "mode": "explicit", "abilities": ["wiki_context"],
            "tools": ["get_time", "get_date", "calculate", "wiki_search", "request_agent_login"],
            "features": ["chat"],
        },
        "usage": {
            "turns_per_agent_per_day": 500,
            "concurrent_runs": 5,
            "tokens_per_guest_per_day": 20000,
            "tokens_per_agent_per_month": 500000,
            "cost_cents_per_agent_per_month": 2500,
        },
        "data": {
            "session_retention_days": 14,
            "max_sessions_per_guest": 5,
            "max_transcript_bytes_per_guest": 1048576,
            "max_total_storage_bytes": 1073741824,
        },
        "chat_ui": {},
    }
    status, embed_update = request_json(api, "PUT", f"/api/v1/agents/{agent_id}", token, {
        "user_id": admin_user_id,
        "user_mode": "anonymous",
        "public_access": public_access,
        "embed": {
            "enabled": True,
            "allowed_domains": [],
            "accent": "#d4a017",
            "title": "Solara Piano Assistant",
            "subtitle": "For visitors, students, and event clients",
            "greeting": "Welcome to Solara Piano. How can I help?",
            "placeholder": "Ask about lessons, students, or events…",
            "launcher_position": "right",
        },
    })
    if status == 409 and (embed_update.get("detail") or {}).get("reason") == "owner_wallet_empty":
        repair_started = time.perf_counter()
        repair_status, repair_body = request_json(api, "POST", f"/admin/users/{admin_user_id}/credits", token, {
            "requesting_user_id": admin_user_id,
            "credit_type": "paid",
            "balance_credits": 100,
        })
        record(
            steps, "J06-S1A", "Allocate the minimum practical owner-wallet sponsorship after the publication gate rejected an empty wallet.",
            "The local admin wallet has 100 available credits for anonymous agent usage.",
            f"HTTP {repair_status}; paid credits={repair_body.get('paid_credits')}",
            "pass" if repair_status < 300 and repair_body.get("paid_credits") == 100 else "fail", repair_started,
        )
        status, embed_update = request_json(api, "PUT", f"/api/v1/agents/{agent_id}", token, {
            "user_id": admin_user_id,
            "user_mode": "anonymous",
            "public_access": public_access,
            "embed": {
                "enabled": True,
                "allowed_domains": [],
                "accent": "#d4a017",
                "title": "Solara Piano Assistant",
                "subtitle": "For visitors, students, and event clients",
                "greeting": "Welcome to Solara Piano. How can I help?",
                "placeholder": "Ask about lessons, students, or events…",
                "launcher_position": "right",
            },
        })
    embed_issue = [] if status < 300 else [str(embed_update.get("detail") or embed_update)]
    record(
        steps, "J06-S1", "Enable the Solara Piano embed with an explicit Visitor policy and owner-wallet funding.",
        "The embed is enabled only if its anonymous usage is explicitly funded.",
        f"HTTP {status}; enabled={((embed_update.get('agent') or {}).get('embed') or {}).get('enabled')}",
        "pass" if status < 300 and ((embed_update.get("agent") or {}).get("embed") or {}).get("enabled") else "fail", t0,
        issues=embed_issue,
    )

    t0 = time.perf_counter()
    descriptor_status, descriptor = request_json(api, "GET", f"/api/v1/agents/{agent_id}/embed")
    snippet = descriptor.get("snippet", "")
    record(
        steps, "J06-S2", "Read the public embed descriptor.",
        "The descriptor names this agent and returns an embeddable script snippet.",
        f"HTTP {descriptor_status}; embeddable={descriptor.get('embeddable')}; snippet returned={bool(snippet)}",
        "pass" if descriptor_status < 300 and descriptor.get("agent_id") == agent_id and snippet else "fail", t0,
    )

    t0 = time.perf_counter()
    guest_status, guest_body = request_json(api, "POST", f"/api/v1/agents/{agent_id}/auth/guest", data={
        "browser_id": "journey-visitor-aprof-solar-20260826-01",
    })
    guest_principal = guest_body.get("principal") or {}
    record(
        steps, "J03-S1", "Enter the embedded agent as an anonymous Visitor.",
        "A funded guest session is minted with the Visitor profile.",
        f"HTTP {guest_status}; profile={((guest_principal.get('profile') or {}).get('name'))}",
        "pass" if guest_status < 300 and (guest_principal.get("profile") or {}).get("name") == "Visitor" else "fail", t0,
    )

    t0 = time.perf_counter()
    page_errors.clear()
    bad_responses.clear()
    page.goto(f"{BASE_URL}/embed/{agent_id}", wait_until="domcontentloaded")
    page.wait_for_timeout(15_000)
    visible_chat = page.locator("#ec-input").is_visible()
    page.locator("#ec-input").fill("Tell me about Solara Piano")
    send_ready = page.locator("#ec-send").is_visible() and page.locator("#ec-send").is_enabled()
    page_title = page.title()
    page.screenshot(path=f".tmp/journey-aprof-solar-20260826-01/{RUN_ID}-embed-visitor.png", full_page=True)
    record(
        steps, "J03-S2", "Open the standalone embedded-agent surface.",
        "The Solara Piano-branded widget becomes visibly usable.",
        f"title={page_title}; composer visible={visible_chat}; drafted send ready={send_ready}; page errors={page_errors}; bad responses={bad_responses}; URL={page.url}",
        "pass" if "Solara Piano Assistant" in page_title and visible_chat and send_ready and not page_errors and not bad_responses else "fail", t0,
        [f"{RUN_ID}-embed-visitor.png"],
    )

    output = {
        "runId": RUN_ID,
        "trajectoryId": "solarapiano-agent-profile-embed",
        "seed": "solarapiano-profiles-20260826",
        "environment": {"baseUrl": BASE_URL, "browser": "Playwright Chromium headless", "browserState": "isolated-cold", "build": "current-working-tree"},
        "startedAt": started_at,
        "finishedAt": iso_now(),
        "result": "pass" if all(step["result"] == "pass" for step in steps) else "fail",
        "cases": [
            {"id": "J01", "result": "pass" if all(s["result"] == "pass" for s in steps if s["id"].startswith("J01")) else "fail", "steps": [s for s in steps if s["id"].startswith("J01")]},
            {"id": "J02", "result": "pass" if all(s["result"] == "pass" for s in steps if s["id"].startswith("J02")) else "fail", "steps": [s for s in steps if s["id"].startswith("J02")]},
            {"id": "J03", "result": "pass" if all(s["result"] == "pass" for s in steps if s["id"].startswith("J03")) else "fail", "steps": [s for s in steps if s["id"].startswith("J03")]},
            {"id": "J04", "result": "pass" if all(s["result"] == "pass" for s in steps if s["id"].startswith("J04")) else "fail", "steps": [s for s in steps if s["id"].startswith("J04")]},
            {"id": "J06", "result": "pass" if all(s["result"] == "pass" for s in steps if s["id"].startswith("J06")) else "fail", "steps": [s for s in steps if s["id"].startswith("J06")]},
        ],
        "metrics": {},
        "artifacts": [f"{RUN_ID}-admin-signed-in.png", f"{RUN_ID}-embed-visitor.png"],
        "limitations": ["The in-app browser service exposed no session, so isolated Playwright Chromium was used."],
        "reproduction": {"agentId": agent_id, "clientUsername": client_username, "embedSnippet": snippet},
    }
    print(json.dumps(output, ensure_ascii=False))
