from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.software_factory.factory.adapters import (
    BedrockProvider,
    ReplayContext,
    ReplayProvider,
    SDKContext,
)
from examples.software_factory.factory.engine import Factory
from examples.software_factory.factory.model import Budget
from examples.software_factory.factory.workspace import Checkout
from examples.software_factory.harbor.report import compare
from examples.software_factory.tests.fixtures.python.grading import ObjectiveGrader, evaluate
from examples.software_factory.tests.fixtures.python.grading import load_task as load_fixture
from examples.software_factory.tests.smoke import ROOT, run_task


class FactoryContracts(unittest.TestCase):
    def test_final_review_sees_the_consolidated_candidate_diff(self):
        factory = self.factory()
        result = factory.run()
        self.assertTrue(result["resolved"])
        self.assertEqual(result["completion_reviews"], 1)
        reviews = [row for row in factory.ledger.rows if row["kind"] == "completion_review"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["diff"], factory.checkout.diff())
        self.assertEqual(reviews[0]["state"], factory.checkout.state())

    def test_initial_completion_does_not_bypass_review_and_revision(self):
        class EarlyFinish(ReplayProvider):
            finished_early = False

            def step(self, messages, tools, max_tokens):
                if not self.finished_early:
                    self.finished_early = True
                    return {
                        "blocks": [{"text": "Done"}],
                        "usage": {},
                        "physical_requests": 0,
                        "cost_usd": 0,
                    }
                return super().step(messages, tools, max_tokens)

        for mode in ("baseline", "natural"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory)
                provider = EarlyFinish(ROOT / self.frozen["replay"]["path"])
                factory = self.factory(mode=mode, provider=provider)
                result = factory.run()
                self.assertTrue(result["resolved"])
                review = next(
                    row for row in factory.ledger.rows if row["kind"] == "completion_review"
                )
                self.assertEqual(review["diff"], "")
                self.assertEqual(result["completion_reviews"], 1)

    def test_unlimited_attempt_policy_completes_without_budget_arithmetic(self):
        budget = Budget(
            turns=None,
            tools=None,
            retrievals=None,
            context_chars=None,
            transcript_chars=None,
            wall_seconds=None,
            response_chars=None,
            output_tokens=128000,
        )
        result = self.factory(budget=budget).run()
        self.assertTrue(result["resolved"])
        self.assertEqual(result["status"], "completed")

    def test_elided_missing_and_mismatched_bodies_have_distinct_reasons(self):
        factory = self.factory()
        path = next(iter(self.task.files))
        line = self.task.files[path].splitlines()[0]
        definition = {"path": path, "start_line": 1, "end_line": 1, "body": line}
        cases = (
            ({"elided": True}, "Retrieved body elided; no complete source body returned"),
            ({"body": ""}, "Retrieved body unavailable"),
            ({"body": line + " mismatch"}, "Retrieved body differs from working source"),
        )
        for change, expected in cases:
            with self.subTest(change=change):
                evidence = factory.context.ground({"definitions": [{**definition, **change}]})
                self.assertTrue(evidence["abstain"])
                self.assertEqual(evidence["rejected"], [expected])

    def test_cached_lookup_still_counts_each_model_tool_choice(self):
        symbol = "validate_name"

        class SelectTwice(ReplayProvider):
            selected = False

            def step(self, messages, tools, max_tokens):
                if not self.selected:
                    self.selected = True
                    return {
                        "blocks": [
                            {
                                "toolUse": {
                                    "toolUseId": name,
                                    "name": "loom_code_symbol",
                                    "input": {"name": symbol},
                                }
                            }
                            for name in ("selected-a", "selected-b")
                        ],
                        "usage": {},
                        "physical_requests": 0,
                        "cost_usd": 0,
                        "cost_status": "offline",
                    }
                return super().step(messages, tools, max_tokens)

        provider = SelectTwice(ROOT / self.frozen["replay"]["path"])
        factory = self.factory(mode="natural", provider=provider)
        result = factory.run()
        self.assertEqual(result["model_selected_loom_requests"], 2)
        self.assertEqual(result["loom_logical_requests"], 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        suite = json.loads((ROOT / "suite.json").read_text())
        self.frozen = suite["tasks"]["name-validation"]
        self.task = load_fixture(ROOT / self.frozen["task"]["path"])

    def factory(self, *, mode="natural", backend=None, budget=None, provider=None):
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

    def test_no_context_dispatch_occurs_without_a_model_tool_choice(self):
        factory = self.factory()
        result = factory.run()
        self.assertTrue(result["resolved"])
        self.assertEqual(result["model_selected_loom_requests"], 0)
        self.assertEqual(result["loom_logical_requests"], 0)
        self.assertEqual(result["loom_physical_requests"], 0)
        self.assertFalse(hasattr(factory, "hook"))
        self.assertFalse(hasattr(factory.context, "lookup"))
        self.assertFalse(any(r["kind"] == "retrieval" for r in factory.ledger.rows))
        self.assertEqual(
            [r["passed"] for r in factory.ledger.rows if r["kind"] == "verification"],
            [False, False, True],
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
                                    "name": "loom_code_symbol",
                                    "input": {"name": "valid_name"},
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
        self.assertEqual(result["memory_rows_written"], 0)

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
        evidence = factory.context.selected("code.symbol", {"name": self.task.symbol})
        self.assertTrue(evidence["abstain"])
        self.assertEqual(evidence["definitions"], [])
        self.assertEqual(evidence["coverage"], "base_index_lower_bound")

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
                with self.assertRaises(PermissionError):
                    factory.context.selected("code.symbol", {"name": self.task.symbol})
                self.assertFalse(any(r["kind"] == "provider_request" for r in factory.ledger.rows))

    def test_retrieval_dedup_and_budget(self):
        factory = self.factory(budget=Budget(retrievals=1))
        args = {"name": self.task.symbol}
        first = factory.context.selected("code.symbol", args)
        self.assertEqual(factory.context.selected("code.symbol", args), first)
        with self.assertRaisesRegex(TimeoutError, "Retrieval budget"):
            factory.context.selected("code.symbol", {"name": "different_public_name"})
        self.assertEqual(factory.context.requests, 1)
        self.assertTrue(factory.run()["resolved"])

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
        with self.assertRaisesRegex(TimeoutError, "Context budget"):
            factory.context.selected("code.symbol", {"name": self.task.symbol})
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
            load_fixture(path)
        with self.assertRaises(ValueError):
            ObjectiveGrader(ROOT / self.frozen["grader"]["path"], "bad")

    def test_all_frozen_replays_edit_and_pass_independent_checks(self):
        for task in json.loads((ROOT / "suite.json").read_text())["tasks"]:
            for mode in ("baseline", "natural"):
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
        self.assertNotIn("timeout_s", agent.options)
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
