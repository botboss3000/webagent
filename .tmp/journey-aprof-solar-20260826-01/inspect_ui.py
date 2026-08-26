import os
import sys

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")
BASE_URL = "http://127.0.0.1:18099"


with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1440, "height": 1000},
        service_workers="block",
    )
    page = context.new_page()
    console_events = []
    page_errors = []
    failed_requests = []
    bad_responses = []
    page.on("console", lambda message: console_events.append(f"{message.type}: {message.text}"))
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("requestfailed", lambda request: failed_requests.append(f"{request.method} {request.url}: {request.failure}"))
    page.on("response", lambda response: bad_responses.append(f"{response.status} {response.url}") if response.status >= 400 else None)
    page.goto(f"{BASE_URL}/login.html", wait_until="networkidle")
    page.get_by_label("Email").fill("admin")
    page.get_by_label("Password").fill(os.environ["WA_JOURNEY_ADMIN_PASSWORD"])
    page.get_by_role("button", name="Sign In").click()
    page.wait_for_url("**/", timeout=15_000)
    page.get_by_role("button", name="Skip setup").click()
    page.wait_for_timeout(30_000)
    if page.get_by_role("button", name="Agents", exact=True).is_visible():
        page.get_by_role("button", name="Agents", exact=True).click()
        page.wait_for_timeout(10_000)
    print("URL", page.url)
    print("TITLE", page.title())
    print(
        "INPUTS",
        page.locator("input").evaluate_all(
            """els => els.map(e => ({
                type: e.type,
                name: e.name,
                id: e.id,
                placeholder: e.placeholder,
                aria: e.getAttribute('aria-label')
            }))"""
        ),
    )
    print(
        "BUTTONS",
        page.locator("button").evaluate_all(
            """els => els.filter(e => e.offsetParent !== null)
                .map(e => (e.innerText || e.getAttribute('aria-label') || e.title || '').trim())
                .filter(Boolean).slice(0, 100)"""
        ),
    )
    print("TEXT", page.locator("body").inner_text()[:2000])
    print("CONSOLE", console_events[-20:])
    print("PAGE_ERRORS", page_errors[-20:])
    print("FAILED_REQUESTS", failed_requests[-20:])
    print("BAD_RESPONSES", bad_responses[-20:])
    print(
        "CLICKABLES",
        page.locator("button, a, [role=button]").evaluate_all(
            """els => els.filter(e => e.offsetParent !== null).map(e => ({
                tag: e.tagName, id: e.id, cls: e.className,
                text: (e.innerText || '').trim().slice(0, 120),
                aria: e.getAttribute('aria-label'), title: e.title, href: e.href
            })).slice(0, 60)"""
        ),
    )
    page.screenshot(
        path=".tmp/journey-aprof-solar-20260826-01/landing.png",
        full_page=True,
    )
