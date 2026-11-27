"""Trusted omitted fixtures stay distinct from editable implementation source."""

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.software_factory.evals.flask.historical import (
    FrozenSource,
    HistoricalGrader,
    fixture_path,
    git,
)
from examples.software_factory.factory.workspace import Checkout


class FixtureExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        git(self.root, "init", "--initial-branch=fixture-test")
        git(self.root, "config", "user.name", "Fixture test")
        git(self.root, "config", "user.email", "fixture@example.invalid")

    def commit(self):
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "Fixture base")
        return git(self.root, "rev-parse", "HEAD").decode().strip()

    def put(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def test_dotfiles_and_binary_are_exact_base_bytes(self):
        self.put("src/flask/example.py", b"x = 1\n")
        self.put("tests/test_apps/.env", b"PUBLIC_FIXTURE=1\n")
        self.put("tests/data/icon.bin", b"\x00\xff\x80\r\n")
        self.put("tests/test_example.py", b"def test_example(): pass\n")
        source = FrozenSource(self.root, self.commit())
        self.put("tests/test_apps/.env", b"FUTURE_CHANGE=2\n")
        fixtures = source.fixtures()
        self.assertEqual(
            {name: base64.b64decode(value, validate=True) for name, value in fixtures.items()},
            {
                "tests/test_apps/.env": b"PUBLIC_FIXTURE=1\n",
                "tests/data/icon.bin": b"\x00\xff\x80\r\n",
            },
        )
        self.assertEqual(source.files("src/flask"), {"src/flask/example.py": "x = 1\n"})
        self.assertNotIn("tests/test_example.py", fixtures)

    def test_symlinks_are_refused(self):
        self.put("src/flask/example.py", b"x = 1\n")
        (self.root / ".hidden-link").symlink_to("/etc/passwd")
        source = FrozenSource(self.root, self.commit())
        with self.assertRaises(PermissionError):
            source.fixtures()

    def test_standalone_grader_restores_frozen_fixtures(self):
        self.put("src/flask/example.py", b"x = 1\n")
        fixture = "tests/test_apps/.flaskenv"
        self.put(fixture, b"PUBLIC_FIXTURE=1\n")
        source = FrozenSource(self.root, self.commit())
        self.put(fixture, b"FUTURE_CHANGE=2\n")
        task = source.task(
            {
                "task_id": "fixture-check",
                "repository": "pallets/flask",
                "base_commit": source.commit,
                "problem": "Read the tracked fixture.",
            },
            "fixture-test",
            "example",
        )
        checkout = Checkout(self.root / "candidate", task)
        grader = HistoricalGrader(source, "unused", [], [], "", self.root / "grade")
        with patch("examples.software_factory.evals.flask.historical.DockerTests") as runner:
            runner.return_value.run.return_value = {"passed": True}
            grader.run(checkout)
        workspace = runner.return_value.run.call_args.args[0]
        self.assertEqual((workspace / fixture).read_bytes(), b"PUBLIC_FIXTURE=1\n")

    def test_task_evidence_is_public_text_at_the_exact_base(self):
        self.put("src/flask/example.py", b"x = 1\n")
        self.put("docs/contract.rst", b"Base contract.\n")
        self.put("tests/test_example.py", b"def test_base(): pass\n")
        self.put("tests/data/binary.bin", b"\xff\x80")
        self.put(".gitignore", b"*.pyc\n")
        source = FrozenSource(self.root, self.commit())
        self.put("docs/contract.rst", b"Future hidden requirements.\n")
        self.put("tests/test_example.py", b"def test_future(): pass\n")
        task = source.task(
            {
                "task_id": "issue",
                "repository": "pallets/flask",
                "base_commit": source.commit,
                "problem": "Fix issue",
            },
            "code",
            "example",
        )
        self.assertEqual(task.files, {"src/flask/example.py": "x = 1\n"})
        self.assertEqual(
            task.read_only_files,
            {
                "docs/contract.rst": "Base contract.\n",
                "tests/test_example.py": "def test_base(): pass\n",
            },
        )
        self.assertEqual(task.read_only_roots, ["docs", "tests"])

    def test_fixture_paths_allow_dotfiles_without_path_escape_or_git_metadata(self):
        for name in ("tests/test_apps/.env", "tests/data/blob.bin", ".flaskenv"):
            self.assertTrue(fixture_path(name), name)
        for name in (
            "",
            ".",
            "..",
            "../x",
            "/x",
            "a/../x",
            "a//x",
            "a/./x",
            "a/",
            "a\\x",
            ".git/config",
            "a/.git/x",
            "x\x00",
        ):
            self.assertFalse(fixture_path(name), name)

    def test_traversal_is_refused_before_blob_read(self):
        self.put("src/flask/example.py", b"x = 1\n")
        source = FrozenSource(self.root, self.commit())
        with patch(
            "examples.software_factory.evals.flask.historical.git",
            return_value=(b"100644 blob " + b"a" * 40 + b" 3\t../escape\0"),
        ) as invoke:
            with self.assertRaises(PermissionError):
                source.fixtures()
            self.assertEqual(invoke.call_count, 1)

    def test_single_blob_budget_applies_to_dotfiles(self):
        self.put(".oversized", b"x" * (1024 * 1024 + 1))
        source = FrozenSource(self.root, self.commit())
        with self.assertRaises(ValueError):
            source.fixtures()

    def test_total_budget_includes_ordinary_and_omitted_files(self):
        self.put("src/flask/example.py", b"x = 1\n")
        source = FrozenSource(self.root, self.commit())
        records = b"".join(
            b"100644 blob " + b"a" * 40 + b" 1048576\tfile" + str(i).encode() + b"\0"
            for i in range(21)
        )
        with patch(
            "examples.software_factory.evals.flask.historical.git", return_value=records
        ) as invoke:
            with self.assertRaises(ValueError):
                source.fixtures()
            self.assertEqual(invoke.call_count, 1)


if __name__ == "__main__":
    unittest.main()
