import json
import unittest

import httpx

from app.agent.response_chaining import (
    _EventState,
    build_responses_request,
    response_chain_capability,
    response_chain_history_hash,
    response_event_chunks,
    responses_input,
    open_responses_stream,
)
from app.db.local import LocalBackend
from app.admin.settings import _assign_slots, _merge_agent_override, _resolve_slot


class ResponseChainingCapabilityTests(unittest.TestCase):
    def test_openresponses_providers_are_enabled(self):
        cases = (
            ("openai", "https://api.openai.com/v1", "gpt-5.6"),
            ("xai", "https://api.x.ai/v1", "grok-4.6"),
            ("openrouter", "https://openrouter.ai/api/v1", "anthropic/claude-opus-4.1"),
            ("azure", "https://example.openai.azure.com/openai/v1", "deployment"),
        )
        for provider, url, model in cases:
            with self.subTest(provider=provider):
                cap = response_chain_capability(
                    provider=provider, base_url=url, model=model,
                )
                self.assertTrue(cap.enabled)
                self.assertEqual(cap.transport, "responses")

    def test_stateless_only_provider_stays_on_chat_completions(self):
        cap = response_chain_capability(
            provider="deepseek", base_url="https://api.deepseek.com/v1", model="deepseek-chat",
        )
        self.assertFalse(cap.enabled)
        self.assertEqual(cap.transport, "chat_completions")

    def test_explicit_disable_wins(self):
        cap = response_chain_capability(
            provider="openai", base_url="https://api.openai.com/v1", model="gpt-5.6",
            mode="disabled",
        )
        self.assertFalse(cap.enabled)

    def test_provider_setting_survives_slot_and_agent_resolution(self):
        entry = {
            "entry_id": "xai", "provider": "xai", "base_url": "https://api.x.ai/v1",
            "api_key": "key", "model": "grok", "enabled": True,
            "text_capable": True, "stateful_responses": "disabled",
        }
        slots = _assign_slots([entry], default_model_id="grok")
        resolved = _resolve_slot(slots, "role", "standard")
        self.assertEqual(resolved["stateful_responses"], "disabled")
        merged = _merge_agent_override(
            {"stateful_responses": "auto"},
            {"stateful_responses": "disabled"},
        )
        self.assertEqual(merged["stateful_responses"], "disabled")


class ResponseRequestTests(unittest.TestCase):
    def test_chained_request_resends_instructions_and_only_sends_delta(self):
        messages = [
            {"role": "system", "content": "Current agent policy"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "system", "content": "Fresh wiki context"},
            {"role": "user", "content": "new question"},
        ]
        body = build_responses_request(
            model="gpt-5.6", messages=messages, tools=None,
            max_output_tokens=1000, previous_response_id="resp_old", delta_start=3,
        )
        self.assertEqual(body["previous_response_id"], "resp_old")
        self.assertEqual(body["input"], [{"role": "user", "content": "new question"}])
        self.assertIn("Current agent policy", body["instructions"])
        self.assertIn("Fresh wiki context", body["instructions"])

    def test_openrouter_uses_sticky_session_without_requesting_store(self):
        body = build_responses_request(
            model="openai/gpt-5.4", messages=[{"role": "user", "content": "hi"}],
            tools=None, max_output_tokens=1000, provider_family_name="openrouter",
            session_key="session",
        )
        self.assertFalse(body["store"])
        self.assertEqual(body["session_id"], "session")

    def test_tool_history_converts_to_responses_items(self):
        messages = [
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]
        items = responses_input(messages)
        self.assertEqual(items[0]["type"], "function_call")
        self.assertEqual(items[1], {
            "type": "function_call_output", "call_id": "call_1", "output": "result",
        })

    def test_history_hash_ignores_dynamic_system_layers(self):
        left = [{"role": "system", "content": "old"}, {"role": "user", "content": "same"}]
        right = [{"role": "system", "content": "new"}, {"role": "user", "content": "same"}]
        self.assertEqual(response_chain_history_hash(left), response_chain_history_hash(right))

    def test_stream_events_normalize_text_tools_and_usage(self):
        state = _EventState()
        text = response_event_chunks(
            {"type": "response.output_text.delta", "delta": "hello"}, state,
        )[0]
        self.assertEqual(text.choices[0].delta.content, "hello")

        added = response_event_chunks({
            "type": "response.output_item.added", "output_index": 2,
            "item": {"type": "function_call", "id": "item_1", "call_id": "call_1", "name": "lookup"},
        }, state)[0]
        self.assertEqual(added.choices[0].delta.tool_calls[0].function.name, "lookup")
        args = response_event_chunks({
            "type": "response.function_call_arguments.delta", "item_id": "item_1", "delta": "{}",
        }, state)[0]
        self.assertEqual(args.choices[0].delta.tool_calls[0].index, 2)

        usage = response_event_chunks({
            "type": "response.completed",
            "response": {"usage": {"input_tokens": 10, "output_tokens": 3,
                                     "input_tokens_details": {"cached_tokens": 7}}},
        }, state)[0]
        self.assertEqual(usage.usage.prompt_tokens, 10)
        self.assertEqual(usage.usage.prompt_tokens_details["cached_tokens"], 7)


class ResponseChainPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_sse_adapter_streams_and_captures_response_id(self):
        async def handler(request):
            self.assertEqual(request.url.path, "/v1/responses")
            self.assertEqual(request.headers["authorization"], "Bearer secret")
            body = json.loads(request.content)
            self.assertTrue(body["stream"])
            sse = (
                'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n'
                'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
                'data: {"type":"response.completed","response":{"id":"resp_1",'
                '"usage":{"input_tokens":4,"output_tokens":1}}}\n\n'
                'data: [DONE]\n\n'
            )
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

        stream = await open_responses_stream(
            base_url="https://example.test/v1", api_key="secret", family="xai",
            body={"model": "grok", "input": "hi", "stream": True},
            transport=httpx.MockTransport(handler),
        )
        chunks = [chunk async for chunk in stream]
        await stream.close()
        self.assertEqual(stream.response_id, "resp_1")
        self.assertEqual(chunks[0].choices[0].delta.content, "hello")
        self.assertEqual(chunks[1].usage.prompt_tokens, 4)

    async def test_chain_state_round_trips_without_clobbering_metadata(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = LocalBackend(f"{tmp}/test.db", seed=False)
            conn = db._get_conn()
            conn.execute(
                "INSERT INTO sessions (id,user_id,metadata) VALUES (?,?,?)",
                ("session", "user", json.dumps({"keep": True})),
            )
            conn.commit()
            conn.close()

            state = {
                "transport": "responses", "family": "xai", "identity": "identity",
                "model": "grok", "previous_response_id": "resp_1",
                "history_hash": "history", "updated_at": 1,
            }
            await db.set_session_response_chain("session", state)
            self.assertEqual(await db.get_session_response_chain("session"), state)

            await db.set_session_response_chain("session", None)
            self.assertIsNone(await db.get_session_response_chain("session"))
            conn = db._get_conn()
            metadata = json.loads(conn.execute(
                "SELECT metadata FROM sessions WHERE id='session'"
            ).fetchone()[0])
            conn.close()
            self.assertTrue(metadata["keep"])


if __name__ == "__main__":
    unittest.main()
