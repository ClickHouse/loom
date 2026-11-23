"""Public inputs are pinned before any server or paid execution."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from examples.software_factory.evals.flask import bootstrap
from examples.software_factory.factory.model import digest


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.lock = json.loads(bootstrap.LOCK.read_text())
        self.row = {
            "instance_id": "sample",
            "repo": "pallets/flask",
            "base_commit": "a" * 40,
            "problem_statement": "Public issue",
            "patch": "REFERENCE_SECRET",
            "test_patch": "HIDDEN_SECRET",
            "FAIL_TO_PASS": '["tests/test_x.py::test_fix"]',
            "PASS_TO_PASS": '["tests/test_x.py::test_existing"]',
        }
        self.pin = {
            "task_id": "sample",
            "base_commit": "a" * 40,
            "record_sha256": digest(self.row),
            "visible_selectors": ["tests/test_x.py::test_existing"],
        }

    def test_record_identity_and_bytes_are_pinned(self):
        self.assertEqual(bootstrap.select_records([self.row], [self.pin]), [self.row])
        changed = dict(self.row, patch="different")
        with self.assertRaises(PermissionError):
            bootstrap.select_records([changed], [self.pin])
        with self.assertRaises(PermissionError):
            bootstrap.select_records([self.row, self.row], [self.pin])

    def test_visible_tests_must_be_pass_to_pass(self):
        pin = dict(self.pin, visible_selectors=["tests/test_x.py::test_fix"])
        with self.assertRaises(PermissionError):
            bootstrap.select_records([self.row], [pin])

    def test_complete_functions_retain_truncated_parameterized_cases(self):
        row = {
            **self.row,
            "PASS_TO_PASS": json.dumps(
                [
                    'tests/test_x.py::test_existing[factory("foo",',
                    "tests/test_x.py::test_existing[other]",
                ]
            ),
        }
        pin = {**self.pin, "record_sha256": digest(row)}
        self.assertEqual(bootstrap.select_records([row], [pin]), [row])
        self.assertEqual(
            bootstrap.hidden_selectors(row),
            [
                "tests/test_x.py::test_fix",
                "tests/test_x.py::test_existing",
            ],
        )

    def test_supplemental_records_require_the_pinned_file_bytes(self):
        import hashlib

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "record.json"
            path.write_text(json.dumps(self.row))
            locked = {
                "supplemental_records": [
                    {
                        "file": "record.json",
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                ]
            }
            with patch.object(bootstrap, "HERE", root):
                self.assertEqual(bootstrap.supplemental_records(locked), ([self.row], [path]))
                path.write_text(json.dumps({**self.row, "patch": "changed"}))
                with self.assertRaises(PermissionError):
                    bootstrap.supplemental_records(locked)

    def test_public_projection_does_not_expose_evaluator_material(self):
        public = bootstrap.public_task(self.row)
        self.assertEqual(set(public), {"task_id", "repository", "base_commit", "problem"})
        self.assertNotIn("SECRET", json.dumps(public))
        evaluation = bootstrap.evaluator_task(self.row)
        self.assertEqual(set(evaluation), {"base_commit", "test_patch"})
        self.assertNotIn("REFERENCE_SECRET", json.dumps(evaluation))

    def test_download_digest_mismatch_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "dataset"
            with patch.object(bootstrap.httpx, "get") as get:
                get.return_value.content = b"wrong"
                with self.assertRaises(PermissionError):
                    bootstrap.download("https://example.com/data", "a" * 64, target)
            self.assertFalse(target.exists())

    def test_recipes_pin_base_all_dependencies_and_artifact_hashes(self):
        dockerfile, build, runtime = bootstrap.dependency_recipe(self.lock, self.lock["tasks"][0])
        self.assertIn("FROM " + self.lock["python_image"], dockerfile)
        self.assertEqual(dockerfile.count("--require-hashes"), 2)
        self.assertEqual(dockerfile.count("--no-index --find-links=/packages"), 2)
        self.assertIn("--no-build-isolation", dockerfile)
        self.assertIn("Werkzeug==2.0.3", runtime)
        self.assertIn("pip==24.3.1", build)
        broken = copy.deepcopy(self.lock)
        broken["package_sha256"].pop("Werkzeug==2.0.3")
        with self.assertRaises((KeyError, ValueError)):
            bootstrap.dependency_recipe(broken, broken["tasks"][0])

    def test_package_selection_checks_hash_architecture_and_python_version(self):
        def wheel(filename, sha):
            return {"filename": filename, "digests": {"sha256": sha}, "packagetype": "bdist_wheel"}

        good = wheel("example-1-cp311-cp311-manylinux2014_aarch64.whl", "a" * 64)
        wrong_arch = wheel("example-1-cp311-cp311-manylinux2014_x86_64.whl", "b" * 64)
        wrong_python = wheel("example-1-cp312-cp312-manylinux2014_aarch64.whl", "c" * 64)
        unknown = wheel("example-1-py3-none-any.whl", "d" * 64)
        hashes = ["a" * 64, "b" * 64, "c" * 64]
        self.assertEqual(
            bootstrap.package_artifact(
                {"urls": [wrong_arch, wrong_python, unknown, good]}, hashes, "arm64"
            ),
            good,
        )
        with self.assertRaises(PermissionError):
            bootstrap.package_artifact(
                {"urls": [wrong_arch, wrong_python, unknown]}, hashes, "arm64"
            )

    def test_bootstrap_inputs_are_checked_before_docker_or_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            assets = Path(directory)
            (assets / "assets.json").write_text(
                json.dumps({"files": {str(assets / "missing"): "a" * 64}})
            )
            with patch.object(bootstrap, "command") as command:
                with self.assertRaises((FileNotFoundError, PermissionError)):
                    bootstrap.build_dependencies(assets)
                command.assert_not_called()

    def test_failed_dependency_build_retains_exact_owned_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            assets = Path(directory)
            source = assets / "source"
            source.write_text("pinned")
            bootstrap.write(
                assets / "assets.json",
                {
                    "files": {str(source): bootstrap.sha(source)},
                },
            )
            with (
                patch.object(bootstrap, "cache_packages", return_value={}),
                patch.object(
                    bootstrap,
                    "command",
                    side_effect=[
                        b"pulled",
                        b'[{"Architecture":"arm64"}]',
                        RuntimeError("build failed"),
                    ],
                ),
            ):
                with self.assertRaises(RuntimeError):
                    bootstrap.build_dependencies(assets)
            receipt = json.loads((assets / "dependency-builds.json").read_text())
            self.assertEqual(len(receipt), 1)
            self.assertTrue(receipt[0]["tag"].startswith("loom-factory-deps:"))
            self.assertEqual(receipt[0]["status"], "started")
            self.assertFalse((assets / "images.json").exists())

    def test_runtime_source_manifest_must_match_container_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "server").mkdir()
            p = source / "server/a.py"
            p.write_text("x=1\n")
            with patch(
                "examples.software_factory.harbor.source.command",
                return_value=b'{"server/a.py":"wrong"}',
            ):
                with self.assertRaises(PermissionError):
                    bootstrap.server_identity(source, "owned-server", "/app")

    def test_scip_tier_requires_real_python_rows_and_exact_watermark(self):
        status = {
            "scip_commit_py": "a" * 40,
            "compiler_tiers": [
                {
                    "producer": "python",
                    "commit": "a" * 40,
                    "tool": "scip-python",
                    "watermark_key": "scip_commit_py",
                },
            ],
        }
        counts = {
            "compiler_definitions": 3,
            "compiler_references": 4,
            "python_definitions": 3,
            "python_references": 4,
        }
        bootstrap.validate_tier(status, counts, "scip", "a" * 40)
        for changed in (dict(counts, python_definitions=0), dict(counts, python_references=0)):
            with self.assertRaises(PermissionError):
                bootstrap.validate_tier(status, changed, "scip", "a" * 40)
        with self.assertRaises(PermissionError):
            bootstrap.validate_tier(dict(status, scip_commit_py="b" * 40), counts, "scip", "a" * 40)
        with self.assertRaises(PermissionError):
            bootstrap.validate_tier(status, counts, "treesitter", "a" * 40)
        bootstrap.validate_tier({}, dict.fromkeys(counts, 0), "treesitter", "a" * 40)

    def test_source_scope_rejects_extra_paths_stale_rows_and_wrong_file_content(self):
        import hashlib

        files = {"src/flask/example.py": "x = 1\n"}
        row = {
            "path": next(iter(files)),
            "indexed_commit": "a" * 40,
            "content_hash": hashlib.sha256(b"x = 1\n").hexdigest(),
        }
        grouped = {"path": row["path"], "commits": ["a" * 40], "rows": 1}
        tables = {
            "code_files": [row],
            "code_chunks": [grouped],
            "code_symbols": [grouped],
            "code_edges": [grouped],
        }
        proof = bootstrap.validate_source_scope(tables, files, "a" * 40)
        self.assertEqual(proof["source_sha256"], digest(files))
        self.assertEqual(proof["paths_sha256"], digest(sorted(files)))
        for table, bad in (
            ("code_edges", dict(grouped, path="tests/test_x.py")),
            ("code_symbols", dict(grouped, commits=["b" * 40])),
            ("code_files", dict(row, content_hash="b" * 64)),
        ):
            changed = dict(tables, **{table: [bad]})
            with self.assertRaises(PermissionError):
                bootstrap.validate_source_scope(changed, files, "a" * 40)

    def test_compiler_tree_contains_only_public_source_and_explicit_configuration(self):
        source = Mock(commit="a" * 40, mirror=Path("public.git"))
        files = {
            "src/flask/example.py": "x = 1\n",
            "tests/test_example.py": "def test_example(): pass\n",
            "docs/design.rst": "Public design\n",
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(bootstrap, "source_files", return_value=files) as exported,
        ):
            target = Path(directory) / "compiler"
            result = bootstrap.stage_compiler_source(source, target)
            exported.assert_called_once_with(
                source.mirror, source.commit, ["src/flask", "tests", "docs"]
            )
            paths = {
                str(p.relative_to(target / "source"))
                for p in (target / "source").rglob("*")
                if p.is_file()
            }
            self.assertEqual(paths, {*files, "pyrightconfig.json"})
            self.assertEqual(
                json.loads((target / "source/pyrightconfig.json").read_text())["include"],
                ["src/flask", "tests", "docs"],
            )
            self.assertEqual(result["source_sha256"], digest(files))
            self.assertEqual(json.loads((target / "environment.json").read_text()), [])


if __name__ == "__main__":
    unittest.main()
