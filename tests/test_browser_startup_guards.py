from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_service_worker_fetches_module_graph_network_first():
    source = (ROOT / "sw.js").read_text(encoding="utf-8")

    code_branch = source.index("CODE_PATTERN.test(url.pathname)")
    passive_branch = source.index("STATIC_PATTERN.test(url.pathname)")
    assert code_branch < passive_branch
    assert "e.respondWith(networkFirstStatic(request))" in source
    assert 'const CACHE = "webagent-v230"' in source


def test_unpaired_connector_does_not_auto_connect():
    background = (
        ROOT / "clients" / "browser-extension" / "background.js"
    ).read_text(encoding="utf-8")
    options = (
        ROOT / "clients" / "browser-extension" / "options.js"
    ).read_text(encoding="utf-8")
    manifest = (
        ROOT / "clients" / "browser-extension" / "manifest.json"
    ).read_text(encoding="utf-8")

    assert "autoConnect: true" in background
    assert "autoConnect: true" in options
    assert "if (!cfg.token)" in background
    assert "if (!cfg.autoConnect || !cfg.token) return;" in background
    assert 'const VERSION = "0.1.1"' in background
    assert '"version": "0.1.1"' in manifest


def test_mobile_navigation_does_not_rerender_its_observed_tab_strip():
    source = (
        ROOT / "ui" / "shared" / "js" / "mobile-navigation.js"
    ).read_text(encoding="utf-8")

    assert "lucide.createIcons" not in source
    assert "new MutationObserver" not in source
