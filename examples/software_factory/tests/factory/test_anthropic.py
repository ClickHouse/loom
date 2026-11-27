"""API requests, native tool blocks and failures retain their observable evidence."""

import json
import unittest

import httpx

from examples.software_factory.factory.anthropic_provider import (
    AnthropicFailure,
    AnthropicProvider,
    engine_blocks,
    wire_messages,
)


class AnthropicContracts(unittest.TestCase):
    def test_user_tool_results_preserve_following_text(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"text": "Context from the verification hook."},
                    {"toolResult": {"toolUseId": "first", "content": [{"json": {"passed": True}}]}},
                    {
                        "toolResult": {
                            "toolUseId": "second",
                            "content": [{"json": {"passed": False}}],
                        }
                    },
                ],
            }
        ]
        native = wire_messages(messages)[0]["content"]
        self.assertEqual(
            [block["type"] for block in native], ["tool_result", "tool_result", "text"]
        )
        self.assertEqual([block["tool_use_id"] for block in native[:2]], ["first", "second"])
        self.assertIn("text", messages[0]["content"][0])

    def test_native_reasoning_and_parallel_calls_round_trip(self):
        native = [
            {"type": "thinking", "thinking": "private fixture", "signature": "signed"},
            {"type": "tool_use", "id": "a", "name": "read", "input": {"path": "a.py"}},
            {"type": "tool_use", "id": "b", "name": "loom", "input": {"symbol": "A"}},
        ]
        messages = [{"role": "assistant", "content": engine_blocks(native)}]
        self.assertEqual(wire_messages(messages)[0]["content"], native)

    def test_direct_generation_retains_raw_usage_without_a_budget_gate(self):
        seen = []
        usage = {
            "input_tokens": 17,
            "output_tokens": 6,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

        def serve(request):
            seen.append((request.url.path, json.loads(request.content)))
            if request.url.path.endswith("count_tokens"):
                return httpx.Response(200, json={"input_tokens": 17})
            return httpx.Response(
                200,
                headers={"request-id": "req_fixture"},
                json={
                    "model": "claude-haiku-5-5",
                    "content": [{"type": "text", "text": "done"}],
                    "usage": usage,
                    "stop_reason": "end_turn",
                },
            )

        provider = self.provider(serve)
        try:
            result = provider.step([{"role": "user", "content": [{"text": "task"}]}], [], 20)
        finally:
            provider.close()
        self.assertEqual([p for p, _ in seen], ["/v1/messages"])
        self.assertEqual(result["usage"], usage)
        self.assertEqual(result["provider_request_id"], "req_fixture")
        self.assertEqual(result["physical_requests"], 1)
        self.assertEqual(result["counting_physical_requests"], 0)
        self.assertNotIn("temperature", seen[0][1])
        self.assertNotIn("tool_choice", seen[0][1])

    def test_large_input_has_no_local_token_gate(self):
        seen = []

        def serve(request):
            seen.append(request.url.path)
            return httpx.Response(
                200,
                json={
                    "model": "claude-haiku-5-5",
                    "content": [],
                    "usage": {"input_tokens": 30000, "output_tokens": 0},
                    "stop_reason": "end_turn",
                },
            )

        provider = self.provider(serve)
        try:
            provider.step([{"role": "user", "content": [{"text": "a" * 100000}]}], [], 20)
        finally:
            provider.close()
        self.assertEqual(seen, ["/v1/messages"])

    def test_failed_dispatch_is_not_retried(self):
        seen = []

        def serve(request):
            seen.append(request.url.path)
            if request.url.path.endswith("count_tokens"):
                return httpx.Response(200, json={"input_tokens": 10})
            return httpx.Response(503, headers={"request-id": "req_failed"})

        provider = self.provider(serve)
        try:
            with self.assertRaises(AnthropicFailure) as caught:
                provider.step([], [], 20)
        finally:
            provider.close()
        self.assertEqual(caught.exception.physical_requests, 1)
        self.assertEqual(caught.exception.provider_request_id, "req_failed")
        self.assertEqual(len(seen), 1)

    def test_tool_errors_are_sent_as_errors(self):
        content = [{"toolResult": {"toolUseId": "a", "content": [{"json": {"error": "bad"}}]}}]
        block = wire_messages([{"role": "user", "content": content}])[0]["content"][0]
        self.assertTrue(block["is_error"])
        self.assertEqual(json.loads(block["content"]), {"error": "bad"})

    @staticmethod
    def provider(serve):
        return AnthropicProvider(
            "fixture-key",
            "claude-haiku-5-5",
            approved_model_id="claude-haiku-5-5",
            transport=httpx.MockTransport(serve),
        )
