"""Provider fallbacks are visible on the response (`degraded`), never only in
a log line: the per-request note list, the classify and rerank fallback
paths, isolation between requests, bounds, and the output-model field."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from server.clients import _http
from server.observability import degradations as D
from server.observability import provider_health
from server.settings import settings


def setup_function() -> None:
    D.begin()
    provider_health.reset()


def test_begin_note_current_round_trip_and_bounds():
    assert D.current() == []
    D.note("classify", "caller_type_kept", RuntimeError("boom"))
    D.note("rerank", "input_order_kept", "malformed payload")
    D.note("x", "y")  # no reason
    assert D.current() == [
        "classify: caller_type_kept (RuntimeError: boom)",
        "rerank: input_order_kept (malformed payload)",
        "x: y",
    ]
    # bounded: at most 8 notes, reasons clipped, newlines flattened
    for _i in range(20):
        D.note("p", "e", "line1\nline2" + "z" * 400)
    cur = D.current()
    assert len(cur) == 8 and "\n" not in "".join(cur) and len(cur[-1]) < 220
    # current() is a copy
    cur.append("nope")
    assert len(D.current()) == 8


def test_notes_feed_provider_health():
    D.note("classify", "caller_type_kept", "HTTP 502")
    st = provider_health.status()
    assert st.get("degraded") is True or any(
        k == "classify" for k in (st.get("providers") or {})
    ), st


@pytest.mark.asyncio
async def test_requests_are_isolated_by_contextvar():
    D.begin()
    D.note("a", "b")

    async def other() -> list[str]:
        D.begin()
        D.note("c", "d")
        return D.current()

    # a task runs in a COPY of the context; begin() inside it must not touch ours
    got = await asyncio.create_task(other())
    assert got == ["c: d"]
    assert D.current() == ["a: b"]


def test_note_never_raises_on_garbage():
    class Bad:
        def __str__(self):
            raise RuntimeError("no str")

    D.note("p", "e", Bad())
    assert len(D.current()) <= 1


@pytest.mark.asyncio
async def test_classify_fallbacks_note_the_degradation(monkeypatch):
    from server.memory.app import classify as C

    class _Resp:
        def __init__(self, payload, status=200):
            self._p, self.status_code = payload, status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("502", request=None, response=None)  # type: ignore[arg-type]

        def json(self):
            return self._p

    class _Client:
        """The model client: answers every post with `resp`."""

        def __init__(self, resp):
            self._resp = resp

        async def post(self, *a, **k):
            return self._resp

    # a model call: the classifier is at a listed gateway host
    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    # 1. transport error → caller's type kept, noted
    monkeypatch.setattr(_http, "get_model_client", lambda: _Client(_Resp({}, 502)))
    D.begin()
    assert (
        await C.classify(
            org="acme",
            namespace="ns",
            endpoint="https://gateway.example/c",
            prompt_template="",
            value="v",
            valid_types=["fact"],
        )
        == ""
    )
    assert D.current() and D.current()[0].startswith("classify: caller_type_kept (HTTPStatusError")
    # 2. unregistered type → noted with the offending type
    monkeypatch.setattr(_http, "get_model_client", lambda: _Client(_Resp({"memory_type": "weird"})))
    D.begin()
    assert (
        await C.classify(
            org="acme",
            namespace="ns",
            endpoint="https://gateway.example/c",
            prompt_template="",
            value="v",
            valid_types=["fact"],
        )
        == ""
    )
    assert "'weird' not a registered type" in D.current()[0]
    # 3. success → nothing degraded, health records success
    monkeypatch.setattr(_http, "get_model_client", lambda: _Client(_Resp({"memory_type": "fact"})))
    D.begin()
    assert (
        await C.classify(
            org="acme",
            namespace="ns",
            endpoint="https://gateway.example/c",
            prompt_template="",
            value="v",
            valid_types=["fact"],
        )
        == "fact"
    )
    assert D.current() == []


@pytest.mark.asyncio
async def test_rerank_http_fallback_notes_the_degradation(monkeypatch):
    from server.memory.app import rerank as R

    class _Client:
        """The model client: every post times out."""

        async def post(self, *a, **k):
            raise httpx.ReadTimeout("slow reranker")

    monkeypatch.setattr(_http, "get_model_client", _Client)
    monkeypatch.setattr(R.settings, "rerank_endpoints_json", '{"slow": "http://x"}')
    cands = [
        R.RerankCandidate(
            memory_key="k", namespace="n", content_excerpt="c", memory_type="fact", similarity=0.5
        )
    ]
    D.begin()
    out = await R.rerank(endpoint="slow", prompt_template="", query="q", candidates=cands)
    assert out == cands  # fail-open order kept
    assert D.current() == ["rerank: input_order_kept (ReadTimeout: slow reranker)"]


def test_output_models_carry_degraded():
    from server.memory.api.models_crud import MemorySetOutput
    from server.memory.api.models_search import MemorySearchOutput

    assert MemorySetOutput(version=1).degraded == []
    assert MemorySearchOutput(results=[], embedding_model="m").degraded == []
    assert (
        MemorySetOutput(version=1, degraded=["classify: caller_type_kept"])
        .degraded[0]
        .startswith("classify")
    )
