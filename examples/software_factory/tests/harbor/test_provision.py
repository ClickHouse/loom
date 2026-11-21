"""Provisioning owns exact resources and never substitutes fake native evidence."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from examples.software_factory.harbor.provision import Provisioner


class ProvisioningContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.run = Mock(return_value=SimpleNamespace(stdout="", stderr="", returncode=0))
        self.provision = Provisioner(
            self.source,
            self.root / "runtime",
            "a" * 32,
            run=self.run,
            gateway={
                "url": "http://controller:9001",
                "token": "opaque-unit-token",
                "controller_container": "b" * 64,
            },
        )

    def test_constructor_has_no_processes_and_refuses_foreign_output(self):
        self.run.assert_not_called()
        with self.assertRaises(PermissionError):
            Provisioner(self.source, self.root / "runtime", "b" * 32, run=self.run)

    def test_missing_gateway_refuses_before_build_or_resource_creation(self):
        instance = Provisioner(self.source, self.root / "without-gateway", "c" * 32, run=self.run)
        with self.assertRaises(PermissionError):
            instance.start()
        self.run.assert_not_called()

    def test_runtime_uses_the_owned_local_model_tag_not_an_image_id_as_repository(self):
        owner = self.provision.owner
        images = {}

        def run(command, **kwargs):
            if command[:2] == ["docker", "build"]:
                tag = command[command.index("--tag") + 1]
                identifier = "sha256:" + ("d" if tag.endswith("local-models") else "e") * 64
                images[tag] = images[identifier] = identifier
                Path(command[command.index("--iidfile") + 1]).write_text(identifier)
                if tag.endswith("runtime"):
                    arg = command[command.index("--build-arg") + 1]
                    self.assertEqual(
                        arg, "LOCAL_MODELS_IMAGE=" + self.provision.prefix + "-local-models"
                    )
                    self.assertNotIn("sha256:", arg)
                return SimpleNamespace(stdout="built", stderr="", returncode=0)
            identifier = images[command[-1]]
            return SimpleNamespace(
                stdout=json.dumps(
                    [{"Id": identifier, "Config": {"Labels": {"loom.factory.owner": owner}}}]
                ),
                stderr="",
                returncode=0,
            )

        self.provision.run = run
        result = self.provision._build_images(self.root)
        self.assertEqual(result.read_text(), "sha256:" + "e" * 64)
        self.assertEqual(len(list((self.provision.output / "logs").glob("build-*.json"))), 2)

    def test_cleanup_never_removes_a_resource_with_another_owner(self):
        self.provision._record("created", kind="container", name="exact-owned-name", id="c" * 64)
        self.run.return_value.stdout = json.dumps(
            [{"Id": "c" * 64, "Config": {"Labels": {"loom.factory.owner": "foreign"}}}]
        )
        with self.assertRaises(PermissionError):
            self.provision.cleanup()
        self.assertFalse(any("rm" in call.args[0] for call in self.run.call_args_list))

    def test_operation_intent_hook_refusal_prevents_native_http(self):
        def refuse(*args):
            raise PermissionError("plan not approved")

        self.provision.before_paid = refuse
        self.provision.runtime = {"loom_url": "http://owned:7777"}
        self.provision.http = Mock()
        with self.assertRaises(PermissionError):
            self.provision._api(
                "memory.set",
                {"org": "o", "namespace": "m", "value": "public"},
                "test-token",
                phase="A",
            )
        self.provision.http.post.assert_not_called()

    def test_cleanup_removes_only_journaled_owned_exact_id(self):
        self.provision._record("created", kind="container", name="owned-one", id="d" * 64)
        self.run.return_value.stdout = json.dumps(
            [{"Id": "d" * 64, "Config": {"Labels": {"loom.factory.owner": "a" * 32}}}]
        )
        result = self.provision.cleanup()
        removes = [call.args[0] for call in self.run.call_args_list if "rm" in call.args[0]]
        self.assertEqual(removes, [["docker", "rm", "-f", "-v", "d" * 64]])
        self.assertTrue(result["complete"])

    def test_shared_twelve_code_bindings_and_thirty_six_private_memory_scopes(self):
        from examples.software_factory.harbor.live_support import schedule

        tasks = {
            f"case-{number}": {
                "repository": "owner/repo",
                "base_commit": "b" * 40,
                "source_git": "public.git",
                "source_roots": ["src/project"],
            }
            for number in range(6)
        }
        fixtures = {
            task: {
                "data": {
                    "public_documents": {}
                    if number < 3
                    else {str(i): "Short public project observation." for i in range(100)}
                }
            }
            for number, task in enumerate(tasks)
        }
        rows = schedule(list(tasks), seed=741)
        groups, bindings = self.provision._layout(tasks, rows, fixtures)
        self.assertEqual(len(groups), 12)
        self.assertEqual(len(bindings), 102)
        memories = [
            (value["org"], value["memory_namespace"])
            for value in bindings.values()
            if value["memory_namespace"] is not None
        ]
        self.assertEqual(len(memories), 36)
        self.assertEqual(len(set(memories)), 36)
        for group in groups.values():
            self.assertEqual(len(group["memory_slots"]), 3)
        self.run.assert_not_called()

    def test_native_backfill_uses_fixed_commit_real_embeddings_and_no_fetch(self):
        from unittest.mock import patch

        from examples.software_factory.harbor.native_setup import external

        with patch(
            "examples.software_factory.harbor.native_setup.subprocess.run",
            return_value=SimpleNamespace(stdout="native progress", stderr="", returncode=0),
        ) as run:
            receipt = external(
                {
                    "operation": "backfill",
                    "body": {"org": "org", "namespace": "code", "commit": "b" * 40},
                }
            )
        command = run.call_args.args[0]
        self.assertIn("--embed", command)
        self.assertIn("--edges-second-pass", command)
        self.assertIn("--no-fetch", command)
        self.assertNotIn("--allow-fake-embedder", command)
        self.assertEqual(command[command.index("--commit") + 1], "b" * 40)
        self.assertEqual(receipt["exit_code"], 0)

    def test_lost_native_write_response_is_recorded_unknown_without_replay(self):
        self.provision.runtime = {"loom_url": "http://owned:7777"}
        self.provision.http = Mock()
        self.provision.http.post.side_effect = TimeoutError("uncertain write")
        with self.assertRaises(TimeoutError):
            self.provision._api(
                "memory.set",
                {"org": "org", "namespace": "memory", "memory_key": "key", "value": "Public note"},
                "opaque",
                phase="A",
            )
        self.assertEqual(self.provision.http.post.call_count, 1)
        receipt = json.loads((self.provision.output / "setup-events.jsonl").read_text())
        self.assertIsNone(receipt["physical_requests"])
        self.assertIsNone(receipt["request_id"])
        self.assertEqual(receipt["error_type"], "TimeoutError")

    def test_interrupted_creation_is_recovered_only_from_exact_owned_intent(self):
        self.provision._record("intent", kind="container", name="exact-intended-name")
        self.run.return_value.stdout = json.dumps(
            [{"Id": "e" * 64, "Config": {"Labels": {"loom.factory.owner": "a" * 32}}}]
        )
        self.provision.cleanup()
        removes = [call.args[0] for call in self.run.call_args_list if "rm" in call.args[0]]
        self.assertEqual(removes, [["docker", "rm", "-f", "-v", "e" * 64]])
        self.assertFalse(any("--filter" in call.args[0] for call in self.run.call_args_list))

    def test_polling_retains_complete_private_calls_but_only_qualified_link_observations(self):
        from unittest.mock import patch

        self.provision._worker = Mock(
            return_value={
                "operation": "memory_code_links",
                "result": {"errors": 0},
                "checked_head": "a" * 40,
            }
        )
        self.provision._api = Mock(
            side_effect=[
                {"response": {"found": True, "code": []}},
                {"response": {"found": True, "code": [{"state": "fresh"}]}},
            ]
        )
        events = []
        with patch("examples.software_factory.harbor.provision.time.sleep"):
            rows, worker = self.provision._gets(
                {"org": "o", "namespace": "code"},
                {"order": 1},
                ["key"],
                "opaque",
                "a" * 40,
                "A",
                events,
                expect=lambda key, row: bool(row["code"]),
            )
        self.assertEqual(self.provision._api.call_count, 2)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["response"]["code"], [{"state": "fresh"}])

    def test_completed_setup_resume_refuses_changed_immutable_artifact(self):
        from examples.software_factory.factory.model import digest

        self.provision.runtime = {}
        inputs = [{}, [], {}, {}]
        artifact = self.provision.output / "public-proof.json"
        artifact.write_text("qualified")
        import hashlib

        (self.provision.output / "prepare-started.json").write_text(
            json.dumps({"inputs_sha256": digest(inputs)})
        )
        (self.provision.output / "setup.json").write_text(
            json.dumps(
                {"files": {str(artifact): hashlib.sha256(artifact.read_bytes()).hexdigest()}}
            )
        )
        self.provision.prepare(*inputs)
        artifact.write_text("changed")
        with self.assertRaises(PermissionError):
            self.provision.prepare(*inputs)
        self.run.assert_not_called()

    def test_current_native_chunk_body_span_context_and_embedding_all_must_match(self):
        from examples.software_factory.harbor.index_setup import check_chunks

        expected = SimpleNamespace(
            path="src/project/a.py",
            scope="run",
            kind="function",
            start_line=1,
            end_line=2,
            content="def run():\n return 2",
            content_hash="current-hash",
        )
        row = {key: getattr(expected, key) for key in vars(expected)}
        row.update(
            chunk_id="unit",
            embedding_dim=384,
            embed_last_error="",
            context_text="Current source context",
            indexed_commit="a" * 40,
        )
        check_chunks([row], {"unit": expected}, {"unit": "Current source context"}, 384)
        for field, value in [
            ("content", "old body"),
            ("end_line", 8),
            ("context_text", "old context"),
            ("embedding_dim", 0),
        ]:
            with self.subTest(field=field), self.assertRaises(PermissionError):
                check_chunks(
                    [{**row, field: value}],
                    {"unit": expected},
                    {"unit": "Current source context"},
                    384,
                )
        with self.assertRaises(PermissionError):
            check_chunks([], {"unit": expected}, {"unit": "Current source context"}, 384)

    def test_build_context_contains_only_declared_public_runtime_inputs(self):
        source = self.source
        tracked = [
            "server/public.py",
            "sdk/python/loom/__init__.py",
            "schemas/schema.sql",
            "scripts/scip_harness/scip_to_delta.py",
        ]
        extras = [
            "Dockerfile",
            "README.md",
            "pyproject.toml",
            "uv.lock",
            "LICENSE",
            "NOTICE",
            "THIRD_PARTY_NOTICES",
        ]
        extras += [
            "examples/software_factory/" + part
            for part in (
                "factory/model.py",
                "harbor/native_setup.py",
                "harbor/index_setup.py",
                "harbor/source_tree.py",
                "harbor/Provisioner.Dockerfile",
            )
        ]
        for name in tracked + extras:
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("public source " + name)
        (source / "Dockerfile").write_text(Path("Dockerfile").read_text())
        (source / ".env").write_text("ANTHROPIC_API_KEY=credential-sentinel")
        private = source / ".task-artifacts/private-evaluator.json"
        private.parent.mkdir()
        private.write_text("hidden-answer-sentinel")
        self.run.return_value.stdout = "\0".join(tracked) + "\0"
        context = self.provision._build_context()
        contents = "".join(path.read_text() for path in context.rglob("*") if path.is_file())
        self.assertNotIn("credential-sentinel", contents)
        self.assertNotIn("hidden-answer-sentinel", contents)
        self.assertFalse((context / ".env").exists())
        self.assertEqual(
            {str(path.relative_to(context)) for path in context.rglob("*") if path.is_file()},
            set(tracked + extras),
        )

    def test_source_tree_rejects_symlinks_traversal_and_unbounded_blobs(self):
        from unittest.mock import patch

        from examples.software_factory.harbor.source_tree import source_files

        def fake(*args, **kwargs):
            if args[1] == "rev-parse":
                return ("a" * 40 + "\n").encode()
            if args[1] == "ls-tree":
                return b"120000 blob " + b"b" * 40 + b"\tsrc/project/link.py\0"
            raise AssertionError("No blob should be read for a symlink")

        with patch("examples.software_factory.harbor.source_tree.git", side_effect=fake):
            with self.assertRaises(PermissionError):
                source_files("repo", "a" * 40, ["src/project"])
            with self.assertRaises(PermissionError):
                source_files("repo", "a" * 40, ["../private"])

    def test_launcher_owned_output_is_accepted_but_foreign_launch_refused(self):
        owned = self.root / "launcher-output"
        owned.mkdir()
        (owned / "launch.json").write_text(
            json.dumps({"owner": "b" * 32, "source_root": str(self.source)})
        )
        Provisioner(self.source, owned, "b" * 32, run=self.run)
        foreign = self.root / "foreign-output"
        foreign.mkdir()
        (foreign / "launch.json").write_text(
            json.dumps({"owner": "c" * 32, "source_root": str(self.source)})
        )
        with self.assertRaises(PermissionError):
            Provisioner(self.source, foreign, "b" * 32, run=self.run)
        self.run.assert_not_called()

    def test_cold_fixture_must_be_explicit_and_hashed_not_inferred_from_missing_data(self):
        with self.assertRaises(PermissionError):
            self.provision._fixtures({"case": {}}, {})

    def test_source_revision_must_be_explicit_even_if_bytes_match_task_base(self):
        from unittest.mock import patch

        from examples.software_factory.factory.model import digest

        task = {"base_commit": "b" * 40, "source_git": "owned.git", "source_roots": ["src"]}
        fixture = {"source_sha256": {"A": digest({"src/a.py": "current"})}}
        with patch(
            "examples.software_factory.harbor.provision.source_files",
            return_value={"src/a.py": "current"},
        ) as read:
            with self.assertRaises(PermissionError):
                self.provision._source(task, fixture, "A")
        read.assert_not_called()

    def test_network_detach_rechecks_controller_owner_without_stopping_it(self):
        self.provision._record(
            "created", kind="network_attachment", name="outer", id="c" * 64, network="n" * 64
        )
        self.provision._owned = Mock(side_effect=[{}, PermissionError("foreign controller")])
        with self.assertRaises(PermissionError):
            self.provision.cleanup()
        self.assertEqual(self.provision._owned.call_count, 2)
        self.run.assert_not_called()
