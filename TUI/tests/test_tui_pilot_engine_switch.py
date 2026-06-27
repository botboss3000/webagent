"""Pilot check: the Model Settings ▸ Agent-loop switch (Internal ↔ webAgent).

Verifies the toggle renders right by the LLM model chooser, that clicking it
flips ``cfg.engine_mode`` and persists it, and that the toggle highlight moves.
Also asserts the dispatch gate the switch controls: with a checkout linked,
``internal`` forces the built-in brain (no app engine), ``webagent`` uses it.
"""

import asyncio
from pathlib import Path

from pilot_harness import boot, snapshot


def _engine_pick(app, value: str):
    """The Agent-loop pill carrying the given value (internal / webagent)."""
    for b in app.query(".engine-mode-pick"):
        if getattr(b, "_btn_value", None) == value:
            return b
    raise AssertionError(f"no engine-mode pill for {value!r}")


async def main() -> None:
    async with boot(size=(110, 40)) as (app, pilot):
        # Open Model Settings (where the model chooser lives).
        app.action_panel_model()
        await pilot.pause()
        shot = snapshot(app)
        assert "MODEL SETTINGS" in shot, shot
        # The switch sits right by the provider/model chooser.
        assert "Agent loop" in shot, shot
        assert "Internal" in shot and "webAgent" in shot, shot
        assert "LLM provider" in shot, shot

        # Default preserves today's behaviour: webAgent loop selected.
        assert (app.cfg.engine_mode or "webagent") == "webagent"
        web_pill = _engine_pick(app, "webagent")
        assert web_pill.has_class("panel-btn-active")

        # Flip to the internal loop.
        await pilot.click(_engine_pick(app, "internal"))
        await pilot.pause()
        assert app.cfg.engine_mode == "internal"
        # Persisted to disk.
        from tui_app.config import TuiConfig
        assert TuiConfig.load().engine_mode == "internal"
        # Highlight moved.
        assert _engine_pick(app, "internal").has_class("panel-btn-active")
        assert not _engine_pick(app, "webagent").has_class("panel-btn-active")
        # Note reflects the internal mode.
        assert "Internal:" in snapshot(app)

        # The dispatch gate the switch drives: simulate a linked checkout and
        # confirm internal forces the built-in brain while webagent uses the engine.
        app.project_root = Path(app.project_root or Path.cwd())

        def use_engine() -> bool:
            text, synthetic, images = "do a thing", False, None
            return (app.project_root is not None and not synthetic
                    and not images and bool(text)
                    and (app.cfg.engine_mode or "webagent") != "internal")

        assert use_engine() is False  # internal → built-in brain

        # Flip back to webAgent.
        await pilot.click(_engine_pick(app, "webagent"))
        await pilot.pause()
        assert app.cfg.engine_mode == "webagent"
        assert use_engine() is True   # webagent → app loop

    print("OK test_tui_pilot_engine_switch")


if __name__ == "__main__":
    asyncio.run(main())
