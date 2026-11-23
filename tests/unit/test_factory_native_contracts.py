"""Native setup contracts run with the server dependencies, alongside serving model checks."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from examples.software_factory.factory.model import digest
from examples.software_factory.harbor.anchors import capture
from examples.software_factory.harbor.index_setup import retire_deleted_paths
from server.code.app.index.ingest_stats import FileIngestStats


class NativeSourceTransition(unittest.IsolatedAsyncioTestCase):
    async def test_only_paths_removed_from_the_verified_source_are_retired(self):
        old = {"src/kept.py": "old", "docs/removed.rst": "retired"}
        current = {"src/kept.py": "changed", "docs/added.rst": "added"}
        request = {
            "org": "org",
            "namespace": "code",
            "source_git": "mirror.git",
            "source_roots": ["src", "docs"],
            "from_commit": "a" * 40,
            "commit": "b" * 40,
            "from_source_sha256": digest(old),
            "source_sha256": digest(current),
        }
        stats = FileIngestStats(path="docs/removed.rst", chunks_tombstoned=2)
        with (
            patch(
                "examples.software_factory.harbor.index_setup.source_files",
                side_effect=[old, current],
            ),
            patch("server.code.store.index.CodeRepo") as repo,
            patch(
                "server.embedding.embedder.get_embedder",
                return_value=SimpleNamespace(model_name="model"),
            ),
            patch(
                "server.code.app.index.ingest.tombstone_file", new=AsyncMock(return_value=stats)
            ) as retire,
        ):
            result = await retire_deleted_paths(request)
        retire.assert_awaited_once_with(
            repo.return_value, "org", "code", path="docs/removed.rst", embedding_model="model"
        )
        self.assertEqual(result["removed_paths"], ["docs/removed.rst"])
        self.assertEqual(result["retired"]["docs/removed.rst"]["chunks_tombstoned"], 2)

    async def test_source_mismatch_prevents_retirement(self):
        request = {
            "source_git": "mirror.git",
            "source_roots": ["src"],
            "from_commit": "a" * 40,
            "commit": "b" * 40,
            "from_source_sha256": "wrong",
            "source_sha256": "wrong",
        }
        with (
            patch(
                "examples.software_factory.harbor.index_setup.source_files",
                return_value={"src/a.py": "body"},
            ),
            patch("server.code.store.index.CodeRepo") as repo,
            patch("server.code.app.index.ingest.tombstone_file", new_callable=AsyncMock) as retire,
            self.assertRaisesRegex(PermissionError, "pinned history"),
        ):
            await retire_deleted_paths(request)
        repo.assert_not_called()
        retire.assert_not_awaited()


class NativeAnchorCapture(unittest.IsolatedAsyncioTestCase):
    async def test_registry_is_loaded_before_any_source_is_parsed(self):
        calls = []

        async def refresh():
            calls.append("registry")

        async def parse(*args):
            self.assertEqual(calls[0], "registry")
            calls.append(args[-1])
            return SimpleNamespace(lines=["source"], defs=[])

        request = {
            "org": "org",
            "namespace": "code",
            "commit": "a" * 40,
            "links": [{"path": path} for path in ("src/a.py", "src/b.py")],
        }
        with (
            patch("server.code.app.memory_links.refresh_repos", side_effect=refresh) as registry,
            patch("server.code.app.memory_links.parsed", side_effect=parse),
        ):
            result = await capture(request)
        registry.assert_awaited_once_with()
        self.assertEqual(calls, ["registry", "src/a.py", "src/b.py"])
        self.assertEqual(len(result["anchors"]), 2)

    async def test_registry_failure_prevents_source_reads(self):
        with (
            patch(
                "server.code.app.memory_links.refresh_repos",
                new=AsyncMock(side_effect=RuntimeError("registry unavailable")),
            ),
            patch("server.code.app.memory_links.parsed", new_callable=AsyncMock) as parse,
            self.assertRaisesRegex(RuntimeError, "registry unavailable"),
        ):
            await capture({"links": [{"path": "src/a.py"}]})
        parse.assert_not_awaited()
