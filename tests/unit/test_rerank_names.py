"""Rerank endpoints are operator-configured names: Loom never posts candidate
text to a URL a caller (or a namespace row) supplies."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from server.clients import _http
from server.code.api.models_search import CodeAskInput, CodeSearchInput
from server.identity.api.models import NamespaceRegisterInput
from server.memory.api.models_search import MemorySearchInput, MemorySearchTraceInput
from server.memory.app import rerank as R
from server.settings import Settings

_ENDPOINTS = '{"acme": "https://rerank.internal/v1"}'
_MEMORY_IDENT = {"org": "o", "namespace": "n", "query": "q"}


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(R.settings, "rerank_endpoints_json", _ENDPOINTS)


def _cands() -> list[R.RerankCandidate]:
    return [
        R.RerankCandidate(
            memory_key=k,
            namespace="n",
            content_excerpt="secret " + k,
            memory_type="fact",
            similarity=0.5,
        )
        for k in ("a", "b")
    ]


class _Recorder:
    """Stands in for the model client and records every POST."""

    posts: list[str] = []

    async def post(self, url: str, json: Any = None, **_: Any) -> Any:
        _Recorder.posts.append(url)

        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {"ranked": [{"memory_key": "b", "namespace": "n"}]}

        return _Resp()


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> type[_Recorder]:
    _Recorder.posts = []
    monkeypatch.setattr(_http, "get_model_client", _Recorder)
    return _Recorder


@pytest.mark.parametrize(
    "build",
    [
        lambda v: MemorySearchInput(**_MEMORY_IDENT, rerank=v),
        lambda v: MemorySearchTraceInput(**_MEMORY_IDENT, rerank=v),
        lambda v: CodeSearchInput(query="q", rerank=v),
        lambda v: CodeAskInput(question="q", rerank=v),
        lambda v: NamespaceRegisterInput(
            org="o", namespace="n", owner_email="a@b.c", rerank_endpoint=v
        ),
    ],
)
@pytest.mark.parametrize("value", ["https://attacker.example/collect", "unknown-name"])
def test_a_url_or_unknown_name_is_refused_naming_the_configured_ones(
    build: Any, value: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        build(value)
    assert "acme" in str(caught.value)


@pytest.mark.parametrize(
    "value", ["", "none", "builtin:openai", "builtin:cohere:rerank-v3", "acme"]
)
def test_names_builtins_and_none_are_accepted(value: str) -> None:
    assert MemorySearchInput(**_MEMORY_IDENT, rerank=value).rerank == value
    assert (
        NamespaceRegisterInput(
            org="o", namespace="n", owner_email="a@b.c", rerank_endpoint=value
        ).rerank_endpoint
        == value
    )


def test_builtin_lookalike_is_refused() -> None:
    with pytest.raises(ValidationError):
        CodeSearchInput(query="q", rerank="builtin:openaiX")


@pytest.mark.asyncio
async def test_a_configured_name_posts_to_the_operator_url(recorder: type[_Recorder]) -> None:
    out = await R.rerank(endpoint="acme", prompt_template="", query="q", candidates=_cands())
    assert recorder.posts == ["https://rerank.internal/v1"]
    assert [c.memory_key for c in out] == ["b", "a"]


@pytest.mark.asyncio
async def test_an_operator_ranking_keeps_every_row_of_a_repeated_key(
    monkeypatch: pytest.MonkeyPatch, recorder: type[_Recorder]
) -> None:
    """The endpoint names rows by key, and extraction writes a key once per session:
    each entry takes the key's next row, so no row is lost or served twice."""

    def _cand(key: str, session: str) -> R.RerankCandidate:
        return R.RerankCandidate(
            memory_key=key,
            namespace="n",
            content_excerpt=f"{key} {session}",
            memory_type="fact",
            similarity=0.5,
        )

    async def _post(self: Any, url: str, json: Any = None, **_: Any) -> Any:
        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, Any]:
                return {"ranked": [{"memory_key": k, "namespace": "n"} for k in ("a", "a", "b")]}

        return _Resp()

    monkeypatch.setattr(_Recorder, "post", _post)
    cands = [_cand("a", "s1"), _cand("b", "s1"), _cand("a", "s3")]
    out = await R.rerank(endpoint="acme", prompt_template="", query="q", candidates=cands)
    assert [c.content_excerpt for c in out] == ["a s1", "a s3", "b s1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["https://legacy.example/rerank", "gone", "none"])
async def test_an_unconfigured_endpoint_posts_nothing(
    recorder: type[_Recorder], endpoint: str
) -> None:
    cands = _cands()
    out = await R.rerank(endpoint=endpoint, prompt_template="", query="q", candidates=cands)
    assert out is cands
    assert recorder.posts == []


@pytest.mark.asyncio
async def test_a_namespace_row_holding_a_legacy_url_reads_as_no_reranker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        async def query_one(self, *_: Any, **__: Any) -> dict[str, str]:
            return {
                "rerank_endpoint": "https://legacy.example/rerank",
                "rerank_prompt_template": "t",
            }

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    assert await R.get_namespace_rerank_config("o", "n") == ("", "")


@pytest.mark.asyncio
async def test_a_namespace_row_holding_a_name_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client:
        async def query_one(self, *_: Any, **__: Any) -> dict[str, str]:
            return {"rerank_endpoint": "acme", "rerank_prompt_template": "t"}

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    assert await R.get_namespace_rerank_config("o", "n") == ("acme", "t")


@pytest.mark.parametrize(
    "raw",
    [
        '{"has space": "https://x.example"}',
        '{"none": "https://x.example"}',
        '{"a": "file:///etc/passwd"}',
        '{"a": "https://"}',
    ],
)
def test_the_endpoint_map_is_checked_at_load(raw: str) -> None:
    with pytest.raises(ValidationError, match="LOOM_RERANK_ENDPOINTS_JSON"):
        Settings(_env_file=None, rerank_endpoints_json=raw)  # type: ignore[call-arg]


def test_a_valid_endpoint_map_loads() -> None:
    s = Settings(_env_file=None, rerank_endpoints_json=_ENDPOINTS)  # type: ignore[call-arg]
    assert s.rerank_endpoint_map() == {"acme": "https://rerank.internal/v1"}
