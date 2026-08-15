"""Provider resolution must remain isolated from process-global environment."""

import os
import unittest
from unittest.mock import AsyncMock, patch

from app.admin import settings


class ProviderRunConfigTests(unittest.IsolatedAsyncioTestCase):
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
                 patch.object(settings, "_load_session_override", AsyncMock(return_value=None)):
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
