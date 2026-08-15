"""Tests for the app-wide voice dictation policy."""

from unittest.mock import patch

from app.admin import settings


def test_voice_dictation_policy_defaults_and_validation() -> None:
    defaults = settings.AppSettings()
    assert defaults.voice_dictation_llm_enabled is True
    assert defaults.voice_dictation_mode == "browser_then_llm"

    with patch.object(
        settings,
        "_load_app_settings",
        lambda: {
            "voice_dictation_llm_enabled": False,
            "voice_dictation_mode": "invalid-mode",
        },
    ):
        assert settings.get_voice_dictation_config() == {
            "llm_enabled": False,
            "mode": "browser_then_llm",
        }

    assert "voice_dictation_llm_enabled" in settings.AppSettings.model_fields
    assert "voice_dictation_mode" in settings.AppSettings.model_fields
