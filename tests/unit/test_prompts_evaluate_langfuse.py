"""`prompts.evaluate` reads a prompt version's traces and scores on Langfuse 3 and 4."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from server.erasure.app.backends import LangfuseBackend
from server.insights.app import admin_routes
from server.insights.app.admin_models import PromptsEvaluateInput

_URL = "http://lf"


class _Resp:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            req = httpx.Request("GET", _URL)
            raise httpx.HTTPStatusError(
                "error", request=req, response=httpx.Response(self.status_code)
            )


class _Langfuse:
    """Answers each path with a fixed response, and records the calls."""

    def __init__(self, routes: dict[str, _Resp]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, headers: Any = None, params: Any = None) -> _Resp:
        path = url.removeprefix(_URL)
        self.calls.append((path, params or {}))
        return self.routes[path]

    async def __aenter__(self) -> _Langfuse:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None


def _health(version: str) -> _Resp:
    return _Resp(200, {"status": "OK", "version": version})


def _scores(*values: Any) -> _Resp:
    return _Resp(200, {"data": [{"value": v} for v in values]})


@pytest.fixture
def langfuse(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(admin_routes.settings, "langfuse_url", _URL)
    monkeypatch.setattr(admin_routes.settings, "langfuse_admin_token", "pk-lf-x:sk-lf-x")

    def install(routes: dict[str, _Resp]) -> _Langfuse:
        fake = _Langfuse(routes)
        monkeypatch.setattr(admin_routes.httpx, "AsyncClient", lambda **kw: fake)
        return fake

    return install


def _input() -> PromptsEvaluateInput:
    return PromptsEvaluateInput(
        prompt_name="p", prompt_version=3, rubric="relevance", min_evals=3, threshold=0.8
    )


@pytest.mark.asyncio
async def test_langfuse_4_finds_the_tagged_traces_on_observations_and_scores_on_v3(
    langfuse: Any,
) -> None:
    fake = langfuse(
        {
            "/api/public/health": _health("4.76.0"),
            "/api/public/v2/observations": _Resp(
                200, {"data": [{"traceId": "t1"}, {"traceId": "t1"}, {"traceId": "t2"}]}
            ),
            "/api/public/v3/scores": _scores(0.8, 0.9, "not a number"),
        }
    )
    out = await admin_routes.admin_prompts_evaluate(_input())
    assert (out.should_promote, out.eval_count) == (True, 4)
    flt = json.loads(dict(fake.calls)["/api/public/v2/observations"]["filter"])
    assert flt == [
        {"type": "arrayOptions", "column": "tags", "operator": "all of", "value": ["prompt:p:3"]}
    ]
    assert [p for p, _ in fake.calls].count("/api/public/v3/scores") == 2


@pytest.mark.asyncio
async def test_langfuse_3_lists_traces_by_tag_and_reads_the_v1_scores(langfuse: Any) -> None:
    fake = langfuse(
        {
            "/api/public/health": _health("3.227.0"),
            "/api/public/traces": _Resp(
                200, {"data": [{"id": "t1"}, {"id": "t2"}], "meta": {"totalPages": 1}}
            ),
            "/api/public/scores": _scores(0.9),
        }
    )
    out = await admin_routes.admin_prompts_evaluate(_input())
    assert (out.should_promote, out.eval_count) == (False, 2)
    assert dict(fake.calls)["/api/public/traces"]["tags"] == ["prompt:p:3"]
    assert dict(fake.calls)["/api/public/scores"] == {
        "traceId": "t2",
        "name": "relevance",
        "limit": 10,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "routes",
    [
        {"/api/public/health": _health("4.76.0"), "/api/public/v2/observations": _Resp(404, {})},
        {
            "/api/public/health": _health("4.76.0"),
            "/api/public/v2/observations": _Resp(200, {"data": [{"traceId": "t1"}]}),
            "/api/public/v3/scores": _Resp(401, {}),
        },
        {"/api/public/health": _Resp(200, {"status": "OK"})},
    ],
    ids=["trace lookup fails", "score read fails", "no server version"],
)
async def test_a_failed_langfuse_read_answers_502_not_a_verdict(
    langfuse: Any, routes: dict[str, _Resp]
) -> None:
    langfuse(routes)
    with pytest.raises(HTTPException) as ei:
        await admin_routes.admin_prompts_evaluate(_input())
    assert ei.value.status_code == 502
    assert ei.value.detail["code"] == "BACKEND_UNAVAILABLE"
    cause = ei.value.detail["message"].removeprefix("Langfuse read failed: ").split(":")[0]
    assert cause in {"HTTPStatusError", "ErasureBackendError"}


@pytest.mark.asyncio
async def test_a_langfuse_that_keeps_timing_out_answers_502_naming_the_timeout(
    langfuse: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout's own text is empty, so the 502 names its type."""
    fake = langfuse({})

    async def _timeout(url: str, headers: Any = None, params: Any = None) -> Any:
        raise httpx.ReadTimeout("")

    monkeypatch.setattr(fake, "get", _timeout)
    monkeypatch.setattr(LangfuseBackend, "_PAGE_BACKOFF_S", 0.0)
    with pytest.raises(HTTPException) as ei:
        await admin_routes.admin_prompts_evaluate(_input())
    assert ei.value.status_code == 502
    assert ei.value.detail["message"] == "Langfuse read failed: ReadTimeout: "


@pytest.mark.asyncio
async def test_a_lookup_for_the_first_traces_stops_paging() -> None:
    backend = LangfuseBackend(_URL, "pk-lf-x:sk-lf-x", 5)
    backend._major = 4
    page = _Resp(200, {"data": [{"traceId": f"t{i}"} for i in range(5)], "meta": {"cursor": "c"}})
    fake = _Langfuse({"/api/public/v2/observations": page})
    ids = await backend.find_trace_ids(fake, tags=["prompt:p:3"], first=3)  # type: ignore[arg-type]
    assert ids == ["t0", "t1", "t2"] and len(fake.calls) == 1


@pytest.mark.asyncio
async def test_a_langfuse_3_lookup_for_the_first_traces_stops_paging() -> None:
    backend = LangfuseBackend(_URL, "pk-lf-x:sk-lf-x", 5)
    backend._major = 3
    page = _Resp(200, {"data": [{"id": f"t{i}"} for i in range(5)], "meta": {"totalPages": 4}})
    fake = _Langfuse({"/api/public/traces": page})
    ids = await backend.find_trace_ids(fake, tags=["prompt:p:3"], first=3)  # type: ignore[arg-type]
    assert ids == ["t0", "t1", "t2"] and len(fake.calls) == 1
