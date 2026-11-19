from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.software_factory.__main__ import ROOT, run_task
from examples.software_factory.adapters import (
    BedrockProvider,
    ReplayContext,
    ReplayProvider,
    SDKContext,
)
from examples.software_factory.engine import Factory
from examples.software_factory.model import Budget, Task
from examples.software_factory.report import compare
from examples.software_factory.workspace import Checkout, ObjectiveGrader, evaluate


class FactoryContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        suite = json.loads((ROOT / "suite.json").read_text())
        self.frozen = suite["tasks"]["name-validation"]
        self.task = Task.load(ROOT / self.frozen["task"]["path"])

    def factory(self, *, mode="orchestrated", backend=None, budget=None, provider=None):
        grader = ObjectiveGrader(
            ROOT / self.frozen["grader"]["path"], self.frozen["grader"]["sha256"]
        )
        provider = provider or ReplayProvider(ROOT / self.frozen["replay"]["path"])
        return Factory(
            self.task,
            provider,
            grader,
            self.root / "run",
            mode=mode,
            backend=None if mode == "baseline" else backend or ReplayContext(self.task),
            budget=budget,
        )

    def test_host_routes_without_agent_loom_calls_and_rejects_stale_context(self):
        factory = self.factory()
        result = factory.run()
        self.assertTrue(result["resolved"])
        self.assertEqual(result["model_selected_loom_requests"], 0)
        self.assertGreaterEqual(result["host_loom_requests"], 3)
        self.assertEqual(result["loom_physical_requests"], 0)
        rows = factory.ledger.rows
        self.assertEqual(
            [r["passed"] for r in rows if r["kind"] == "verification"], [False, False, True]
        )
        retrievals = [r for r in rows if r["kind"] == "retrieval"]
        self.assertEqual(retrievals[0]["stage"], "intake")
        self.assertTrue(all(r["request"]["namespace"] == self.task.namespace for r in retrievals))
        stale = [r for r in rows if r["kind"] == "context" and r["stage"] == "diff"]
        self.assertTrue(all(r["evidence"]["rejected"] for r in stale))
        failure_states = {
            r["state"] for r in rows if r["kind"] == "context" and r["stage"] == "failure"
        }
        self.assertEqual(len(failure_states), 2)
        for i, row in enumerate(rows):
            if row["kind"] == "verification" and row["state"] != self.task.source_sha256:
                self.assertTrue(
                    any(
                        r["kind"] == "context"
                        and r["stage"] == "diff"
                        and r["state"] == row["state"]
                        for r in rows[:i]
                    )
                )

    def test_baseline_has_no_context_schema_instruction_or_backend(self):
        factory = self.factory(mode="baseline")
        self.assertNotIn("loom", json.dumps([factory.tools, factory.messages]).lower())
        self.assertTrue(factory.run()["resolved"])
        self.assertFalse(any(r["kind"] in {"context", "retrieval"} for r in factory.ledger.rows))
        self.assertIsNone(factory.context)

    def test_natural_arm_exposes_optional_agent_selected_lookup(self):
        original = ReplayProvider(ROOT / self.frozen["replay"]["path"])

        class SelectOnce:
            label = "scripted-replay"
            used = False

            def step(self, messages, tools, max_tokens):
                if not self.used:
                    self.used = True
                    return {
                        "blocks": [
                            {
                                "toolUse": {
                                    "toolUseId": "lookup",
                                    "name": "loom",
                                    "input": {"symbol": "valid_name"},
                                }
                            }
                        ],
                        "physical_requests": 0,
                        "cost_usd": 0,
                        "usage": {},
                    }
                return original.step(messages, tools, max_tokens)

        result = self.factory(mode="natural", provider=SelectOnce()).run()
        self.assertTrue(result["resolved"])
        self.assertEqual(result["model_selected_loom_requests"], 1)
        self.assertEqual(result["host_loom_requests"], 0)

    def test_natural_nonuse_is_retained(self):
        result = self.factory(mode="natural").run()
        self.assertTrue(result["resolved"])
        self.assertEqual(result["loom_logical_requests"], 0)

    def test_abstention_is_not_treated_as_a_known_empty_index(self):
        backend = ReplayContext(self.task)
        original = backend.request

        def abstain(op, body):
            receipt = original(op, body)
            receipt["data"] = {"abstain": True}
            return receipt

        backend.request = abstain
        factory = self.factory(backend=backend)
        self.assertTrue(factory.run()["resolved"])
        contexts = [r["evidence"] for r in factory.ledger.rows if r["kind"] == "context"]
        self.assertTrue(all(c == {"abstain": True, "reason": "index_abstained"} for c in contexts))

    def test_permission_and_scope_fail_closed(self):
        for mutation in ("permission", "commit", "repository", "namespace"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                backend = ReplayContext(self.task)
                original = backend.request

                def corrupt(op, body, original=original, mutation=mutation):
                    receipt = original(op, body)
                    if mutation == "permission":
                        receipt["permission"] = "restricted"
                    else:
                        receipt["scope"][mutation] = "wrong"
                    return receipt

                backend.request = corrupt
                factory = self.factory(backend=backend)
                result = factory.run()
                self.assertEqual(result["status"], "stopped")
                self.assertIn("PermissionError", result["error"])
                self.assertEqual(result["provider_logical_requests"], 0)

    def test_retrieval_dedup_and_budget(self):
        factory = self.factory(budget=Budget(retrievals=1))
        first = factory.context.lookup("intake")
        self.assertEqual(factory.context.lookup("intake"), first)
        result = factory.run()
        self.assertTrue(result["resolved"])
        self.assertEqual(result["loom_logical_requests"], 1)
        self.assertTrue(any(r["kind"] == "context_budget" for r in factory.ledger.rows))

    def test_early_finish_does_not_grade_itself(self):
        class ClaimSuccess:
            label = "scripted-replay"

            def step(self, *args):
                return {
                    "blocks": [{"text": "All tests pass"}],
                    "physical_requests": 0,
                    "cost_usd": 0,
                    "usage": {},
                }

        result = self.factory(mode="baseline", provider=ClaimSuccess()).run()
        self.assertFalse(result["resolved"])
        self.assertFalse(result["hidden_grade"]["passed"])

    def test_provider_exception_retains_attempt_and_unknown_billing(self):
        class ProviderError(Exception):
            pass

        class BrokenProvider:
            label = "failed-provider"

            def step(self, *args):
                raise ProviderError("Transport failed before a complete receipt")

        factory = self.factory(mode="baseline", provider=BrokenProvider())
        result = factory.run()
        self.assertEqual(result["status"], "stopped")
        self.assertIn("ProviderError", result["error"])
        self.assertIsNone(result["total_cost_usd"])
        self.assertIsNone(result["provider_physical_requests"])
        self.assertIsNone(result["retry_count"])
        self.assertTrue((factory.output / "result.json").exists())

    def test_wall_and_context_character_budgets(self):
        factory = self.factory(budget=Budget(context_chars=1))
        evidence = factory.context.lookup("intake")
        self.assertEqual(evidence, {"abstain": True, "reason": "context_budget"})
        factory.deadline = 0
        result = factory.run()
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["provider_logical_requests"], 0)

    def test_smoke_needs_no_network(self):
        with patch("socket.socket", side_effect=AssertionError("Network disabled")):
            self.assertTrue(self.factory().run()["resolved"])

    def test_turn_tool_and_transcript_budgets_remain_failed_attempts(self):
        for budget in (Budget(turns=1), Budget(tools=1), Budget(transcript_chars=10)):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                result = self.factory(mode="baseline", budget=budget).run()
                self.assertFalse(result["resolved"])
                self.assertIn(result["status"], {"stopped", "turn_budget"})

    def test_file_and_hidden_grader_isolation(self):
        checkout = Checkout(self.root / "checkout", self.task)
        for path in (
            "../secret",
            "/etc/passwd",
            ".git/objects/foo",
            "graders/name-validation.json",
        ):
            with self.assertRaises(PermissionError):
                checkout.read(path)
        source = checkout.path("src/subject.py")
        source.unlink()
        source.symlink_to(ROOT / "graders/name-validation.json")
        with self.assertRaises(PermissionError):
            checkout.read("src/subject.py")

    def test_expression_evaluator_rejects_execution_and_unbounded_operations(self):
        for source in (
            "import os\ndef valid_name(name):\n return True",
            'def valid_name(name):\n return __import__("os").system(name)',
            'def valid_name(name):\n return "x" * 1000000000',
            "def valid_name(name):\n return name.__class__",
        ):
            with self.assertRaises(ValueError):
                evaluate(source, "valid_name", ["x"])

    def test_frozen_inputs_and_grader_hashes(self):
        path = self.root / "task.json"
        data = json.loads((ROOT / self.frozen["task"]["path"]).read_text())
        data["files"]["src/subject.py"] = "malicious replacement"
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            Task.load(path)
        with self.assertRaises(ValueError):
            ObjectiveGrader(ROOT / self.frozen["grader"]["path"], "bad")

    def test_all_frozen_replays_edit_and_pass_independent_checks(self):
        for task in json.loads((ROOT / "suite.json").read_text())["tasks"]:
            for mode in ("baseline", "orchestrated", "natural"):
                with self.subTest(task=task, mode=mode):
                    result = run_task(task, self.root / f"{task}-{mode}", mode)
                    self.assertTrue(result["resolved"])
                    self.assertFalse(result["benchmark_claims"])
                    self.assertEqual(result["cost_status"], "offline")
                    patch_bytes = (self.root / f"{task}-{mode}" / "candidate.diff").read_bytes()
                    self.assertEqual(
                        result["patch_sha256"], hashlib.sha256(patch_bytes).hexdigest()
                    )

    def test_comparison_includes_failed_cost_and_unknown_cost(self):
        success = self.factory(mode="baseline").run()
        success["total_cost_usd"] = 2
        failure = {**success, "resolved": False, "total_cost_usd": 3}
        arm = compare([success, failure])["arms"][0]
        self.assertEqual(arm["quality"], 0.5)
        self.assertEqual(arm["cost_per_success_usd"], 5)
        failure["total_cost_usd"] = None
        self.assertIsNone(compare([success, failure])["arms"][0]["cost_per_success_usd"])


class AdapterContracts(unittest.TestCase):
    def test_provider_uses_real_converse_contract_without_temperature(self):
        class Client:
            class meta:
                class config:
                    retries = {"total_max_attempts": 1}
                    read_timeout = 45
                    connect_timeout = 5

            def converse(self, **args):
                self.args = args
                return {
                    "output": {"message": {"content": [{"text": "done"}]}},
                    "usage": {"inputTokens": 5, "outputTokens": 2, "cacheReadInputTokens": 1},
                }

        client = Client()
        adapter = BedrockProvider(client, "explicit-model", approved_model_id="explicit-model")
        reply = adapter.step([{"role": "user", "content": [{"text": "task"}]}], [], 100)
        self.assertEqual(client.args["inferenceConfig"], {"maxTokens": 100})
        self.assertEqual(reply["usage"]["cacheReadInputTokens"], 1)
        self.assertIsNone(reply["cost_usd"])
        with self.assertRaises(PermissionError):
            BedrockProvider(client, "opaque-alias", approved_model_id="different-model")

    def test_sdk_adapter_counts_scope_checks_and_disables_retries(self):
        sent = [0]

        class Reply:
            data = {"definitions": []}
            request_id = "request-id"

        class Agent:
            auto_run = False
            mentor_brief = False

            async def call_with_meta(self, op, body, **options):
                sent[0] += 1
                self.options = options
                return Reply()

        async def verify(agent, body):
            sent[0] += 1
            return {
                "permission": "allowed",
                "scope": {
                    "commit": "a" * 40,
                    "org": "fixture",
                    "namespace": "frozen",
                    "repository": "authored/offline-fixtures",
                },
                "physical_requests": 1,
            }

        agent = Agent()
        expected = {
            "commit": "a" * 40,
            "org": "fixture",
            "namespace": "frozen",
            "repository": "authored/offline-fixtures",
        }
        adapter = SDKContext(agent, verify, lambda: sent[0], expected_scope=expected)
        self.addCleanup(adapter.close)
        receipt = adapter.request("code.symbol", {"org": "fixture", "namespace": "frozen"})
        self.assertEqual(agent.options["retry"], {"max_attempts": 1})
        self.assertEqual(receipt["physical_requests"], 3)
        self.assertEqual(receipt["request_id"], "request-id")
        # Reuse the same async runner across sequential SDK operations.
        self.assertEqual(
            adapter.request("code.symbol", {"org": "fixture", "namespace": "frozen"})[
                "physical_requests"
            ],
            3,
        )


if __name__ == "__main__":
    unittest.main()
