import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_chat_ui_v3_uses_two_row_pill_schema():
    config = json.loads((ROOT / "data/config/chat_ui.json").read_text(encoding="utf-8"))
    pill = config["chat_common"]["active_footer"]["chat_pill"]

    assert config["version"] == 3
    assert pill["rows"] == [
        {"center": {"controls": ["textarea"]}},
        {
            "left": {
                "controls": ["attach"],
                "align": "bottom",
                "padding": "4px 2px 20px 20px",
            },
            "center": {
                "carousel": ["stats"],
                "align": "bottom",
                "padding": "0 0 28px 0",
            },
            "right": {
                "controls": ["stop", "continue", "mic_send"],
                "align": "bottom",
                "padding": "4px 20px 20px 0",
            },
        },
    ]
    assert {
        "textarea", "stats", "mic", "send", "mic_send", "attach",
        "stop", "continue",
    } <= set(
        pill["controls"]
    )
    above = config["chat_common"]["active_footer"]["above_pill"]
    assert above["enabled"] is True
    assert above["rows"][0] == {
        "left": [],
        "center": ["activity"],
        "right": ["scroll_bottom"],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_pill_normalizer_supports_legacy_and_distinct_buttons():
    script = textwrap.dedent(
        """
        const path = await import('node:path');
        const { pathToFileURL } = await import('node:url');
        const moduleUrl = pathToFileURL(
          path.resolve('ui/shared/js/chat-pill-config.js'),
        );
        const mod = await import(moduleUrl.href);
        const controls = value => mod.configuredRowControls(
          mod.normalizeChatPill(value),
        );
        const result = {
          legacyCombined: controls({
            layout: {
              textarea: '1,2',
              stats: '2,2',
              buttons: '1,3',
              attach: '2,3',
            },
            buttons: {
              voice: { enabled: true },
              send: { enabled: true },
            },
            attach: { enabled: true },
          }),
          legacySendOnly: controls({
            buttons: { voice: { enabled: false } },
          }),
          distinct: controls({
            rows: [
              { center: ['textarea'], right: ['mic', 'send'] },
              { center: ['stats'], right: ['attach'] },
            ],
            controls: {},
          }),
          exclusive: controls({
            rows: [{ right: ['mic_send', 'mic', 'send'] }, {}],
            controls: {},
          }),
          disabledCombined: controls({
            rows: [{ center: ['textarea'], right: ['mic_send'] }, {}],
            controls: { mic_send: { enabled: false } },
          }),
          configuredRuntimeActions: controls({
            rows: [
              { center: ['textarea'] },
              { center: ['stats', 'stop', 'continue'] },
            ],
            controls: {},
          }),
        };
        console.log(JSON.stringify(result));
        """
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-"],
        cwd=ROOT,
        input=script,
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(completed.stdout)

    assert "mic_send" in result["legacyCombined"]
    assert "mic_send" not in result["legacySendOnly"]
    assert "send" in result["legacySendOnly"]
    assert {"mic", "send"} <= set(result["distinct"])
    assert result["exclusive"] == ["mic_send"]
    assert "mic_send" not in result["disabledCombined"]
    assert result["configuredRuntimeActions"] == [
        "textarea", "stats", "stop", "continue",
    ]


def test_unified_zone_renderer_resets_static_and_generated_controls():
    source = (ROOT / "ui/chat-controls/chat-controls-config.js").read_text(
        encoding="utf-8"
    )

    assert "'[data-header-control], [data-element-origin]'" in source


def test_static_chat_composer_is_visible_before_async_control_hydration():
    partial = (ROOT / "ui/chat/chat-side-panel.html").read_text(encoding="utf-8")
    marker = '<div id="chat-input-row"'
    composer_tag = partial[partial.index(marker):partial.index('>', partial.index(marker))]

    assert "display:none" not in composer_tag
    assert "chat-pill-boot" in composer_tag
    assert "chat-pill-row-layout" in composer_tag
    assert "chat-pill-send-only" in composer_tag

    css = (ROOT / "ui/shared/css/app1.css").read_text(encoding="utf-8")
    assert 'class="chat-pill-layout-row" data-pill-row="0"' in partial
    assert 'class="chat-pill-layout-row" data-pill-row="1"' in partial
    assert "padding:4px 2px 20px 20px" in partial
    assert "padding:4px 20px 20px 0" in partial
    assert partial.index('id="chat-attach-btn"') < partial.index('id="chat-send"')
    assert 'id="chat-continue-btn"' in partial
    continue_tag = partial[
        partial.index('<button id="chat-continue-btn"'):
        partial.index('>', partial.index('<button id="chat-continue-btn"'))
    ]
    assert "hidden" in continue_tag
    assert "#chat-input-row.chat-pill-boot .chat-pill-attach" in css
    assert "#chat-input-row.chat-pill-boot .chat-pill-voice" in css
    assert "width: 36px !important" in css
    assert "min-height: 88px" in css
    assert "padding: 28px 20px 13px 20px" in css
    assert "--chat-pill-font-size: 16px" in css
    assert "font-size: var(--chat-pill-font-size)" in css
    assert "font-family: inherit" in css
    assert "font-weight: 400" in css
    assert "border-radius: 41px" in css
    assert "color: var(--fg-4)" in css
    assert "opacity: 0.4" in css
    assert "pointer-events: none" in css
    assert '.chat-pill-layout-row[data-pill-row="1"]' in css
    assert "flex: 1 1 auto" in css


def test_chat_pill_rebuild_restores_focused_textarea_after_reparenting():
    source = (ROOT / "ui/shared/js/chat-pill-config.js").read_text(
        encoding="utf-8"
    )
    css = (ROOT / "ui/shared/css/app1.css").read_text(encoding="utf-8")

    assert "document.activeElement === els.input" in source
    assert "pillEl.classList.add('chat-pill-caret-suppressed')" in source
    assert "void els.input.offsetHeight" in source
    assert "els.input.blur()" in source
    assert "els.input.focus({ preventScroll: true })" in source
    assert "els.input.setSelectionRange(" in source
    assert "els.input.addEventListener('pointerdown', revealCaret, true)" in source
    assert "els.input.addEventListener('beforeinput', revealCaret, true)" in source
    assert "#chat-input-row.chat-pill-caret-suppressed .chat-pill-input" in css
    assert "caret-color: transparent !important" in css


def test_initial_empty_composer_skips_the_hydrated_active_pill_frame():
    source = (
        ROOT / "ui/chat-controls/chat-controls-config.js"
    ).read_text(encoding="utf-8")

    assert "async function _buildActiveFooter({ initialIdle = false } = {})" in source
    assert "if (profile.chat_pill && !initialIdle)" in source
    initial_mode = source.index("const shouldStartIdle =")
    build = source.index("await _buildActiveFooter({ initialIdle: shouldStartIdle })")
    recheck = source.index("const commitIdle = shouldStartIdle", build)
    idle_commit = source.index("await switchFooterMode('idle')", build)
    assert initial_mode < build < recheck < idle_commit
    assert "await switchFooterMode('active')" in source[recheck:]


def test_idle_chat_composer_keeps_runtime_controls():
    config = json.loads((ROOT / "data/config/chat_ui.json").read_text(encoding="utf-8"))
    row = config["chat_common"]["idle_footer"]["chat_pill"]["rows"][0]

    assert row["left"]["controls"] == ["attach"]
    assert row["center"]["controls"] == ["textarea"]
    assert row["right"]["controls"] == ["stop", "continue", "mic_send"]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_composer_max_width_preserves_css_percentage_units():
    source = (ROOT / "ui/chat/js/chat-send.js").read_text(encoding="utf-8")
    start = source.index("function _resolvePillMaxWidth(rawValue, availableW)")
    end = source.index("\n\n// Needed by chat-ui.js", start)
    helper = source[start:end]
    script = helper + textwrap.dedent(
        """

        console.log(JSON.stringify({
          percentage: _resolvePillMaxWidth('100%', 356),
          partialPercentage: _resolvePillMaxWidth('50%', 356),
          pixels: _resolvePillMaxWidth('1000px', 356),
          fallback: _resolvePillMaxWidth('', 356),
        }));
        """
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "-"],
        cwd=ROOT,
        input=script,
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(completed.stdout) == {
        "percentage": 356,
        "partialPercentage": 178,
        "pixels": 1000,
        "fallback": 356,
    }
    assert "const configuredMaxW = _resolvePillMaxWidth(" in source
