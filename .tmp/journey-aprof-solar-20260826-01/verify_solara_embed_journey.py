import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


sys.stdout.reconfigure(encoding="utf-8")
AGENT_ID = "bc87ad99-6a29-45e2-86a4-f289fe620710"
HOST_URL = "http://127.0.0.1:8000/"
ARTIFACT = Path(__file__).with_name("APROF-SOLAR-20260826-01-solara-host-embed.png")

with sync_playwright() as runtime:
    browser = runtime.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={"width": 1440, "height": 1000}, service_workers="block"
    )
    page = context.new_page()
    page_errors = []
    failed_responses = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on(
        "response",
        lambda response: failed_responses.append(
            {"status": response.status, "url": response.url}
        )
        if response.status >= 400
        else None,
    )

    page.goto(HOST_URL, wait_until="domcontentloaded")
    host = page.locator(f'[data-webagent-embed="{AGENT_ID}"]')
    host.wait_for(state="attached", timeout=15_000)
    launcher = host.locator(".wa-launch")
    launcher.wait_for(state="visible", timeout=10_000)
    launcher_initially_visible = launcher.is_visible()
    launcher_box = launcher.bounding_box()
    launcher.click()

    iframe = host.locator("iframe.wa-frame")
    iframe.wait_for(state="visible", timeout=10_000)
    frame = page.frame_locator("iframe.wa-frame")
    composer = frame.locator("#ec-input")
    composer.wait_for(state="visible", timeout=15_000)
    composer.fill("Tell me about Solara Piano")
    send = frame.locator("#ec-send")
    send_ready = send.is_visible() and send.is_enabled()
    page.screenshot(path=str(ARTIFACT), full_page=True)

    iframe_src = iframe.get_attribute("src") or ""
    panel_box = host.locator(".wa-panel").bounding_box()
    result = {
        "runId": "APROF-SOLAR-20260826-01",
        "caseId": "J07",
        "result": "pass"
        if AGENT_ID in iframe_src
        and composer.is_visible()
        and send_ready
        and not page_errors
        and not failed_responses
        else "fail",
        "hostUrl": page.url,
        "agentId": AGENT_ID,
        "launcherInitiallyVisible": launcher_initially_visible,
        "launcherHiddenWhileOpen": not launcher.is_visible(),
        "launcherBox": launcher_box,
        "panelBox": panel_box,
        "iframeSrc": iframe_src,
        "composerVisible": composer.is_visible(),
        "draftedSendReady": send_ready,
        "pageErrors": page_errors,
        "failedResponses": failed_responses,
        "evidence": [ARTIFACT.name],
    }
    print(json.dumps(result, ensure_ascii=False))
    browser.close()
