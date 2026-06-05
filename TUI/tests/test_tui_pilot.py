"""Pilot smoke test for the Server-Manager TUI.

Boots the app off-screen and drives it the way a user would — types into the
prompt, cycles the theme, opens a new session tab — then asserts the widget
tree and the text snapshot reflect each action. Pure UI: no LLM, no server,
no network. Run with the TUI venv::

    .venv/Scripts/python.exe tests/test_tui_pilot.py
"""

import asyncio

from pilot_harness import boot, snapshot


async def main() -> None:
    async with boot(size=(110, 34)) as (app, pilot):
        # --- boots with the expected chrome focused on the prompt ---
        assert app.query_one("#log"), "transcript log missing"
        prompt = app.query_one("#prompt")
        assert app.focused is prompt, f"prompt not focused, got {app.focused}"
        print("  ok  app boots off-screen with prompt focused")

        # --- typing lands in the prompt input ---
        await pilot.press("t", "e", "s", "t")
        await pilot.pause()
        assert prompt.text == "test", f"prompt text = {prompt.text!r}"
        print("  ok  keypresses type into the prompt pill")

        # --- Ctrl+T cycles the visual theme ---
        before = app.theme
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.theme != before, "theme did not change on Ctrl+T"
        print(f"  ok  Ctrl+T cycles theme ({before} -> {app.theme})")

        # --- the + button opens a fresh session tab ---
        tabs_before = len(app._open_tabs)
        app.action_new_session_tab()
        await pilot.pause()
        assert len(app._open_tabs) == tabs_before + 1, "new tab not added"
        print(f"  ok  new-session tab added ({tabs_before} -> {len(app._open_tabs)})")

        # --- capture a text screenshot for the post-run assertion ---
        shot = snapshot(app)

    # print/assert AFTER the run_test block so output doesn't route through
    # the app's print-capture.
    assert "WEBAGENT" in shot, "expected chrome missing from snapshot"
    print("  ok  text snapshot renders the WEBAGENT chrome")
    print("\nTUI pilot smoke: ALL PASSED")


asyncio.run(main())
