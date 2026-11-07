"""`prompts.evaluate` reads a prompt version's traces and scores on Langfuse 3 and 4."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.erasure.app.backends import LangfuseBackend
from server.insights.app import admin_routes


class _Resp:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class _Client:
    def __init__(self, routes: dict[str, dict[str, Any]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, headers: Any = None, params: Any = None) -> _Resp:
        path = url.removeprefix("http://lf")
        self.calls.append((path, params or {}))
        return _Resp(self.routes[path])


_BACKEND = LangfuseBackend("http://lf", "pk-lf-x:sk-lf-x", 5)


@pytest.mark.asyncio
async def test_langfuse_4_finds_traces_by_the_tag_on_observations() -> None:
    c = _Client(
        {
            "/api/public/v2/observations": {
                "data": [
                    {"traceId": "t1"},
                    {"traceId": "t1"},
                    {"traceId": "t2"},
                    {"id": "no-trace"},
                ]
            }
        }
    )
    ids = await admin_routes._traces_tagged(c, _BACKEND, "http://lf", "prompt:p:3", 4)
    assert ids == ["t1", "t2"]
    ((path, params),) = c.calls
    assert json.loads(params["filter"]) == [
        {"type": "arrayOptions", "column": "tags", "operator": "all of", "value": ["prompt:p:3"]}
    ]


@pytest.mark.asyncio
async def test_langfuse_3_lists_traces_by_tag() -> None:
    c = _Client({"/api/public/traces": {"data": [{"id": "t1"}, {"id": "t2"}]}})
    ids = await admin_routes._traces_tagged(c, _BACKEND, "http://lf", "prompt:p:3", 3)
    assert ids == ["t1", "t2"]
    assert c.calls[0][1]["tags"] == "prompt:p:3"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("major", "path"), [(3, "/api/public/scores"), (4, "/api/public/v3/scores")]
)
async def test_scores_are_read_where_each_version_serves_them(major: int, path: str) -> None:
    c = _Client({path: {"data": [{"value": 0.75}, {"value": "x"}, {"value": None}]}})
    got = await admin_routes._rubric_scores(
        c, _BACKEND, "http://lf", ["t1"], "retrieval_relevance", major
    )
    assert got == [0.75]
    assert c.calls == [(path, {"traceId": "t1", "name": "retrieval_relevance", "limit": 10})]
