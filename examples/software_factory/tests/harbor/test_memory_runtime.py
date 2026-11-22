"""Live treatment wiring is fully bound before an optional model call."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.arms import ALL
from examples.software_factory.harbor.runtime import backend, episode_notes, task_inputs
from examples.software_factory.harbor.scopes import binding_for, selected_scopes


class RuntimeBindings(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = {"src/public.py": "def public():\n    return 1\n"}
        self.source = {
            "verified": True,
            "base_commit": "a" * 40,
            "source_sha256": digest(self.files),
            "paths_sha256": digest(sorted(self.files)),
            "file_count": 1,
            "tables": {},
        }
        counts = {
            "compiler_definitions": 0,
            "compiler_references": 0,
            "python_definitions": 0,
            "python_references": 0,
        }
        self.slots = []
        bindings = {}
        for order, arm in enumerate(("baseline", "loom-treesitter", "loom-treesitter-memory")):
            spec = ALL[arm]
            self.slots.append(
                {"order": order, "task_id": "task", "repeat": 0, "arm": arm, **spec.identity()}
            )
            token = self.root / (str(order) + ".token")
            token.write_text("test-slot-" + str(order))
            bindings[str(order)] = {
                "task_id": "task",
                "index_tier": spec.index_tier,
                "org": "org" if spec.index_tier else "-",
                "code_namespace": "code" if spec.index_tier else "-",
                "memory_namespace": "working-" + str(order) if spec.memory_enabled else None,
                "token_file": str(token) if spec.index_tier else None,
                "base_commit": "a" * 40,
                "repository_tag": "repo:0123456789",
                "compiler_rows": counts if spec.index_tier else None,
                "source_scope": self.source if spec.index_tier else None,
                "memory_history_sha256": digest(
                    {"decision": "Public project document."} if spec.history_seeded else {}
                )
                if spec.memory_enabled
                else None,
            }
        certificate = self.root / "index.json"
        certificate.write_text(
            json.dumps(
                {
                    "scopes": {
                        "task:treesitter": {
                            "org": "org",
                            "namespace": "code",
                            "repo": "owner/repo",
                            "git_head": "a" * 40,
                            "build_commit": "b" * 40,
                            "compiler_rows": counts,
                            "source_scope": self.source,
                        }
                    }
                }
            )
        )
        fixture = self.root / "fixture.json"
        fixture.write_text(
            json.dumps({"public_documents": {"decision": "Public project document."}})
        )
        self.protocol = {
            "suite": {"adapter": "examples.software_factory.evals.flask.profile"},
            "schedule": self.slots,
            "attempt_scopes": bindings,
            "tasks": {"task": {"base_commit": "a" * 40}},
            "server_commit": "b" * 40,
            "loom_url": "http://unused",
            "index_certificate_file": str(certificate),
            "index_certificate_sha256": hashlib.sha256(certificate.read_bytes()).hexdigest(),
            "memory_histories": {
                "task": {
                    "fixture_file": str(fixture),
                    "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
                }
            },
        }
        self.task = SimpleNamespace(
            task_id="task",
            namespace="code",
            repository="owner/repo",
            base_commit="a" * 40,
            files=self.files,
            source_sha256=digest(self.files),
        )

    def test_shared_read_only_code_scope_deduplicates_and_memory_is_private(self):
        self.assertEqual(selected_scopes(self.protocol), [{"org": "org", "namespace": "code"}])
        self.assertEqual(selected_scopes(self.protocol, self.slots[0]), [])
        self.assertEqual(binding_for(self.protocol, self.slots[2])["memory_namespace"], "working-2")

    def test_model_identity_and_no_old_global_namespace_fallback(self):
        for change in ({"memory_enabled": False}, {"index_tier": "scip"}, {"mode": "baseline"}):
            altered = {**self.slots[2], **change}
            with self.assertRaises(PermissionError):
                binding_for(self.protocol, altered)
        del self.protocol["attempt_scopes"]["2"]
        self.protocol["org"] = "old-global"
        with self.assertRaises(PermissionError):
            binding_for(self.protocol, self.slots[2])

    def test_backend_uses_only_bound_token_org_and_code_namespace(self):
        with (
            patch("loom.Agent") as agent,
            patch("examples.software_factory.harbor.runtime.SDKContext") as sdk,
        ):
            context, audit = backend(
                self.protocol, self.task, self.root / "transport.jsonl", slot=self.slots[2]
            )
            self.assertEqual(agent.call_args.kwargs["api_key"], "test-slot-2")
            self.assertEqual(agent.call_args.kwargs["org"], "org")
            self.assertEqual(agent.call_args.kwargs["namespace"], "code")
            self.assertFalse(agent.call_args.kwargs["auto_run"])
            self.assertFalse(agent.call_args.kwargs["coalesce_sets"])
            agent.return_value.call_with_meta.assert_not_called()
            self.assertIs(context, sdk.return_value)
            self.assertEqual(audit.count(), 0)
        with patch("loom.Agent") as agent:
            with self.assertRaises(PermissionError):
                backend(self.protocol, self.task, self.root / "none", slot=self.slots[0])
            agent.assert_not_called()

    def test_only_seeded_arms_receive_history_and_fixture_hash_is_checked(self):
        for slot in self.slots:
            notes = episode_notes(self.protocol, slot, self.root / str(slot["order"]))
            self.assertEqual(
                notes.project,
                {"decision": "Public project document."} if ALL[slot["arm"]].history_seeded else {},
            )
            self.assertEqual(notes.execute("notes_list", {"scope": "working"})["keys"], [])
        Path(self.protocol["memory_histories"]["task"]["fixture_file"]).write_text("{}")
        with self.assertRaises(PermissionError):
            episode_notes(self.protocol, self.slots[0], self.root / "changed")

    def test_task_inputs_propagates_bound_scope_to_profile(self):
        profile = Mock()
        profile.task_inputs.return_value = "profile-result"
        with patch("examples.software_factory.harbor.runtime.load_profile", return_value=profile):
            self.assertEqual(
                task_inputs(self.protocol, "task", "treesitter", slot=self.slots[2]),
                "profile-result",
            )
            self.assertEqual(
                profile.task_inputs.call_args.kwargs["binding"],
                self.protocol["attempt_scopes"]["2"],
            )

    def test_code_certificate_and_task_source_cannot_be_substituted(self):
        self.task.files = {**self.files, "tests/hidden.py": "private"}
        self.task.source_sha256 = digest(self.task.files)
        with patch("loom.Agent") as agent:
            with self.assertRaises(PermissionError):
                backend(self.protocol, self.task, self.root / "bad", slot=self.slots[1])
            agent.assert_not_called()
        self.protocol["attempt_scopes"]["2"]["memory_namespace"] = "code"
        with self.assertRaises(PermissionError):
            binding_for(self.protocol, self.slots[2])

    def test_snapshot_groups_org_and_selects_only_requested_code_scopes(self):
        from examples.software_factory.harbor.index import (
            TABLES,
            fingerprint,
            select_snapshot,
            snapshot,
        )

        tables = {table: [] for table in TABLES}
        tables["code_symbols"] = [
            {"org": "a", "namespace": "code", "name": "first"},
            {"org": "b", "namespace": "code", "name": "second"},
        ]
        full = fingerprint(tables)
        selected = select_snapshot(full, [{"org": "b", "namespace": "code"}])
        rows = next(table["scopes"] for table in selected if table["table"] == "code_symbols")
        self.assertEqual(
            [(row["org"], row["namespace"], row["rows"]) for row in rows], [("b", "code", 1)]
        )
        with patch("examples.software_factory.harbor.index.subprocess.run") as run:
            self.assertEqual(snapshot("unused", []), fingerprint({table: [] for table in TABLES}))
            run.assert_not_called()

    def run_agent(self, slot, *, qualification=None, invalid_history=False):
        import asyncio
        from unittest.mock import AsyncMock

        from examples.software_factory.factory.adapters import SDKContext
        from examples.software_factory.factory.model import Task
        from examples.software_factory.harbor.agent import FactoryAgent

        self.protocol.update(
            model_id="claude-haiku-5-5",
            credentials_file="unused",
            inference={"credentials_file": "unused", "base_url": "http://unit-gateway"},
            attempt_budget={},
            harbor_specs={"task": {"read_only_sha256": digest({}), "read_only_roots": []}},
        )
        namespace = "-" if qualification or slot["mode"] == "baseline" else "code"
        task = Task(
            "task",
            "fixture",
            "owner/repo",
            "a" * 40,
            namespace,
            "Public issue",
            "public",
            self.files,
            digest(self.files),
        )
        agent = FactoryAgent.__new__(FactoryAgent)
        agent.logs_dir = self.root / "agent"
        agent.model_name = self.protocol["model_id"]
        agent.protocol_path = self.root / "protocol.json"
        agent.qualification = qualification
        native = SimpleNamespace(
            auto_run=False, mentor_brief=False, __aexit__=AsyncMock(), call_with_meta=AsyncMock()
        )
        code = SDKContext(
            native,
            AsyncMock(),
            lambda: 0,
            expected_scope={
                "org": self.protocol["attempt_scopes"][str(slot["order"])]["org"],
                "namespace": "code",
                "repository": "owner/repo",
                "commit": "a" * 40,
            },
        )
        marks = []

        class Provider:
            label = "unit-offline"

            def step(self, *args):
                marks.append("inference")
                return {
                    "blocks": [{"text": "Finished inspection."}],
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                    "physical_requests": 0,
                    "cost_usd": 0,
                }

            def close(self):
                pass

        class Grader:
            supports_regression_tests = False

            def run(self, *args, **kwargs):
                return {"passed": True}

            sha256 = "unit-grader"

        profile = SimpleNamespace(
            VisibleGrader=Mock(return_value=Grader()), ControlProvider=Mock(return_value=Provider())
        )
        environment = SimpleNamespace(exec=AsyncMock(), upload_file=AsyncMock())
        context = SimpleNamespace()

        def qualify(*args):
            marks.append("history-verified")
            if invalid_history:
                raise PermissionError("bad native history")
            return {}

        with (
            patch("examples.software_factory.harbor.agent.read", return_value=self.protocol),
            patch("examples.software_factory.harbor.agent.verify"),
            patch(
                "examples.software_factory.harbor.agent.task_inputs",
                return_value=({}, None, task, {}),
            ) as inputs,
            patch("examples.software_factory.harbor.agent.load_profile", return_value=profile),
            patch(
                "examples.software_factory.harbor.agent.backend",
                return_value=(code, SimpleNamespace(count=lambda: 0)),
            ) as build,
            patch(
                "examples.software_factory.harbor.agent.dotenv_values",
                return_value={"ANTHROPIC_API_KEY": "unit-fake"},
            ),
            patch(
                "examples.software_factory.harbor.agent.AnthropicProvider", return_value=Provider()
            ) as provider,
            patch("examples.software_factory.harbor.agent.native_audit"),
            patch("examples.software_factory.harbor.agent.trajectory", return_value={}),
            patch(
                "examples.software_factory.harbor.memory.validate_history_certificate",
                side_effect=qualify,
            ) as validate,
        ):
            instruction = json.dumps({"schedule_order": slot["order"], "problem": task.problem})
            self.provider_constructor = provider
            self.native_agent = native
            try:
                asyncio.run(agent.run(instruction, environment, context))
            finally:
                if not build.called:
                    code.close()
            return context, build, provider, validate, marks, native, inputs

    def test_memory_arm_zero_adoption_is_retained_and_shared_client_closes_once(self):
        context, build, provider, validate, marks, native, inputs = self.run_agent(self.slots[2])
        result = context.metadata["factory"]
        self.assertTrue(result["memory_enabled"])
        self.assertEqual(result["memory_namespace"], "working-2")
        self.assertEqual(result["model_selected_loom_requests"], 0)
        self.assertEqual(result["loom_transport_dispatches"], 0)
        self.assertEqual(result["memory_rows_written"], 0)
        self.assertEqual(marks, ["history-verified", "inference"])
        native.call_with_meta.assert_not_called()
        self.assertEqual(native.__aexit__.await_count, 1)
        validate.assert_called_once()

    def test_baseline_and_qualification_never_construct_loom_clients(self):
        context, build, *rest = self.run_agent(self.slots[0])
        build.assert_not_called()
        self.assertFalse(context.metadata["factory"]["memory_enabled"])
        self.assertEqual(context.metadata["factory"]["loom_transport_dispatches"], 0)

    def test_invalid_native_history_refuses_before_provider_creation(self):
        with self.assertRaises(PermissionError):
            self.run_agent(self.slots[2], invalid_history=True)
        self.assertFalse((self.root / "agent/factory-result.json").exists())
        self.provider_constructor.assert_not_called()
        self.assertEqual(self.native_agent.__aexit__.await_count, 1)

    def test_qualification_is_baseline_even_when_schedule_slot_is_memory(self):
        context, build, provider, validate, marks, native, inputs = self.run_agent(
            self.slots[2], qualification="base"
        )
        build.assert_not_called()
        provider.assert_not_called()
        validate.assert_not_called()
        self.assertEqual(context.metadata["factory"]["mode"], "baseline")
        self.assertFalse(context.metadata["factory"]["memory_enabled"])
        self.assertEqual(context.metadata["factory"]["index_tier"], None)
        self.assertEqual(context.metadata["factory"]["actual_org"], "-")
        self.assertIsNone(inputs.call_args.kwargs["slot"])

    def test_scip_memory_arm_uses_same_optional_loop_and_own_namespace(self):
        slot = {**self.slots[2], "order": 3, "arm": "loom-scip-memory", "index_tier": "scip"}
        self.protocol["schedule"].append(slot)
        binding = {
            **self.protocol["attempt_scopes"]["2"],
            "org": "scip-org",
            "index_tier": "scip",
            "memory_namespace": "scip-working",
        }
        self.protocol["attempt_scopes"]["3"] = binding
        certificate = Path(self.protocol["index_certificate_file"])
        data = json.loads(certificate.read_text())
        data["scopes"]["task:scip"] = {**data["scopes"]["task:treesitter"], "org": "scip-org"}
        certificate.write_text(json.dumps(data))
        self.protocol["index_certificate_sha256"] = hashlib.sha256(
            certificate.read_bytes()
        ).hexdigest()
        context, build, provider, validate, marks, native, inputs = self.run_agent(slot)
        self.assertEqual(context.metadata["factory"]["index_tier"], "scip")
        self.assertEqual(context.metadata["factory"]["memory_namespace"], "scip-working")
        self.assertEqual(context.metadata["factory"]["model_selected_loom_requests"], 0)
        self.assertEqual(context.metadata["factory"]["actual_org"], "scip-org")

    def test_replicates_share_code_certificate_but_reused_memory_scope_refuses(self):
        repeated = {**self.slots[2], "order": 3, "repeat": 1}
        self.protocol["schedule"].append(repeated)
        self.protocol["attempt_scopes"]["3"] = {
            **self.protocol["attempt_scopes"]["2"],
            "memory_namespace": "working-3",
        }
        self.assertEqual(binding_for(self.protocol, repeated)["code_namespace"], "code")
        self.assertEqual(selected_scopes(self.protocol), [{"org": "org", "namespace": "code"}])
        self.protocol["attempt_scopes"]["3"]["memory_namespace"] = "working-2"
        with self.assertRaises(PermissionError):
            binding_for(self.protocol, repeated)
