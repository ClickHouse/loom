"""Compiler preparation uses pinned, source-only containers and authenticated artifact bytes."""

import ast
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.software_factory.evals.flask import compiler_setup as setup


class CompilerSetup(unittest.TestCase):
    def test_public_tool_pins_match_suite_container_and_npm_locks(self):
        for architecture in ("amd64", "arm64"):
            pins = setup.tool_pins(architecture)
            self.assertEqual(pins["node"], "22.23.4")
            self.assertEqual(pins["scip_python"], "0.6.6")
            self.assertEqual(len(pins["node_sha256"]), 64)
            self.assertIn("https://nodejs.org/dist/v22.23.4/", pins["node_url"])
            self.assertTrue(setup.CONVERTER.is_file())

    def test_nine_revisions_share_only_the_authored_dependency_environment(self):
        lock = setup._read(setup.LOCK)
        with tempfile.TemporaryDirectory() as directory:
            assets = Path(directory)
            (assets / "assets.json").write_text(json.dumps(dict(source_git="/public/flask.git")))
            images = {
                row["task_id"]: dict(image="sha256:" + str(i) * 64)
                for i, row in enumerate(lock["tasks"], 1)
            }
            (assets / "images.json").write_text(json.dumps(dict(images=images)))
            fixtures, tasks = {}, {}
            for i in range(3):
                key = "authored-" + str(i)
                fixtures[key] = dict(
                    source_git="/owned/" + key,
                    revisions={
                        label: dict(commit=f"{i * 2 + j + 1:040x}")
                        for j, label in enumerate(("A", "B"))
                    },
                )
                tasks[key] = dict(image=images["pallets__flask-4935"]["image"])
            rows = setup.compiler_revisions(assets, dict(fixtures=fixtures, tasks=tasks))
            self.assertEqual(len(rows), 9)
            self.assertEqual(len({row["base_commit"] for row in rows}), 9)
            self.assertEqual(sum(row["revision"] == "A" for row in rows), 3)
            self.assertEqual(
                {row["dependency_task_id"] for row in rows[3:]}, {"pallets__flask-4935"}
            )
            tasks["authored-0"]["image"] = "wrong-image"
            with self.assertRaises(PermissionError):
                setup.compiler_revisions(assets, dict(fixtures=fixtures, tasks=tasks))

    def test_derived_images_use_the_exact_test_environment_and_no_online_build_step(self):
        first = setup.compiler_dockerfile("qualified-dependency:first")
        later = setup.compiler_dockerfile("qualified-dependency:second", "owned-tools")
        self.assertTrue(first.startswith("FROM qualified-dependency:first"))
        self.assertTrue(later.startswith("FROM qualified-dependency:second"))
        self.assertIn("COPY --from=owned-tools /opt/compiler-tools", later)
        self.assertNotIn("pip install", first + later)
        self.assertNotIn("npm install", first + later)
        self.assertNotIn("curl", first + later)
        self.assertIn("RUN python /install.py", first)
        with self.assertRaisesRegex(ValueError, "tag"):
            setup.compiler_dockerfile("sha256:" + "a" * 64)
        for source in (setup._INSTALL, setup._RUNNER, setup._VALIDATE):
            ast.parse(source)

    def test_container_has_readonly_source_empty_owned_cache_and_no_network_or_credentials(self):
        command = setup.compiler_container_command(
            "owned-image", Path("/owned/project"), "owned-job", owner="a" * 32
        )
        for value in (
            "--network=none",
            "--read-only",
            "--cpus=2",
            "--memory=2g",
            "--pids-limit=128",
        ):
            self.assertIn(value, command)
        self.assertIn("type=bind,src=/owned/project/source,dst=/job/source,readonly", command)
        self.assertIn("type=bind,src=/owned/project/worker-cache,dst=/cache", command)
        self.assertNotIn("docker.sock", " ".join(command))
        self.assertNotIn("API_KEY", " ".join(command))
        self.assertIn("assert not any(cache.iterdir())", setup._RUNNER)
        self.assertIn("actual==spec['packages']", setup._RUNNER)

    def test_venv_interpreter_symlink_is_preserved_for_locked_site_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "python"
            executable.symlink_to(sys.executable)
            self.assertEqual(setup._normalizer_python(str(executable)), str(executable))
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            setup._normalizer_python(None)

    def test_verified_download_reuses_cache_and_refuses_changed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, target = root / "cache", root / "target"
            candidate.write_bytes(b"published bytes")
            integrity = (
                "sha512-"
                + base64.b64encode(hashlib.sha512(candidate.read_bytes()).digest()).decode()
            )
            with patch.object(
                setup.urllib.request, "urlopen", side_effect=AssertionError("offline")
            ):
                setup._download("https://registry.npmjs.org/pinned", integrity, target, candidate)
                self.assertEqual(target.read_bytes(), candidate.read_bytes())
                target.write_bytes(b"changed")
                with self.assertRaises(PermissionError):
                    setup._download(
                        "https://registry.npmjs.org/pinned", integrity, target, candidate
                    )

    def test_normalization_is_dry_run_and_schema_validation_uses_separate_locked_python(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            with patch.object(
                setup, "_run", side_effect=[b"normalized", b'{"schema_validated":true}']
            ) as run:
                result = setup._normalize(
                    project, dict(base_commit="a" * 40), dict(scip_python="0.6.6"), "/locked/python"
                )
            self.assertTrue(result["schema_validated"])
            first, second = run.call_args_list
            self.assertEqual(first.args[0][0], "/locked/python")
            self.assertIn("--dry-run", first.args[0])
            self.assertIn("--full", first.args[0])
            self.assertNotIn("--token", first.args[0])
            self.assertNotIn("--loom-url", first.args[0])
            self.assertIn("ScipDelta.model_validate", second.args[0][-1])
            self.assertNotIn("ANTHROPIC_API_KEY", first.kwargs["env"])

    def test_cached_raw_requires_matching_source_recipe_image_and_file_hashes(self):
        pins = setup.tool_pins("arm64")
        revision = dict(
            base_commit="a" * 40,
            dependency_image="sha256:" + "b" * 64,
            dependency_task_id="pallets__flask-4935",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original"
            original.mkdir()
            project = root / "project"
            project.mkdir()
            (project / "source").mkdir()
            (project / "source/example.py").write_text("x=1\n")
            source_hashes = {"example.py": setup._sha(project / "source/example.py")}
            contents = {
                "index.scip": "raw bytes",
                "compiler-environment.json": json.dumps(
                    dict(
                        node="v22.23.4",
                        scip_python="0.6.6",
                        python="3.11.18",
                        network="none",
                        cache_initially_empty=True,
                        source_before=source_hashes,
                        source_after=source_hashes,
                        packages=setup._expected_packages("pallets__flask-4935"),
                    )
                ),
                "compiler-command.json": "[]",
                "source.json": json.dumps(dict(base_commit="a" * 40, source_sha256="c" * 64)),
            }
            records = {}
            for name, value in contents.items():
                path = original / name
                path.write_text(value)
                records[name] = dict(path=str(path), sha256=setup._sha(path))
            entry = {**pins, **revision, "source_sha256": "c" * 64, "files": records}
            self.assertTrue(
                setup._cached_raw(
                    dict(revisions={"compiled": entry}), revision, "c" * 64, pins, project
                )
            )
            self.assertFalse(
                setup._cached_raw(
                    dict(revisions={"compiled": entry}), revision, "d" * 64, pins, project
                )
            )
            (original / "index.scip").write_bytes(b"changed")
            with self.assertRaises(PermissionError):
                setup._cached_raw(
                    dict(revisions={"compiled": entry}), revision, "c" * 64, pins, project
                )

    def test_image_ownership_is_journaled_before_a_failed_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archives = root / "archives"
            archives.mkdir()
            receipt = dict(owned_images=[], owner="a" * 32)
            with patch.object(
                setup,
                "_run",
                side_effect=[("sha256:" + "a" * 64).encode(), RuntimeError("build failed")],
            ):
                with self.assertRaisesRegex(RuntimeError, "build failed"):
                    setup._build_images(
                        root, archives, {"sha256:" + "a" * 64: "qualified:base"}, receipt
                    )
            saved = setup._read(root / "compiler-artifacts.json")
            self.assertEqual(saved["owned_images"][0]["status"], "building")
            self.assertTrue(saved["owned_images"][0]["tag"].startswith("loom-factory-compiler:"))
            self.assertIsNone(saved["owned_images"][0]["id"])

    def test_complete_matching_cache_does_not_build_or_start_a_compiler_container(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / "assets"
            assets.mkdir()
            task_ids = ["dependency"]
            (assets / "images.json").write_text(
                json.dumps(
                    dict(images={task_id: dict(architecture="arm64") for task_id in task_ids})
                )
            )
            revisions = [
                dict(
                    key=f"task-{i}:B",
                    task_id=f"task-{i}",
                    revision="B",
                    base_commit=f"{i + 1:040x}",
                    source_git="/public.git",
                    dependency_task_id="dependency",
                    dependency_image="immutable",
                )
                for i in range(9)
            ]

            def stage(source, project):
                project.mkdir()
                (project / "source").mkdir()
                (project / "source/value.py").write_text("x=1\n")
                for name in (
                    "index.scip",
                    "delta.json",
                    "source.json",
                    "allowed-paths.txt",
                    "environment.json",
                    "compiler-environment.json",
                    "compiler-command.json",
                    "validation.json",
                ):
                    (project / name).write_text("{}")
                return dict(source_sha256="a" * 64, source_root=str(project / "source"))

            with (
                patch.object(setup, "compiler_revisions", return_value=revisions),
                patch.object(setup, "FrozenSource", return_value=SimpleNamespace()),
                patch(
                    "examples.software_factory.evals.flask.bootstrap.stage_compiler_source",
                    side_effect=stage,
                ),
                patch.object(setup, "_cached_raw", return_value=True),
                patch.object(
                    setup,
                    "_normalize",
                    return_value=dict(schema_validated=True, symbols=2, references=1),
                ),
                patch.object(setup, "_build_images", side_effect=AssertionError("No images")),
                patch.object(setup, "_run", side_effect=AssertionError("No Docker")),
            ):
                result = setup.prepare_compiler_artifacts(
                    assets, {}, root / "output", loom_python=sys.executable
                )
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["language"], "python")
            self.assertEqual(result["producer"], "scip-python")
            self.assertEqual(
                result["producer_count_fields"],
                {
                    "definitions": "python_definitions",
                    "references": "python_references",
                },
            )
            self.assertEqual(len(result["revisions"]), 9)
            self.assertTrue(all(row["reused_raw"] for row in result["revisions"].values()))
            self.assertEqual(result["containers"], [])
            self.assertEqual(result["owned_images"], [])

    def test_pinned_npm_archives_need_not_use_package_as_their_root(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "types.tgz"
            with tarfile.open(path, "w:gz") as archive:
                value = json.dumps(dict(version="1.2.3")).encode()
                member = tarfile.TarInfo("node/package.json")
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            self.assertEqual(setup._package_root(path, "1.2.3"), "node")
            with self.assertRaises(PermissionError):
                setup._package_root(path, "wrong-version")

    def test_offline_image_installer_extracts_verified_nonstandard_package_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            inputs.mkdir()

            def archive(path, entries, mode):
                with tarfile.open(path, mode) as tar:
                    for name, value in entries.items():
                        member = tarfile.TarInfo(name)
                        member.size = len(value)
                        tar.addfile(member, io.BytesIO(value))

            archive(inputs / "node.tar.xz", {"node-test/bin/node": b"not executed"}, "w:xz")
            archive(inputs / "types.tgz", {"node/package.json": b'{"version":"1"}'}, "w:gz")
            (inputs / "packages.json").write_text(
                json.dumps(
                    [
                        dict(
                            path="node_modules/@types/node",
                            archive="types.tgz",
                            archive_root="node",
                        )
                    ]
                )
            )
            script = setup._INSTALL.replace("/opt/compiler-tools", str(root / "tools"))
            script = script.replace("/inputs", str(inputs))
            subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)
            self.assertEqual((root / "tools/node/bin/node").read_bytes(), b"not executed")
            self.assertTrue(
                (root / "tools/toolchains/node_modules/@types/node/package.json").is_file()
            )
            self.assertIn('"$@"', (root / "tools/toolchains/bin/scip-python").read_text())
