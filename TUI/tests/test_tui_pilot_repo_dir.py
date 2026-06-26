"""Pilot test for the Admin ▸ Repo directory field.

Boots the app off-screen, opens the Admin panel, confirms the new Repo
directory field renders, then drives Save / Clear and asserts the value
round-trips through the manager's own SQLite store (the `settings` table,
key `repo_dir`). Pure UI: no LLM, no server, no network (no AI key is
configured in the harness, so Save persists + nudges rather than handing
the agent a turn). Run with the TUI venv::

    .venv/Scripts/python.exe tests/test_tui_pilot_repo_dir.py
"""

import asyncio

import tui_app.app as appmod
from textual.widgets import Input

from pilot_harness import boot, snapshot, hdr_button


def panel_btn(app, action):
    """The side-panel button (a .panel-btn Static) carrying this _btn_action."""
    for w in app.query(".panel-btn"):
        if getattr(w, "_btn_action", None) == action:
            return w
    raise AssertionError(f"panel button {action!r} not found")


async def main() -> None:
    async with boot(size=(110, 40)) as (app, pilot):
        # start from a clean slate so the assertions are unambiguous
        app.store.set_setting("repo_dir", "")

        # --- open the Admin panel ---
        await pilot.click(hdr_button(app, "panel_admin"))
        await pilot.pause()
        inp = app.query_one("#repo-dir-input", Input)
        assert inp.value == "", f"field should start empty, got {inp.value!r}"
        print("  ok  Admin panel shows an empty Repo directory field")

        # --- Ctrl+V pastes from the (stubbed) OS clipboard, not the empty buffer ---
        target = r"C:\webagent"
        appmod.read_clipboard = lambda: target
        inp.focus()
        await pilot.pause()
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert inp.value == target, f"Ctrl+V paste failed, got {inp.value!r}"
        print("  ok  Ctrl+V pastes from the OS clipboard into the field")

        # --- Save ---
        await pilot.click(panel_btn(app, "repo_dir_save"))
        await pilot.pause()
        await pilot.pause()
        assert app.store.get_setting("repo_dir") == target, (
            f"DB not updated, got {app.store.get_setting('repo_dir')!r}")
        print("  ok  Save persists the path to the settings table (repo_dir)")

        # --- the panel re-renders pre-filled from the saved entry ---
        inp2 = app.query_one("#repo-dir-input", Input)
        assert inp2.value == target, f"field not pre-filled, got {inp2.value!r}"
        save_shot = snapshot(app)

        # --- Clear forgets it ---
        await pilot.click(panel_btn(app, "repo_dir_clear"))
        await pilot.pause()
        await pilot.pause()
        assert app.store.get_setting("repo_dir") == "", (
            f"Clear left a value: {app.store.get_setting('repo_dir')!r}")
        print("  ok  Clear empties the saved entry")

    # print/assert AFTER the run_test block (print-capture is live inside it)
    assert "Repo directory" in save_shot, "field label missing from snapshot"
    assert "webagent" in save_shot, "saved path missing from snapshot"
    print("  ok  snapshot shows the labelled field with the saved path")
    print("\nTUI pilot repo-dir: ALL PASSED")


asyncio.run(main())
