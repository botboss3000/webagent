"""Tests for the self-healing Playbook: pure logic, the store/coordinator, the
closed detect→apply→verify→learn loop driven through the real Watchdog, and the
agent tools via the registry. Plain-script style (no pytest) like the siblings."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from webagent import playbook as pb
from webagent import monstate
from tui_app.db import Store
from tui_app.pb_coordinator import PlaybookCoordinator
from tui_app.watchdog import Watchdog
from tui_app.tools.base import ToolContext
from tui_app.tools import ToolRegistry


def run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _store():
    return Store(Path(tempfile.mktemp(suffix=".db")))


def _cfg(**over):
    cfg = {
        "enabled": True, "interval_seconds": 5, "autonomy": "auto_restart",
        "channels": ["desktop"], "auto_restart": True, "restart_backoff_seconds": 0,
        "max_restarts_per_hour": 5, "error_rate_threshold": 10,
        "disk_percent_threshold": 90, "mem_percent_threshold": 90, "cpu_percent_threshold": 0,
        "remediation_mode": "safe_auto", "remediation_confidence_threshold": 0.6,
        "program_trigger_after": 3,
    }
    cfg.update(over)
    return cfg


class FakeNotifier:
    async def notify(self, *a, **k):
        return None


def _watchdog(store, state, guardian_present=False):
    async def restart():
        state["restarts"] += 1
        return "restarted"

    async def clear_port():
        state["cleared"] += 1
        return "cleared"

    async def run_command(cmd):
        state["commands"].append(cmd)
        return "ran"

    return Watchdog(
        get_project_root=lambda: Path("."),
        restart_server=restart,
        notifier=FakeNotifier(),
        log=lambda s: None,
        inject_event=lambda t: state["injects"].append(t),
        coordinator=PlaybookCoordinator(store),
        clear_port=clear_port,
        run_command=run_command,
        # These playbook tests exercise the watchdog's OWN restart path; pin the
        # Stage-3 guardian check explicitly so they don't depend on whether a real
        # keep-alive guardian happens to be running on the dev machine (which would
        # otherwise make the watchdog defer and restart 0×). Guardian coordination
        # has its own test below (test_restart_defers_to_guardian).
        guardian_owns_restart=lambda: guardian_present,
    )


# ── 1. pure logic ────────────────────────────────────────────────────────────
def test_pure_logic():
    # confidence: untried sits at 0.5, success moves it up.
    assert abs(pb.confidence({}) - 0.5) < 1e-9
    good = {"times_helped": 3, "times_didnt_help": 0}
    bad = {"times_helped": 0, "times_didnt_help": 3}
    assert pb.confidence(good) > pb.confidence(bad)
    # ranking: the better remedy wins; disabled sinks.
    ranked = pb.rank_remedies([
        {"id": "b", "kind": "command", "status": "approved", **bad},
        {"id": "g", "kind": "command", "status": "approved", **good},
        {"id": "d", "kind": "command", "status": "disabled", "times_helped": 9},
    ])
    assert ranked[0]["id"] == "g" and ranked[-1]["id"] == "d"
    assert pb.best_remedy(ranked)["id"] == "g"

    # gating: document never acts; safe_auto runs safe builtins per autonomy.
    restart = {"kind": pb.RESTART}
    assert not pb.gate(restart, mode="document", autonomy="auto_restart",
                       auto_restart=True, conf=1.0, threshold=0.6)
    assert pb.gate(restart, mode="safe_auto", autonomy="auto_restart",
                   auto_restart=True, conf=0.5, threshold=0.6)
    assert not pb.gate(restart, mode="safe_auto", autonomy="notify",
                       auto_restart=True, conf=0.5, threshold=0.6)  # autonomy too low
    # escalate needs self_heal.
    esc = {"kind": pb.ESCALATE}
    assert not pb.gate(esc, mode="safe_auto", autonomy="auto_restart",
                       auto_restart=True, conf=1.0, threshold=0.6)
    assert pb.gate(esc, mode="safe_auto", autonomy="self_heal",
                   auto_restart=True, conf=1.0, threshold=0.6)
    # command: only autonomous + approved; unproven may try, proven-bad stops.
    cmd = {"kind": pb.COMMAND, "status": "approved", "times_tried": 0}
    assert pb.gate(cmd, mode="autonomous", autonomy="auto_restart",
                   auto_restart=True, conf=0.5, threshold=0.6)
    assert not pb.gate(cmd, mode="safe_auto", autonomy="auto_restart",
                       auto_restart=True, conf=0.5, threshold=0.6)  # mode too low
    proven_bad = {"kind": pb.COMMAND, "status": "approved", "times_tried": 3,
                  "times_helped": 0, "times_didnt_help": 3}
    assert not pb.gate(proven_bad, mode="autonomous", autonomy="auto_restart",
                       auto_restart=True, conf=pb.confidence(proven_bad), threshold=0.6)
    suggested = {"kind": pb.COMMAND, "status": "suggested", "times_tried": 0}
    assert not pb.gate(suggested, mode="autonomous", autonomy="self_heal",
                       auto_restart=True, conf=0.5, threshold=0.6)  # not approved

    # fingerprint clusters volatile detail; programming after N.
    k1 = pb.diagnostic_key("error", "agent", "Timeout after 30s on run 0xAB12")
    k2 = pb.diagnostic_key("error", "agent", "Timeout after 9s on run 0xFF00")
    assert k1 == k2, "near-identical errors should cluster"
    assert pb.should_program_trigger({"kind": "diagnostic", "programmed": 0, "occurrences": 3}, 3)
    assert not pb.should_program_trigger({"kind": "diagnostic", "programmed": 0, "occurrences": 2}, 3)
    assert not pb.should_program_trigger({"kind": "builtin", "programmed": 0, "occurrences": 9}, 3)
    print("  ok  pure logic: confidence, ranking, gating, fingerprint, program-trigger")


# ── 2. store + coordinator ───────────────────────────────────────────────────
def test_store_coordinator():
    store = _store()
    co = PlaybookCoordinator(store)
    # record a built-in issue → occurrence bumps + default remedy seeded.
    i1 = co.record("server_down", "Server down", "builtin")
    assert i1["occurrences"] == 1
    i2 = co.record("server_down", "Server down", "builtin")
    assert i2["occurrences"] == 2
    rems = store.pb_remedies_for("server_down")
    assert len(rems) == 1 and rems[0]["kind"] == pb.RESTART
    # tally moves confidence; best_remedy reflects it.
    a = store.pb_add_remedy("server_down", "command", payload="echo a", status="approved")
    store.pb_record_outcome(a["id"], True); store.pb_record_outcome(a["id"], True)
    store.pb_record_outcome(rems[0]["id"], False)
    best = pb.best_remedy(store.pb_remedies_for("server_down"))
    assert best["id"] == a["id"], "the proven command should now outrank the failed restart"
    # forget cascades.
    assert store.pb_forget("server_down")
    assert store.pb_get_issue("server_down") is None
    assert store.pb_remedies_for("server_down") == []
    print("  ok  store/coordinator: upsert, seed default, tally→ranking, forget cascade")


def test_program_trigger():
    store = _store()
    co = PlaybookCoordinator(store)
    calls = []
    orig = monstate.add_alarm
    monstate.add_alarm = lambda **kw: calls.append(kw) or {"id": "a1"}
    try:
        key = pb.diagnostic_key("error", "agent", "boom at step 5")
        for _ in range(3):
            iss = co.record(key, "agent error: boom", "diagnostic",
                            {"contains": "boom", "level": "error", "category": "agent"})
        assert pb.should_program_trigger(iss, 3)
        co.program(key)
        assert len(calls) == 1, "a recurring diagnostic should program exactly one alarm"
        assert calls[0]["level"] == "error" and calls[0]["category"] == "agent"
        assert store.pb_get_issue(key)["programmed"] == 1
        co.program(key)  # idempotent
        assert len(calls) == 1
    finally:
        monstate.add_alarm = orig
    print("  ok  self-programming: recurring diagnostic auto-creates one alarm, idempotent")


# ── 3. closed loop through the real Watchdog ─────────────────────────────────
def test_loop_safe_auto():
    store = _store()
    state = {"restarts": 0, "cleared": 0, "commands": [], "injects": []}
    wd = _watchdog(store, state)
    cfg = _cfg(remediation_mode="safe_auto")
    # Server down → the restart remedy auto-applies and an incident is open.
    run(wd._remediate(cfg, key="server_down", label="Server down", kind="builtin",
                      trigger={"health": "stopped"}))
    assert state["restarts"] == 1, "safe_auto should auto-restart"
    assert len(wd._open_incidents) == 1
    rid = wd._open_incidents[0]["remedy_id"]
    # Verify: health back to running → resolved + the remedy is credited.
    run(wd._verify_open_incidents(cfg, "running", []))
    assert wd._open_incidents == []
    rem = store.pb_get_remedy(rid)
    assert rem["times_helped"] == 1 and rem["times_didnt_help"] == 0
    incs = store.pb_recent_incidents("server_down")
    assert incs[0]["outcome"] == "resolved"
    print("  ok  loop(safe_auto): restart applied, verified, remedy credited as helped")


def test_loop_failure_learns():
    store = _store()
    state = {"restarts": 0, "cleared": 0, "commands": [], "injects": []}
    wd = _watchdog(store, state)
    cfg = _cfg(remediation_mode="safe_auto", autonomy="self_heal")
    run(wd._remediate(cfg, key="server_down", label="Server down", kind="builtin",
                      trigger={}))
    rid = wd._open_incidents[0]["remedy_id"]
    # Force the verification window to have expired and the condition to persist.
    wd._open_incidents[0]["deadline"] = 0.0
    run(wd._verify_open_incidents(cfg, "stopped", []))
    assert wd._open_incidents == []
    rem = store.pb_get_remedy(rid)
    assert rem["times_didnt_help"] == 1, "a remedy that didn't clear the issue is marked so"
    assert state["injects"], "self_heal escalates to the agent when a remedy fails"
    print("  ok  loop(failure): unhelpful remedy recorded as didn't-help + escalated")


def test_loop_document_only():
    store = _store()
    state = {"restarts": 0, "cleared": 0, "commands": [], "injects": []}
    wd = _watchdog(store, state)
    cfg = _cfg(remediation_mode="document")
    run(wd._remediate(cfg, key="server_down", label="Server down", kind="builtin",
                      trigger={"health": "stopped"}))
    assert state["restarts"] == 0, "document mode must never act"
    assert wd._open_incidents == []          # nothing queued for verification
    incs = store.pb_recent_incidents("server_down")
    assert incs and incs[0]["outcome"] == "documented"
    # ...but the issue is still recorded + occurrences grow (it's learning).
    assert store.pb_get_issue("server_down")["occurrences"] == 1
    print("  ok  loop(document): records + ranks but takes no action")


# ── 3b. Stage-3: two restart layers coordinate (guardian owns relaunch) ──────
def test_restart_defers_to_guardian():
    store = _store()
    cfg = _cfg()

    # Guardian PRESENT → the watchdog stands down: no second server spawn, and it
    # doesn't burn its own restart budget. It still reports action-in-hand (True) so
    # the playbook's verify window can confirm recovery once /health returns.
    st = {"restarts": 0, "cleared": 0, "commands": [], "injects": []}
    wd = _watchdog(store, st, guardian_present=True)
    issued = run(wd._attempt_restart(cfg))
    assert issued is True, "deferral reports action-in-hand (guardian will relaunch)"
    assert st["restarts"] == 0, "watchdog must NOT restart while a live guardian owns it"
    assert wd._restart_times == [], "deferral must not consume the watchdog restart budget"

    # Guardian ABSENT → the watchdog restarts itself, exactly as before Stage 3.
    st2 = {"restarts": 0, "cleared": 0, "commands": [], "injects": []}
    wd2 = _watchdog(store, st2, guardian_present=False)
    issued2 = run(wd2._attempt_restart(cfg))
    assert issued2 is True and st2["restarts"] == 1, "no guardian → watchdog restarts itself"
    assert len(wd2._restart_times) == 1, "a real restart IS counted against the budget"

    # Through the playbook: guardian present → the restart remedy applies as a
    # deferral (no spawn), the incident is still tracked, and /health recovering
    # (the guardian did it) resolves the incident cleanly.
    st3 = {"restarts": 0, "cleared": 0, "commands": [], "injects": []}
    wd3 = _watchdog(store, st3, guardian_present=True)
    run(wd3._remediate(cfg, key="server_down", label="Server down", kind="builtin",
                       trigger={"health": "stopped"}))
    assert st3["restarts"] == 0, "playbook restart remedy is deferred too"
    assert len(wd3._open_incidents) == 1, "still tracked for verification"
    run(wd3._verify_open_incidents(cfg, "running", []))
    assert wd3._open_incidents == [], "the guardian's relaunch clears the open incident"
    print("  ok  stage-3: watchdog defers restart to a live guardian, restarts when absent")


# ── 3c. Stage-4: readiness recovery of an up-but-wedged server ───────────────
def test_readiness_wedge_recovery():
    import tui_app.watchdog as wdmod
    store = _store()
    cfg = _cfg(readiness_recovery=True, readiness_fail_ticks=2)

    async def not_ready(*a, **k):
        return "not_ready"

    async def ready(*a, **k):
        return "ready"

    orig = wdmod.server_ready
    try:
        # Guardian PRESENT — proves a readiness wedge does NOT defer (the guardian
        # only checks liveness, so it can't see a wedged-but-listening server).
        st = {"restarts": 0, "cleared": 0, "commands": [], "injects": []}
        wd = _watchdog(store, st, guardian_present=True)

        wdmod.server_ready = not_ready
        run(wd._handle_readiness(cfg, "running"))          # 1st fail: streak 1 < 2 → wait
        assert st["restarts"] == 0 and wd._notready_streak == 1
        run(wd._handle_readiness(cfg, "running"))          # 2nd fail: threshold → recover
        assert st["restarts"] == 1, "a sustained wedge must restart even with a live guardian"
        assert wd._notready_streak == 0, "streak resets after acting"

        # One 'ready' result clears an in-progress streak (no false accumulation).
        wdmod.server_ready = not_ready
        run(wd._handle_readiness(cfg, "running"))          # streak → 1
        wdmod.server_ready = ready
        run(wd._handle_readiness(cfg, "running"))          # ready → reset, no restart
        assert wd._notready_streak == 0 and st["restarts"] == 1

        # Server not live → readiness is a no-op (the liveness path owns "down").
        wdmod.server_ready = not_ready
        run(wd._handle_readiness(cfg, "stopped"))
        assert st["restarts"] == 1 and wd._notready_streak == 0

        # Master switch off → never probes or acts.
        st2 = {"restarts": 0, "cleared": 0, "commands": [], "injects": []}
        wd2 = _watchdog(store, st2, guardian_present=True)
        run(wd2._handle_readiness(_cfg(readiness_recovery=False), "running"))
        assert st2["restarts"] == 0 and wd2._notready_streak == 0
    finally:
        wdmod.server_ready = orig
    print("  ok  stage-4: sustained /health/ready wedge recovers the server (guardian can't see it)")


# ── 4. agent tools via the registry ──────────────────────────────────────────
def test_tools_roundtrip():
    store = _store()
    reg = ToolRegistry()
    ctx = ToolContext(project_root=Path("."), writes_enabled=True, autonomous=False,
                      log=lambda s: None, audit=lambda *a: None, store=store)
    # seed an issue so a remedy can attach.
    store.pb_upsert_issue("port_zombie", "Port held by a zombie", "builtin")
    out = run(reg.dispatch(ctx, "playbook_record_remedy",
                           {"issue_key": "port_zombie", "kind": "command",
                            "payload": "taskkill /F /PID 1"}))
    assert "recorded" in out and "suggested" in out, out  # commands start suggested
    rems = store.pb_remedies_for("port_zombie")
    assert len(rems) == 1 and rems[0]["status"] == "suggested"
    # approve it through the tool, then it should be approved in the store.
    run(reg.dispatch(ctx, "playbook_set_remedy",
                     {"remedy_id": rems[0]["id"], "status": "approved"}))
    assert store.pb_get_remedy(rems[0]["id"])["status"] == "approved"
    listed = run(reg.dispatch(ctx, "playbook_list", {}))
    assert "port_zombie" in listed and "1×" in listed, listed
    print("  ok  tools: record_remedy (gated suggested) → set_remedy approve → list")


if __name__ == "__main__":
    test_pure_logic()
    test_store_coordinator()
    test_program_trigger()
    test_loop_safe_auto()
    test_loop_failure_learns()
    test_loop_document_only()
    test_restart_defers_to_guardian()
    test_readiness_wedge_recovery()
    test_tools_roundtrip()
    print("\nplaybook: ALL PASSED")
