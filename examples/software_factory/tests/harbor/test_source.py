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
