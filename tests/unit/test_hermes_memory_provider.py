"""Offline behavior checks; Hermes doubles do not qualify its real plugin loader."""

from __future__ import annotations

import http.client
import importlib.util
import io
import json
import sys
import types
import unittest
import urllib.error
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

PLUGIN = Path(__file__).resolve().parents[2] / "integrations" / "hermes-memory"
SECRETS = {
    "HERMES_LOOM_BASE_URL": "https://loom.example.com",
    "HERMES_LOOM_API_KEY": "test-token-not-a-credential",
    "HERMES_LOOM_ORG": "example-org",
    "HERMES_LOOM_NAMESPACE": "example-memory",
    "HERMES_LOOM_USER_ID": "example-user",
}


class _ProviderDouble:
    def prefetch(self, query, *, session_id=""):
        return ""

    def queue_prefetch(self, query, *, session_id=""):
        pass

    def sync_turn(self, *args, **kwargs):
        pass

    def on_session_end(self, messages):
        pass

    def on_memory_write(self, *args, **kwargs):
        pass

    def on_session_switch(self, *args, **kwargs):
        pass


class _Response:
    def __init__(self, body, *, url="https://loom.example.com/v1/memory.search"):
        self.raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.url = url
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def read(self, size):
        return self.raw[:size]

    def geturl(self):
        return self.url


class _BrokenBody(io.BytesIO):
    def __init__(self, failure):
        super().__init__(b"private diagnostic")
        self.failure = failure

    def read(self, size=-1):
        raise self.failure


class HermesMemoryProviderTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.secret = Mock(side_effect=SECRETS.get)
        self.open = Mock()
        modules = {}
        for name in (
            "agent",
            "agent.memory_provider",
            "agent.secret_scope",
            "hermes_cli",
            "hermes_cli.urllib_security",
        ):
            modules[name] = types.ModuleType(name)
            self.stack.enter_context(patch.dict(sys.modules, {name: modules[name]}))
        modules["agent.memory_provider"].MemoryProvider = _ProviderDouble
        modules["agent.secret_scope"].get_secret = self.secret
        modules["hermes_cli.urllib_security"].open_credentialed_url = self.open
        spec = importlib.util.spec_from_file_location(
            "_loom_hermes_test", PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)]
        )
        assert spec is not None and spec.loader is not None
        self.package = importlib.util.module_from_spec(spec)
        self.stack.enter_context(patch.dict(sys.modules, {spec.name: self.package}))
        spec.loader.exec_module(self.package)
        self.provider = self.package.LoomMemoryProvider()
        self.provider.initialize("native-session", hermes_home="example-profile")
        self.addCleanup(self.provider.shutdown)

    def reply(self, body, operation="memory.search"):
        response = _Response(body, url=f"https://loom.example.com/v1/{operation}")
        self.open.return_value = response
        return response

    def call(self, name, args, **kwargs):
        return json.loads(self.provider.handle_tool_call(name, args, **kwargs))

    def sent(self):
        request = self.open.call_args.args[0]
        return request, json.loads(request.data)

    def test_register_constructs_an_instance_per_profile(self):
        ctx = Mock()
        self.package.register(ctx)
        self.package.register(ctx)
        instances = [call.args[0] for call in ctx.register_memory_provider.call_args_list]
        self.assertIsNot(instances[0], instances[1])
        self.assertEqual(instances[0].name, "loom-memory")
        self.open.assert_not_called()

    def test_availability_uses_scoped_secrets_without_network(self):
        self.assertTrue(self.provider.is_available())
        self.secret.side_effect = lambda name: (
            "*" if name == "HERMES_LOOM_USER_ID" else SECRETS.get(name)
        )
        self.assertFalse(self.provider.is_available())
        self.open.assert_not_called()
        self.assertTrue(
            all(field["env_var"] and field["secret"] for field in self.provider.get_config_schema())
        )

    def test_native_schema_shape_and_closed_scope(self):
        schemas = {item["name"]: item for item in self.provider.get_tool_schemas()}
        self.assertEqual(set(schemas), {"loom_recall", "loom_read", "loom_remember"})
        for item in schemas.values():
            self.assertNotIn("function", item)
            self.assertFalse(item["parameters"]["additionalProperties"])
            self.assertNotIn("namespace", item["parameters"]["properties"])
        self.assertNotIn("if_match_sha256", schemas["loom_remember"]["parameters"]["required"])

    def test_search_scope_has_no_session_filter_or_recall_writes(self):
        body = {"abstain": True, "results": [], "abstain_reason": "no evidence"}
        response = self.reply(body)
        self.assertEqual(self.call("loom_recall", {"query": "prior deployment decision"}), body)
        request, payload = self.sent()
        self.assertEqual(request.full_url, "https://loom.example.com/v1/memory.search")
        self.assertEqual(
            payload,
            {
                "org": "example-org",
                "namespace": "example-memory",
                "agent": "*",
                "user_id": "example-user",
                "query": "prior deployment decision",
                "top_k": 4,
                "scope": "private",
                "read_scope": "own_user",
                "namespace_scope": "this",
                "search_mode": "rrf",
                "auto_touch": False,
                "explain": False,
            },
        )
        self.assertEqual(self.open.call_args.kwargs["timeout"], 5.0)
        self.assertNotIn("X-loom-session-protection", request.headers)
        self.assertTrue(response.closed)

    def test_point_fact_identity_survives_session_switch(self):
        self.provider.on_session_switch("resumed-session")
        self.reply(
            {"found": True, "value": "Use the staging cluster", "code": [{"state": "changed"}]},
            "memory.get",
        )
        body = self.call("loom_read", {"memory_key": "deployment/cluster"})
        _, payload = self.sent()
        self.assertEqual(payload["session_id"], "-")
        self.assertEqual(payload["user_id"], "example-user")
        self.assertEqual(body["code"][0]["state"], "changed")

    def test_explicit_write_keeps_derived_provenance_cas_and_receipt(self):
        receipt = {
            "memory_id": "example-row",
            "version_text": "9007199254740993",
            "conflict_verdict": "conflict",
            "status": "proposed",
            "content_sha256": "b" * 64,
        }
        self.reply(receipt, "memory.set")
        self.assertEqual(
            self.call(
                "loom_remember",
                {
                    "memory_key": "deployment/cluster",
                    "content": "Use the staging cluster",
                    "if_match_sha256": "a" * 64,
                },
            ),
            receipt,
        )
        request, payload = self.sent()
        self.assertEqual(payload["source_class"], "derived")
        self.assertEqual(payload["confidence"], 0.5)
        self.assertEqual(payload["if_match_sha256"], "a" * 64)
        self.assertNotIn("source_trace", payload)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token-not-a-credential")
        self.assertNotIn("X-loom-session-protection", request.headers)

    def test_no_automatic_hook_sends_session_content(self):
        self.assertEqual(self.provider.prefetch("private prompt"), "")
        self.provider.queue_prefetch("private prompt")
        self.provider.sync_turn("private prompt", "private answer")
        self.provider.on_session_end([{"content": "private transcript"}])
        self.provider.on_memory_write("add", "memory", "private note")
        self.open.assert_not_called()

    def test_model_cannot_override_profile_scope_or_input_caps(self):
        for name, args in [
            ("loom_recall", {"query": "fact", "namespace": "other"}),
            ("loom_remember", {"memory_key": "k", "content": "fact", "user_id": "other"}),
            ("loom_recall", {"query": "a" * 2001}),
            ("loom_recall", {"query": "unpaired surrogate: \ud800"}),
            ("loom_remember", {"memory_key": "k", "content": "a" * 8001}),
            ("loom_remember", {"memory_key": "k", "content": "fact", "if_match_sha256": "wrong"}),
            ("unknown", {}),
        ]:
            with self.subTest(name=name, args=list(args)):
                self.assertIn("error", self.call(name, args))
        self.assertEqual(
            self.call("loom_recall", {"query": "fact"}, hermes_home="other-profile")["error"],
            "PROFILE_MISMATCH",
        )
        with self.assertRaises(ValueError):
            self.provider.initialize("another-session", hermes_home="other-profile")
        self.open.assert_not_called()

    def test_http_refusals_do_not_retry_or_expose_server_messages(self):
        for status, code in [
            (401, "UNAUTHENTICATED"),
            (403, "FORBIDDEN"),
            (409, "SESSION_PROTECTION_REQUIRED"),
        ]:
            with self.subTest(status=status):
                self.open.reset_mock()
                error = {"error": {"code": code, "message": "private diagnostic"}}
                self.open.side_effect = urllib.error.HTTPError(
                    "https://loom.example.com",
                    status,
                    "refused",
                    {},
                    io.BytesIO(json.dumps(error).encode()),
                )
                result = self.call("loom_remember", {"memory_key": "k", "content": "fact"})
                self.assertEqual(result, {"error": code, "retryable": False, "http_status": status})
                self.open.assert_called_once()

    def test_uncertain_writes_are_not_reported_as_unsaved(self):
        self.open.side_effect = TimeoutError()
        result = self.call("loom_remember", {"memory_key": "k", "content": "fact"})
        self.assertIn("write_outcome", result)
        self.assertNotIn("persisted", result)
        self.open.assert_called_once()

    def test_truncated_success_reply_returns_unknown_write_outcome(self):
        response = self.reply({}, "memory.set")
        response.read = Mock(side_effect=http.client.IncompleteRead(b"partial", 12))
        result = self.call("loom_remember", {"memory_key": "k", "content": "fact"})
        self.assertEqual(result["error"], "CONNECTION_FAILED")
        self.assertIn("write_outcome", result)
        self.assertNotIn("persisted", result)
        self.assertTrue(response.closed)
        self.open.assert_called_once()

    def test_truncated_read_reply_has_no_write_outcome(self):
        response = self.reply({})
        response.read = Mock(side_effect=http.client.IncompleteRead(b"partial", 12))
        self.assertEqual(
            self.call("loom_recall", {"query": "fact"}),
            {"error": "CONNECTION_FAILED", "retryable": False},
        )
        self.assertTrue(response.closed)
        self.open.assert_called_once()

    def test_error_body_failures_keep_status_and_write_uncertainty(self):
        for status in (409, 503):
            for failure in (
                TimeoutError("private diagnostic"),
                http.client.IncompleteRead(b"private", 12),
            ):
                for tool, args in (
                    ("loom_recall", {"query": "fact"}),
                    ("loom_remember", {"memory_key": "k", "content": "fact"}),
                ):
                    with self.subTest(status=status, failure=type(failure).__name__, tool=tool):
                        self.open.reset_mock()
                        body = _BrokenBody(failure)
                        self.open.side_effect = urllib.error.HTTPError(
                            "https://loom.example.com", status, "refused", {}, body
                        )
                        result = self.call(tool, args)
                        self.assertEqual(result["http_status"], status)
                        self.assertEqual(result["error"], f"HTTP_{status}")
                        self.assertFalse(result["retryable"])
                        self.assertEqual(
                            "write_outcome" in result, tool == "loom_remember" and status >= 500
                        )
                        self.assertNotIn("persisted", result)
                        self.assertNotIn("private", json.dumps(result))
                        self.assertTrue(body.closed)
                        self.open.assert_called_once()

    def test_oversized_malformed_and_redirected_responses_refuse(self):
        for body, url, code in [
            (b"x" * 65_537, "https://loom.example.com/v1/memory.search", "RESPONSE_TOO_LARGE"),
            (b"not JSON", "https://loom.example.com/v1/memory.search", "INVALID_RESPONSE"),
            ([], "https://loom.example.com/v1/memory.search", "INVALID_RESPONSE"),
            ({}, "https://different.example.com/v1/memory.search", "REDIRECT_REFUSED"),
        ]:
            with self.subTest(code=code):
                response = _Response(body, url=url)
                self.open.return_value = response
                self.assertEqual(self.call("loom_recall", {"query": "fact"})["error"], code)
                self.assertTrue(response.closed)

    def test_mirrored_read_latches_write_refusal_across_session_switch(self):
        body = {"results": [{"value": {"mirrored": True, "content": "source text"}}]}
        self.reply(body)
        self.assertEqual(self.call("loom_recall", {"query": "fact"}), body)
        self.provider.on_session_switch("child-session", parent_session_id="native-session")
        result = self.call("loom_remember", {"memory_key": "k", "content": "fact"})
        self.assertEqual(result["error"], "PROVIDER_WRITE_REFUSED")
        self.open.assert_called_once()

    def test_subagents_cannot_write_and_shutdown_cannot_rebind(self):
        provider = self.package.LoomMemoryProvider()
        provider.initialize("child", hermes_home="example-profile", agent_context="subagent")
        result = json.loads(
            provider.handle_tool_call("loom_remember", {"memory_key": "k", "content": "fact"})
        )
        self.assertEqual(result["error"], "PROVIDER_WRITE_REFUSED")
        provider.shutdown()
        result = json.loads(provider.handle_tool_call("loom_recall", {"query": "fact"}))
        self.assertEqual(result["error"], "PROVIDER_NOT_ACTIVE")
        with self.assertRaises(ValueError):
            provider.initialize("again", hermes_home="example-profile")
        self.open.assert_not_called()

    def test_url_policy_rejects_credentials_paths_and_remote_cleartext(self):
        for url in [
            "http://remote.example.com",
            "https://name:secret@loom.example.com",
            "https://loom.example.com/mcp",
            "https://loom.example.com?token=secret",
            "https://loom.example.com:0",
            "https://loom.example.com:wrong",
        ]:
            with self.subTest(url=url):
                self.secret.side_effect = lambda name, url=url: (
                    url if name == "HERMES_LOOM_BASE_URL" else SECRETS.get(name)
                )
                self.assertFalse(self.provider.is_available())
        for url in ["https://loom.example.com", "http://127.0.0.1:7777", "http://[::1]:7777"]:
            with self.subTest(url=url):
                self.secret.side_effect = lambda name, url=url: (
                    url if name == "HERMES_LOOM_BASE_URL" else SECRETS.get(name)
                )
                self.assertTrue(self.provider.is_available())
        self.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
