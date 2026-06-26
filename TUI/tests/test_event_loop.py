import asyncio, sys, tempfile, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tui_app.agent import ServerManagerAgent
from tui_app.db import Store
from tui_app.llm import Completion, ToolCall
from tui_app.subagents import SubagentRegistry
from tui_app.config import TuiConfig

class FakeLLM:
    def __init__(self, script): self.script = list(script); self.calls = 0
    async def complete(self, messages, tools=None, temperature=0.0, max_tokens=4096):
        c = self.script[self.calls]; self.calls += 1; return c
    async def aclose(self): pass

def make_agent(script):
    cfg = TuiConfig(); cfg.max_turns = 6
    store = Store(__import__("pathlib").Path(tempfile.mktemp(suffix=".db")))
    ag = ServerManagerAgent(cfg, None, FakeLLM(script), store)
    ag.subagents = SubagentRegistry()
    async def spawn(task, tools, mode, group_id):
        tid = "sa1"
        ag.subagents.register(task_id=tid, session_id="subsess", description=task,
                              tools=tools, mode=mode, group_id=group_id)
        # simulate the subagent finishing immediately and surfacing (notify)
        ag.subagents.complete(tid, status="completed", summary="bcrypt was missing; installed it")
        return f"Task started: {tid}"
    ag.spawn_subagent = spawn
    return ag, store

def run(c): return asyncio.new_event_loop().run_until_complete(c)

async def main():
    # Turn 1: model calls delegate_async(notify). Turn 2: after seeing the surfaced
    # result, model answers with final text.
    script = [
        Completion(content="Delegating a check.",
                   tool_calls=[ToolCall(id="t1", name="delegate_async",
                       arguments={"task":"check auth","tools":["web_search"],"mode":"notify"})],
                   raw={"tool_calls":[{"id":"t1","type":"function",
                        "function":{"name":"delegate_async","arguments":"{}"}}]}),
        Completion(content="The subagent found bcrypt was missing and fixed it. Done.",
                   tool_calls=[], raw={}),
    ]
    ag, store = make_agent(script)
    events = []
    async def on_event(ev): events.append((ev.kind, ev.text[:40] if ev.text else ""))
    await ag.run_turn("sess1", "Fix the auth bug.", on_event)

    hist = store.history("sess1")
    roles = [(r["role"], (r["content"] or "")[:50]) for r in hist]
    # The synthetic subagent-result message must have been injected as a user msg.
    injected = [c for role,c in roles if role=="user" and "subagent" in c and "completed" in c]
    assert injected, f"no synthetic subagent message injected; history={roles}"
    # Final answer references the fix and the loop ended (2 LLM calls).
    assert ag.llm.calls == 2, f"expected 2 LLM calls, got {ag.llm.calls}"
    assert any(k=="final" for k,_ in events), events
    # Registry shows the task completed, nothing still pending.
    assert ag.subagents.pending_count() == 0
    assert not ag.subagents.has_surfaced_pending()  # drained during the turn
    print("  ok  delegate_async(notify) result surfaced + folded into the next turn")
    print("  ok  loop auto-continued and produced a final answer (2 LLM calls)")

    # --- group gather: 3 subagents, only fires once all done ---
    reg = SubagentRegistry()
    for t in ("a","b","c"):
        reg.register(task_id=t, session_id="s", description=f"d{t}", tools=["x"], mode="gather", group_id="diag")
    assert not reg.complete("a", summary="1").auto_trigger
    assert not reg.complete("b", summary="2").auto_trigger
    assert reg.complete("c", summary="3").auto_trigger
    print("  ok  group gather fires exactly once, after all 3 complete")
    print("\nevent-loop integration: ALL PASSED")

run(main())
