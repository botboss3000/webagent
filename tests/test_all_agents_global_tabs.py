from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ability_delete, agents


ROOT = Path(__file__).resolve().parents[1]


def test_all_agents_card_exposes_sessions_and_abilities_tabs():
    source = (ROOT / "ui/main-panel/agents/js/view.js").read_text(encoding="utf-8")

    assert "function _renderAllAgentsCard(container)" in source
    assert "[['sessions', 'Sessions'], ['abilities', 'Abilities']]" in source
    assert "canEdit: _userIsAdmin" in source
    assert "setSessionsAgentContext(null, body)" in source


def test_global_ability_table_has_a_read_only_mode():
    table = (ROOT / "ui/shared/js/admin-ability-table.js").read_text(encoding="utf-8")
    credentials = (ROOT / "ui/shared/js/dom-utils.js").read_text(encoding="utf-8")

    assert "const canEdit = cfg.canEdit !== false" in table
    assert "canEdit && !lockedOn" in table
    assert "if (canEdit) row.appendChild(_buildAbilityMoreButton" in table
    assert "opts.canEdit !== false && data.can_edit !== false" in credentials


def test_admin_settings_mutations_have_a_server_side_guard():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")

    assert "async def _admin_settings_mutation_guard" in source
    assert 'request.url.path.startswith("/admin/")' in source
    assert "await get_db().is_user_admin(caller_id)" in source


class _NonAdminDB:
    async def is_user_admin(self, _user_id):
        return False


def test_ability_repository_mutations_reject_non_admin(monkeypatch):
    monkeypatch.setattr("app.auth.identity.decode_token", lambda _token: {"user_id": "regular-user"})
    monkeypatch.setattr("app.db.get_db", lambda: _NonAdminDB())
    app = FastAPI()
    app.include_router(ability_delete.router)

    with TestClient(app) as client:
        response = client.put(
            "/api/v1/abilities/example/skill",
            headers={"Authorization": "Bearer regular-user"},
            json={"content": "cannot write"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "App admin access required."


def test_global_ability_config_values_are_public_read_only(monkeypatch):
    class _Config:
        @staticmethod
        async def ensure_bootstrapped(_db):
            return None

        @staticmethod
        def get_ability_config(ability_id):
            assert ability_id == "example"
            return {"mode": "safe"}

    monkeypatch.setattr("app.admin.ability_config.ensure_bootstrapped", _Config.ensure_bootstrapped)
    monkeypatch.setattr("app.admin.ability_config.get_ability_config", _Config.get_ability_config)
    monkeypatch.setattr(agents, "get_db", lambda: object())
    app = FastAPI()
    app.include_router(agents.router)

    with TestClient(app) as client:
        response = client.get("/api/v1/abilities/example/config")

    assert response.status_code == 200
    assert response.json() == {"ability_settings": {"mode": "safe"}}
