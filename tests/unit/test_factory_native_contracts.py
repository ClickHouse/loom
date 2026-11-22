"""Native setup contracts run with the server dependencies, alongside serving model checks."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from examples.software_factory.harbor.anchors import capture


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
