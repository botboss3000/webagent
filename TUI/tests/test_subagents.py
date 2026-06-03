"""Unit tests for the Textual-free subagent core (mk2 event-driven model).

Run from the TUI project root:  python -m pytest tests/test_subagents.py
or standalone:                  python tests/test_subagents.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webagent.subagents import (  # noqa: E402
    BACKGROUND, COMPLETED, ERROR, GATHER, NOTIFY,
    SubagentRegistry, format_delivery,
)


def _reg_with(reg, tid, mode, group=""):
    reg.register(task_id=tid, session_id=f"s-{tid}", description=f"do {tid}",
                 tools=["read_source"], mode=mode, group_id=group, now=0.0)


def test_notify_surfaces_and_triggers():
    reg = SubagentRegistry()
    _reg_with(reg, "A", NOTIFY)
    dec = reg.complete("A", status=COMPLETED, summary="found it", now=1.0)
    assert dec.auto_trigger is True
    assert reg.has_surfaced_pending() is True
    drained = reg.drain_surfaced()
    assert len(drained) == 1 and drained[0].task_id == "A"
    assert reg.has_surfaced_pending() is False          # drain marked it injected
    assert "found it" in format_delivery(drained)


def test_background_stores_only():
    reg = SubagentRegistry()
    _reg_with(reg, "B", BACKGROUND)
    dec = reg.complete("B", status=COMPLETED, summary="side info", now=1.0)
    assert dec.auto_trigger is False
    assert reg.has_surfaced_pending() is False          # never surfaces
    # ...but check_task can still read it:
    assert reg.get("B").result_summary == "side info"


def test_standalone_gather_surfaces_immediately():
    reg = SubagentRegistry()
    _reg_with(reg, "G", GATHER)                          # no group_id
    dec = reg.complete("G", status=COMPLETED, summary="ok", now=1.0)
    assert dec.auto_trigger is True
    assert reg.has_surfaced_pending() is True


def test_group_gather_waits_for_all_then_fires_once():
    reg = SubagentRegistry()
    for t in ("A", "B", "C"):
        _reg_with(reg, t, GATHER, group="diagnose")

    d1 = reg.complete("A", summary="import error", now=1.0)
    assert d1.auto_trigger is False                      # group not complete
    assert reg.has_surfaced_pending() is False

    d2 = reg.complete("C", summary=".env ok", now=2.0)   # out-of-order arrival
    assert d2.auto_trigger is False

    d3 = reg.complete("B", summary="logs repeat", now=3.0)
    assert d3.auto_trigger is True                       # last member → fire
    assert d3.is_group is True and d3.group_id == "diagnose"

    drained = reg.drain_surfaced()
    assert {b.task_id for b in drained} == {"A", "B", "C"}   # one combined batch
    msg = format_delivery(drained)
    assert "diagnose" in msg and "import error" in msg and ".env ok" in msg
    assert reg.has_surfaced_pending() is False


def test_group_error_member_does_not_hang_group():
    reg = SubagentRegistry()
    for t in ("X", "Y"):
        _reg_with(reg, t, GATHER, group="g")
    reg.complete("X", status=ERROR, summary="crashed", now=1.0)
    dec = reg.complete("Y", status=COMPLETED, summary="done", now=2.0)
    assert dec.auto_trigger is True                      # errored member still terminal
    drained = reg.drain_surfaced()
    assert len(drained) == 2


def test_pending_snapshot_lists_only_running():
    reg = SubagentRegistry()
    _reg_with(reg, "R1", NOTIFY)
    _reg_with(reg, "R2", GATHER, group="g")
    reg.complete("R1", summary="x", now=1.0)
    assert reg.pending_count() == 1                      # R2 still running
    desc = reg.describe_pending()
    assert "R2" in desc and "R1" not in desc


def test_mixed_batch_concatenates():
    reg = SubagentRegistry()
    _reg_with(reg, "N1", NOTIFY)
    _reg_with(reg, "N2", NOTIFY)
    reg.complete("N1", summary="one", now=1.0)
    reg.complete("N2", summary="two", now=2.0)           # both surfaced, not yet drained
    drained = reg.drain_surfaced()
    msg = format_delivery(drained)
    assert "one" in msg and "two" in msg


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
