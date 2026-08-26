import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import uploads


class FakeDB:
    async def assert_session_owned(self, _user_id, _session_id):
        return None


def _request():
    return Request({"type": "http", "method": "POST", "path": "/upload", "headers": []})


def test_tier_attachment_limit_is_enforced(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(uploads, "get_db", lambda: db)
    monkeypatch.setattr("app.auth.identity.assert_caller_is", AsyncMock(return_value="user"))

    async def capabilities(_user_id, **_kwargs):
        return {
            "subject": {"class": "registered"}, "features": {"attachments": True},
            "limits": {"max_attachment_bytes": 10, "max_storage_bytes": None},
        }

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    with pytest.raises(HTTPException) as error:
        asyncio.run(uploads._upload_context(_request(), "user", "session", 11))
    assert error.value.status_code == 413
    assert error.value.detail["code"] == "quota_exceeded"


def test_attachment_feature_denial_happens_before_storage(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(uploads, "get_db", lambda: db)
    monkeypatch.setattr("app.auth.identity.assert_caller_is", AsyncMock(return_value="anon_guest"))

    async def capabilities(_user_id, **_kwargs):
        return {"subject": {"class": "anonymous"}, "features": {}, "limits": {}}

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    with pytest.raises(HTTPException) as error:
        asyncio.run(uploads._upload_context(_request(), "anon_guest", "session", 1))
    assert error.value.detail["code"] == "authentication_required"
