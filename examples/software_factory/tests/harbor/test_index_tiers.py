import asyncio
import unittest
from collections import Counter
from types import SimpleNamespace

from examples.software_factory.evals.flask.indexing import ScopeVerifier
from examples.software_factory.harbor.live_support import schedule

ZERO = dict(
    compiler_definitions=0, compiler_references=0, python_definitions=0, python_references=0
)


class TierContracts(unittest.TestCase):
    def test_three_arms_are_balanced_by_position_within_each_task(self):
        rows = schedule(["a", "b", "c"], seed=741)
        self.assertEqual(len(rows), 33)
        self.assertEqual(sum(r["repeat"] == "aa" for r in rows), 6)
        for task in ["a", "b", "c"]:
            positions = {arm: Counter() for arm in ["baseline", "loom-treesitter", "loom-scip"]}
            for repeat in range(3):
                triplet = [r for r in rows if r["task_id"] == task and r["repeat"] == repeat]
                self.assertEqual(len(triplet), 3)
                self.assertEqual(
                    [r["order"] for r in triplet],
                    list(range(triplet[0]["order"], triplet[0]["order"] + 3)),
                )
                for pos, row in enumerate(triplet):
                    positions[row["arm"]][pos] += 1
                    self.assertEqual(
                        row["mode"], "baseline" if row["arm"] == "baseline" else "natural"
                    )
                    self.assertEqual(
                        row["index_tier"],
                        None if row["arm"] == "baseline" else row["arm"].removeprefix("loom-"),
                    )
            self.assertTrue(all(p == Counter({0: 1, 1: 1, 2: 1}) for p in positions.values()))
        self.assertTrue(all(r["index_tier"] is None for r in rows if r["repeat"] == "aa"))

    def verifier(self, tier, counts):
        return ScopeVerifier(
            dict(org="org", namespace=tier, repository="repo", commit="a" * 40),
            index_repository="repo",
            build_commit="b" * 40,
            certificate_sha256="c" * 64,
            index_tier=tier,
            compiler_rows=counts,
        )

    def status(self, tier):
        data = dict(
            org="org",
            namespace=tier,
            repo="repo",
            git_head="a" * 40,
            build_commit="b" * 40,
            scip_commit="",
            scip_commit_py="",
            compiler_tiers=[],
        )
        if tier == "scip":
            data.update(
                scip_commit="a" * 40,
                scip_commit_py="a" * 40,
                scip_producer="python",
                compiler_tiers=[
                    dict(
                        producer="python",
                        tool="scip-python",
                        watermark_key="scip_commit_py",
                        commit="a" * 40,
                    )
                ],
            )
        return data

    def call(self, verifier, data):
        class Agent:
            async def call_with_meta(self, *args, **kwargs):
                return SimpleNamespace(data=data, request_id="status")

        return asyncio.run(verifier(Agent(), dict(org="org", namespace=data["namespace"])))

    def test_scip_requires_real_python_rows_and_matching_live_compiler_watermark(self):
        counts = dict(
            compiler_definitions=10,
            compiler_references=12,
            python_definitions=10,
            python_references=12,
        )
        verifier = self.verifier("scip", counts)
        self.assertEqual(self.call(verifier, self.status("scip"))["index_tier"], "scip")
        for change in [
            dict(scip_commit_py="d" * 40),
            dict(compiler_tiers=[]),
            dict(
                compiler_tiers=[
                    dict(
                        producer="python",
                        tool="scip-python",
                        watermark_key="scip_commit_py",
                        commit="d" * 40,
                    )
                ]
            ),
        ]:
            with self.subTest(change=change), self.assertRaises(PermissionError):
                self.call(verifier, {**self.status("scip"), **change})
        with self.assertRaises(PermissionError):
            self.verifier("scip", ZERO)

    def test_treesitter_refuses_compiler_contamination(self):
        verifier = self.verifier("treesitter", ZERO)
        self.assertEqual(self.call(verifier, self.status("treesitter"))["index_tier"], "treesitter")
        with self.assertRaises(PermissionError):
            self.call(verifier, {**self.status("treesitter"), "scip_commit_py": "a" * 40})
        with self.assertRaises(PermissionError):
            self.verifier("treesitter", {**ZERO, "compiler_definitions": 1})

    def test_task_source_scope_is_neutral_until_a_treatment_tier_is_explicit(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock, patch

        from examples.software_factory.harbor.runtime import task_inputs

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "public.json").write_text(
                json.dumps({"tasks": [{"task_id": "task", "base_commit": "a" * 40}]})
            )
            (root / "evaluation.json").write_text(json.dumps({"base_commit": "a" * 40}))
            protocol = {
                "suite": {"adapter": "examples.software_factory.evals.flask.profile"},
                "historical_manifest": str(root / "public.json"),
                "tasks": {
                    "task": {
                        "source_git": "public.git",
                        "symbol": "Handler.open",
                        "evaluation_file": str(root / "evaluation.json"),
                        "indexes": {
                            "treesitter": {"namespace": "parse"},
                            "scip": {"namespace": "compile"},
                        },
                    }
                },
            }
            source = Mock()
            source.task.side_effect = lambda public, ns, symbol: SimpleNamespace(
                namespace=ns, base_commit=public["base_commit"]
            )
            with patch(
                "examples.software_factory.evals.flask.profile.FrozenSource", return_value=source
            ):
                self.assertEqual(task_inputs(protocol, "task")[2].namespace, "-")
                self.assertEqual(task_inputs(protocol, "task", "treesitter")[2].namespace, "parse")
                self.assertEqual(task_inputs(protocol, "task", "scip")[2].namespace, "compile")
                with self.assertRaises(ValueError):
                    task_inputs(protocol, "task", "per-arm")

    def test_backend_selects_only_the_certified_treatment_token_and_namespace(self):
        import hashlib
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from examples.software_factory.factory.model import digest
        from examples.software_factory.harbor.runtime import backend

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "reader.token"
            token.write_text("test-reader")
            counts = dict(
                compiler_definitions=2,
                compiler_references=3,
                python_definitions=2,
                python_references=3,
            )
            files = {"src/public.py": "public source"}
            source_proof = dict(
                verified=True,
                base_commit="a" * 40,
                source_sha256=digest(files),
                paths_sha256=digest(sorted(files)),
                file_count=1,
                tables={},
            )
            expected = dict(
                org="org",
                namespace="compiled",
                repo="repo",
                git_head="a" * 40,
                build_commit="b" * 40,
                compiler_rows=counts,
                source_scope=source_proof,
            )
            certificate = root / "certificate.json"
            certificate.write_text(json.dumps({"scopes": {"task:scip": expected}}))
            protocol = dict(
                suite={"adapter": "examples.software_factory.evals.flask.profile"},
                org="org",
                server_commit="b" * 40,
                loom_url="http://unused",
                index_certificate_file=str(certificate),
                index_certificate_sha256=hashlib.sha256(certificate.read_bytes()).hexdigest(),
                tasks={
                    "task": {
                        "indexes": {
                            "scip": dict(
                                namespace="compiled",
                                reader_token_file=str(token),
                                compiler_rows=counts,
                                source_scope=source_proof,
                            )
                        }
                    }
                },
            )
            task = SimpleNamespace(
                task_id="task",
                namespace="compiled",
                repository="repo",
                base_commit="a" * 40,
                files=files,
                source_sha256=digest(files),
            )
            with (
                patch("loom.Agent") as agent,
                patch("examples.software_factory.harbor.runtime.SDKContext"),
            ):
                backend(protocol, task, root / "transport.jsonl", index_tier="scip")
                self.assertEqual(agent.call_args.kwargs["namespace"], "compiled")
                self.assertEqual(agent.call_args.kwargs["api_key"], "test-reader")
            with self.assertRaises(ValueError):
                backend(protocol, task, root / "none.jsonl", index_tier=None)
            task.namespace = "parse"
            with self.assertRaises(PermissionError):
                backend(protocol, task, root / "wrong.jsonl", index_tier="scip")
            task.namespace = "compiled"
            task.files = {**files, "tests/extra.py": "not public source"}
            task.source_sha256 = digest(task.files)
            with self.assertRaisesRegex(PermissionError, "public task source"):
                backend(protocol, task, root / "coverage.jsonl", index_tier="scip")
            task.files = files
            task.source_sha256 = digest(files)
            certificate.write_text("{}")
            with self.assertRaises(PermissionError):
                backend(protocol, task, root / "changed.jsonl", index_tier="scip")

    def test_frozen_compiler_proof_rejects_missing_bool_and_inverted_counts(self):
        for counts in [{}, {**ZERO, "python_definitions": True}, {**ZERO, "python_definitions": 1}]:
            with self.subTest(counts=counts), self.assertRaises(PermissionError):
                self.verifier("scip", counts)

    def test_treatment_tier_does_not_change_prompt_or_optional_tool_schema(self):
        import tempfile
        from dataclasses import replace
        from pathlib import Path

        from examples.software_factory.factory.adapters import ReplayContext, ReplayProvider
        from examples.software_factory.factory.engine import Factory
        from examples.software_factory.tests.factory.test_factory import ROOT, FactoryContracts
        from examples.software_factory.tests.fixtures.python.grading import ObjectiveGrader

        fixture = FactoryContracts()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with tempfile.TemporaryDirectory() as directory:
            factories = []
            for tier in ["treesitter", "scip"]:
                task = replace(fixture.task, namespace=tier)
                factories.append(
                    Factory(
                        task,
                        ReplayProvider(ROOT / fixture.frozen["replay"]["path"]),
                        ObjectiveGrader(
                            ROOT / fixture.frozen["grader"]["path"],
                            fixture.frozen["grader"]["sha256"],
                        ),
                        Path(directory) / tier,
                        mode="natural",
                        backend=ReplayContext(task),
                    )
                )
            self.assertEqual(factories[0].messages, factories[1].messages)
            self.assertEqual(factories[0].tools, factories[1].tools)
            self.assertTrue(all(f.run()["loom_logical_requests"] == 0 for f in factories))
