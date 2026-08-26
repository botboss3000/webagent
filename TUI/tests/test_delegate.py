import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tui_app.tools.base import ToolContext
from tui_app.tools import delegate
from tui_app.subagents import SubagentRegistry

AVAIL = ["read_source", "search_source", "run_command", "delegate_async", "check_task"]

def make_ctx(reg, spawned):
    async def spawn(task, tools, mode, group_id):
        spawned.append((task, tools, mode, group_id))
        tid = f"t{len(spawned)}"
        reg.register(task_id=tid, session_id=f"s{tid}", description=task,
                     tools=tools, mode=mode, group_id=group_id, now=0.0)
        return f"Task started: {tid} ({mode})."
    return ToolContext(
        project_root=None, writes_enabled=True, autonomous=True,
        log=lambda s: None, audit=lambda *a: None,
        spawn_subagent=spawn, subagents=reg, available_tools=AVAIL,
    )

def run(coro): return asyncio.run(coro)

def test_async_scopes_tools_and_drops_unknown_and_delegation():
    reg = SubagentRegistry(); spawned = []
    ctx = make_ctx(reg, spawned)
    out = run(delegate.delegate_async(ctx, "investigate", ["read_source", "bogus", "delegate_async"], "notify"))
    assert spawned, "spawn not called"
    task, tools, mode, gid = spawned[0]
    assert tools == ["read_source"]          # bogus + delegate_async filtered out
    assert mode == "notify"
    assert "note:" in out                     # warned about the drops
    print("  ok  scopes tools, drops unknown + nested-delegation")

def test_async_rejects_when_no_valid_tools():
    reg = SubagentRegistry(); spawned = []
    ctx = make_ctx(reg, spawned)
    out = run(delegate.delegate_async(ctx, "x", ["bogus"], "gather"))
    assert not spawned and out.startswith("Error")
    print("  ok  rejects when no valid tools survive")

def test_background_mode_constant():
    reg = SubagentRegistry(); spawned = []
    ctx = make_ctx(reg, spawned)
    run(delegate.delegate_background(ctx, "lookup", ["search_source"]))
    assert spawned[0][2] == "background"
    print("  ok  delegate_background uses background mode")

def test_check_task_states():
    reg = SubagentRegistry(); spawned = []
    ctx = make_ctx(reg, spawned)
    run(delegate.delegate_async(ctx, "job", ["read_source"], "gather", "g1"))
    tid = "t1"
    assert "still running" in run(delegate.check_task(ctx, tid))
    reg.complete(tid, summary="all good", now=1.0)
    msg = run(delegate.check_task(ctx, tid))
    assert "completed" in msg and "all good" in msg
    assert "No task" in run(delegate.check_task(ctx, "nope"))
    print("  ok  check_task reports running / completed / missing")

def test_unavailable_without_host():
    reg = SubagentRegistry()
    ctx = ToolContext(project_root=None, writes_enabled=True, autonomous=True,
                      log=lambda s: None, audit=lambda *a: None,
                      spawn_subagent=None, subagents=reg, available_tools=AVAIL)
    out = run(delegate.delegate_async(ctx, "x", ["read_source"], "gather"))
    assert "isn't available" in out
    print("  ok  reports unavailable when no worker host")

for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
    fn()
print("\nall delegate tests passed")
