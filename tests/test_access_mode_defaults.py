from app.admin.settings import normalize_access_mode


def test_fresh_install_defaults_to_open_registration():
    assert normalize_access_mode(None) == "public_registered"
    assert normalize_access_mode("") == "public_registered"


def test_unknown_access_mode_still_fails_closed():
    assert normalize_access_mode("unexpected-mode") == "admin_approval"
