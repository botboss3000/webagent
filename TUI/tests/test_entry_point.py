"""The TUI's only importable entry point is the ``tui_app`` package — there is NO
``webagent`` module. ``pyproject.toml`` maps a ``webagent`` *console script* to
``tui_app.__main__:main``, which is not the same thing as an importable module.
Several launchers used to invoke the non-existent ``python -m webagent`` and died
with "No module named webagent"; these tests pin the entry point so that
regression can't come back.

Run from the TUI project root:  python -m pytest tests/test_entry_point.py
(or just:  python tests/test_entry_point.py)
"""
import os
import subprocess
import sys

TUI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../TUI
OUTSIDE = os.path.dirname(TUI_DIR)                                       # run from outside it


def _run_module(module, *args):
    """Run ``python -m <module>`` the way the real launchers do: from OUTSIDE the
    TUI dir, with the TUI dir on PYTHONPATH (mirrors install-termux.sh). So a pass
    proves the package resolves via PYTHONPATH, not just because cwd happens to be
    the project dir."""
    env = dict(os.environ)
    env["PYTHONPATH"] = TUI_DIR + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=OUTSIDE, env=env, capture_output=True, text=True, timeout=60,
    )


def test_tui_app_help_launches_from_checkout():
    """``python -m tui_app --help`` runs from a checkout and exits cleanly — the
    module resolves and the entry point answers without needing a terminal."""
    p = _run_module("tui_app", "--help")
    combined = p.stdout + p.stderr
    assert p.returncode == 0, f"exit={p.returncode} stdout={p.stdout!r} stderr={p.stderr!r}"
    assert "tui_app" in combined, combined
    assert "No module named" not in combined, combined


def test_webagent_module_does_not_exist():
    """The legacy ``python -m webagent`` invocation must fail — it documents WHY
    the launchers run ``-m tui_app``: there is no importable ``webagent`` module."""
    p = _run_module("webagent")
    combined = p.stdout + p.stderr
    assert p.returncode != 0, f"expected failure but exit=0; output={combined!r}"
    assert "No module named webagent" in combined, combined


if __name__ == "__main__":
    test_tui_app_help_launches_from_checkout()
    print("ok  test_tui_app_help_launches_from_checkout")
    test_webagent_module_does_not_exist()
    print("ok  test_webagent_module_does_not_exist")
    print("all entry-point tests passed")
