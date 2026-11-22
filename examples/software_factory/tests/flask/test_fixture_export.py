"""Trusted omitted fixtures stay distinct from editable implementation source."""

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.software_factory.evals.flask.historical import FrozenSource, fixture_path, git


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
