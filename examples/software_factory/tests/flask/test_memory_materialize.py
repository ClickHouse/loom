"""Owned Git fixtures preserve full base data and isolate private grading inputs."""

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.software_factory.evals.flask.historical import FrozenSource, git
from examples.software_factory.factory.model import digest
from examples.software_factory.tests.fixtures.flask_history import materialize


class MaterializeCases(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        git(self.repo, "init", "--initial-branch=public-base")
        git(self.repo, "config", "user.name", "Fixture test")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        data = {
            "src/flask/example.py": b'value = "original"\n',
            "tests/test_smoke.py": b"def test_smoke(): pass\n",
            "tests/.env": b"PUBLIC_FIXTURE=1\n",
            "tests/icon.bin": b"\x00\xff\r\n",
            "LICENSE.rst": b"Public fixture license\n",
            "requirements/dev.txt": b"pytest==7.4.4\n",
        }
        for name, value in data.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "Public base")
        self.commit = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.before = git(self.repo, "show-ref")
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.put("assets.json", dict(source_git=str(self.repo)))
        self.put("images.json", dict(images={"dependency": dict(image="sha256:" + "a" * 64)}))
        self.public = dict(
            task_id="cold-case",
            repository="pallets/flask",
            base_commit=self.commit,
            problem="Public cold issue",
        )
        self.put("historical.json", dict(tasks=[self.public]))
        self.put("trusted/tasks.json", [dict(instance_id="cold-case", base_commit=self.commit)])
        self.case = dict(case_id="authored-case", function="public_function")
        self.manifest = dict(
            base_commit=self.commit,
            dependency_task_id="dependency",
            cases=[self.case],
        )
        self.lock = self.root / "suite.lock.json"
        self.lock.write_text(
            json.dumps(
                dict(
                    tasks=[
                        dict(
                            task_id="dependency",
                            visible_selectors=["tests/test_smoke.py::test_smoke"],
                        )
                    ]
                )
            )
        )
        self.source_a = {"src/flask/example.py": 'value = "policy_a"\n'}
        self.source_b = {"src/flask/example.py": 'value = "broken_b"\n'}
        self.prepared = dict(
            case_id="authored-case",
            problem="Public maintenance issue",
            family=materialize.FAMILY,
            source_revisions=dict(A=self.source_a, B=self.source_b),
            source_sha256=dict(A=digest(self.source_a), B=digest(self.source_b)),
            public_documents={"notes/contract.md": "Public project contract"},
            history_events=[dict(key="contract", value="Public project contract", files=[])],
            history_counts=dict(current_rows=1, code_linked_rows=0, unlinked_rows=1),
        )
        self.hidden = dict(
            test_patch="PRIVATE_EVALUATOR_SENTINEL\n",
            hidden_selectors=["tests/private.py::test_x"],
            evaluator_sha256="b" * 64,
        )
        for name, value in (
            ("load_manifest", self.manifest),
            ("prepare_case", self.prepared),
            ("evaluator_spec", self.hidden),
        ):
            context = patch.object(materialize, name, return_value=value)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(materialize, "LOCK", self.lock)
        context.start()
        self.addCleanup(context.stop)

    def put(self, name, value):
        path = self.assets / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def run_materialize(self, name="out"):
        return materialize.materialize_cases(self.assets, self.root / name)

    def test_real_commits_are_deterministic_and_the_original_repository_is_unchanged(self):
        first, second = self.run_materialize("first"), self.run_materialize("second")
        one, two = first["fixtures"]["authored-case"], second["fixtures"]["authored-case"]
        self.assertEqual(one["revisions"], two["revisions"])
        repo = Path(one["source_git"])
        self.assertEqual(
            git(repo, "rev-parse", one["revisions"]["B"]["commit"] + "^").decode().strip(),
            one["revisions"]["A"]["commit"],
        )
        self.assertEqual(git(self.repo, "show-ref"), self.before)
        self.assertEqual((self.repo / "src/flask/example.py").read_text(), 'value = "original"\n')
        self.assertFalse((repo / "objects/info/alternates").exists())
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["provider_requests"], 0)
        self.assertEqual(first["index_writes"], 0)

    def test_full_base_fixtures_and_requirements_survive_but_editable_source_stays_scoped(self):
        receipt = self.run_materialize()
        fixture = receipt["fixtures"]["authored-case"]
        source = FrozenSource(Path(fixture["source_git"]), fixture["revisions"]["B"]["commit"])
        self.assertEqual(source.files("src/flask"), self.source_b)
        self.assertEqual(source.files()["LICENSE.rst"], "Public fixture license\n")
        self.assertEqual(source.files()["requirements/dev.txt"], "pytest==7.4.4\n")
        fixtures = {k: base64.b64decode(v) for k, v in source.fixtures().items()}
        self.assertEqual(
            fixtures, {"tests/.env": b"PUBLIC_FIXTURE=1\n", "tests/icon.bin": b"\x00\xff\r\n"}
        )

    def test_public_private_split_and_reference_patch_match_native_control_contract(self):
        receipt = self.run_materialize()
        public = json.loads(Path(receipt["historical_manifest"]).read_text())["tasks"]
        self.assertEqual([row["task_id"] for row in public], ["cold-case", "authored-case"])
        self.assertEqual(public[-1]["scenario_family"], materialize.FAMILY)
        controls = json.loads(Path(receipt["reference_controls_file"]).read_text())
        authored = controls[-1]
        evaluation = json.loads(
            Path(receipt["tasks"]["authored-case"]["evaluation_file"]).read_text()
        )
        self.assertEqual(evaluation["base_commit"], authored["base_commit"])
        self.assertEqual(evaluation["test_patch"], authored["test_patch"])
        self.assertEqual(authored["test_patch"], self.hidden["test_patch"])
        self.assertEqual(json.loads(authored["FAIL_TO_PASS"]), self.hidden["hidden_selectors"])
        self.assertEqual(json.loads(authored["PASS_TO_PASS"]), ["tests/test_smoke.py::test_smoke"])
        checkout = self.root / "patch-check"
        path = checkout / "src/flask/example.py"
        path.parent.mkdir(parents=True)
        path.write_text(self.source_b["src/flask/example.py"])
        git(checkout, "apply", "-", data=authored["patch"].encode())
        self.assertEqual(path.read_text(), 'value = "original"\n')
        for path in (self.root / "out/public").rglob("*.json"):
            self.assertNotIn("PRIVATE_EVALUATOR_SENTINEL", path.read_text())
            self.assertNotIn('"hidden_selectors"', path.read_text())
        for name, sha in receipt["files"].items():
            self.assertEqual(len(sha), 64)
            self.assertTrue(Path(name).is_file())

    def test_existing_output_is_refused_and_no_shared_main_ref_is_created(self):
        receipt = self.run_materialize()
        with self.assertRaises(FileExistsError):
            self.run_materialize()
        fixture = receipt["fixtures"]["authored-case"]
        names = git(Path(fixture["source_git"]), "for-each-ref", "--format=%(refname)").decode()
        self.assertNotIn("refs/heads/main", names)
        self.assertIn("refs/heads/fixture-a", names)
        self.assertIn("refs/heads/fixture-b", names)

    def test_public_source_object_database_without_named_refs_is_supported(self):
        git(self.repo, "update-ref", "-d", "refs/heads/public-base")
        receipt = self.run_materialize()
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(git(self.repo, "for-each-ref"), b"")

    def test_profile_uses_explicit_binding_and_common_kind_for_both_families(self):
        from examples.software_factory.evals.flask import profile

        receipt = self.run_materialize()
        protocol = dict(tasks=receipt["tasks"], historical_manifest=receipt["historical_manifest"])
        baseline = profile.task_inputs(protocol, "authored-case")[2]
        treatment = profile.task_inputs(
            protocol, "authored-case", binding={"code_namespace": "bound"}
        )[2]
        self.assertEqual(baseline.namespace, "-")
        self.assertEqual(treatment.namespace, "bound")
        self.assertEqual(baseline.kind, "flask-e2e")
        self.assertEqual(baseline.files, self.source_b)
        cold_evaluator = self.root / "cold-evaluation.json"
        cold_evaluator.write_text(json.dumps(dict(base_commit=self.commit, test_patch="")))
        protocol["tasks"]["cold-case"] = dict(
            source_git=str(self.repo), symbol="example", evaluation_file=str(cold_evaluator)
        )
        self.assertEqual(profile.task_inputs(protocol, "cold-case")[2].kind, "flask-e2e")
        with self.assertRaises(TypeError):
            profile.task_inputs(protocol, "authored-case", "treesitter")
        with self.assertRaises(PermissionError):
            profile.task_inputs(protocol, "authored-case", binding={"code_namespace": "-"})
        with self.assertRaises(KeyError):
            profile.task_inputs(protocol, "authored-case", binding={})
