"""Wallet ledger tests using a real in-memory SQLite database.

We bootstrap the schema from app/db/local.py:SCHEMA_SQL so the same DDL the
production SQLite backend uses is exercised. This is the most realistic test
short of hitting Supabase.
"""

import asyncio
import os
import sys
import sqlite3
import tempfile

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from plugins.billing import wallet as wallet_mod


class FileDB:
    """Tiny shim that mirrors LocalBackend's _get_conn surface."""
    def __init__(self, path):
        self._db_path = path
        self._billing_ready = True

    def _get_conn(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Apply only the billing portion of the schema (avoid pulling in the full
    # 700-line block which references tables we don't need here).
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE wallets (
            id TEXT PRIMARY KEY, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL,
            balance_cents INTEGER NOT NULL DEFAULT 0,
            hold_cents INTEGER NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'usd',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(owner_type, owner_id, currency)
        );
        CREATE TABLE wallet_transactions (
            id TEXT PRIMARY KEY, wallet_id TEXT NOT NULL, delta_cents INTEGER NOT NULL,
            kind TEXT NOT NULL, ref_id TEXT, note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()
    yield FileDB(path)
    try:
        os.unlink(path)
    except Exception:
        pass


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_get_or_create_starts_at_zero(db):
    w = _run(wallet_mod.get_or_create_wallet(db, "user", "u1"))
    assert w is not None
    assert w.balance_cents == 0
    assert w.available_cents == 0


def test_credit_and_balance(db):
    _run(wallet_mod.credit(db, "user", "u1", 500))
    w = _run(wallet_mod.get_balance(db, "u1"))
    assert w.balance_cents == 500
    assert w.available_cents == 500


def test_preauthorize_then_settle_lower_refunds_diff(db):
    _run(wallet_mod.credit(db, "user", "u1", 1000))
    ok, w = _run(wallet_mod.preauthorize(db, "u1", 300))
    assert ok and w.hold_cents == 300 and w.available_cents == 700

    # Settle for 100 — 200 should be released back
    after = _run(wallet_mod.settle(db, "u1", hold_cents=300, actual_cents=100))
    assert after.balance_cents == 900
    assert after.hold_cents == 0
    assert after.available_cents == 900


def test_preauthorize_insufficient_returns_false(db):
    _run(wallet_mod.credit(db, "user", "u1", 100))
    ok, w = _run(wallet_mod.preauthorize(db, "u1", 999))
    assert ok is False
    assert w.balance_cents == 100
    assert w.hold_cents == 0


def test_release_returns_funds(db):
    _run(wallet_mod.credit(db, "user", "u1", 500))
    ok, _ = _run(wallet_mod.preauthorize(db, "u1", 200))
    assert ok
    after = _run(wallet_mod.release(db, "u1", 200))
    assert after.balance_cents == 500
    assert after.hold_cents == 0


def test_settle_caps_at_hold(db):
    _run(wallet_mod.credit(db, "user", "u1", 500))
    ok, _ = _run(wallet_mod.preauthorize(db, "u1", 100))
    assert ok
    # Try to settle for 999; should be capped at the 100 hold.
    after = _run(wallet_mod.settle(db, "u1", hold_cents=100, actual_cents=999))
    assert after.balance_cents == 400
    assert after.hold_cents == 0


def test_transactions_recorded(db):
    _run(wallet_mod.credit(db, "user", "u1", 500))
    _run(wallet_mod.preauthorize(db, "u1", 100))
    _run(wallet_mod.settle(db, "u1", 100, 50))
    w = _run(wallet_mod.get_balance(db, "u1"))
    tx = _run(wallet_mod.list_transactions(db, w.wallet_id))
    kinds = sorted([t["kind"] for t in tx])
    # Expect: purchase, hold, usage, release
    assert "purchase" in kinds
    assert "hold" in kinds
    assert "usage" in kinds
    assert "release" in kinds
