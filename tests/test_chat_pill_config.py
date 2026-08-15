import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_chat_ui_v3_uses_two_row_pill_schema():
    config = json.loads((ROOT / "data/config/chat_ui.json").read_text(encoding="utf-8"))
    pill = config["chat_common"]["chat_pill"]

    assert config["version"] == 3
    assert pill["rows"] == [
        {"left": ["attach"], "center": ["textarea"], "right": ["mic_send"]},
        {"center": ["stats", "stop", "continue"]},
    ]
    assert {
        "textarea", "stats", "mic", "send", "mic_send", "attach",
        "stop", "continue",
    } <= set(
        pill["controls"]
    )
    assert config["chat_common"]["above_pill"] == {
        "enabled": True,
        "left": ["activity"],
        "right": ["scroll_bottom"],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_pill_normalizer_supports_legacy_and_distinct_buttons():
    script = textwrap.dedent(
        """
        const fs = await import('node:fs');
        const source = fs.readFileSync(
          'ui/shared/js/chat-pill-config.js',
          'utf8',
        );
        const mod = await import(
          'data:text/javascript;base64,' + Buffer.from(source).toString('base64')
        );
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