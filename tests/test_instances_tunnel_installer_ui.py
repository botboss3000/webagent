from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_tunnel_button_offers_background_cloudflared_install() -> None:
    source = (ROOT / "ui/main-panel/instances/instances.js").read_text(
        encoding="utf-8"
    )
    css = (ROOT / "ui/main-panel/instances/instances.css").read_text(
        encoding="utf-8"
    )

    assert "function _showCloudflaredInstallPanel" in source
    assert "Install Cloudflare Tunnel?" in source
    assert "/admin/instances/tunnel/install" in source
    assert "/admin/instances/device/action-status?job_id=" in source
    assert "_tunnelAction('start', iid" in source
    assert "cloudflared_installed === false" in source
    assert ".inst-tunnel-install-popover" in css


def test_linux_and_unconfigured_devices_get_the_tunnel_action() -> None:
    source = (ROOT / "ui/main-panel/instances/instances.js").read_text(
        encoding="utf-8"
    )
    actions = source[
        source.index("function _tunnelActionsHtml"):
        source.index("// Server/repo fleet buttons")
    ]

    assert "const t = d.tunnel || {};" in actions
    assert "const provider = t.provider || 'cloudflare';" in actions
    assert "['win', 'linux'].includes" in actions
    assert "!t.provider" not in actions
