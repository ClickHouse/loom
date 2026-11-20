"""Optional client dependencies; actual transports with local stub responses."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import boto3
import httpx
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from loom import Agent

from examples.software_factory.bindings import bind_bedrock, bind_context
from examples.software_factory.live_support import ScopeVerifier, TransportAudit


class Body:
    def stream(self, **kwargs):
        yield json.dumps(
            {
                "output": {"message": {"role": "assistant", "content": [{"text": "done"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4},
                "metrics": {"latencyMs": 1},
            }
        ).encode()


class BindingContracts(unittest.TestCase):
    def test_actual_botocore_transport_counts_response_failure_and_pre_io_cap(self):
        # Nonfunctional fixture credentials prevent metadata/credential discovery.
        client = boto3.client(
            "bedrock-runtime",
            region_name="us-east-2",
            aws_access_key_id="fixture",
            aws_secret_access_key="fixture",
            config=Config(retries={"total_max_attempts": 1}, read_timeout=45, connect_timeout=5),
        )
        audit = TransportAudit(max_requests=2)
        provider = bind_bedrock(
            client, "fixture-model", approved_model_id="fixture-model", audit=audit
        )
        messages = [{"role": "user", "content": [{"text": "hi"}]}]
        tools = [{"name": "read", "description": "fixture", "input_schema": {"type": "object"}}]
        try:
            with patch.object(client._endpoint.http_session, "send") as send:
                send.return_value = AWSResponse(
                    "https://fixture.invalid",
                    200,
                    {"content-type": "application/json", "x-amzn-requestid": "fixture-aws-id"},
                    Body(),
                )
                response = provider.step(messages, tools, 5)
                self.assertEqual(response["physical_requests"], 1)
                self.assertEqual(response["provider_request_id"], "fixture-aws-id")
                self.assertEqual(audit.rows[-1]["request_id"], "fixture-aws-id")
                send.side_effect = OSError("fixture transport failure")
                with self.assertRaises(RuntimeError) as failure:
                    provider.step(messages, tools, 5)
                self.assertEqual(failure.exception.physical_requests, 1)
                with self.assertRaises(RuntimeError):
                    provider.step(messages, tools, 5)
                self.assertEqual(send.call_count, 2)
                self.assertEqual(audit.count(), 2)
        finally:
            client.close()

    def test_actual_sdk_transport_counts_scope_checks_and_preserves_server_ids(self):
        scope = {
            "org": "fixture",
            "namespace": "frozen",
            "repository": "pallets/flask",
            "commit": "a" * 40,
        }
        agent = Agent(
            org="fixture",
            namespace="frozen",
            agent="factory-fixture",
            base_url="http://fixture.invalid",
            api_key="fixture-static-not-a-key",
            auto_run=False,
            mentor_brief=False,
            pacing=False,
            retry={"max_attempts": 1},
        )
        seen = []

        def response(request):
            seen.append(request.url.path)
            data = (
                {
                    **{k: scope[k] for k in ("org", "namespace")},
                    "repo": "https://github.com/pallets/flask",
                    "git_head": "a" * 40,
                    "build_commit": "b" * 40,
                }
                if request.url.path.endswith("code.index_status")
                else {"definitions": []}
            )
            return httpx.Response(200, json=data, headers={"x-request-id": f"scope-{len(seen)}"})

        # Replace only network I/O in the private fixture, retaining SDK client/hooks/auth.
        agent.transport._client._transport = httpx.MockTransport(response)
        agent.transport._client._mounts = {}
        audit = TransportAudit(max_requests=3)
        context = bind_context(
            agent,
            ScopeVerifier(
                scope,
                index_repository="https://github.com/pallets/flask",
                build_commit="b" * 40,
                certificate_sha256="c" * 64,
            ),
            expected_scope=scope,
            audit=audit,
        )
        try:
            result = context.request(
                "code.symbol", {"org": "fixture", "namespace": "frozen", "name": "Flask"}
            )
            self.assertEqual(result["physical_requests"], 3)
            self.assertEqual(result["request_id"], "scope-2")
            self.assertEqual(
                [r["request_id"] for r in result["scope_checks"]], ["scope-1", "scope-3"]
            )
            with self.assertRaises(RuntimeError):
                context.request(
                    "code.symbol", {"org": "fixture", "namespace": "frozen", "name": "Flask"}
                )
            self.assertEqual(len(seen), 3)
            self.assertEqual(audit.count(), 3)
            self.assertNotIn("fixture-static-not-a-key", json.dumps(audit.rows))
        finally:
            context.close()

    def test_unqualified_authentication_side_traffic_refuses_before_context_binding(self):
        from types import SimpleNamespace

        agent = SimpleNamespace(transport=SimpleNamespace(_auth=object(), session_protection=None))
        with self.assertRaisesRegex(PermissionError, "side traffic"):
            bind_context(agent, None, expected_scope={}, audit=TransportAudit())


if __name__ == "__main__":
    unittest.main()
