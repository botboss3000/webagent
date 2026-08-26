from app import abilities


def test_memory_is_a_configurable_knowledge_ability():
    abilities.reload()
    catalog = abilities.ui_catalog()

    knowledge = next(group for group in catalog["groups"] if group["name"] == "Knowledge")
    assert knowledge["id"] == "memory"
    assert knowledge["members"][:2] == ["memory", "wiki_context"]

    memory = catalog["abilities"]["memory"]
    assert memory["display_name"] == "Memory"
    assert memory["tools"] == ["memory"]
    assert memory["simple"] is False

    settings = {
        field["key"]: field
        for field in abilities.ability_config_schema("memory")["settings"]
    }
    assert settings["memory_recall"]["default"] is True
    assert settings["memory_save"]["default"] is True


def test_memory_tool_is_not_duplicated_under_core_base():
    abilities.reload()
    catalog = abilities.ui_catalog()

    assert "memory" not in catalog["abilities"]["base"]["tools"]
    assert abilities.virtual_ability_for_tool("memory") == "memory"
