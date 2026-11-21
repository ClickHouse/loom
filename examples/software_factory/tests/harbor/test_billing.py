"""Recorded provider costs require a complete scoped physical-request join."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.billing import audit_provider_costs, load_ledger


class ProviderBilling(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.protocol = dict(
            output_root=str(self.root),
            server_container="owned-server",
            org="org",
            tasks={
                "task": {
                    "indexes": {
                        "treesitter": {"namespace": "parse"},
                        "scip": {"namespace": "compile"},
                    }
                }
            },
            schedule=[
                dict(order=0, task_id="task", mode="baseline", arm="baseline", index_tier=None),
                dict(
                    order=1,
                    task_id="task",
                    mode="natural",
                    arm="loom-treesitter",
                    index_tier="treesitter",
                ),
            ],
        )
        self.entries = []
        for slot in self.protocol["schedule"]:
            folder = self.root / str(slot["order"])
            (folder / "agent").mkdir(parents=True)
            self.entries.append(
                dict(
                    order=slot["order"],
                    result={
                        **slot,
                        "protocol_sha256": digest(self.protocol),
                        "harbor_trial": str(folder),
                        "loom_physical_requests": 0,
                        "loom_transport_dispatches": 0,
                        "loom_logical_requests": 0,
                    },
                )
            )

    def trace(self, identifiers=("one",), *, status=200):
        result = self.entries[1]["result"]
        count = len(identifiers)
        result.update(
            loom_physical_requests=count, loom_transport_dispatches=count, loom_logical_requests=1
        )
        rows = []
        for identifier in identifiers:
            rows += [
                dict(phase="dispatch", path="/v1/code.symbol", request_id=None),
                dict(
                    phase="response", path="/v1/code.symbol", status=status, request_id=identifier
                ),
            ]
        (Path(result["harbor_trial"]) / "agent/loom-transport.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows)
        )

    def ledger(self, identifier="one", **changes):
        return dict(
            org="org",
            request_id=identifier,
            namespace="parse",
            operation="code.symbol",
            success=True,
            error_code="",
            input_tokens=10,
            output_tokens=2,
            embedding_tokens=3,
            response_tokens=10000,
            upstream_cost_usd="0.000012",
            **changes,
        )

    def audit(self, rows):
        loader = Mock(return_value=rows)
        receipt = audit_provider_costs(self.protocol, self.entries, ledger_loader=loader)
        return receipt, loader

    def test_explicit_zero_adoption_and_baseline_need_no_database_query(self):
        receipt, loader = self.audit([])
        loader.assert_not_called()
        self.assertTrue(receipt["cohort_complete"])
        self.assertEqual(receipt["ledger_query_status"], "not_needed")
        for row in receipt["attempts"].values():
            self.assertEqual(row["proof_status"], "known")
            self.assertEqual(row["loom_provider_cost_usd"], 0)
            self.assertEqual(row["physical_requests_matched"], 0)
            self.assertNotIn("memory_rows_written", row)
        saved = json.loads((self.root / "provider-costs.json").read_text())
        self.assertEqual(saved["protocol_sha256"], digest(self.protocol))

    def test_all_ids_are_loaded_in_one_scoped_batch_and_response_tokens_stay_separate(self):
        self.trace(("one", "two"))
        receipt, loader = self.audit([self.ledger(), self.ledger("two")])
        loader.assert_called_once_with(self.protocol, ["one", "two"])
        row = receipt["attempts"]["1"]
        self.assertEqual(row["proof_status"], "known")
        self.assertEqual(row["physical_requests_matched"], 2)
        self.assertEqual(row["physical_request_ids_sha256"], digest(["one", "two"]))
        self.assertAlmostEqual(row["loom_provider_cost_usd"], 0.000024)
        self.assertEqual(row["loom_provider_input_tokens"], 20)
        self.assertEqual(row["loom_provider_output_tokens"], 4)
        self.assertEqual(row["loom_embedding_tokens"], 6)
        self.assertEqual(row["loom_response_tokens"], 20000)

    def test_missing_duplicate_wrong_scope_failed_or_unpriced_ledger_is_unknown(self):
        self.trace()
        valid = self.ledger()
        cases = [
            [],
            [valid, dict(valid)],
            [{**valid, "namespace": "compile"}],
            [{**valid, "org": "other"}],
            [{**valid, "operation": "code.grep"}],
            [{**valid, "success": False}],
            [{k: v for k, v in valid.items() if k != "upstream_cost_usd"}],
            [{**valid, "input_tokens": None}],
            [{**valid, "upstream_cost_usd": "NaN"}],
            [{**valid, "upstream_cost_usd": "-1"}],
            [{**valid, "embedding_tokens": True}],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                receipt, _ = self.audit(rows)
                self.assertEqual(receipt["attempts"]["1"]["proof_status"], "unknown")
                self.assertIsNone(receipt["attempts"]["1"]["loom_provider_cost_usd"])
                self.assertIsNone(receipt["attempts"]["1"]["loom_provider_input_tokens"])

    def test_failed_missing_or_duplicate_physical_responses_never_become_zero(self):
        for identifiers, status in [(("one",), 500), ((None,), 200), (("one", "one"), 200)]:
            with self.subTest(identifiers=identifiers, status=status):
                self.trace(identifiers, status=status)
                receipt, loader = self.audit([])
                loader.assert_not_called()
                self.assertEqual(receipt["attempts"]["1"]["proof_status"], "unknown")
                self.assertIsNone(receipt["attempts"]["1"]["loom_provider_cost_usd"])

    def test_unverified_zero_count_mismatched_counts_and_old_protocol_refuse(self):
        for change in [
            dict(loom_physical_requests=None),
            dict(loom_transport_dispatches=1),
            dict(protocol_sha256="other"),
        ]:
            result = self.entries[1]["result"]
            before = dict(result)
            result.update(change)
            receipt, loader = self.audit([])
            loader.assert_not_called()
            self.assertIsNone(receipt["attempts"]["1"]["loom_provider_cost_usd"])
            result.clear()
            result.update(before)

    def test_partial_cohort_and_query_failure_keep_unknowns(self):
        loader = Mock()
        receipt = audit_provider_costs(self.protocol, self.entries[:1], ledger_loader=loader)
        loader.assert_not_called()
        self.assertFalse(receipt["cohort_complete"])
        self.assertTrue(
            all(row["proof_status"] == "unknown" for row in receipt["attempts"].values())
        )
        self.trace()
        failed = Mock(side_effect=RuntimeError("not a billing receipt"))
        receipt = audit_provider_costs(self.protocol, self.entries, ledger_loader=failed)
        self.assertEqual(receipt["ledger_query_status"], "failed:RuntimeError")
        self.assertIsNone(receipt["attempts"]["1"]["loom_provider_cost_usd"])
        self.assertEqual(receipt["attempts"]["0"]["loom_provider_cost_usd"], 0)

    def test_same_response_id_cannot_be_billed_to_two_attempts(self):
        slot = dict(order=2, task_id="task", mode="natural", arm="loom-scip", index_tier="scip")
        self.protocol["schedule"].append(slot)
        folder = self.root / "2"
        (folder / "agent").mkdir(parents=True)
        self.entries.append(
            dict(
                order=2,
                result={
                    **slot,
                    "harbor_trial": str(folder),
                    "loom_physical_requests": 1,
                    "loom_transport_dispatches": 1,
                    "loom_logical_requests": 1,
                },
            )
        )
        for entry in self.entries:
            entry["result"]["protocol_sha256"] = digest(self.protocol)
        self.trace()
        original = Path(self.entries[1]["result"]["harbor_trial"]) / "agent/loom-transport.jsonl"
        (folder / "agent/loom-transport.jsonl").write_bytes(original.read_bytes())
        receipt, loader = self.audit([])
        loader.assert_not_called()
        for order in ("1", "2"):
            self.assertEqual(receipt["attempts"][order]["proof_status"], "unknown")
            self.assertIsNone(receipt["attempts"][order]["loom_provider_cost_usd"])

    def test_loader_uses_bound_org_and_request_ids_on_only_the_frozen_container(self):
        with patch(
            "examples.software_factory.harbor.billing.subprocess.run",
            return_value=SimpleNamespace(stdout="[]"),
        ) as run:
            self.assertEqual(load_ledger(self.protocol, ["one"]), [])
        args = run.call_args.args[0]
        self.assertEqual(
            args[:6], ["docker", "exec", "-i", "owned-server", "/opt/venv/bin/python", "-c"]
        )
        self.assertIn("WHERE org={org:String}", args[6])
        self.assertIn("request_id IN {ids:Array(String)}", args[6])
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"org": "org", "ids": ["one"]})
