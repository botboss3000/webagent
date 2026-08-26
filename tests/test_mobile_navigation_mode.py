from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mobile_mode_forces_compact_navigation_at_every_viewport():
    source = (
        ROOT / "ui" / "shared" / "js" / "mobile-navigation.js"
    ).read_text(encoding="utf-8")
    css = (
        ROOT / "ui" / "shared" / "css" / "design-system.css"
    ).read_text(encoding="utf-8")

    assert "_setMobileNavigationActive(true);" in source
    assert "MOBILE_VIEWPORT" not in source
    assert "window.matchMedia" not in source
    assert (
        "@media (max-width: 800px) {\n"
        "  body.mobile-mode #main-tabs-wrap"
    ) not in css
    assert "body.mobile-mode #main-tabs-wrap { display: none; }" in css
    assert "body.mobile-mode .mobile-nav-toggle { display: inline-flex; }" in css
    assert "body.mobile-mode .mobile-back-main-btn { display: inline-flex; }" in css
