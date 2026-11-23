"""The public launcher orchestrates only exact owned resources, without live Docker."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.software_factory.harbor import launch
from examples.software_factory.harbor.arms import PRIMARY
from examples.software_factory.harbor.build_recipe import COMPILER_INSTALL
from examples.software_factory.harbor.catalog import load_evaluation


class FakeDocker:
    key = "CREDENTIAL_SENTINEL"

    def __init__(self, *, interrupt=False, foreign=False, exit_code=0):
        self.calls, self.containers = [], {}
        self.image = None
        self.interrupted, self.foreign, self.exit_code = interrupt, foreign, exit_code

    def run(self, args, **options):
        self.calls.append((list(args), options))
        if args[0] == "version":
            return "29.1.3"
        if args[0] == "build":
            context = Path(args[-1])
            assert not (context / ".env").exists()
            assert not (context / "server/.env").exists()
            assert "--extra scip --extra extractor" in (context / "Dockerfile").read_text()
            labels = dict(
                arg.split("=", 1) for i, arg in enumerate(args) if i and args[i - 1] == "--label"
            )
            self.image = dict(id="sha256:" + "a" * 64, labels=labels)
            if self.foreign:
                self.image["labels"][launch.OWNER_LABEL] = "another-owner"
            return ""
        if args[:2] == ["image", "inspect"]:
            return json.dumps(self.image) if self.image else None
        if args[0] == "create":
            labels = dict(
                arg.split("=", 1) for i, arg in enumerate(args) if i and args[i - 1] == "--label"
            )
            name = args[args.index("--name") + 1]
            identifier = format(len(self.containers) + 1, "064x")
            action = args[args.index(self.image["id"]) + 1]
            self.containers[name] = dict(
                id=identifier,
                labels=labels,
                running=False,
                exit_code=0,
                action=action,
                output=args[args.index("--output") + 1],
            )
            return identifier
        if args[:2] == ["container", "inspect"]:
            return json.dumps(self.containers[args[-1]]) if args[-1] in self.containers else None
        if args[0] in {"rm", "stop"}:
            for name, item in list(self.containers.items()):
                if item["id"] == args[-1]:
                    if args[0] == "rm":
                        del self.containers[name]
                    else:
                        item["running"] = False
            return ""
        if args[:2] == ["image", "rm"]:
            self.image = None
            return ""
        raise AssertionError(args)

    def attach(self, name, **options):
        item = self.containers[name]
        if self.interrupted and item["action"] != "cleanup":
            item["running"] = True
            raise KeyboardInterrupt
        item["running"] = False
        item["exit_code"] = self.exit_code if item["action"] != "cleanup" else 0
        Path(item["output"], "cleanup-receipt.json").write_text(
            json.dumps(dict(owner=item["labels"][launch.OWNER_LABEL], complete=True))
        )
        return 0  # Docker attachment success alone must not replace the native exit status.

    def interrupt(self, name):
        self.calls.append((["graceful-interrupt", name], {}))
        self.containers[name].update(running=False, exit_code=130)


class PublicLauncher(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "source"
        self.root.mkdir()
        here = self.root / launch.HERE
        here.mkdir(parents=True)
        for name in ("controller.Dockerfile", "requirements.txt"):
            shutil.copyfile(launch.ROOT / launch.HERE / name, here / name)
        (here / "one_shot.py").write_text("# Inner controller fixture\n")
        base = (
            "python:3.12-slim@sha256:"
            "a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
        )
        (self.root / "Dockerfile").write_text(
            f"FROM {base} AS py-builder\n"
            + COMPILER_INSTALL
            + "\n"
            + "RUN uv sync --locked --extra local-embedder\n"
            "FROM py-builder AS local-models\nRUN unavailable-heavy-stage\n"
        )
        for name in ("pyproject.toml", "uv.lock", "README.md"):
            (self.root / name).write_text("public fixture\n")
        for name in ("server", "sdk/python"):
            (self.root / name).mkdir(parents=True)
            (self.root / name / "public.py").write_text("value = 1\n")
        (self.root / ".env").write_text("CREDENTIAL_SENTINEL")
        (self.root / "server/.env").write_text("CREDENTIAL_SENTINEL")
        self.output = self.root / "factory-run"
        for relative in ("embedding/local_models.py", "core/llm_provider.py"):
            destination = self.root / "server" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(launch.ROOT / "server" / relative, destination)
        for evaluation in ("flask", "sample"):
            profile = self.root / "examples/software_factory/evals" / evaluation
            profile.mkdir(parents=True)
            data = {
                "id": evaluation,
                "title": "Public evaluation",
                "adapter": f"examples.software_factory.evals.{evaluation}.profile",
                "suite_lock": "project-lock.json",
                "scenario_manifest": "scenarios.json",
                "memory_catalog": "records.json",
            }
            (profile / "eval.json").write_text(json.dumps(data))
            (profile / "project-lock.json").write_text(
                json.dumps(
                    {
                        "model_id": "claude-haiku-5-5",
                        "source_roots": ["src"],
                        "tasks": [{"task_id": "cold"}],
                    }
                )
            )
            (profile / "scenarios.json").write_text(json.dumps({"cases": [{"case_id": "history"}]}))
            (profile / "records.json").write_text(
                json.dumps(
                    {
                        "records": [
                            {"key": "linked", "files": ["src/a.py"]},
                            {"key": "plain", "files": []},
                        ]
                    }
                )
            )

    def test_plan_is_stdlib_only_and_never_starts_docker(self):
        result = subprocess.run(
            [sys.executable, "-S", "-m", "examples.software_factory", "plan", "--json"],
            cwd=launch.ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        plan = json.loads(result.stdout)
        self.assertEqual(plan["total_attempts"], 51)
        self.assertEqual(plan["evaluation"]["id"], "flask")
        self.assertEqual(plan["primary_attempts"], 45)
        self.assertEqual(plan["aa_attempts"], 6)
        self.assertEqual(plan["blind_judges"]["requests"], 12)
        self.assertTrue(plan["blind_judges"]["fixed_only"])
        self.assertEqual(plan["blind_judges"]["model"], plan["model"])
        self.assertEqual(plan["model"], "claude-opus-5-5")
        self.assertEqual(plan["native_provider"], "anthropic")
        self.assertEqual(plan["native_model"], "claude-sonnet-5-5")
        self.assertEqual(plan["arms"], list(PRIMARY))
        self.assertEqual(
            (
                plan["memory"]["project_records"],
                plan["memory"]["linked"],
                plan["memory"]["unlinked"],
            ),
            (30, 14, 16),
        )
        self.assertEqual(plan["memory"]["cases_without_history"], 0)
        self.assertEqual(plan["memory"]["memory_namespaces"], 18)
        self.assertEqual(plan["memory"]["memory_disabled_primary_attempts"], 27)
        self.assertFalse(plan["memory"]["equal_plain_notes_all_arms"])
        self.assertEqual(plan["embedding"]["revision"], "aa8f8b060edb00e03bfdd08813a2949946c8ba55")
        self.assertEqual(len(plan["embedding"]["files"]), 5)

    def test_pilot_plan_declares_one_issue_and_five_attempts(self):
        result = launch.plan(pilot_task="pallets__flask-4230")
        self.assertEqual(result["study_mode"], "pilot")
        self.assertEqual((result["cases"], result["repeats"], result["total_attempts"]), (1, 1, 5))
        self.assertEqual(result["aa_attempts"], 0)
        self.assertEqual(result["blind_judges"]["requests"], 1)
        self.assertEqual(result["memory"]["memory_namespaces"], 2)
        with self.assertRaises(launch.LaunchError):
            launch.plan(pilot_task="unknown")

    def test_pilot_can_freeze_the_compact_catalog(self):
        result = launch.plan(
            pilot_task="pallets__flask-4230", cache_pairs=True, loom_schema_profile="compact"
        )
        self.assertEqual(result["loom_schema_profile"], "compact")
        self.assertEqual(result["total_attempts"], 10)
        self.assertFalse(result["paid_requests"])

    def test_run_is_one_command_with_owned_images_and_identical_readonly_paths(self):
        docker = FakeDocker()
        self.assertEqual(launch.execute("run", self.output, root=self.root, docker=docker), 0)
        self.assertEqual(docker.containers, {})
        self.assertIsNone(docker.image)
        record = launch.read(self.output / "launch.json")
        self.assertEqual(record["state"], "completed")
        self.assertTrue(record["outer_cleanup_complete"])
        create = next(args for args, _ in docker.calls if args[0] == "create")
        self.assertIn(f"type=bind,src={self.root},dst={self.root},readonly", create)
        self.assertIn(
            f"type=bind,src={self.root}/sdk/python,dst={self.root}/sdk/python,readonly", create
        )
        self.assertIn("type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock", create)
        self.assertIn("ANTHROPIC_API_KEY", create)
        self.assertEqual(create[create.index("--eval") + 1], "flask")
        self.assertNotIn(docker.key, json.dumps(docker.calls))
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertNotIn(docker.key, path.read_text())

    def test_linked_worktree_mounts_its_exact_git_object_store_readonly(self):
        common = self.root.parent / "objects.git"
        (common / "objects").mkdir(parents=True)
        worktree = common / "worktrees/evaluation"
        worktree.mkdir(parents=True)
        (worktree / "commondir").write_text("../..")
        (self.root / ".git").write_text("gitdir: " + str(worktree))
        docker = FakeDocker()
        self.assertEqual(launch.execute("run", self.output, root=self.root, docker=docker), 0)
        create = next(args for args, _ in docker.calls if args[0] == "create")
        self.assertIn(f"type=bind,src={common},dst={common},readonly", create)

    def test_budget_and_existing_output_refuse_before_docker(self):
        docker = FakeDocker()
        docker.key = ""
        with self.assertRaises(launch.LaunchError):
            launch.execute("run", self.output, root=self.root, docker=docker)
        self.assertEqual(docker.calls, [])
        for invalid in ("0", "-1", "NaN", "Infinity"):
            with self.assertRaises(launch.argparse.ArgumentTypeError):
                launch.budget(invalid)
        with self.assertRaisesRegex(launch.LaunchError, "without a spending cap"):
            launch.execute("run", self.output, "10", root=self.root, docker=docker)
        self.output.mkdir()
        docker.key = "fixture-key"
        with self.assertRaisesRegex(launch.LaunchError, "fresh output path"):
            launch.execute("run", self.output, root=self.root, docker=docker)
        self.assertEqual(docker.calls, [])

    def test_no_cap_is_forwarded_as_an_unlimited_frozen_run(self):
        docker = FakeDocker()
        self.assertEqual(launch.execute("run", self.output, root=self.root, docker=docker), 0)
        record = launch.read(self.output / "launch.json")
        self.assertIsNone(record["budget_usd"])
        create = next(args for args, _ in docker.calls if args[0] == "create")
        self.assertNotIn("--budget-usd", create)

    def test_interrupt_gracefully_cleans_without_replaying_a_paid_run(self):
        docker = FakeDocker(interrupt=True)
        self.assertEqual(launch.execute("run", self.output, root=self.root, docker=docker), 130)
        record = launch.read(self.output / "launch.json")
        self.assertEqual([r["action"] for r in record["containers"]], ["run", "cleanup"])
        self.assertTrue(any(args[0] == "graceful-interrupt" for args, _ in docker.calls))
        self.assertEqual(docker.containers, {})
        self.assertIsNotNone(docker.image)
        cleanup_create = [args for args, _ in docker.calls if args[0] == "create"][-1]
        self.assertNotIn("ANTHROPIC_API_KEY", cleanup_create)
        docker.interrupted = False
        self.assertEqual(launch.execute("resume", self.output, root=self.root, docker=docker), 0)
        self.assertTrue((self.output / "cleanup-receipt-prior-2.json").exists())
        self.assertIsNone(docker.image)

    def test_native_failure_is_not_replaced_by_successful_docker_attachment(self):
        docker = FakeDocker(exit_code=7)
        self.assertEqual(launch.execute("run", self.output, root=self.root, docker=docker), 7)
        self.assertEqual(launch.read(self.output / "launch.json")["state"], "failed")
        docker.key = ""
        self.assertEqual(launch.execute("cleanup", self.output, root=self.root, docker=docker), 0)
        self.assertIsNone(docker.image)

    def test_foreign_owner_is_a_stopped_guard_not_a_cleanup_fallback(self):
        docker = FakeDocker(foreign=True)
        with self.assertRaises(launch.OwnershipError):
            launch.execute("run", self.output, root=self.root, docker=docker)
        self.assertFalse(
            any(
                args[0] in {"create", "rm"} or args[:2] == ["image", "rm"]
                for args, _ in docker.calls
            )
        )

    def test_source_and_treatment_changes_block_resume(self):
        docker = FakeDocker(interrupt=True)
        launch.execute("run", self.output, root=self.root, docker=docker)
        before = len(docker.calls)
        with self.assertRaisesRegex(launch.LaunchError, "frozen Loom MCP schema profile"):
            launch.execute(
                "resume", self.output, root=self.root, docker=docker, loom_schema_profile="compact"
            )
        (self.root / "server/public.py").write_text("changed = True\n")
        with self.assertRaises(launch.LaunchError):
            launch.execute("resume", self.output, root=self.root, docker=docker)
        self.assertEqual(len(docker.calls), before)

    def test_credentials_are_forwarded_by_name_and_scrubbed_from_output(self):
        with patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "CREDENTIAL_SENTINEL", "OTHER_SECRET": "private"}
        ):
            docker = launch.Docker()
            self.assertNotIn("ANTHROPIC_API_KEY", docker.environment())
            self.assertNotIn("OTHER_SECRET", docker.environment(True))
            self.assertEqual(docker.environment(True)["ANTHROPIC_API_KEY"], "CREDENTIAL_SENTINEL")
            process = type(
                "Process", (), {"stdout": io.StringIO("CREDENTIAL_SENTINEL\n"), "wait": lambda _: 0}
            )()
            with (
                patch.object(launch.subprocess, "Popen", return_value=process) as popen,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                docker.attach("owned", secret=True)
            self.assertNotIn("CREDENTIAL_SENTINEL", output.getvalue())
            self.assertNotIn("CREDENTIAL_SENTINEL", repr(popen.call_args.args))

    def test_build_auth_uses_host_proxy_and_steps_use_container_proxy(self):
        docker = launch.Docker()
        docker.proxy_url = "http://factory:PROXY_SENTINEL@host.docker.internal:1234"
        docker.proxy_host_url = "http://factory:PROXY_SENTINEL@127.0.0.1:1234"
        docker.proxy_token = "PROXY_SENTINEL"
        process = type(
            "Process", (), {"stdout": io.StringIO(docker.proxy_host_url), "wait": lambda _: 0}
        )()
        with (
            patch.object(launch.subprocess, "Popen", return_value=process) as popen,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            docker.run(["build", "."])
        self.assertEqual(popen.call_args.kwargs["env"]["HTTPS_PROXY"], docker.proxy_host_url)
        self.assertEqual(
            popen.call_args.args[0],
            ["docker", "build", "--build-arg", "HTTPS_PROXY=" + docker.proxy_url, "."],
        )
        self.assertEqual(docker.environment()["HTTPS_PROXY"], docker.proxy_url)
        self.assertNotIn("PROXY_SENTINEL", output.getvalue())

    def test_output_lock_prevents_concurrent_resume_or_cleanup(self):
        docker = FakeDocker(interrupt=True)
        launch.execute("run", self.output, root=self.root, docker=docker)
        before = len(docker.calls)
        with launch.output_lock(self.output):
            with self.assertRaisesRegex(launch.LaunchError, "Another launcher"):
                launch.execute("resume", self.output, root=self.root, docker=docker)
            with self.assertRaisesRegex(launch.LaunchError, "Another launcher"):
                launch.execute("cleanup", self.output, root=self.root, docker=docker)
        self.assertEqual(len(docker.calls), before)

    def test_atomic_record_write_recovers_without_overwriting_unknown_scratch(self):
        self.output.mkdir()
        stale = self.output / "launch.json.stale.tmp"
        stale.write_text("unrelated scratch")
        launch.save(self.output / "launch.json", {"owner": "first"})
        launch.save(self.output / "launch.json", {"owner": "second"})
        self.assertEqual(launch.read(self.output / "launch.json"), {"owner": "second"})
        self.assertEqual(stale.read_text(), "unrelated scratch")

    def test_source_context_and_sdk_symlinks_never_copy_external_inputs(self):
        secret = Path(self.temporary.name) / "private.txt"
        secret.write_text("CREDENTIAL_SENTINEL")
        (self.root / "sdk/python/linked.py").symlink_to(secret)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(launch.LaunchError, "source link"):
                launch.build_context(self.root, Path(directory))

    def test_another_profile_inherits_the_same_planner_without_flask_file_names(self):
        description = launch.plan(self.root, evaluation="sample")
        self.assertEqual(description["evaluation"]["id"], "sample")
        self.assertEqual(description["total_attempts"], 34)
        self.assertEqual(description["blind_judges"]["requests"], 8)
        self.assertEqual(description["memory"]["project_records"], 2)
        self.assertEqual(description["arms"], list(PRIMARY))

    def test_profile_identity_cannot_change_on_resume_or_cleanup(self):
        docker = FakeDocker(interrupt=True)
        launch.execute("run", self.output, root=self.root, docker=docker)
        before = len(docker.calls)
        for action in ("resume", "cleanup"):
            with self.subTest(action=action), self.assertRaises(launch.LaunchError):
                launch.execute(
                    action, self.output, root=self.root, docker=docker, evaluation="sample"
                )
        self.assertEqual(len(docker.calls), before)

    def test_catalog_rejects_traversal_or_external_symlink_inputs(self):
        for evaluation in ("../sample", "sample/child", "/tmp/sample"):
            with self.assertRaises(ValueError):
                load_evaluation(self.root, evaluation)
        profile = self.root / "examples/software_factory/evals/sample"
        descriptor = json.loads((profile / "eval.json").read_text())
        for value in ("../project-lock.json", "/tmp/project-lock.json", "..\\secret"):
            descriptor["suite_lock"] = value
            (profile / "eval.json").write_text(json.dumps(descriptor))
            with self.assertRaises(ValueError):
                load_evaluation(self.root, "sample")
        outside = Path(self.temporary.name) / "private.json"
        outside.write_text("CREDENTIAL_SENTINEL")
        (profile / "foreign.json").symlink_to(outside)
        descriptor["suite_lock"] = "foreign.json"
        (profile / "eval.json").write_text(json.dumps(descriptor))
        with self.assertRaises(ValueError):
            load_evaluation(self.root, "sample")


if __name__ == "__main__":
    unittest.main()
