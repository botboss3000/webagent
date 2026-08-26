"""Provider resolution must remain isolated from process-global environment."""

import os
import unittest
from unittest.mock import AsyncMock, patch

from app.admin import settings


class ProviderRunConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_custom_session_selection_survives_roster_reordering_by_entry_id(self):
        def entry(entry_id, model):
            return {
                "entry_id": entry_id, "provider": "openai",
                "base_url": "https://api.openai.com/v1", "api_key": entry_id,
                "model": model, "enabled": True, "text_capable": True,
            }

        selected = {
            "use_default": False, "selection_type": "custom",
            "entry_id": "model-b", "custom_position": 2,
        }
        for custom_order in (
            [entry("model-a", "a"), entry("model-b", "b")],
            [entry("model-b", "b"), entry("model-a", "a")],
        ):
            base = {
                **entry("standard", "standard"),
                "multi_providers": [entry("standard", "standard"), *custom_order],
            }
            with patch.object(settings, "_load_app_settings", return_value={
                "extend_llm_to_agents": True,
            }):
                resolved = settings._merge_agent_override(base, selected)
            self.assertEqual(resolved["entry_id"], "model-b")
            self.assertEqual(resolved["model"], "b")
            self.assertEqual(resolved["_slot_ref"], "entry:model-b")

    def test_selected_model_never_borrows_another_roster_key(self):
        base = {
            "provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
            "api_key": "deepseek-key", "model": "deepseek-v4-flash",
            "multi_providers": [
                {"provider": "abacus", "base_url": "https://routellm.abacus.ai/v1",
                 "api_key": "abacus-key", "model": "deepseek-ai/DeepSeek-V4-Pro", "high_effort_capable": True},
                {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
                 "api_key": "deepseek-key", "model": "deepseek-v4-flash", "enabled": True},
            ],
        }
        override = {
            "provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
            "api_key": "", "model": "deepseek-v4-flash",
            "multi_providers": [{"provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
                                 "api_key": "", "model": "deepseek-v4-flash", "enabled": False}],
        }
        with patch.object(settings, "_load_app_settings", return_value={"extend_llm_to_agents": True}):
            resolved = settings._merge_agent_override(base, override)
        self.assertEqual(resolved["api_key"], "deepseek-key")

    async def test_non_env_resolution_keeps_each_provider_key_local(self):
        original = {key: os.environ.get(key) for key in ("LLM_API_KEY", "LLM_MODEL")}
        os.environ["LLM_API_KEY"] = "unrelated-process-key"
        os.environ["LLM_MODEL"] = "unrelated-process-model"
        configs = {
            "one": {"provider": "openai", "base_url": "https://api.openai.com/v1", "api_key": "key-one", "model": "gpt-one"},
            "two": {"provider": "groq", "base_url": "https://api.groq.com/openai/v1", "api_key": "key-two", "model": "llama-two"},
        }
        try:
            with patch.object(settings, "_resolve_user_config", AsyncMock(side_effect=lambda user: configs[user])), \
                 patch.object(settings, "_ensure_tool_capable", AsyncMock(side_effect=lambda config, _user: config)), \
                 patch.object(settings, "_load_session_override", AsyncMock(return_value=None)), \
                 patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value={
                     "models": {"allow_byo": True, "max_byo_entries": 1,
                                "allowed_entry_ids": [], "roster_id": "roster-free"},
                 })):
                first = await settings.apply_provider_for_run("one", apply_env=False)
                second = await settings.apply_provider_for_run("two", apply_env=False)

            self.assertEqual((first["base_url"], first["api_key"], first["model"]),
                             ("https://api.openai.com/v1", "key-one", "gpt-one"))
            self.assertEqual((second["base_url"], second["api_key"], second["model"]),
                             ("https://api.groq.com/openai/v1", "key-two", "llama-two"))
            self.assertEqual(os.environ["LLM_API_KEY"], "unrelated-process-key")
            self.assertEqual(os.environ["LLM_MODEL"], "unrelated-process-model")
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    async def test_session_model_is_clamped_after_all_override_layers(self):
        base = {
            "provider": "openai", "base_url": "https://api.openai.com/v1",
            "api_key": "standard-key", "model": "standard",
            "_platform_roster_id": "roster-free",
            "multi_providers": [{
                "entry_id": "free-standard", "provider": "openai",
                "base_url": "https://api.openai.com/v1", "api_key": "standard-key",
                "model": "standard",
            }],
        }
        forbidden = {"use_default": False, "provider": "openai", "api_key": "injected",
                     "model": "premium", "model_effort": {"premium": "high"}}
        capabilities = {"models": {
            "roster_id": "roster-free", "allowed_entry_ids": ["free-standard"],
            "allow_byo": True, "max_byo_entries": 1, "max_reasoning_effort": "medium",
        }}
        with patch.object(settings, "_resolve_user_config", AsyncMock(return_value=base)), \
             patch.object(settings, "_ensure_tool_capable", AsyncMock(side_effect=lambda config, _user: config)), \
             patch.object(settings, "_load_session_override", AsyncMock(return_value=forbidden)), \
             patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value=capabilities)):
            result = await settings.apply_provider_for_run("user", apply_env=False)

        self.assertEqual(result["model"], "standard")
        self.assertEqual(result["api_key"], "standard-key")
        self.assertNotEqual(result.get("reasoning_effort"), "high")

    async def test_model_picker_only_exposes_clamped_roster_and_effort(self):
        base = {
            "provider": "openai", "base_url": "https://api.openai.com/v1",
            "api_key": "k1", "model": "standard", "_platform_roster_id": "roster-free",
            "multi_providers": [
                {"entry_id": "standard-id", "provider": "openai", "base_url": "https://api.openai.com/v1", "api_key": "k1", "model": "standard"},
                {"entry_id": "premium-id", "provider": "openai", "base_url": "https://api.openai.com/v1", "api_key": "k2", "model": "premium", "high_effort_capable": True},
            ],
        }
        session = {"model_effort": {"role:standard": "high"}}
        capabilities = {"models": {
            "roster_id": "roster-free", "allowed_entry_ids": ["standard-id"],
            "allow_byo": True, "max_byo_entries": 1, "max_reasoning_effort": "medium",
        }}
        with patch.object(settings, "_resolve_user_config", AsyncMock(return_value=base)), \
             patch.object(settings, "_load_session_override", AsyncMock(return_value=session)), \
             patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value=capabilities)):
            result = await settings.resolve_agent_models("user")

        self.assertEqual([slot["model"] for slot in result["slots"]], ["standard"])
        self.assertEqual(result["model_effort"]["role:standard"], "medium")
        self.assertNotIn("api_key", result["slots"][0])

    async def test_unavailable_platform_roster_yields_no_runtime_or_picker_model(self):
        base = {
            "provider": "openai", "base_url": "https://api.openai.com/v1",
            "api_key": "platform-key", "model": "fallback",
            "_platform_roster_id": "roster-missing",
            "multi_providers": [{
                "entry_id": "fallback-id", "provider": "openai",
                "base_url": "https://api.openai.com/v1", "api_key": "platform-key",
                "model": "fallback",
            }],
        }
        capabilities = {"models": {
            "roster_id": "roster-missing", "available": False,
            "allowed_entry_ids": [], "allow_byo": True, "max_byo_entries": 1,
            "max_reasoning_effort": "medium",
        }}
        with patch.object(settings, "_resolve_user_config", AsyncMock(return_value=base)), \
             patch.object(settings, "_load_session_override", AsyncMock(return_value=None)), \
             patch.object(settings, "_ensure_tool_capable", AsyncMock(side_effect=lambda config, _user: config)), \
             patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value=capabilities)):
            runtime = await settings.apply_provider_for_run("user", apply_env=False)
            picker = await settings.resolve_agent_models("user")

        self.assertEqual(runtime["model"], "")
        self.assertEqual(runtime["api_key"], "")
        self.assertEqual(picker["slots"], [])
