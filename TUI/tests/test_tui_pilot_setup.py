"""Pilot UI test for the Setup / dependency dashboard.

Boots the TUI off-screen, seeds a FIXED dependency list (so the test is offline +
deterministic), renders the Setup panel, and asserts the rows: a missing row shows
an [Install] button, a blocked row does not, an ok row with manage actions shows
its manage buttons, and the header shows [Install all].

Renders the panel directly via ``_render_panel`` (the same body every panel opener
runs) rather than driving the live probe worker — that keeps the test deterministic
and avoids leaving the "checking…" dots timer running into teardown. The probe
itself is covered by ``test_deps.py``.

Run from the TUI project root:  python tests/test_tui_pilot_setup.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                 # tests/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # TUI/

from pilot_harness import boot, snapshot  # noqa: E402
from tui_app import deps as D  # noqa: E402

# A deterministic checklist: an ok prereq, two installable-missing rows, one
# missing row BLOCKED by a still-missing prereq, an ok Ubuntu row with manage
# actions, and a not-applicable browser row.
FIXED_DEPS = [
    D.Dep("internet", "Internet (GitHub + PyPI)", D.OK, "reachable", installable=False),
    D.Dep("git", "git", D.MISSING, "missing", installable=True),  # unblocked → [Install]
    D.Dep("repo", "webAgent code", D.MISSING, "not present", installable=True),  # unblocked → [Install]
    D.Dep("venv", "Python environment", D.MISSING, "not built", installable=True,
          blocked_by=["repo"]),  # blocked: repo is still missing → no button, shows hint
    D.Dep("ubuntu", "Ubuntu environment", D.OK, "installed (~900 MB)", installable=False,
          manage_actions=[("Update", "ubuntu_update"), ("Remove", "ubuntu_remove")]),
    D.Dep("browser", "Headless browser", D.NA, "not available on Android", installable=False,
          required=False),
]


def _check(install_ids, manage_ids, btn_actions, snap) -> None:
    assert install_ids == {"git", "repo"}, f"install buttons wrong: {install_ids}"
    assert "venv" not in install_ids, "blocked row should NOT offer an Install button"
    assert manage_ids == {"ubuntu_update", "ubuntu_remove"}, f"manage buttons wrong: {manage_ids}"
    assert "Install folder" in snap, "install-folder field missing"
    assert "[Install all" in snap, "Install all button missing"
    assert "git" in snap and "Ubuntu environment" in snap, "rows not rendered"
    assert "do the steps above first" in snap, "blocked-row hint missing"
    # The Logs button sits below the visible fold, so assert on the widget, not text.
    assert "setup_logs" in btn_actions, "Logs button missing from the setup panel"


def _check_logs(snap, log_actions, copy_ids) -> None:
    assert "SETUP LOGS" in snap, "setup-logs panel title missing"
    # Buttons can clip in the narrow panel snapshot, so assert on the widgets.
    assert "setup_logs_clear" in log_actions, "Clear button missing from the logs viewer"
    assert "panel_setup" in log_actions, "Back button missing from the logs viewer"
    assert copy_ids and any("Cloning webAgent" in (t or "") for t in copy_ids), \
        "Copy button missing or carries the wrong payload"
    assert "Cloning webAgent" in snap, "captured log lines not shown"


async def main() -> str:
    async with boot(size=(120, 40)) as (app, pilot):
        # Seed a fixed, ready checklist and render the Setup panel directly (the
        # body every panel opener runs) — no live probe / dots timer.
        app._deps = FIXED_DEPS
        app._deps_state = "ready"
        app._deps_busy = ""
        app._deps_target = ""
        app._panel_kind = "setup"
        await app._render_panel("setup")
        await pilot.pause()

        install_ids = {getattr(w, "_btn_value", None) for w in app.query(".dep-install-btn")}
        manage_ids = {getattr(w, "_btn_value", None) for w in app.query(".dep-manage-btn")}
        btn_actions = {getattr(w, "_btn_action", None) for w in app.query(".panel-btn")}
        snap = snapshot(app)

        # Now seed a couple of captured activity lines and render the Logs viewer.
        app._setup_log = ["• Installing: repo", "Cloning webAgent into C:\\webagent…", "done"]
        app._panel_kind = "setuplogs"
        await app._render_panel("setuplogs")
        await pilot.pause()
        copy_ids = [getattr(w, "_copy_text", None) for w in app.query(".copy-btn")]
        log_actions = {getattr(w, "_btn_action", None) for w in app.query(".panel-btn")}
        snap_logs = snapshot(app)

    # Assert after the run_test block (printing inside routes through print-capture).
    _check(install_ids, manage_ids, btn_actions, snap)
    _check_logs(snap_logs, log_actions, copy_ids)
    return snap + "\n\n--- SETUP LOGS ---\n\n" + snap_logs


if __name__ == "__main__":
    out = asyncio.run(main())
    print(out)
    print("\nsetup pilot test: PASSED")
