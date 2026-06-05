"""Pilot test that exercises the *LLM path* through the UI — with a scripted LLM.

A chat turn is a Textual worker that calls `app.agent.llm.complete(...)`. By
swapping in a `FakeLLM` we drive a full turn through the entire UI pipeline —
prompt submit -> agent loop -> assistant bubble in the transcript -> Stop pill
toggling -> token HUD — deterministically, offline, and free. No real provider,
no network, no cost.

This is the recommended way to "use the LLM through the Pilot". For a genuine
live-provider smoke test, skip `use_fake_llm` and let `drive_turn` call out (needs
an API key + network; slower and nondeterministic).

Run:
    .venv/Scripts/python.exe tests/test_tui_pilot_llm.py
"""

import asyncio

from pilot_harness import boot, snapshot, use_fake_llm, drive_turn, assistant, call


async def main() -> None:
    async with boot(size=(110, 34)) as (app, pilot):
        # --- case 1: a plain text answer renders in the transcript ---
        fake = use_fake_llm(app, [assistant("Hello from the scripted agent.")])
        await drive_turn(app, pilot, "say hi")
        assert not app._busy, "turn never finished (still busy)"
        assert fake.calls >= 1, "the agent never called the LLM"
        hist = app.store.history(app.session_id)
        roles = [(r["role"], (r["content"] or "")) for r in hist]
        assert any(role == "user" and "say hi" in c for role, c in roles), roles
        assert any(role == "assistant" and "scripted agent" in c for role, c in roles), roles
        print("  ok  scripted text turn: user msg stored, assistant reply rendered")

        shot1 = snapshot(app)
        assert "scripted agent" in shot1, "assistant text not visible on screen"
        print("  ok  assistant reply is visible in the text snapshot")

        # --- case 2: a tool call executes for real, then a final answer ---
        # Script: turn 1 calls a read-only manager tool; turn 2 answers. The agent
        # actually runs the tool between the two completions (same mechanism proven
        # in test_event_loop.py); here we only assert the loop made 2 LLM calls and
        # produced the final text, so it's tool-agnostic.
        fake2 = use_fake_llm(app, [
            call("list_files", {"path": "."}, content="Checking the tree."),
            assistant("Done — listed the project."),
        ])
        await drive_turn(app, pilot, "list the files")
        assert not app._busy
        assert fake2.calls >= 2, f"expected the loop to continue past the tool call, got {fake2.calls}"
        hist2 = app.store.history(app.session_id)
        assert any((r["content"] or "").find("Done — listed") >= 0 for r in hist2), \
            "final answer after the tool call was not stored"
        print(f"  ok  tool-call turn looped and produced a final answer ({fake2.calls} LLM calls)")

    print("\nTUI pilot LLM path: ALL PASSED")


asyncio.run(main())
