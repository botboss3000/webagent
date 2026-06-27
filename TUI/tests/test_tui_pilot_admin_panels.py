"""Pilot test: every Admin/side panel builds without crashing.

Regression guard for the Reset panel, which used to crash on open because its
note ``Static`` passed ``id=`` to the Rich ``Text`` renderable (a widget-only
kwarg) instead of to the ``Static`` itself — ``Text.__init__()`` rejected it and
the render worker raised ``TypeError``, taking the app down. Opening Admin ▸ Reset
hit this every time.

This walks every panel kind through ``_render_panel`` and asserts none raise, so
a stray widget kwarg on a Rich renderable can't silently ship in any panel again.
Pure UI: no LLM, no server, no network. Run with the TUI venv::

    .venv/Scripts/python.exe tests/test_tui_pilot_admin_panels.py
"""

import asyncio
from pathlib import Path

from pilot_harness import boot

# Every kind `_render_panel` knows how to build (see app.py `_render_panel`).
PANEL_KINDS = ["admin", "reset", "setup", "git", "connect", "sessions", "config",
               "model", "scene", "diag", "logs", "playbook", "server"]


async def main() -> None:
    async with boot(size=(120, 40)) as (app, pilot):
        # A linked project so panels take their real (non-onboarding) path.
        app.project_root = Path(__file__).resolve().parents[2]

        for kind in PANEL_KINDS:
            app._panel_kind = kind
            # `_render_panel` is the worker body every action_* panel opener runs;
            # if it raises, the Textual worker fails and the app crashes.
            await app._render_panel(kind)
            await pilot.pause()
            print(f"  ok  {kind} panel renders")

        # Drive the Reset panel exactly as the user does: Admin ▸ [Reset].
        app.action_admin_reset()
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#reset-note"), "reset note static missing after open"
        assert app.query_one("#reset-db"), "reset checkboxes missing after open"
        print("  ok  Admin > Reset opens (the original crash) and shows its controls")

    print("\nTUI admin-panels pilot: ALL PASSED")


asyncio.run(main())
