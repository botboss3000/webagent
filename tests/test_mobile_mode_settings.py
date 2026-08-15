from app.admin import settings


def test_mobile_mode_defaults_off(monkeypatch):
    monkeypatch.setattr(settings, "_load_app_settings", lambda: {})
    assert settings.get_mobile_mode() is False
    assert settings.AppSettings().mobile_mode is False


def test_mobile_mode_requires_json_true(monkeypatch):
    monkeypatch.setattr(settings, "_load_app_settings", lambda: {"mobile_mode": True})
    assert settings.get_mobile_mode() is True

    monkeypatch.setattr(settings, "_load_app_settings", lambda: {"mobile_mode": "true"})
    assert settings.get_mobile_mode() is False

