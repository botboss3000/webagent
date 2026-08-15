from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile

from app.agent.cache_profiles import (
    PROFILE_ABILITIES,
    ordered_ability_ids,
    profile_abilities,
    profile_from_metadata,
    profile_layer_blocks,
    with_cache_profile,
)
from app.agent.prompts import build_system_prompt_parts
from app.agent.loop import _build_layered_messages
from app.db.local import LocalBackend
from app.tools.tool_modes import render_ability_index


def run(coro):
    return asyncio.run(coro)


def test_profiles_are_nested_prefixes():
    simple = PROFILE_ABILITIES["simple"]
    standard = PROFILE_ABILITIES["standard"]
    advanced = PROFILE_ABILITIES["advanced"]

    assert standard[: len(simple)] == simple
    assert advanced[: len(standard)] == standard
    assert profile_abilities("advanced") == list(advanced)


def test_profile_layers_are_exact_nested_message_prefixes():
    simple = profile_layer_blocks("simple")
    standard = profile_layer_blocks("standard")
    advanced = profile_layer_blocks("advanced")
    assert standard[: len(simple)] == simple
    assert advanced[: len(standard)] == standard


def test_profile_order_is_stable_and_extensions_follow_profile():
    unordered = {
        "automation",
        "visualizer",
        "web_access",
        "browser_control",
        "email",
    }
    ordered = ordered_ability_ids(unordered)
    assert ordered[:4] == [
        "web_access",
        "visualizer",
        "browser_control",
        "automation",
    ]
    assert ordered[-1] == "email"


def test_ability_index_ignores_message_ranking_for_shared_cache_block():
    entries = [
        {"id": "automation", "name": "Automation", "desc": "Schedule work."},
        {"id": "web_access", "name": "Web Access", "desc": "Search the web."},
        {"id": "visualizer", "name": "Visualizer", "desc": "Render a UI."},
    ]
    first = render_ability_index(entries, order=["automation", "visualizer", "web_access"])
    second = render_ability_index(entries, order=["visualizer", "web_access", "automation"])
    assert first == second
    assert first.index("`web_access`") < first.index("`visualizer`") < first.index("`automation`")


def test_prompt_parts_keep_brain_context_out_of_shared_prefix(monkeypatch):
    monkeypatch.setattr(
        "app.admin.settings.get_global_system_prompt",
        lambda: "GLOBAL CORE",
    )
    parts = run(build_system_prompt_parts(
        [{"context_type": "agent", "content": "AGENT PERSONA"}],
        brain_context="USER MEMORY",
    ))
    assert parts.shared_core == "GLOBAL CORE"
    assert "AGENT PERSONA" in parts.agent_context
    assert "USER MEMORY" in parts.turn_context
    assert "USER MEMORY" not in parts.shared_core


def test_layered_messages_put_turn_context_after_history():
    messages = _build_layered_messages(
        shared_system="PLATFORM",
        capability_parts=["SIMPLE", "STANDARD"],
        agent_system="PERSONA",
        turn_parts=["MEMORY", "ROUTING HINT"],
        history=[
            {"role": "user", "content": "earlier"},
            {"role": "assistant", "content": "reply"},
        ],
        user_message="current",
    )
    assert [m["role"] for m in messages] == [
        "system", "system", "system", "system", "user", "assistant", "system", "user",
    ]
    assert messages[0]["content"] == "PLATFORM"
    assert messages[1]["content"] == "SIMPLE"
    assert messages[2]["content"] == "STANDARD"
    assert messages[-2]["content"] == "MEMORY\n\nROUTING HINT"


def test_custom_agent_profile_persists_metadata_and_exact_abilities():
    with tempfile.TemporaryDirectory() as tmp:
        db = LocalBackend(db_path=f"{tmp}/cache-profile.db")
        agent = run(db.create_custom_agent(
            user_id="user-1",
            name="Standard Agent",
            template_id="none",
            seed_abilities=False,
            capability_profile="standard",
            capability_extensions=["browser_control"],
        ))

        metadata = json.loads(agent["metadata"])
        assert profile_from_metadata(metadata) == "standard"
        assert metadata["discovery_default"] == "discoverable"
        assert metadata["capability_extensions"] == ["browser_control"]

        conn = sqlite3.connect(f"{tmp}/cache-profile.db")
        rows = conn.execute(
            """SELECT connection_type FROM agent_connections
               WHERE agent_id = ? AND section = 'ability' AND enabled = 1""",
            (agent["id"],),
        ).fetchall()
        conn.close()
        enabled = {row[0] for row in rows}
        assert enabled == set(PROFILE_ABILITIES["standard"]) | {"browser_control"}


def test_default_webagent_is_provisioned_as_exact_advanced_profile():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = f"{tmp}/default-profile.db"
        db = LocalBackend(db_path=db_path)
        agent = run(db.create_agent_for_user("new-user"))

        metadata = json.loads(agent["metadata"])
        assert profile_from_metadata(metadata) == "advanced"
        assert metadata["pre_enabled_connections"] == list(PROFILE_ABILITIES["advanced"])

        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """SELECT connection_type FROM agent_connections
               WHERE agent_id = ? AND section = 'ability' AND enabled = 1""",
            (agent["id"],),
        ).fetchall()
        conn.close()
        assert {row[0] for row in rows} == set(PROFILE_ABILITIES["advanced"])


def test_default_profile_metadata_is_advanced_and_discoverable():
    metadata = with_cache_profile({"pre_enabled_connections": ["*"]}, "advanced")
    assert metadata["capability_profile"] == "advanced"
    assert metadata["discovery_default"] == "discoverable"
    assert metadata["pre_enabled_connections"] == list(PROFILE_ABILITIES["advanced"])
