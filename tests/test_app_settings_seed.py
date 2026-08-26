import json

from app.admin import settings
from app.util import config_seed


def test_shipped_app_settings_seed_is_complete_and_safe():
    seed = settings.load_app_settings_defaults()

    assert set(seed) == set(settings.AppSettings.model_fields)
    assert "safety_lock_active" not in seed
    assert settings.AppSettings(**seed).model_dump() == seed

    expected_laptop_defaults = {
        "mobile_mode": True,
        "access_mode": "public_registered",
        "shared_default_agent_enabled": True,
        "splash_enabled": True,
        "always_on_display": True,
        "app_control_quick_message": False,
        "session_completion_notifications": False,
        "chat_pill_min_height": "96px",
        "chat_pill_padding": "4px 4px 4px 14px",
        "max_active_sessions": 3,
        "run_frozen_threshold_seconds": 360,
        "run_watchdog_poll_seconds": 30,
    }
    for key, value in expected_laptop_defaults.items():
        assert seed[key] == value


def test_runtime_app_settings_override_the_shipped_seed(monkeypatch, tmp_path):
    seed_path = tmp_path / "seed.json"
    runtime_path = tmp_path / "app-settings.json"
    seed_path.write_text(
        json.dumps({"mobile_mode": True, "splash_enabled": True}),
        encoding="utf-8",
    )
    runtime_path.write_text(
        json.dumps({"splash_enabled": False}),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "APP_SETTINGS_DEFAULTS_FILE", seed_path)
    monkeypatch.setattr(settings, "APP_SETTINGS_FILE", runtime_path)

    assert settings._load_app_settings() == {
        "mobile_mode": True,
        "splash_enabled": False,
    }


def test_config_library_seeds_the_tracked_app_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(config_seed, "CONFIG_DIR", str(tmp_path))

    result = config_seed.seed_missing()

    assert "app-settings.json" in result["written"]
    seeded = json.loads((tmp_path / "app-settings.json").read_text(encoding="utf-8"))
    assert seeded == settings.load_app_settings_defaults()
