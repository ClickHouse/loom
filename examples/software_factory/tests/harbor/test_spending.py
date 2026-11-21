"""A provider dispatch is reserved durably and never retried with unknown cost."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from examples.software_factory.harbor.gateway import Gateway
from examples.software_factory.harbor.spending import Spending


class SpendingContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "spending.jsonl"
        self.journal = Spending(self.path, dollars="1", model="claude-haiku-5-5")
        self.addCleanup(self.journal.close)
        self.payload = {
            "model": "claude-haiku-5-5",
            "max_tokens": 1000,
            "messages": [{"role": "user", "content": "Fix the issue."}],
        }
        self.headers = {
            "x-api-key": "private-gate",
            "anthropic-version": "2023-06-01",
            "x-factory-role": "solver",
        }

    def response(self, *, model="claude-haiku-5-5", **usage):
        return {
            "model": model,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                **usage,
            },
            "content": [],
        }

    def gateway(self, handler):
        gateway = Gateway(
            self.journal, "UPSTREAM_SECRET", "private-gate", transport=httpx.MockTransport(handler)
        )
        self.addCleanup(gateway.close)
        return gateway

    def test_reservation_is_flushed_before_upstream_and_usage_is_priced(self):
        calls = []

        def provider(request):
            rows = [json.loads(line) for line in self.path.read_text().splitlines()]
            self.assertEqual(rows[-1]["event"], "reserved")
            self.assertEqual(request.headers["x-api-key"], "UPSTREAM_SECRET")
            calls.append(request)
            return httpx.Response(
                200,
                headers={"request-id": "actual-request"},
                json={
                    "model": "claude-haiku-5-5",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                    "content": [],
                },
            )

        result = self.gateway(provider).forward(self.payload, self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(str(self.journal.spent), "0.00002")
        self.assertEqual(self.journal.pending, {})
        self.assertNotIn("UPSTREAM_SECRET", self.path.read_text())
        self.assertNotIn("private-gate", self.path.read_text())
        self.assertNotIn("Fix the issue", self.path.read_text())

    def test_failed_dispatch_retains_reservation_and_blocks_the_next(self):
        calls = []

        def provider(request):
            calls.append(request)
            raise httpx.ReadTimeout("response lost", request=request)

        gateway = self.gateway(provider)
        with self.assertRaises(httpx.ReadTimeout):
            gateway.forward(self.payload, self.headers)
        with self.assertRaises(PermissionError):
            gateway.forward(self.payload, self.headers)
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.journal.pending)
        self.assertTrue(self.journal.blocked)

    def test_wrong_credentials_or_model_never_dispatch(self):
        def provider(_):
            self.fail("A refused request reached the paid provider")

        gateway = self.gateway(provider)
        with self.assertRaises(PermissionError):
            gateway.forward(self.payload, {**self.headers, "x-api-key": "wrong"})
        with self.assertRaises(PermissionError):
            gateway.forward({**self.payload, "model": "other-model"}, self.headers)
        self.assertFalse(any(row["event"] == "reserved" for row in self.journal.rows))

    def test_concurrent_requests_cannot_reserve_more_than_the_allowance(self):
        self.journal.reserve({**self.payload, "max_tokens": 128000}, role="solver")
        self.journal.reserve({**self.payload, "max_tokens": 128000}, role="solver")
        self.journal.reserve({**self.payload, "max_tokens": 128000}, role="judge")
        with self.assertRaises(PermissionError):
            self.journal.reserve({**self.payload, "max_tokens": 128000}, role="solver")

    def test_missing_or_cached_usage_remains_unknown(self):
        for index, usage in enumerate(
            ({}, {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 5})
        ):
            with self.subTest(usage=usage):
                meter = Spending(
                    self.path.with_name(f"case-{index}.jsonl"),
                    dollars="1",
                    model="claude-haiku-5-5",
                )
                gateway = Gateway(
                    meter,
                    "UPSTREAM_SECRET",
                    "private-gate",
                    transport=httpx.MockTransport(
                        lambda _, usage=usage: httpx.Response(
                            200,
                            headers={"request-id": "request"},
                            json={"model": "claude-haiku-5-5", "usage": usage},
                        )
                    ),
                )
                try:
                    with self.assertRaises((KeyError, ValueError)):
                        gateway.forward(self.payload, self.headers)
                    self.assertTrue(meter.blocked)
                    self.assertEqual(meter.spent, 0)
                finally:
                    gateway.close()
                    meter.close()

    def test_restart_refuses_to_replay_an_unsettled_dispatch(self):
        self.journal.reserve(self.payload, role="solver")
        self.journal.close()
        restored = Spending(self.path, dollars="1", model="claude-haiku-5-5")
        self.addCleanup(restored.close)
        with self.assertRaises(PermissionError):
            restored.reserve(self.payload, role="solver")

    def test_altered_journal_and_changed_allowance_refuse(self):
        self.journal.reserve(self.payload, role="solver")
        self.journal.close()
        with self.assertRaises(PermissionError):
            Spending(self.path, dollars="2", model="claude-haiku-5-5")
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        rows[-1]["reserved_usd"] = "0"
        self.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        with self.assertRaises(PermissionError):
            Spending(self.path, dollars="1", model="claude-haiku-5-5")

    def test_missing_cache_fields_cannot_establish_a_known_uncached_price(self):
        gateway = self.gateway(
            lambda _: httpx.Response(
                200,
                headers={"request-id": "id"},
                json={
                    "model": "claude-haiku-5-5",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
            )
        )
        with self.assertRaises((ValueError, KeyError)):
            gateway.forward(self.payload, self.headers)
        self.assertTrue(self.journal.blocked)
        self.assertTrue(self.journal.pending)

    def test_native_default_is_priced_separately_without_lowering_its_output_limit(self):
        gateway = self.gateway(
            lambda _: httpx.Response(
                200,
                headers={"request-id": "native-id"},
                json=self.response(model="claude-haiku-4-5-20251001"),
            )
        )
        gateway.forward(
            {**self.payload, "model": "claude-haiku-4-5", "max_tokens": 8192},
            {**self.headers, "x-factory-role": "loom"},
        )
        self.assertEqual(str(self.journal.spent), "0.0002")
        row = self.journal.rows[-1]
        self.assertEqual(row["request_model"], "claude-haiku-4-5")
        self.assertEqual(row["response_model"], "claude-haiku-4-5-20251001")
        self.assertEqual(row["input_usd_per_million"], "1")
        self.assertEqual(row["output_usd_per_million"], "5")

    def test_inflight_settlement_and_failure_keep_the_reserved_phase(self):
        self.journal.phase = "setup"
        ticket = self.journal.reserve(self.payload, role="solver")
        self.journal.phase = "live:7"
        self.journal.settle(ticket, self.response(), "phase-id")
        self.assertEqual(self.journal.rows[-1]["phase"], "setup")
        ticket = self.journal.reserve(self.payload, role="judge")
        self.journal.phase = "cleanup"
        self.journal.unknown(ticket, "ReadTimeout")
        self.assertEqual(self.journal.rows[-1]["phase"], "live:7")

    def test_failed_fsync_poison_prevents_any_later_dispatch(self):
        with patch(
            "examples.software_factory.harbor.spending.os.fsync",
            side_effect=OSError("disk failure"),
        ):
            with self.assertRaises(OSError):
                self.journal.reserve(self.payload, role="solver")
        with self.assertRaises(PermissionError):
            self.journal.reserve(self.payload, role="solver")

    def test_provider_request_id_cannot_be_billed_twice_across_restart(self):
        ticket = self.journal.reserve(self.payload, role="solver")
        self.journal.settle(ticket, self.response(), "same-provider-id")
        self.journal.close()
        restored = Spending(self.path, dollars="1", model="claude-haiku-5-5")
        self.addCleanup(restored.close)
        ticket = restored.reserve(self.payload, role="judge")
        with self.assertRaises((ValueError, PermissionError)):
            restored.settle(ticket, self.response(), "same-provider-id")
        self.assertEqual(str(restored.spent), "0.00002")
        self.assertTrue(restored.blocked)
        self.assertTrue(restored.pending)

    def test_only_allowlisted_usage_reaches_the_spending_journal(self):
        gateway = self.gateway(
            lambda _: httpx.Response(
                200,
                headers={"request-id": "usage-id"},
                json=self.response(private_debug="PRIVATE_PROVIDER_DEBUG"),
            )
        )
        result = gateway.forward(self.payload, self.headers)
        self.assertEqual(result.json()["usage"]["private_debug"], "PRIVATE_PROVIDER_DEBUG")
        self.assertNotIn("PRIVATE_PROVIDER_DEBUG", self.path.read_text())

    def test_summary_retains_pending_and_unattributed_native_costs(self):
        ticket = self.journal.reserve({**self.payload, "model": "claude-haiku-4-5"}, role="loom")
        self.journal.settle(ticket, self.response(model="claude-haiku-4-5"), "native-summary")
        self.journal.phase = "live:2"
        self.journal.reserve(self.payload, role="solver")
        summary = self.journal.summary()
        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["known_requests"], 1)
        self.assertEqual(summary["pending_requests"], 1)
        self.assertGreater(summary["pending_reserved_usd"], 0)
        self.assertEqual(summary["by_role"]["loom"]["spent_usd"], 0.0002)
        self.assertEqual(summary["unattributed_requests"], 1)
        self.assertFalse(summary["complete"])

    def test_actual_http_forwarding_preserves_body_usage_ids_and_strips_private_headers(self):
        seen = []

        def provider(request):
            seen.append(request)
            return httpx.Response(200, headers={"request-id": "wire-id"}, json=self.response())

        gateway = self.gateway(provider)
        port = gateway.start(host="127.0.0.1")
        raw = json.dumps(self.payload, indent=2).encode()
        response = httpx.post(
            f"http://127.0.0.1:{port}/v1/messages",
            content=raw,
            headers={
                **self.headers,
                "X-Factory-Order": "2",
                "X-Factory-Org": "episode",
                "X-Factory-Namespace": "working",
                "X-Factory-Feature": "memory.set",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["request-id"], "wire-id")
        self.assertEqual(response.json(), self.response())
        self.assertEqual(seen[0].content, raw)
        self.assertFalse(any(name.startswith("x-factory-") for name in seen[0].headers))
        reserved = next(row for row in self.journal.rows if row["event"] == "reserved")
        self.assertEqual(reserved["attribution"]["namespace"], "working")
        self.assertEqual(reserved["input_token_bound"], len(raw) + 4096)

    def test_invalid_roles_and_nonfinite_or_duplicate_json_do_not_dispatch(self):
        seen = []
        gateway = self.gateway(lambda request: seen.append(request))
        for role, model in (
            ("solver", "claude-haiku-4-5"),
            ("judge", "claude-haiku-4-5"),
            ("loom", "claude-haiku-5-5"),
            ("other", "claude-haiku-5-5"),
        ):
            with self.subTest(role=role, model=model), self.assertRaises(PermissionError):
                gateway.forward(
                    {**self.payload, "model": model}, {**self.headers, "x-factory-role": role}
                )
        port = gateway.start(host="127.0.0.1")
        for raw in (
            b'{"model":"other","model":"claude-haiku-5-5","max_tokens":10}',
            b'{"model":"claude-haiku-5-5","max_tokens":NaN}',
        ):
            response = httpx.post(
                f"http://127.0.0.1:{port}/v1/messages", content=raw, headers=self.headers
            )
            self.assertEqual(response.status_code, 503)
        self.assertEqual(seen, [])
        self.assertFalse(any(row["event"] == "reserved" for row in self.journal.rows))

    def test_http_provider_error_never_exposes_private_payload_and_blocks_retry(self):
        seen = []

        def provider(request):
            seen.append(request)
            return httpx.Response(500, json={"error": "UPSTREAM_SECRET", "request": self.payload})

        gateway = self.gateway(provider)
        port = gateway.start(host="127.0.0.1")
        for _ in range(2):
            response = httpx.post(
                f"http://127.0.0.1:{port}/v1/messages", json=self.payload, headers=self.headers
            )
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("UPSTREAM_SECRET", response.text)
            self.assertNotIn("Fix the issue", response.text)
        self.assertEqual(len(seen), 1)
        self.assertTrue(self.journal.pending)

    def test_secret_echo_in_attribution_or_success_response_is_refused(self):
        gateway = self.gateway(
            lambda _: httpx.Response(
                200,
                headers={"request-id": "safe-id"},
                json=self.response(private_debug="UPSTREAM_SECRET"),
            )
        )
        with self.assertRaises(ValueError):
            gateway.forward(self.payload, {**self.headers, "x-factory-feature": "UPSTREAM_SECRET"})
        self.assertNotIn("UPSTREAM_SECRET", self.path.read_text())
        with self.assertRaises(ValueError):
            gateway.forward(self.payload, self.headers)
        self.assertNotIn("UPSTREAM_SECRET", self.path.read_text())

    def test_solver_and_judge_clients_use_opaque_route_with_real_usage_unchanged(self):
        from examples.software_factory.factory.anthropic_provider import AnthropicProvider

        seen = []

        def provider(request):
            seen.append(request)
            return httpx.Response(
                200,
                headers={"request-id": f"actual-{len(seen)}"},
                json={**self.response(), "stop_reason": "end_turn"},
            )

        gateway = self.gateway(provider)
        port = gateway.start(host="127.0.0.1")
        for role in ("solver", "judge"):
            client = AnthropicProvider(
                "private-gate",
                "claude-haiku-5-5",
                approved_model_id="claude-haiku-5-5",
                base_url=f"http://127.0.0.1:{port}",
                role=role,
                order=2 if role == "solver" else None,
            )
            try:
                result = client.step([], [], 100)
            finally:
                client.close()
            self.assertEqual(result["usage"], self.response()["usage"])
            self.assertEqual(result["provider_request_id"], f"actual-{len(seen)}")
            self.assertEqual(result["retries"], 0)
        reservations = [row for row in self.journal.rows if row["event"] == "reserved"]
        self.assertEqual([row["role"] for row in reservations], ["solver", "judge"])
        self.assertEqual(reservations[0]["attribution"]["order"], "2")
        for request in seen:
            self.assertEqual(
                set(json.loads(request.content)), {"model", "messages", "tools", "max_tokens"}
            )

    def test_journal_is_exclusive_and_empty_allowance_is_frozen(self):
        with self.assertRaises(BlockingIOError):
            Spending(self.path, dollars="1", model="claude-haiku-5-5")
        self.journal.close()
        with self.assertRaises(PermissionError):
            Spending(self.path, dollars="2", model="claude-haiku-5-5")

    def test_cancelled_upstream_keeps_reservation_and_blocks_all_future_requests(self):
        def interrupted(_):
            raise KeyboardInterrupt

        gateway = self.gateway(interrupted)
        with self.assertRaises(KeyboardInterrupt):
            gateway.forward(self.payload, self.headers)
        self.assertTrue(self.journal.pending)
        self.assertTrue(self.journal.blocked)

    def test_unpriced_media_hosted_tools_and_cache_controls_refuse_before_dispatch(self):
        seen = []
        gateway = self.gateway(lambda request: seen.append(request))
        requests = [
            {
                **self.payload,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "url", "url": "https://example.com/image"},
                            }
                        ],
                    }
                ],
            },
            {**self.payload, "tools": [{"type": "web_search_20250305", "name": "web_search"}]},
            {**self.payload, "cache_control": {"type": "ephemeral"}},
            {**self.payload, "service_tier": "priority"},
        ]
        for payload in requests:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                gateway.forward(payload, self.headers)
        self.assertEqual(seen, [])
        self.assertFalse(any(row["event"] == "reserved" for row in self.journal.rows))
