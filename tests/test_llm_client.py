import unittest

from app.llm_client import (
    _anthropic_headers,
    _anthropic_response_events,
    _anthropic_url,
    _usage_dict,
    build_anthropic_payload,
    build_chat_payload,
    prompt_cache_enabled,
)


class ChatPayloadTest(unittest.TestCase):
    def test_builds_existing_provider_neutral_shape_without_enabling_cache(self):
        messages = [{"role": "user", "content": "hello"}]
        tools = [{"type": "function", "function": {"name": "Ping"}}]

        payload = build_chat_payload(
            "example/model",
            messages,
            tools,
            max_tokens=0,
            reasoning_effort=" high ",
        )

        self.assertEqual(payload["model"], "example/model")
        self.assertIs(payload["messages"], messages)
        self.assertIs(payload["tools"], tools)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertEqual(payload["max_tokens"], 1)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertNotIn("cache_control", payload)
        self.assertNotIn("session_id", payload)

    def test_disabling_thinking_overrides_the_saved_effort(self):
        payload = build_chat_payload(
            "example/model",
            [],
            reasoning_effort="high",
            thinking_enabled=False,
        )

        self.assertEqual(payload["reasoning_effort"], "none")

    def test_omits_optional_fields_when_they_are_not_requested(self):
        payload = build_chat_payload("example/model", [])

        self.assertNotIn("tools", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("reasoning_effort", payload)


class PromptCachePayloadTest(unittest.TestCase):
    def setUp(self):
        self.provider = {
            "provider_type": "openrouter",
            "prompt_cache_ttl": "5m",
            "base_url": "https://openrouter.ai/api/v1",
        }

    def test_marks_last_stable_assistant_and_keeps_original_messages_untouched(self):
        messages = [
            {"role": "system", "content": "stable instructions"},
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "stable answer"},
            {"role": "user", "content": "current question"},
        ]
        tools = [
            {"type": "function", "function": {"name": "Zulu"}},
            {"type": "function", "function": {"name": "Alpha"}},
        ]

        payload = build_chat_payload(
            "anthropic/claude-sonnet-4",
            messages,
            tools,
            provider=self.provider,
            session_id="dwell-chat:test",
        )

        self.assertEqual(messages[2]["content"], "stable answer")
        self.assertEqual(
            payload["messages"][2]["content"],
            [{
                "type": "text",
                "text": "stable answer",
                "cache_control": {"type": "ephemeral"},
            }],
        )
        self.assertEqual(payload["messages"][3], messages[3])
        self.assertEqual(
            [tool["function"]["name"] for tool in payload["tools"]],
            ["Alpha", "Zulu"],
        )
        self.assertEqual(payload["session_id"], "dwell-chat:test")
        self.assertNotIn("cache_control", payload)

    def test_supports_one_hour_breakpoint_without_old_beta_header(self):
        provider = {**self.provider, "prompt_cache_ttl": "1h"}
        payload = build_chat_payload(
            "anthropic/claude-opus-4",
            [{"role": "assistant", "content": "anchor"}],
            provider=provider,
            session_id="dwell-chat:test",
        )

        marker = payload["messages"][0]["content"][0]["cache_control"]
        self.assertEqual(marker, {"type": "ephemeral", "ttl": "1h"})

    def test_cache_guards_leave_other_requests_provider_neutral(self):
        cases = [
            ({**self.provider, "provider_type": "generic"}, "anthropic/claude-sonnet-4", "chat"),
            ({**self.provider, "base_url": "https://relay.example/v1"}, "anthropic/claude-sonnet-4", "chat"),
            (self.provider, "google/gemini-2.5-pro", "chat"),
            (self.provider, "anthropic/claude-sonnet-4", None),
        ]
        for provider, model, session_id in cases:
            with self.subTest(provider=provider, model=model, session_id=session_id):
                payload = build_chat_payload(
                    model,
                    [{"role": "assistant", "content": "answer"}],
                    provider=provider,
                    session_id=session_id,
                )
                self.assertNotIn("session_id", payload)
                self.assertEqual(payload["messages"][0]["content"], "answer")

        self.assertTrue(prompt_cache_enabled(self.provider, "anthropic/claude-sonnet-4"))
        self.assertFalse(prompt_cache_enabled(self.provider, "openai/gpt-5"))

    def test_opt_in_claude_compatible_relay_uses_the_same_cache_marker(self):
        relay = {
            "provider_type": "claude_compatible",
            "prompt_cache_ttl": "5m",
            "base_url": "https://relay.example/v1",
        }
        payload = build_chat_payload(
            "[CCMAX]claude-opus-4-6",
            [{"role": "assistant", "content": "stable answer"}],
            provider=relay,
            session_id="dwell-chat:test",
        )

        self.assertEqual(
            payload["messages"][0]["content"][0]["cache_control"],
            {"type": "ephemeral"},
        )
        self.assertNotIn("session_id", payload)
        self.assertTrue(prompt_cache_enabled(relay, "[CCMAX]claude-opus-4-6"))
        self.assertFalse(prompt_cache_enabled(relay, "[AG]gemini-3.5-flash"))


class AnthropicMessagesPayloadTest(unittest.TestCase):
    def setUp(self):
        self.provider = {
            "provider_type": "claude_compatible",
            "prompt_cache_ttl": "1h",
            "base_url": "https://relay.example/v1",
        }

    def test_converts_system_history_tools_and_cache_anchor(self):
        messages = [
            {"role": "system", "content": "stable instructions"},
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "stable answer"},
            {"role": "user", "content": "next question"},
        ]
        tools = [{
            "type": "function",
            "function": {
                "name": "Lookup",
                "description": "Look something up",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            },
        }]

        payload = build_anthropic_payload(
            "[CCMAX]claude-opus-4-6",
            messages,
            tools,
            provider=self.provider,
            session_id="dwell-chat:test",
            reasoning_effort="high",
        )

        self.assertEqual(payload["system"], [{"type": "text", "text": "stable instructions"}])
        self.assertEqual([item["role"] for item in payload["messages"]], [
            "user", "assistant", "user",
        ])
        self.assertEqual(
            payload["messages"][1]["content"][0]["cache_control"],
            {"type": "ephemeral", "ttl": "1h"},
        )
        self.assertEqual(payload["tools"][0]["name"], "Lookup")
        self.assertEqual(payload["tools"][0]["input_schema"]["type"], "object")
        self.assertEqual(payload["max_tokens"], 4096)
        self.assertEqual(messages[2]["content"], "stable answer")

    def test_converts_tool_calls_results_and_images(self):
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "tool-1",
                "type": "function",
                "function": {"name": "Lookup", "arguments": '{"query":"cat"}'},
            }]},
            {"role": "tool", "tool_call_id": "tool-1", "content": "found"},
            {"role": "user", "content": [{
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
            }]},
        ]

        payload = build_anthropic_payload(
            "claude-opus-4-6", messages, provider=self.provider,
            session_id="dwell-chat:test",
        )

        tool_use = payload["messages"][0]["content"][0]
        self.assertEqual(tool_use["type"], "tool_use")
        self.assertEqual(tool_use["input"], {"query": "cat"})
        tool_result = payload["messages"][1]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        image = payload["messages"][1]["content"][1]
        self.assertEqual(image["source"], {
            "type": "base64", "media_type": "image/png", "data": "AAAA",
        })

    def test_builds_native_endpoint_and_both_auth_modes(self):
        self.assertEqual(
            _anthropic_url("https://relay.example/v1/"),
            "https://relay.example/v1/messages",
        )
        api_key_headers = _anthropic_headers("secret", "x-api-key")
        self.assertEqual(api_key_headers["x-api-key"], "secret")
        self.assertNotIn("Authorization", api_key_headers)
        self.assertEqual(api_key_headers["anthropic-version"], "2023-06-01")

        bearer_headers = _anthropic_headers("secret", "bearer")
        self.assertEqual(bearer_headers["Authorization"], "Bearer secret")
        self.assertNotIn("x-api-key", bearer_headers)


class AnthropicStreamParsingTest(unittest.IsolatedAsyncioTestCase):
    async def test_merges_usage_and_reassembles_tool_input(self):
        class Response:
            async def aiter_lines(self):
                lines = [
                    'data: {"type":"message_start","message":{"usage":{"input_tokens":1000,"cache_creation_input_tokens":700}}}',
                    'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":"h"}}',
                    'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"i"}}',
                    'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":"o"}}',
                    'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"k"}}',
                    'data: {"type":"content_block_start","index":2,"content_block":{"type":"tool_use","id":"tool-1","name":"Lookup","input":{}}}',
                    'data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"{\\"query\\":"}}',
                    'data: {"type":"content_block_delta","index":2,"delta":{"type":"input_json_delta","partial_json":"\\"cat\\"}"}}',
                    'data: {"type":"message_delta","usage":{"output_tokens":20,"cache_read_input_tokens":250}}',
                    'data: {"type":"message_stop"}',
                ]
                for line in lines:
                    yield line

        events = [event async for event in _anthropic_response_events(Response())]

        self.assertEqual(events[0], {"type": "thinking", "thinking": "h"})
        self.assertEqual(events[1], {"type": "thinking", "thinking": "i"})
        self.assertEqual(events[2:4], [
            {"type": "text", "text": "o"},
            {"type": "text", "text": "k"},
        ])
        self.assertEqual(events[-2]["calls"][0], {
            "id": "tool-1", "name": "Lookup", "arguments": '{"query":"cat"}',
        })
        usage = events[-1]["usage"]
        self.assertEqual(usage["input_tokens"], 1000)
        self.assertEqual(usage["context_input_tokens"], 1950)
        self.assertEqual(usage["total_tokens"], 1970)
        self.assertEqual(usage["output_tokens"], 20)
        self.assertEqual(usage["cache_write_tokens"], 700)
        self.assertEqual(usage["cached_tokens"], 250)


class UsageNormalizationTest(unittest.TestCase):
    def test_normalizes_openrouter_cache_and_cost_metrics(self):
        usage = _usage_dict({
            "prompt_tokens": 1200,
            "completion_tokens": 80,
            "total_tokens": 1280,
            "prompt_tokens_details": {
                "cached_tokens": 900,
                "cache_write_tokens": 250,
            },
            "completion_tokens_details": {"reasoning_tokens": 30},
            "cost": 0.01234567,
            "cost_details": {"upstream_inference_cost": 0.011},
        })

        self.assertEqual(usage, {
            "input_tokens": 50,
            "context_input_tokens": 1200,
            "output_tokens": 80,
            "total_tokens": 1280,
            "cached_tokens": 900,
            "cache_write_tokens": 250,
            "cache_write_5m_tokens": 0,
            "cache_write_1h_tokens": 0,
            "reasoning_tokens": 30,
            "cost": 0.01234567,
            "upstream_cost": 0.011,
        })

    def test_normalizes_anthropic_cache_breakdown(self):
        usage = _usage_dict({
            "input_tokens": 1000,
            "output_tokens": 20,
            "cache_read_input_tokens": 700,
            "cache_creation_input_tokens": 240,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 40,
                "ephemeral_1h_input_tokens": 200,
            },
        })

        self.assertEqual(usage["input_tokens"], 1000)
        self.assertEqual(usage["context_input_tokens"], 1940)
        self.assertEqual(usage["total_tokens"], 1960)
        self.assertEqual(usage["cached_tokens"], 700)
        self.assertEqual(usage["cache_write_tokens"], 240)
        self.assertEqual(usage["cache_write_5m_tokens"], 40)
        self.assertEqual(usage["cache_write_1h_tokens"], 200)

    def test_does_not_invent_usage_when_provider_reports_no_tokens(self):
        self.assertEqual(_usage_dict({"cost": 1.5}), {})


if __name__ == "__main__":
    unittest.main()
