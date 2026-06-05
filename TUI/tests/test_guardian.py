"""Unit tests for the keep-alive guardian (webagent/guardian.py).

Pure-logic, no TUI, no network: we point the data dir at a throwaway temp folder
(``WEBAGENT_DATA_DIR``) so nothing touches the real ``TUI/`` runtime files, then
exercise the on/off state, the singleton claim, the clean-exit marker, and the
two supervision decisions (server + TUI) by stubbing the process/health probes.

Plain ``assert`` script (no pytest dependency), like the rest of ``tests/``. Run:

    PYTHONUTF8=1 ../.venv/Scripts/python.exe test_guardian.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Isolate the data dir BEFORE importing the module under test.
_TMP = tempfile.mkdtemp(prefix="wa-guardian-test-")
os.environ["WEBAGENT_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webagent import guardian as g  # noqa: E402

DEAD_PID = 999_999            # not a real running process
LIVE_PID = os.getpid()        # definitely alive


def _reset_state() -> None:
    for p in (g._gpid_file(), g._gstate_file(), g._tpid_file(),
              g._clean_exit_file(), g._glog_file()):
        try:
            p.unlink()
        except OSError:
            pass


def test_enabled_state() -> None:
    _reset_state()
    assert g.read_enabled() is True, "absent guardian.json must default to ON"
    g.write_enabled(False)
    assert g.read_enabled() is False
    g.write_enabled(True)
    assert g.read_enabled() is True
    print("ok: enabled-state default + round-trip")


def test_singleton_claim() -> None:
    _reset_state()
    guard = g.Guardian()

    # Fresh: claim succeeds and writes our pid.
    assert guard._claim_singleton() is True
    assert g._read_pid(g._gpid_file()) == os.getpid()

    # A LIVE owner (not us) blocks the claim.
    g._write_json_atomic(g._gpid_file(), {"pid": LIVE_PID, "port": g.PORT})
    if LIVE_PID != os.getpid():
        assert g.Guardian()._claim_singleton() is False
    # Simulate a different live owner via a stub so the assertion is unambiguous.
    real_alive = g._pid_alive
    g._pid_alive = lambda pid: True  # type: ignore[assignment]
    g._write_json_atomic(g._gpid_file(), {"pid": LIVE_PID + 1, "port": g.PORT})
    try:
        assert g.Guardian()._claim_singleton() is False, "a live owner must block the claim"
    finally:
        g._pid_alive = real_alive  # type: ignore[assignment]

    # A STALE (dead) owner is reclaimed.
    g._write_json_atomic(g._gpid_file(), {"pid": DEAD_PID, "port": g.PORT})
    assert g.Guardian()._claim_singleton() is True
    assert g._read_pid(g._gpid_file()) == os.getpid()
    print("ok: singleton claim — fresh / live-owner-blocks / stale-reclaims")


def test_clean_exit_marker() -> None:
    _reset_state()
    assert not g._clean_exit_file().exists()
    g.write_clean_exit()
    assert g._clean_exit_file().exists()
    g.clear_clean_exit()
    assert not g._clean_exit_file().exists()
    print("ok: clean-exit marker write/clear")


def test_supervise_server() -> None:
    _reset_state()
    guard = g.Guardian()
    launched: list[str] = []
    root = Path(_TMP)  # any non-None path; venv probe is stubbed

    real_health, real_venv, real_ports = g._is_healthy, g._venv_python, g._pids_on_port
    g._venv_python = lambda r: Path("py")           # type: ignore[assignment]
    g._pids_on_port = lambda port=g.PORT: set()      # type: ignore[assignment]
    guard._spawn_server = lambda py, r: 4321         # type: ignore[assignment]
    try:
        # Healthy → never launches.
        g._is_healthy = lambda: True                 # type: ignore[assignment]
        guard._supervise_server(root)
        assert launched == [] and guard._srv_launched_at == 0.0

        # Unhealthy → launches once, records server.pid.
        g._is_healthy = lambda: False                # type: ignore[assignment]
        guard._spawn_server = lambda py, r: launched.append("spawn") or 4321  # type: ignore[assignment]
        guard._supervise_server(root)
        assert launched == ["spawn"], "an unhealthy server must be (re)launched"
        assert g._read_pid(g._server_pid_file()) == 4321, "server.pid must be written"

        # Within the settle window → does NOT double-launch.
        guard._supervise_server(root)
        assert launched == ["spawn"], "must not relaunch while the server is settling"

        # No checkout linked → no-op.
        guard2 = g.Guardian()
        guard2._spawn_server = lambda py, r: launched.append("spawn2") or 1  # type: ignore[assignment]
        guard2._supervise_server(None)
        assert "spawn2" not in launched
    finally:
        g._is_healthy, g._venv_python, g._pids_on_port = real_health, real_venv, real_ports
    print("ok: server supervision — healthy skip / relaunch / settle / no-root")


def test_supervise_tui() -> None:
    _reset_state()
    guard = g.Guardian()
    guard._seen_tui = True                            # we've already seen a TUI check in
    relaunched: list[str] = []
    guard._relaunch_tui = lambda: relaunched.append("tui")  # type: ignore[assignment]

    # Dead TUI pid, no clean-exit marker → crash → relaunch.
    g._write_json_atomic(g._tpid_file(), {"pid": DEAD_PID, "ts": 0})
    guard._last_tui_relaunch = 0.0
    guard._supervise_tui()
    assert relaunched == ["tui"], "a vanished TUI without the marker must be relaunched"

    # Dead TUI pid WITH a clean-exit marker → deliberate quit → no relaunch, marker consumed.
    relaunched.clear()
    g.write_clean_exit()
    guard._seen_tui = True
    guard._last_tui_relaunch = 0.0
    guard._supervise_tui()
    assert relaunched == [], "a clean quit must NOT be relaunched"
    assert not g._clean_exit_file().exists(), "the clean-exit marker must be consumed"

    # Live TUI pid → nothing to do.
    relaunched.clear()
    g._write_json_atomic(g._tpid_file(), {"pid": LIVE_PID, "ts": 0})
    guard._last_tui_relaunch = 0.0
    guard._supervise_tui()
    assert relaunched == [], "a live TUI must not be relaunched"
    print("ok: TUI supervision — crash relaunch / clean-quit skip / live skip")


def main() -> int:
    test_enabled_state()
    test_singleton_claim()
    test_clean_exit_marker()
    test_supervise_server()
    test_supervise_tui()
    print("\nALL GUARDIAN TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
