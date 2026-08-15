"""Admin user-table billing summaries and trial balance adjustments."""

import asyncio
import os
import sqlite3
import tempfile

import pytest
from fastapi import HTTPException

import app.admin.users as users_mod
from app.admin.users import (
    _anonymous_user_exists,
    _billing_summaries,
    _set_trial_credit_total,
)


class FileDB:
    def __init__(self, path):
        self.path = path
        self._billing_ready = True

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE billing_configs (scope TEXT PRIMARY KEY);
        CREATE TABLE wallets (
            id TEXT PRIMARY KEY, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL,
            balance_cents INTEGER NOT NULL DEFAULT 0,
            hold_cents INTEGER NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'usd'
        );
        CREATE TABLE trials (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, agent_id TEXT NOT NULL,
            started_at TEXT, expires_at TEXT, credit_cents INTEGER,
            remaining_cents INTEGER
        );
        CREATE TABLE user_profiles (
            user_id TEXT PRIMARY KEY, is_admin INTEGER DEFAULT 0,
            default_agent_id TEXT, created_at TEXT, updated_at TEXT, last_login_at TEXT
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE interactions (id TEXT PRIMARY KEY, session_id TEXT NOT NULL);
        CREATE TABLE channel_identities (
            id TEXT PRIMARY KEY, channel TEXT, external_id TEXT, user_id TEXT,
            user_tier TEXT, display_name TEXT, created_at TEXT, updated_at TEXT
        );
        INSERT INTO wallets VALUES ('w1', 'user', 'u1', 725, 25, 'usd');
        INSERT INTO trials VALUES ('new', 'u1', 'a1', '2026-08-10', '2099-01-01', 500, 300);
        INSERT INTO trials VALUES ('old', 'u1', 'a2', '2026-08-01', NULL, 200, 100);
        INSERT INTO trials VALUES ('expired', 'u1', 'a3', '2025-01-01', '2025-02-01', 900, 900);
        INSERT INTO channel_identities VALUES (
            'ci1', 'browser', 'browser-device-1', 'anon_123', 'anonymous', '',
            '2026-08-01', '2026-08-10'
        );
        INSERT INTO sessions VALUES ('s1', 'anon_123', '2026-08-01', '2026-08-12');
        INSERT INTO interactions VALUES ('i1', 's1');
    """)
    conn.commit()
    conn.close()
    yield FileDB(path)
    os.unlink(path)


def run(coro):
    return asyncio.run(coro)


def trial_balances(db):
    conn = db._get_conn()
    try:
        return {
            row["id"]: row["remaining_cents"]
            for row in conn.execute("SELECT id, remaining_cents FROM trials").fetchall()
        }
    finally:
        conn.close()


def test_billing_summary_separates_paid_and_current_trial_credits(db):
    summary = run(_billing_summaries(db))["u1"]
    assert summary["billing_status"] == "trial"
    assert summary["trial_credits"] == 400
    assert summary["has_trial_grant"] is True
    assert summary["paid_credits"] == 725
    assert summary["paid_hold_credits"] == 25


def test_set_trial_total_reduces_current_grants_without_touching_expired(db):
    run(_set_trial_credit_total(db, "u1", 50))
    balances = trial_balances(db)
    assert balances == {"new": 0, "old": 50, "expired": 900}


def test_set_trial_total_adds_to_most_recent_grant(db):
    run(_set_trial_credit_total(db, "u1", 550))
    balances = trial_balances(db)
    assert balances == {"new": 450, "old": 100, "expired": 900}


def test_set_trial_total_requires_a_current_grant(db):
    with pytest.raises(HTTPException) as exc:
        run(_set_trial_credit_total(db, "missing", 100))
    assert exc.value.status_code == 409


def test_anonymous_user_exists_from_identity_or_session(db):
    assert run(_anonymous_user_exists(db, "anon_123")) is True
    assert run(_anonymous_user_exists(db, "anon_missing")) is False
    assert run(_anonymous_user_exists(db, "registered-user")) is False


def test_user_stats_returns_anonymous_accounts(db, monkeypatch):
    async def allow_admin(_db, _uid):
        return None

    async def no_registered_users():
        return []

    monkeypatch.setattr(users_mod, "get_db", lambda: db)
    monkeypatch.setattr(users_mod, "get_app_db", lambda: db)
    monkeypatch.setattr(users_mod, "_require_admin", allow_admin)
    monkeypatch.setattr(users_mod, "list_users", no_registered_users)

    result = run(users_mod.list_users_with_stats("admin"))
    assert result["users"] == []
    assert len(result["anonymous_users"]) == 1
    anon = result["anonymous_users"][0]
    assert anon["user_id"] == "anon_123"
    assert anon["channels"] == ["browser"]
    assert anon["identity_tiers"] == ["anonymous"]
    assert anon["session_count"] == 1
    assert anon["interaction_count"] == 1
    assert anon["last_seen_at"] == "2026-08-12"
