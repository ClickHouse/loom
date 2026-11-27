"""Source identities include common dependencies and exclude environment payloads."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.software_factory.harbor import source


class SourceIdentity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in (
            "examples/software_factory/shared.py",
            "sdk/python/client.py",
            "bench/bootstrap.py",
            "pyproject.toml",
            "uv.lock",
            "server/app.py",
            "schemas/001.sql",
        ):
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("initial\n")
        (self.root / "examples/software_factory/host.env").write_text("PRIVATE_ENV_SENTINEL")
        source._git(self.root, "init", "--initial-branch=source-fixture")
        source._git(self.root, "add", ".")
        source._git(
            self.root,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "Fixture",
        )

    def test_controller_census_includes_shared_bootstrap_and_actual_git_dirty_state(self):
        first = source.controller_identity(self.root)
        self.assertFalse(first["dirty"])
        self.assertIn("bench/bootstrap.py", first["relative_files"])
        self.assertNotIn("examples/software_factory/host.env", first["relative_files"])
        self.assertNotIn("PRIVATE_ENV_SENTINEL", json.dumps(first))
        (self.root / "bench/bootstrap.py").write_text("changed\n")
        second = source.controller_identity(self.root)
        self.assertTrue(second["dirty"])
        self.assertEqual(first["commit"], second["commit"])
        self.assertNotEqual(
            first["relative_files"]["bench/bootstrap.py"],
            second["relative_files"]["bench/bootstrap.py"],
        )

    def test_server_bytes_are_verified_without_sending_environment_payloads(self):
        expected = {
            name: source.sha(self.root / name) for name in ("server/app.py", "schemas/001.sql")
        }
        with patch.object(source, "command", return_value=json.dumps(expected).encode()) as run:
            result = source.server_identity(self.root, "owned", "/runtime")
        self.assertTrue(result["container_bytes_verified"])
        self.assertFalse(result["dirty"])
        payload = json.loads(run.call_args.kwargs["data"])
        self.assertEqual(set(payload["paths"]), set(expected))
        self.assertNotIn("PRIVATE_ENV_SENTINEL", run.call_args.kwargs["data"].decode())
        with (
            patch.object(source, "command", return_value=b"{}"),
            self.assertRaises(PermissionError),
        ):
            source.server_identity(self.root, "owned", "/runtime")

    def test_index_roots_include_original_tests_and_docs_but_skip_binary_fixtures(self):
        from examples.software_factory.harbor.source_tree import source_files

        files = {
            "tests/test_original.py": b"def test_original(): pass\n",
            "docs/design.rst": b"Public design\n",
            "tests/icon.bin": b"\0data",
            "tests/image.bin": b"\xffdata",
            "tests/.env": b"PRIVATE_ENV_SENTINEL",
        }
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        source._git(self.root, "add", ".")
        source._git(
            self.root,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "Public index roots",
        )
        commit = source._git(self.root, "rev-parse", "HEAD").decode().strip()
        actual = source_files(self.root, commit, ["server", "tests", "docs"])
        self.assertEqual(
            actual,
            {
                "server/app.py": "initial\n",
                "tests/test_original.py": "def test_original(): pass\n",
                "docs/design.rst": "Public design\n",
            },
        )

    def test_native_repository_exclusions_match_hidden_source_paths(self):
        from examples.software_factory.harbor.source_tree import SOURCE_EXCLUDE_GLOBS, ordinary
        from server.code.app.index.denylist import Denylist

        rules = Denylist(patterns=SOURCE_EXCLUDE_GLOBS, builtin=False)
        for path in (
            ".env",
            "tests/test_apps/.env",
            "tests/test_apps/.flaskenv",
            "docs/.private/page.rst",
        ):
            with self.subTest(path=path):
                self.assertFalse(ordinary(path))
                self.assertTrue(rules.reason(path))
        for path in ("src/flask/app.py", "tests/test_cli.py", "docs/_static/style.css"):
            with self.subTest(path=path):
                self.assertTrue(ordinary(path))
                self.assertFalse(rules.reason(path))
