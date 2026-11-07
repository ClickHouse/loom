"""The Langfuse erasure lookup picks the trace list (Langfuse 3) or the
observations list (Langfuse 4) by the server's version, asks for core fields
only, retries a page on timeout / 429 / 5xx with backoff, and names the
exception type in the saga's failure message (str(ReadTimeout) is empty)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from server.erasure.app import backends as eb


class _Resp:
    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        self.status_code = status
        self._body = body or {}
        self.text = "err"

    def json(self) -> dict[str, Any]:
        return self._body


class _NotJson(_Resp):
    """A 200 whose body is not JSON (a proxy's HTML page)."""

    def __init__(self) -> None:
        super().__init__(200)

    def json(self) -> dict[str, Any]:
        raise ValueError("not json")


class _JsonList(_Resp):
    """A 200 whose JSON body is not an object."""

    def __init__(self) -> None:
        super().__init__(200)

    def json(self) -> Any:
        return ["OK"]


class _Client:
    """Scripted GET: each entry is a _Resp or an exception to raise."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def get(self, url: str, headers: Any = None, params: Any = None) -> Any:
        self.calls.append({"url": url, **(params or {})})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> eb.LangfuseBackend:
    monkeypatch.setattr(eb.LangfuseBackend, "_PAGE_BACKOFF_S", 0.0)
    return eb.LangfuseBackend("https://lf.example", "pk:sk", 30.0)


def _page(ids: list[str], total_pages: int) -> _Resp:
    return _Resp(200, {"data": [{"id": i} for i in ids], "meta": {"totalPages": total_pages}})


@pytest.mark.asyncio
async def test_lookup_asks_for_core_fields_and_follows_pages(backend) -> None:
    c = _Client([_page(["a", "b"], 2), _page(["c"], 2)])
    ids = await backend._find_trace_ids_v3(c, tags=["namespace:x", "org:o"], user_id="")
    assert ids == ["a", "b", "c"]
    assert all(call["fields"] == "core" for call in c.calls)
    assert [call["page"] for call in c.calls] == [1, 2]
    assert c.calls[0]["tags"] == ["namespace:x", "org:o"] and c.calls[0]["limit"] == 100


@pytest.mark.asyncio
async def test_a_timed_out_page_is_retried_then_read(backend) -> None:
    c = _Client([httpx.ReadTimeout("", request=None), _page(["a"], 1)])
    ids = await backend._find_trace_ids_v3(c, tags=["namespace:x", "org:o"], user_id="")
    assert ids == ["a"] and len(c.calls) == 2


@pytest.mark.asyncio
async def test_a_5xx_page_is_retried_and_the_last_status_is_reported(backend) -> None:
    c = _Client([_Resp(502), _Resp(503), _Resp(500)])
    with pytest.raises(eb.ErasureBackendError) as ei:
        await backend._find_trace_ids_v3(c, tags=["namespace:x", "org:o"], user_id="")
    assert "HTTP 500" in str(ei.value) and len(c.calls) == 3


@pytest.mark.asyncio
async def test_three_timeouts_fail_closed_with_the_exception_named(backend, monkeypatch) -> None:
    c = _Client([httpx.ReadTimeout("", request=None)] * 3)

    class _Ctx:
        async def __aenter__(self) -> Any:
            return c

        async def __aexit__(self, *a: Any) -> None:
            return None

    monkeypatch.setattr(eb.httpx, "AsyncClient", lambda **kw: _Ctx())
    # The version is known, so all three timeouts land on the lookup page.
    backend._major = 3
    with pytest.raises(eb.ErasureBackendError) as ei:
        await backend.delete_namespace("o", "x")
    assert "transport error: ReadTimeout" in str(ei.value), str(ei.value)
    assert len(c.calls) == 3 and all("page" in call for call in c.calls)


@pytest.mark.asyncio
async def test_a_4xx_page_is_not_retried(backend) -> None:
    c = _Client([_Resp(401)])
    with pytest.raises(eb.ErasureBackendError) as ei:
        await backend._find_trace_ids_v3(c, tags=["namespace:x", "org:o"], user_id="")
    assert "HTTP 401" in str(ei.value) and len(c.calls) == 1


def test_describe_names_the_type_when_the_message_is_empty() -> None:
    assert eb._describe(httpx.ReadTimeout("", request=None)) == "ReadTimeout"
    assert eb._describe(ValueError("bad")) == "ValueError: bad"
    _ = SimpleNamespace  # keep the import honest


def _health(version: str) -> _Resp:
    return _Resp(200, {"status": "OK", "version": version})


def _obs_page(trace_ids: list[str], cursor: str | None) -> _Resp:
    meta = {"cursor": cursor} if cursor else {}
    return _Resp(
        200,
        {"data": [{"id": f"o{i}", "traceId": t} for i, t in enumerate(trace_ids)], "meta": meta},
    )


@pytest.mark.asyncio
async def test_langfuse_3_uses_the_trace_list(backend) -> None:
    c = _Client([_health("3.227.0"), _page(["a"], 1)])
    ids = await backend._find_trace_ids(c, tags=["org:o"], user_id="u1")
    assert ids == ["a"]
    assert c.calls[0]["url"].endswith("/api/public/health")
    assert c.calls[1]["url"].endswith("/api/public/traces")
    assert c.calls[1]["userId"] == "u1" and c.calls[1]["tags"] == ["org:o"]


@pytest.mark.asyncio
async def test_langfuse_4_reads_distinct_trace_ids_from_observations(backend) -> None:
    c = _Client(
        [
            _health("4.76.0"),
            _obs_page(["t1", "t1", "t2"], "c1"),
            # A page may be empty while a cursor is still present.
            _obs_page([], "c2"),
            _obs_page(["t2", "t3"], None),
        ]
    )
    ids = await backend._find_trace_ids(c, tags=["namespace:x", "org:o"], user_id="u1")
    assert ids == ["t1", "t2", "t3"]
    pages = c.calls[1:]
    assert all(p["url"].endswith("/api/public/v2/observations") for p in pages)
    assert [p.get("cursor") for p in pages] == [None, "c1", "c2"]
    assert pages[0]["fields"] == "core" and pages[0]["limit"] == 1000
    assert json.loads(pages[0]["filter"]) == [
        {
            "type": "arrayOptions",
            "column": "tags",
            "operator": "all of",
            "value": ["namespace:x", "org:o"],
        },
        {"type": "string", "column": "userId", "operator": "=", "value": "u1"},
    ]


@pytest.mark.asyncio
async def test_langfuse_4_namespace_lookup_filters_on_tags_only(backend) -> None:
    c = _Client([_health("4.0.0"), _obs_page(["t1"], None)])
    await backend._find_trace_ids(c, tags=["namespace:x", "org:o"])
    assert [f["column"] for f in json.loads(c.calls[1]["filter"])] == ["tags"]


@pytest.mark.asyncio
async def test_langfuse_4_lookup_error_fails_closed(backend) -> None:
    c = _Client([_health("4.76.0"), _Resp(400)])
    with pytest.raises(eb.ErasureBackendError) as ei:
        await backend._find_trace_ids(c, tags=["org:o"])
    assert "HTTP 400" in str(ei.value)


@pytest.mark.asyncio
async def test_langfuse_4_lookup_refuses_a_truncated_set(backend, monkeypatch) -> None:
    monkeypatch.setattr(eb.LangfuseBackend, "_MAX_TRACES", 2)
    c = _Client([_health("4.76.0"), _obs_page(["t1", "t2", "t3"], "c1")])
    with pytest.raises(eb.ErasureBackendError, match="truncated"):
        await backend._find_trace_ids(c, tags=["org:o"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resp",
    [
        _Resp(200, {"status": "OK"}),
        _Resp(404),
        _Resp(200, {"version": "dev"}),
        _NotJson(),
        _JsonList(),
    ],
)
async def test_an_unreadable_server_version_fails_closed(backend, resp) -> None:
    c = _Client([resp])
    with pytest.raises(eb.ErasureBackendError, match="server version"):
        await backend._find_trace_ids(c, tags=["org:o"])
    assert len(c.calls) == 1


@pytest.mark.asyncio
async def test_the_server_version_is_probed_once_per_backend(backend) -> None:
    c = _Client([_health("4.76.0"), _obs_page(["t1"], None), _obs_page(["t2"], None)])
    await backend._find_trace_ids(c, tags=["namespace:a", "org:o"])
    await backend._find_trace_ids(c, tags=["namespace:b", "org:o"])
    assert [call["url"].rsplit("/", 1)[-1] for call in c.calls] == [
        "health",
        "observations",
        "observations",
    ]


@pytest.mark.asyncio
async def test_langfuse_4_lookup_fails_closed_on_a_repeated_cursor(backend) -> None:
    c = _Client([_health("4.76.0"), _obs_page(["t1"], "c1"), _obs_page([], "c1")])
    with pytest.raises(eb.ErasureBackendError, match="cursor"):
        await backend._find_trace_ids(c, tags=["org:o"])


@pytest.mark.asyncio
async def test_langfuse_4_lookup_fails_closed_past_the_page_cap(backend, monkeypatch) -> None:
    monkeypatch.setattr(eb.LangfuseBackend, "_V4_MAX_PAGES", 2)
    c = _Client([_health("4.76.0"), _obs_page([], "c1"), _obs_page([], "c2")])
    with pytest.raises(eb.ErasureBackendError, match="2 pages"):
        await backend._find_trace_ids(c, tags=["org:o"])


@pytest.mark.asyncio
async def test_a_namespace_list_probes_the_version_once(backend, monkeypatch) -> None:
    """The lookups run concurrently; the probe runs before them."""
    calls: list[str] = []

    class _Concurrent:
        async def get(self, url: str, headers: Any = None, params: Any = None) -> Any:
            calls.append(url.rsplit("/", 1)[-1])
            await asyncio.sleep(0)
            if url.endswith("/health"):
                return _health("4.76.0")
            return _obs_page([], None)

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

    monkeypatch.setattr(eb.httpx, "AsyncClient", lambda **kw: _Concurrent())
    assert await backend.delete_namespaces("o", ["a", "b", "c", "d"]) == 0
    assert calls.count("health") == 1 and calls.count("observations") == 4
