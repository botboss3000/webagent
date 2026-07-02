"""Pilot check: the Model Settings ▸ Agent-loop switch (Internal ↔ WebAgent).

Verifies the toggle renders right by the LLM model chooser, that clicking it
flips ``cfg.engine_mode`` and persists it, and that the toggle highlight moves.
Also asserts the dispatch gate the switch controls: with a checkout linked,
``internal`` forces the built-in brain (no app engine), ``webagent`` uses it.
"""

import asyncio
import os
import tempfile
from pathlib import Path

# Isolate this test's config + DB in a throwaway data dir BEFORE the app boots, so
# saving the model/key never touches the developer's real TUI config. data_dir()
# reads WEBAGENT_DATA_DIR at app-construction time.
_TMP_DATA = tempfile.mkdtemp(prefix="wa-engine-switch-")
os.environ["WEBAGENT_DATA_DIR"] = _TMP_DATA

from pilot_harness import boot, snapshot  # noqa: E402


def _engine_pick(app, value: str):
    """The Agent-loop pill carrying the given value (internal / webagent)."""
    for b in app.query(".engine-mode-pick"):
        if getattr(b, "_btn_value", None) == value:
            return b
    raise AssertionError(f"no engine-mode pill for {value!r}")


async def main() -> None:
    try:
        await _run()
        # Print success AFTER the run_test block has fully torn down (printing inside
        # routes through the app's print capture and can deadlock/encode-crash).
        print("OK test_tui_pilot_engine_switch")
    finally:
        import shutil
        shutil.rmtree(_TMP_DATA, ignore_errors=True)


async def _run() -> None:
    # Saving the internal LLM leaves panel-render workers in flight; Textual's
    # teardown can cancel one and surface a CancelledError from the boot() __aexit__.
    # That's shutdown noise, not a test failure — swallow it ONLY once every assertion
    # in the block has run (``asserts_done`` is the block's last statement).
    asserts_done = False
    try:
        async with boot(size=(110, 40)) as (app, pilot):
            asserts_done = await _body(app, pilot)
    except asyncio.CancelledError:
        if not asserts_done:
            raise


async def _body(app, pilot) -> bool:
    if True:
        # The dataclass default is WebAgent; pin a deterministic baseline so the test
        # doesn't depend on whatever engine_mode happens to be on disk.
        assert __import__("tui_app.config", fromlist=["TuiConfig"]).TuiConfig().engine_mode == "webagent"
        app.cfg.engine_mode = "webagent"
        # Open Model Settings (where the model chooser lives).
        app.action_panel_model()
        await pilot.pause()
        shot = snapshot(app)
        assert "MODEL SETTINGS" in shot, shot
        # The switch sits right by the provider/model chooser.
        assert "Agent loop" in shot, shot
        assert "Internal" in shot and "WebAgent" in shot, shot
        assert "LLM provider" in shot, shot

        # WebAgent loop is the active selection.
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

        # In internal mode, saving the model fields must write to the TUI's OWN
        # config and NEVER call the web-app server. Stub set_provider to catch leaks.
        called = {"web": 0}

        async def _boom(_cfg):
            called["web"] += 1
            return _cfg

        app._webapp.set_provider = _boom  # type: ignore[assignment]

        # Stub the web-app reads so the later flip back to WebAgent (which reloads
        # config from the server) stays offline and instant — no real HTTP, no hang.
        async def _empty_settings():
            return {}

        async def _empty_provider():
            return {}

        app._webapp.get_app_settings = _empty_settings  # type: ignore[assignment]
        app._webapp.get_provider = _empty_provider      # type: ignore[assignment]
        # The model panel is already open in internal mode — wait for its fields to
        # render (the rebuild after the toggle flip is a worker), then type into them.
        from textual.widgets import Input
        for _ in range(40):
            if app.query("#cfg-model"):
                break
            await asyncio.sleep(0.05)   # yield without waiting for screen idle
        app.query_one("#cfg-model", Input).value = "test/internal-model"
        app.query_one("#cfg-apikey", Input).value = "sk-internal-test"
        app.action_cfg_save_auth()
        for _ in range(40):
            if app.cfg.model == "test/internal-model":
                break
            await asyncio.sleep(0.05)
        assert called["web"] == 0, "internal save must not touch the web app server"
        assert app.cfg.model == "test/internal-model"
        assert app.cfg.api_key == "sk-internal-test"
        assert app.cfg.provider_override is True
        # The live internal LLM picked it up immediately.
        assert app.provider.model == "test/internal-model"
        assert TuiConfig.load().model == "test/internal-model"

        # Flip back to WebAgent and confirm the gate re-opens the app loop. (The
        # real click handler is exercised for the internal flip above; here we set
        # the mode directly to avoid spawning the network-bound rebuild at teardown.)
        app.cfg.engine_mode = "webagent"
        assert use_engine() is True   # webagent → app loop
        return True   # every assertion passed before teardown


if __name__ == "__main__":
    asyncio.run(main())
