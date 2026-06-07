"""The guardian's "one server only" reaper must NEVER kill the live serving tree,
and MUST flag a duplicate run.py tree. These tests exercise the pure classifier on
synthetic process maps modelling the real topologies we've seen on Windows (the
venv launcher `python run.py` spawns the actual uvicorn interpreter as a child, so
the LISTENER is a child of the tracked pid)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from webagent.guardian import _foreign_server_pids, _server_tree, _is_server_proc


def test_reaps_nonserving_duplicate_keeps_serving_tree():
    # bat tree (SERVING): cmd 15284 -> uv 1556 -> python run.py 1508 -> uvicorn 8540 (listens)
    # guardian tree (NOT serving, failed bind): guardian 9368 -> python run.py 13388 -> 15816
    parents = {8540: 1508, 1508: 1556, 1556: 15284, 15284: 1984,
               15816: 13388, 13388: 9368, 9368: 3952}
    runpy = {8540, 1508, 1556, 15816, 13388}     # uv 'run python run.py' also matches
    listeners = {8540}
    own = _server_tree(listeners, runpy, parents)
    assert own == {8540, 1508, 1556}             # the whole serving tree
    foreign = _foreign_server_pids(listeners, runpy, parents)
    assert foreign == {15816, 13388}             # the non-serving duplicate
    assert 8540 not in foreign and 1508 not in foreign


def test_serving_tree_is_never_foreign_when_guardian_serves():
    parents = {8540: 1508, 1508: 1556, 1556: 15284,
               15816: 13388, 13388: 9368}
    runpy = {8540, 1508, 1556, 15816, 13388}
    listeners = {15816}                           # guardian's server is the live one
    foreign = _foreign_server_pids(listeners, runpy, parents)
    assert foreign == {8540, 1508, 1556}
    assert 15816 not in foreign and 13388 not in foreign


def test_failsafe_no_listener_in_server_procs_kills_nothing():
    # listener pid isn't among the known server procs (can't anchor) → reap nothing
    parents = {8540: 1508, 1508: 1556}
    runpy = {8540, 1508, 1556}
    assert _foreign_server_pids({99999}, runpy, parents) == set()
    assert _foreign_server_pids(set(), runpy, parents) == set()


def test_single_clean_server_has_no_foreign():
    parents = {15816: 13388, 13388: 9368}
    runpy = {15816, 13388}
    assert _foreign_server_pids({15816}, runpy, parents) == set()


def test_is_server_proc_excludes_tui_and_guardian_and_scanners():
    assert _is_server_proc(r"C:\proj\.venv\Scripts\python.exe run.py")
    assert _is_server_proc("uv run python run.py")
    assert _is_server_proc("python -m app.main")
    assert not _is_server_proc("python -m webagent.guardian")
    assert not _is_server_proc("python -m webagent")
    assert not _is_server_proc("powershell -NoProfile -Command Get-CimInstance Win32_Process")
    assert not _is_server_proc("")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print(f"ok  {name}")
    print("all guardian-reap tests passed")
