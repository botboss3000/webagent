"""Unit tests for the billing pricing engine.

Covers:
- Each of the 5 strategies × {default-LLM, BYO-LLM}.
- Trial short-circuit (returns is_trial=True, zero charge).
- Exemption short-circuit (returns is_exempt=True, zero charge).
- Free strategy stays at zero.
- Platform-fee split: end_user = fee + earnings invariant.

The pricing engine is pure-logic; we mock the DB with a tiny in-memory shim
so we never hit SQLite/Supabase.
"""

import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

import pytest

# Make sure /home/user/webagent is on sys.path when run from any cwd
HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.billing.pricing import (
    Usage,
    Strategy,
    resolve_charge,
    _is_byo_llm,
)


class FakeDB:
    """Minimal DB stub. _has_billing_tables sees ._get_conn so we monkey-patch."""
    def __init__(self):
        self.tables: Dict[str, List[dict]] = {
            "billing_configs": [],
            "billing_exemptions": [],
            "trials": [],
            "subscriptions": [],
        }
        self._billing_ready = True  # bypass the runtime probe

    # Pretend to be a sqlite-backed db so _fetch_one's first branch is taken.
    def _get_conn(self):
        return _FakeConn(self.tables)


class _FakeConn:
    def __init__(self, tables):
        self.tables = tables

    def execute(self, sql: str, params: tuple = ()):
        return _FakeCursor(self.tables, sql, params)

    def commit(self): pass
    def close(self): pass


class _FakeCursor:
    def __init__(self, tables, sql, params):
        self.tables = tables
        self.sql = sql.strip()
        self.params = params

    def _parse_select(self):
        # Extremely simple: "SELECT * FROM <table> WHERE k1=? AND k2=? LIMIT 1"
        low = self.sql.lower()
        if not low.startswith("select"):
            return None
        from_idx = low.index(" from ") + 6
        rest = self.sql[from_idx:]
        parts = rest.split(None, 1)
        table = parts[0]
        rows = self.tables.get(table, [])
        # Strip optional " AS x" alias
        if " " in table:
            table = table.split(" ", 1)[0]
            rows = self.tables.get(table, [])
        if " where " not in low:
            return rows
        where = self.sql[low.index(" where ") + 7:]
        where = where.split(" LIMIT")[0].split(" limit")[0].split(" ORDER")[0]
        clauses = [c.strip() for c in where.split(" AND ")]
        keys = []
        for c in clauses:
            if "=?" in c:
                keys.append(c.split("=", 1)[0].strip())
        filt = dict(zip(keys, self.params))
        out = []
        for r in rows:
            ok = True
            for k, v in filt.items():
                if r.get(k) != v:
                    ok = False
                    break
            if ok:
                out.append(r)
        return out

    def fetchone(self):
        r = self._parse_select() or []
        if not r:
            return None
        return _Row(r[0])

    def fetchall(self):
        r = self._parse_select() or []
        return [_Row(d) for d in r]


class _Row:
    def __init__(self, d):
        self._d = d
    def __getitem__(self, k):
        return self._d.get(k)
    def keys(self):
        return list(self._d.keys())
    def get(self, k, default=None):
        return self._d.get(k, default)


def _agent(byo=False, llm_config=None):
    if llm_config is None:
        llm_config = {"byo": True} if byo else {"use_default": True}
    return {"id": "agent-1", "metadata": json.dumps({"llm_config": llm_config})}


def _platform_cfg(strategy="free", **kwargs):
    base = {
        "scope": "platform",
        "strategy": strategy,
        "allowed_strategies": json.dumps(["credits", "per_message", "per_token", "subscription", "trial"]),
        "allowed_processors": json.dumps(["stripe"]),
        "rate_card_default_llm": json.dumps({
            "credits_per_message_cents": 100,
            "credits_per_1k_input_tokens": 50,
            "credits_per_1k_output_tokens": 150,
            "per_message_cents": 200,
            "per_1k_input_token_cents": 50,
            "per_1k_output_token_cents": 150,
            "llm_markup_pct": 50.0,
            "min_charge_cents": 1,
        }),
        "rate_card_byo_llm": json.dumps({
            "credits_per_message_cents": 50,
            "credits_per_1k_input_tokens": 20,
            "credits_per_1k_output_tokens": 60,
            "per_message_cents": 80,
            "per_1k_input_token_cents": 20,
            "per_1k_output_token_cents": 60,
            "llm_markup_pct": 0,
            "min_charge_cents": 1,
        }),
        "platform_fee_pct": 20.0,
        "platform_fee_flat_cents": 0,
        "trial_config": json.dumps({}),
        "subscription_price_cents": 1000,
    }
    base.update(kwargs)
    return base


def _agent_cfg(scope, strategy=None, **kwargs):
    out = {"scope": scope, "strategy": strategy or "free"}
    out.update(kwargs)
    return out


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def db():
    d = FakeDB()
    d.tables["billing_configs"].append(_platform_cfg(strategy="free"))
    return d


# ── BYO detection ──

def test_byo_detection():
    assert _is_byo_llm({"metadata": json.dumps({"llm_config": {"use_default": True}})}) is False
    assert _is_byo_llm({"metadata": json.dumps({"llm_config": {"byo": True}})}) is True
    assert _is_byo_llm({"metadata": json.dumps({"llm_config": {"api_key": "sk-x"}})}) is True


# ── Free strategy ──

def test_free_returns_zero(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="free"))
    r = _run(resolve_charge(_agent(), "u1", Usage(input_tokens=100, output_tokens=200), db))
    assert r.end_user_charge_cents == 0
    assert r.platform_fee_cents == 0
    assert r.strategy == "free"
    assert not r.is_exempt and not r.is_trial


# ── Credits strategy ──

def test_credits_default_llm(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits"))
    # 1000 in tok @ 50c/1k + 500 out tok @ 150c/1k + 50% markup on $1 provider cost
    usage = Usage(input_tokens=1000, output_tokens=500, provider_cost_cents=100)
    r = _run(resolve_charge(_agent(byo=False), "u1", usage, db))
    # 50 + 75 + 50 = 175c
    assert r.end_user_charge_cents == 175
    # 20% platform fee
    assert r.platform_fee_cents == 35
    assert r.agent_admin_earnings_cents == 140
    assert r.strategy == "credits"
    assert r.is_byo_llm is False


def test_credits_byo_llm(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits"))
    usage = Usage(input_tokens=1000, output_tokens=500, provider_cost_cents=999_999)
    r = _run(resolve_charge(_agent(byo=True), "u1", usage, db))
    # BYO uses lower rates + 0% markup
    # 20 + 30 = 50c
    assert r.end_user_charge_cents == 50
    assert r.is_byo_llm is True
    assert r.platform_fee_cents + r.agent_admin_earnings_cents == r.end_user_charge_cents


# ── Per-message strategy ──

def test_per_message_default(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="per_message"))
    usage = Usage(input_tokens=0, output_tokens=0, provider_cost_cents=0)
    r = _run(resolve_charge(_agent(byo=False), "u1", usage, db))
    assert r.end_user_charge_cents == 200
    assert r.platform_fee_cents == 40
    assert r.agent_admin_earnings_cents == 160


def test_per_message_byo(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="per_message"))
    r = _run(resolve_charge(_agent(byo=True), "u1", Usage(), db))
    assert r.end_user_charge_cents == 80


# ── Per-token strategy ──

def test_per_token_default(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="per_token"))
    usage = Usage(input_tokens=2000, output_tokens=1000, provider_cost_cents=200)
    r = _run(resolve_charge(_agent(byo=False), "u1", usage, db))
    # 100 + 150 + 100 (markup) = 350
    assert r.end_user_charge_cents == 350
    assert r.platform_fee_cents + r.agent_admin_earnings_cents == 350


# ── Exemptions ──

def test_user_global_exemption_short_circuits(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="per_token"))
    db.tables["billing_exemptions"].append({"kind": "user", "user_id": "u1", "agent_id": None})
    r = _run(resolve_charge(_agent(), "u1", Usage(input_tokens=10000), db))
    assert r.is_exempt is True
    assert r.end_user_charge_cents == 0
    assert r.exempt_reason == "user"


def test_agent_wide_exemption_short_circuits(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits"))
    db.tables["billing_exemptions"].append({"kind": "agent", "agent_id": "agent-1", "user_id": None})
    r = _run(resolve_charge(_agent(), "anyone", Usage(input_tokens=999), db))
    assert r.is_exempt is True
    assert r.exempt_reason == "agent"


def test_user_for_agent_exemption(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="per_message"))
    db.tables["billing_exemptions"].append({
        "kind": "user_for_agent", "agent_id": "agent-1", "user_id": "u1"
    })
    r = _run(resolve_charge(_agent(), "u1", Usage(), db))
    assert r.is_exempt is True
    assert r.exempt_reason == "user_for_agent"
    # Other users still pay
    r2 = _run(resolve_charge(_agent(), "u2", Usage(), db))
    assert not r2.is_exempt
    assert r2.end_user_charge_cents > 0


# ── Trial ──

def test_trial_active_zero_charge(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits"))
    db.tables["trials"].append({
        "user_id": "u1", "agent_id": "agent-1",
        "messages_remaining": 5, "tokens_remaining": 1000,
    })
    r = _run(resolve_charge(_agent(), "u1", Usage(input_tokens=100, output_tokens=100), db))
    assert r.is_trial is True
    assert r.end_user_charge_cents == 0


def test_trial_expired_falls_through_to_strategy(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="per_message"))
    db.tables["trials"].append({
        "user_id": "u1", "agent_id": "agent-1",
        "messages_remaining": 0, "tokens_remaining": 0,
    })
    r = _run(resolve_charge(_agent(), "u1", Usage(), db))
    assert r.is_trial is False
    assert r.end_user_charge_cents > 0


# ── Subscription ──

def test_active_subscription_is_free(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="per_message"))
    db.tables["subscriptions"].append({
        "user_id": "u1", "agent_id": "agent-1", "status": "active",
    })
    r = _run(resolve_charge(_agent(), "u1", Usage(), db))
    assert r.end_user_charge_cents == 0
    assert r.strategy == "subscription"


def test_cancelled_subscription_falls_through(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="per_message"))
    db.tables["subscriptions"].append({
        "user_id": "u1", "agent_id": "agent-1", "status": "cancelled",
    })
    r = _run(resolve_charge(_agent(), "u1", Usage(), db))
    assert r.end_user_charge_cents == 200
