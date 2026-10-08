"""The Langfuse erasure lookup asks for core fields only,
retries a page on timeout / 429 / 5xx with backoff, and names the exception
type in the saga's failure message (str(ReadTimeout) is empty)."""

from __future__ import annotations

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


class _Client:
    """Scripted GET: each entry is a _Resp or an exception to raise."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def get(self, url: str, headers: Any = None, params: Any = None) -> Any:
        self.calls.append(dict(params or {}))
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
    ids = await backend._find_trace_ids(c, {"tags": ["namespace:x", "org:o"]})  # type: ignore[arg-type]
    assert ids == ["a", "b", "c"]
    assert all(call["fields"] == "core" for call in c.calls)
    assert [call["page"] for call in c.calls] == [1, 2]
    assert c.calls[0]["tags"] == ["namespace:x", "org:o"] and c.calls[0]["limit"] == 100


@pytest.mark.asyncio
async def test_a_timed_out_page_is_retried_then_read(backend) -> None:
    c = _Client([httpx.ReadTimeout("", request=None), _page(["a"], 1)])
    ids = await backend._find_trace_ids(c, {"tags": ["namespace:x", "org:o"]})  # type: ignore[arg-type]
    assert ids == ["a"] and len(c.calls) == 2


@pytest.mark.asyncio
async def test_a_5xx_page_is_retried_and_the_last_status_is_reported(backend) -> None:
    c = _Client([_Resp(502), _Resp(503), _Resp(500)])
    with pytest.raises(eb.ErasureBackendError) as ei:
        await backend._find_trace_ids(c, {"tags": ["namespace:x", "org:o"]})  # type: ignore[arg-type]
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
    with pytest.raises(eb.ErasureBackendError) as ei:
        await backend.delete_namespace("o", "x")
    assert "transport error: ReadTimeout" in str(ei.value), str(ei.value)
    assert len(c.calls) == 3


@pytest.mark.asyncio
async def test_a_4xx_page_is_not_retried(backend) -> None:
    c = _Client([_Resp(401)])
    with pytest.raises(eb.ErasureBackendError) as ei:
        await backend._find_trace_ids(c, {"tags": ["namespace:x", "org:o"]})  # type: ignore[arg-type]
    assert "HTTP 401" in str(ei.value) and len(c.calls) == 1


def test_describe_names_the_type_when_the_message_is_empty() -> None:
    assert eb._describe(httpx.ReadTimeout("", request=None)) == "ReadTimeout"
    assert eb._describe(ValueError("bad")) == "ValueError: bad"
    _ = SimpleNamespace  # keep the import honest
