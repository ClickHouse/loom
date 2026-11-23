"""Other repository profiles inherit the same frozen execution and measurement policy."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor import planning
from examples.software_factory.harbor.arms import PRIMARY
from examples.software_factory.harbor.charts import METRICS
from examples.software_factory.harbor.live_support import schedule
from examples.software_factory.harbor.source import sha


class SharedPlanning(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = "a" * 40
        self.server = "b" * 40
        self.fixture = self.root / "fixture.json"
        self.fixture.write_text(json.dumps(dict(case_id="sample")))
        self.rows = schedule(["sample"], seed=741)
        source = dict(
            verified=True,
            base_commit=self.base,
            source_sha256="c" * 64,
            paths_sha256="d" * 64,
            file_count=1,
        )
        counts = {
            tier: dict(
                compiler_definitions=0 if tier == "treesitter" else 2,
                compiler_references=0 if tier == "treesitter" else 3,
            )
            for tier in ("treesitter", "scip")
        }
        scopes = {
            f"sample:{tier}": dict(
                org="org-" + tier,
                namespace="code",
                git_head=self.base,
                build_commit=self.server,
                compiler_rows=counts[tier],
                source_scope=source,
            )
            for tier in counts
        }
        self.certificate = self.root / "certificate.json"
        self.certificate.write_text(json.dumps(dict(scopes=scopes)))
        public = self.root / "public-project-cases.json"
        public.write_text(json.dumps({"tasks": [{"task_id": "sample"}]}))
        content = self.root / "index.json"
        content.write_text("{}")
        bindings = {}
        for slot in self.rows:
            tier = slot["index_tier"]
            binding = dict(
                task_id="sample",
                index_tier=tier,
                base_commit=self.base,
                org="-",
                code_namespace="-",
                token_file=None,
                memory_namespace=None,
                memory_history_sha256=None,
            )
            if tier is not None:
                binding.update(
                    org="org-" + tier,
                    code_namespace="code",
                    token_file="private-token",
                    repository_tag="repo:0123456789",
                    compiler_rows=counts[tier],
                    source_scope=source,
                )
            if slot["memory_enabled"]:
                binding.update(
                    memory_namespace="working-" + str(slot["order"]), memory_history_sha256="e" * 64
                )
            bindings[str(slot["order"])] = binding
        self.prepared = dict(
            tasks={
                "sample": dict(
                    base_commit=self.base,
                    source_roots=["services/widget.ts"],
                    scenario_family="published-project-history",
                )
            },
            public_inputs={str(self.fixture): sha(self.fixture)},
            schedule=self.rows,
            memory_histories={
                "sample": dict(fixture_file=str(self.fixture), fixture_sha256=sha(self.fixture))
            },
            historical_manifest=str(public),
            reference_controls_file="private-project-controls.json",
        )
        identity = dict(commit=self.server, dirty=False, relative_files={}, files={})
        self.setup = dict(
            loom_mcp={
                "schema_profile": "compact",
                "tool_catalog_sha256": "a" * 64,
                "standard_catalog_sha256": "b" * 64,
            },
            files={str(self.certificate): sha(self.certificate), str(content): sha(content)},
            runtime=dict(
                server_container="owned",
                loom_url="http://local.invalid",
                server_commit=self.server,
                server_source_identity=identity,
            ),
            tasks={"sample": dict(indexes={"treesitter": {}, "scip": {}})},
            attempt_scopes=bindings,
            index_certificate_file=str(self.certificate),
            index_certificate_sha256=sha(self.certificate),
            index_content_file=str(content),
            index_content_sha256=sha(content),
        )
        self.lock = dict(
            id="other",
            title="Polyglot E2E",
            repository="example/widget",
            repository_url="https://example.invalid/widget",
            languages=["typescript", "rust"],
            source_roots=["services", "crates"],
            adapter="examples.software_factory.evals.other.profile",
            model_id="claude-haiku-5-5",
            schedule_seed=741,
        )
        self.identity = dict(
            commit="f" * 40, dirty=True, relative_files={"shared.py": "c" * 64}, files={}
        )

    def freeze(self, factory=lambda lock, plan: dict(dataset=dict(repository="example/widget"))):
        with (
            patch.object(planning, "controller_identity", return_value=self.identity),
            patch(
                "examples.software_factory.harbor.runtime.load_profile",
                return_value=SimpleNamespace(task_inputs=lambda *_args, **_kwargs: None),
            ),
        ):
            path = planning.freeze_plan(
                self.prepared,
                self.setup,
                self.root / "output",
                lock=self.lock,
                public_metadata_factory=factory,
                inference=dict(kind="shared-gateway"),
                budget_usd="10",
            )
        return json.loads(path.read_text())

    def test_fake_other_profile_inherits_arms_metrics_goals_sources_and_certificates(self):
        plan = self.freeze()
        self.assertEqual(plan["suite"]["languages"], ["typescript", "rust"])
        self.assertEqual(plan["tasks"]["sample"]["source_roots"], ["services/widget.ts"])
        self.assertEqual(plan["metrics"], METRICS)
        self.assertEqual(set(plan["arms"]), set(PRIMARY))
        self.assertEqual(plan["controller_source_identity"], self.identity)
        self.assertEqual(plan["inference"], dict(kind="shared-gateway"))
        self.assertEqual(plan["index_certificate_sha256"], sha(self.certificate))
        self.assertEqual(plan["schedule_sha256"], digest(self.rows))
        self.assertTrue(
            all(row["provider_cost_ratio_max"] == 0.8 for row in plan["success_target"].values())
        )
        self.assertEqual(plan["public_reproduction"]["model"]["id"], self.lock["model_id"])
        self.assertEqual(plan["loom_mcp"], self.setup["loom_mcp"])
        self.assertEqual(plan["public_reproduction"]["loom_mcp"], self.setup["loom_mcp"])
        text = Path(planning.__file__).read_text()
        self.assertNotIn("evals.flask", text)
        self.assertNotIn("src/flask", text)

    def test_profile_cannot_override_shared_public_measurement_fields(self):
        with self.assertRaisesRegex(PermissionError, "common measurement"):
            self.freeze(lambda lock, plan: dict(model=dict(id="different")))

    def test_changed_certificate_or_input_refuses_before_output(self):
        before = copy.deepcopy(self.prepared)
        self.fixture.write_text("changed")
        with self.assertRaises(PermissionError):
            self.freeze()
        self.assertFalse((self.root / "output").exists())
        self.assertEqual(self.prepared, before)

    def test_profile_metadata_hook_cannot_mutate_the_frozen_common_policy(self):
        def metadata(lock, plan):
            plan["model_id"] = "unrelated"
            plan["success_target"].clear()
            return dict(dataset=dict(repository="example/widget"))

        plan = self.freeze(metadata)
        self.assertEqual(plan["model_id"], self.lock["model_id"])
        self.assertTrue(plan["success_target"])
