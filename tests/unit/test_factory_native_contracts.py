"""Native setup contracts run with the server dependencies, alongside serving model checks."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from examples.software_factory.harbor.anchors import capture
from examples.software_factory.harbor.contracts import requests
from examples.software_factory.harbor.native_setup import validate_requests


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


class RequestContracts(unittest.TestCase):
    def test_grep_tool_pattern_bounds_match_the_serving_model(self):
        from examples.software_factory.factory.retrieval_tools import schemas
        from server.code.api.models_grep import CodeGrepInput

        tool = next(item for item in schemas() if item["name"] == "loom_code_grep")
        projected = tool["input_schema"]["properties"]["pattern"]
        native = CodeGrepInput.model_json_schema()["properties"]["pattern"]
        for field in ("minLength", "maxLength"):
            self.assertEqual(projected[field], native[field])

    def test_code_and_memory_shapes_are_accepted(self):
        self.assertEqual(validate_requests(requests())["validated"], 17)

    def test_enclosing_qualifies_cold_and_memory_namespace_lesson_shapes(self):
        rows = [row for row in requests() if row["operation"] == "code.enclosing"]
        self.assertEqual([row["body"]["include_lessons"] for row in rows], [False, True])
        self.assertEqual(
            [row["body"].get("lesson_namespaces", []) for row in rows], [[], ["memory"]]
        )
        self.assertEqual(validate_requests(rows)["validated"], 2)

    def test_unsupported_fields_still_refuse_before_a_dispatch(self):
        row = next(row for row in requests() if row["operation"] == "code.enclosing")
        row["body"]["undeclared_qualification_field"] = True
        with self.assertRaisesRegex(ValueError, "undeclared arguments"):
            validate_requests([row])
