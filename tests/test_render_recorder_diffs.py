import json
from pathlib import Path

from app.admin import ability_config
from plugins.abilities.Administrator.render_recorder import render_recorder as recorder_module
from plugins.abilities.Administrator.render_recorder.render_recorder import (
    DEFAULT_CLIENT_CONFIG,
    RenderRecorder,
)


def _recorder() -> RenderRecorder:
    recorder = object.__new__(RenderRecorder)
    recorder.max_html_bytes = 400_000
    return recorder


def test_default_capture_is_bounded_and_chat_scoped():
    assert DEFAULT_CLIENT_CONFIG["capture_duration_ms"] == 30_000
    assert DEFAULT_CLIENT_CONFIG["capture_max_bytes"] == 2_000_000
    assert DEFAULT_CLIENT_CONFIG["capture_max_operations"] == 5_000
    assert DEFAULT_CLIENT_CONFIG["capture_whole_page"] is False
    assert DEFAULT_CLIENT_CONFIG["capture_decorative_animations"] is False


def test_mutation_payload_preserves_capture_coordinates():
    payload = json.dumps({
        "version": 1,
        "frame": 3,
        "operations": [{"op": "text", "old_value": "loading", "value": "ready"}],
    })
    record = _recorder()._normalize_incoming(
        {
            "kind": "mutation",
            "ts": "2026-08-25T12:00:00Z",
            "recording_id": "capture-123",
            "elapsed_ms": 417,
            "label": "mutation-frame",
            "value_num": 1,
            "detail": {"frame": 3, "operations": 1},
            "html": payload,
        },
        user_id="admin",
        recv_ts="2026-08-25T12:00:01Z",
    )

    assert record is not None
    assert record["kind"] == "mutation"
    assert record["html"] == payload
    assert record["html_bytes"] == len(payload)
    detail = json.loads(record["detail"])
    assert detail == {
        "frame": 3,
        "operations": 1,
        "recording_id": "capture-123",
        "elapsed_ms": 417,
    }


def test_capture_coordinates_wrap_legacy_scalar_detail():
    record = _recorder()._normalize_incoming(
        {
            "kind": "console",
            "recording_id": "capture-456",
            "elapsed_ms": "9",
            "detail": "legacy detail",
        },
        user_id=None,
        recv_ts="2026-08-25T12:00:01Z",
    )

    assert record is not None
    assert json.loads(record["detail"]) == {
        "data": "legacy detail",
        "recording_id": "capture-456",
        "elapsed_ms": 9,
    }


def test_client_records_mutations_instead_of_periodic_snapshots():
    source = (Path(__file__).parents[1] / "ui" / "chat" / "js" / "recorder.js").read_text(encoding="utf-8")

    assert "new MutationObserver(_onMutations)" in source
    assert "requestAnimationFrame(_emitMutationFrame)" in source
    assert "_record('mutation'" in source
    assert "attributeOldValue: true" in source
    assert "characterDataOldValue: true" in source
    assert "_scheduleSnapshot" not in source
    assert "_takeSnapshot('baseline')" in source


def test_primary_toggle_is_in_persistent_header_immediately_before_chat():
    root = Path(__file__).parents[1]
    source = (root / "index.html").read_text(encoding="utf-8")

    status = source.index('<span id="status-right">')
    recorder = source.index('id="render-recorder-toggle"')
    chat = source.index('id="chat-toggle-btn"')
    assert status < recorder < chat
    assert source.index('id="main-tabs"') < status
    assert './ui/shared/js/render-recorder-toggle.js' in source


def test_app_function_switch_only_controls_header_visibility():
    root = Path(__file__).parents[1]
    app_functions = (root / "ui" / "main-panel" / "instances" / "settings" / "app-settings" / "app-functions.js").read_text(encoding="utf-8")
    header_toggle = (root / "ui" / "shared" / "js" / "render-recorder-toggle.js").read_text(encoding="utf-8")
    recorder_api = (root / "plugins" / "abilities" / "Administrator" / "render_recorder" / "render_recorder.py").read_text(encoding="utf-8")

    assert "app-function-changed" in app_functions
    assert "/api/v1/recordings/enabled" not in app_functions
    assert "_available = data.available === true" in header_toggle
    assert "body: JSON.stringify({ enabled: desired })" in header_toggle
    assert "enabled = rec.enabled" in recorder_api
    assert "enabled = available and rec.enabled" not in recorder_api


def test_expandable_app_function_exposes_bounded_capture_settings():
    root = Path(__file__).parents[1]
    descriptor = json.loads((root / "plugins" / "abilities" / "Administrator" / "render_recorder" / "render_recorder.json").read_text(encoding="utf-8"))
    fields = {field["key"]: field for field in descriptor["config"]["settings"]}

    assert descriptor["simple"] is False
    assert fields["capture_duration_seconds"]["min"] == 0
    assert "max" not in fields["capture_duration_seconds"]
    assert fields["capture_duration_seconds"]["default"] == 30
    assert fields["capture_max_megabytes"]["max"] == 10
    assert fields["capture_max_operations"]["max"] == 20_000
    assert fields["capture_scope"]["default"] == "chat"


def test_ability_settings_are_typed_clamped_and_applied(monkeypatch):
    monkeypatch.setattr(recorder_module, "_read_settings", lambda: {})
    monkeypatch.setattr(
        ability_config,
        "get_ability_config",
        lambda _ability_id: {
            "capture_duration_seconds": "900",
            "capture_max_megabytes": "0.1",
            "capture_max_operations": "25000",
            "capture_scope": "whole_page",
            "capture_decorative_animations": "false",
            "capture_lag": "false",
            "capture_network": "true",
        },
    )

    config = _recorder().client_config()
    assert config["capture_duration_ms"] == 900_000
    assert config["capture_max_bytes"] == 250_000
    assert config["capture_max_operations"] == 20_000
    assert config["capture_whole_page"] is True
    assert config["capture_decorative_animations"] is False
    assert config["capture_lag"] is False
    assert config["capture_network"] is True


def test_zero_duration_disables_automatic_stop(monkeypatch):
    monkeypatch.setattr(recorder_module, "_read_settings", lambda: {})
    monkeypatch.setattr(
        ability_config,
        "get_ability_config",
        lambda _ability_id: {"capture_duration_seconds": "0"},
    )

    assert _recorder().client_config()["capture_duration_ms"] == 0


def test_header_switch_has_a_persistent_recording_status_popover():
    root = Path(__file__).parents[1]
    html = (root / "index.html").read_text(encoding="utf-8")
    css = (root / "ui" / "shared" / "css" / "app1.css").read_text(encoding="utf-8")
    js = (root / "ui" / "shared" / "js" / "render-recorder-toggle.js").read_text(encoding="utf-8")

    assert 'class="render-recorder-switch"' in html
    assert 'id="render-recorder-popover"' in html
    assert ".render-recorder-control.is-recording .render-recorder-popover" in css
    assert "pointer-events: none" in css
    assert "_renderPopover" in js
    assert "This panel stays visible while capture is active." in js


def test_zero_duration_is_unlimited_and_decorative_backgrounds_are_filtered():
    root = Path(__file__).parents[1]
    source = (root / "ui" / "chat" / "js" / "recorder.js").read_text(encoding="utf-8")
    header = (root / "ui" / "shared" / "js" / "render-recorder-toggle.js").read_text(encoding="utf-8")

    assert "_scheduleDurationStop(durationMs)" in source
    assert "unlimited: durationMs === 0" in source
    assert "Math.min(60000" not in source
    assert "DECORATIVE_BACKGROUND_SELECTOR" in source
    assert "#stargaze-bg" in source
    assert "[data-wa-bg-surface]" in source
    assert "_stripDecorativeClone" in source
    assert "_isExcludedCaptureNode(m.target)" in source
    assert "RECORDER_INTERNAL_SELECTOR" in source
    assert "No time limit" in header
