"""Logic tests for the dependency model + probe (tui_app/deps.py).

Textual-free — the probe chain (deps → env_probe → tools.install) is stdlib-only,
so this runs on any Python without the TUI's UI dependencies.

Run from the TUI project root:  python tests/test_deps.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tui_app import deps as D  # noqa: E402
from tui_app.env_probe import MachineProbe  # noqa: E402


def _machine(termux: bool = False, os_label: str = "Linux", git: bool = True) -> MachineProbe:
    return MachineProbe(
        os_label=("Android (Termux)" if termux else os_label),
        arch="x86_64",
        is_termux=termux,
        browser_capable=not termux,
        runtime_python="3.12.0",
        system_python="/usr/bin/python3",
        system_python_supported=True,
        git_present=git,
    )


def test_desktop_list_has_no_proot_rows():
    deps = D.probe_dependencies("/tmp/wa-test-desktop-xyz", _machine(termux=False))
    ids = [d.id for d in deps]
    assert "proot_distro" not in ids and "ubuntu" not in ids, ids
    assert "python" in ids, "desktop needs a host Python 3.11/3.12 row"
    assert "browser" in ids
    # ordering anchors: internet first, verify last
    assert ids[0] == "internet"
    assert ids[-1] == "verify"
    # browser is optional on desktop
    browser = next(d for d in deps if d.id == "browser")
    assert browser.required is False


def test_termux_list_has_proot_and_browser_na():
    deps = D.probe_dependencies("~/webagent", _machine(termux=True))
    ids = [d.id for d in deps]
    for key in ("proot_distro", "ubuntu", "ubuntu_toolchain"):
        assert key in ids, f"{key} missing on Termux: {ids}"
    # the manager's host Python isn't a row on Termux (the server's Python lives in Ubuntu)
    assert "python" not in ids
    # browser is not applicable on Android
    browser = next(d for d in deps if d.id == "browser")
    assert browser.status == D.NA and browser.installable is False
    # the Ubuntu sandbox chains: ubuntu needs proot-distro, toolchain needs ubuntu
    ubuntu = next(d for d in deps if d.id == "ubuntu")
    toolchain = next(d for d in deps if d.id == "ubuntu_toolchain")
    assert ubuntu.blocked_by == ["proot_distro"]
    assert toolchain.blocked_by == ["ubuntu"]


def test_blocked_by_gating():
    repo = D.Dep("repo", "repo", D.MISSING)
    venv = D.Dep("venv", "venv", D.MISSING, blocked_by=["repo"])
    index = D.status_index([repo, venv])
    assert D.is_blocked(venv, index) is True
    repo.status = D.OK
    index = D.status_index([repo, venv])
    assert D.is_blocked(venv, index) is False


def test_satisfied_treats_na_as_done():
    assert D.Dep("x", "x", D.OK).satisfied is True
    assert D.Dep("x", "x", D.NA).satisfied is True
    assert D.Dep("x", "x", D.MISSING).satisfied is False


def test_pending_install_ids_and_summary():
    # A target that definitely isn't a checkout → repo/venv/config/verify pending.
    deps = D.probe_dependencies("/nonexistent-wa-xyz-123", _machine(termux=False))
    pending = D.pending_install_ids(deps)
    assert isinstance(pending, list)
    assert "repo" in pending
    # repo precedes venv in the list so [Install all] satisfies it first
    assert pending.index("repo") < pending.index("venv")
    text = D.render_text(deps)
    assert "ready" in text  # the summary headline is present


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("ok  ", _name)
            except Exception as e:  # noqa: BLE001
                failures += 1
                print("FAIL", _name, "->", type(e).__name__, e)
    if failures:
        print(f"\n{failures} test(s) failed")
        sys.exit(1)
    print("\nall deps tests passed")
