import sys

from playwright.sync_api import sync_playwright


sys.stdout.reconfigure(encoding="utf-8")
url = "http://127.0.0.1:18099/embed/bc87ad99-6a29-45e2-86a4-f289fe620710"
with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1280, "height": 900}, service_workers="block")
    page = context.new_page()
    console = []
    errors = []
    responses = []
    page.on("console", lambda message: console.append(f"{message.type}: {message.text}"))
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", lambda response: responses.append(f"{response.status} {response.url}") if response.status >= 400 else None)
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(20_000)
    print("TITLE", page.title())
    print("TEXT", page.locator("body").inner_text()[:3000])
    print("CONSOLE", console[-50:])
    print("PAGE_ERRORS", errors[-50:])
    print("BAD_RESPONSES", responses[-50:])
    page.screenshot(path=".tmp/journey-aprof-solar-20260826-01/embed-debug.png", full_page=True)
