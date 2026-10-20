"""LOOM_READS_LLM_FREE: every read op (`Operation.read_only`) runs with no LLM
call, no namespace or deployment reranker and one query embedding; an ask whose
answer a model writes answers 409 naming the setting."""

from __future__ import annotations

import sys
import typing
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from server.core import llm_free
from server.core.errors import PreconditionFailedError
from server.operations.registry import ALL_OPERATIONS
from tests.unit import test_memory_search_handler_gates as _gates
from tests.unit.ch_fake import RecordingCH
from tests.unit.test_memory_search_handler_gates import _run, _svc_hit

# The handler-path fixtures: a faked search pipeline, and every LLM call recorded and refused.
search_env = _gates.search_env
llm_attempts = _gates.llm_attempts

_QUESTION = "how many model kits have I bought?"
_HINTS: dict[str, Any] = {
    "org": "o",
    "namespace": "ns",
    "agent": "a",
    "query": _QUESTION,
    "question": _QUESTION,
    "text": _QUESTION,
    "situation": "fixing a flaky test",
    "title": "the export test fails",
    "body": "the export test fails sometimes",
    "session_id": "-",
    "user_id": "-",
}


def _body(model: type) -> dict[str, Any] | None:
    """A minimal body for an input model; None when a required field has no stand-in."""
    out: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        if name in _HINTS:
            out[name] = _HINTS[name]
            continue
        if not field.is_required():
            continue
        ann = field.annotation
        origin, args = typing.get_origin(ann), typing.get_args(ann)
        if ann is str:
            out[name] = "x"
        elif ann is int:
            out[name] = 1
        elif ann is float:
            out[name] = 1.0
        elif ann is bool:
            out[name] = False
        elif origin in (list, tuple) or ann is list:
            out[name] = []
        elif origin is dict or ann is dict:
            out[name] = {}
        elif origin is typing.Literal and args:
            out[name] = args[0]
        else:
            return None
    return out


@contextmanager
def _serving(op: Any, monkeypatch: pytest.MonkeyPatch, *, on: bool = True) -> Iterator[None]:
    from server.settings import settings

    monkeypatch.setattr(settings, "reads_llm_free", on)
    with llm_free.serving(op):
        yield


def _op(name: str) -> Any:
    return next(op for op in ALL_OPERATIONS if op.name == name)


@pytest.fixture
def every_module_faked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """ClickHouse answers no rows, the embedder a constant, and every LLM client
    records and raises, in every server module that imported them."""
    import server.core.ch as ch_mod
    import server.embedding as emb_mod
    import server.llm.client as llm_client

    attempts: list[str] = []

    class _RefusingLLM:
        model = "refusing"

        async def complete(self, system: str = "", user: str = "", **kw: Any) -> Any:
            attempts.append(user[:60])
            raise AssertionError("an LLM call")

    class _Embedder:
        model_name = "fake-embed"
        dim = 8

        async def embed(self, text: str) -> list[float]:
            return [0.1] * 8

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            return [[0.1] * 8 for _ in texts]

    ch = RecordingCH(strict=False)
    fakes = {
        ch_mod.get_client: lambda: ch,
        llm_client.get_llm_client: lambda *a, **k: _RefusingLLM(),
        llm_client.get_llm_client_for_model: lambda *a, **k: _RefusingLLM(),
        emb_mod.get_embedder: lambda *a, **k: _Embedder(),
    }
    for mod in list(sys.modules.values()):
        if not getattr(mod, "__name__", "").startswith("server"):
            continue
        for attr in ("get_client", "get_llm_client", "get_llm_client_for_model", "get_embedder"):
            current = getattr(mod, attr, None)
            if current is not None and current in fakes:
                monkeypatch.setattr(mod, attr, fakes[current])
    return attempts


async def _serve(op: Any) -> None:
    """The op's handler on its minimal body; a refusal naming the setting is an
    answer, and any other failure of the empty fake data is no LLM call."""
    try:
        await op.handler(op.input_model(**(_body(op.input_model) or {})))
    except PreconditionFailedError as e:
        assert "LOOM_READS_LLM_FREE" in str(e)
    except Exception:  # noqa: BLE001 — the fake data, not the setting, failed it
        pass


_READS = [op for op in ALL_OPERATIONS if op.read_only and _body(op.input_model) is not None]


@pytest.mark.parametrize("op", _READS, ids=lambda op: op.name)
async def test_every_read_op_makes_no_llm_call(
    every_module_faked, monkeypatch: pytest.MonkeyPatch, op: Any
) -> None:
    # Walks the registry: a read op added later is covered the day it is registered.
    with _serving(op, monkeypatch):
        await _serve(op)
    assert every_module_faked == []


def test_the_walk_covers_the_read_ops() -> None:
    reads = [op.name for op in ALL_OPERATIONS if op.read_only]
    walked = {op.name for op in _READS}
    assert len(walked) >= 0.9 * len(reads)
    assert {"memory.search", "search.query", "context.pack", "code.search", "memory.ask"} <= walked


@pytest.mark.parametrize("name", ["memory.search", "search.query", "code.ask"])
async def test_off_the_setting_the_walk_sees_their_llm_calls(
    every_module_faked, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # A positive control: the same walk with the setting off reaches the model client.
    op = _op(name)
    with _serving(op, monkeypatch, on=False):
        await _serve(op)
    assert every_module_faked


async def test_an_asks_synthesis_answers_409_naming_the_setting(monkeypatch) -> None:
    from server.code.api.models_search import CodeAskInput
    from server.memory.api.models_ask import MemoryAskInput

    for name, body in (
        ("memory.ask", MemoryAskInput(org="o", namespace="ns", query=_QUESTION)),
        ("code.ask", CodeAskInput(org="o", namespace="ns", question=_QUESTION)),
    ):
        op = _op(name)
        with _serving(op, monkeypatch), pytest.raises(PreconditionFailedError) as e:
            await op.handler(body)
        assert "LOOM_READS_LLM_FREE" in str(e.value)
        assert e.value.status_code == 409


@pytest.mark.parametrize(
    "posture",
    [{}, {"recipe": "quality"}, {"recipe": "graph"}, {"retrieval_budget": "deep"}],
    ids=["default", "quality", "graph", "deep"],
)
@pytest.mark.parametrize(
    "query",
    [
        "what is my favorite color",
        _QUESTION,
        "how many days passed between the day I got my binoculars and the day I "
        "noticed the goldfinches?",
    ],
)
async def test_any_memory_search_makes_no_llm_call(
    search_env, llm_attempts, monkeypatch, query: str, posture: dict[str, Any]
) -> None:
    with _serving(_op("memory.search"), monkeypatch):
        out = await _run(search_env, query=query, observation_date="2023-07-15", **posture)
    assert llm_attempts == []
    assert "hyde=off" in out.retrieval_plan and out.retrieval_cost_estimate["llm_calls"] == 0


async def test_an_ask_for_the_evidence_alone_makes_no_llm_call(
    search_env, llm_attempts, monkeypatch
) -> None:
    from server.memory.api.models_ask import MemoryAskInput

    op = _op("memory.ask")
    with _serving(op, monkeypatch):
        await op.handler(
            MemoryAskInput(
                org="o", namespace="ns", query=_QUESTION, allow_synthesis=False, decompose="always"
            )
        )
    assert llm_attempts == []


async def test_a_read_runs_no_namespace_reranker_but_one_the_request_names(
    search_env, llm_attempts, monkeypatch
) -> None:
    import server.memory.app.rerank as rerank_mod

    looked_up: list[str] = []
    reranked: list[str] = []
    real_rerank = rerank_mod.rerank

    async def namespace_rerank(org: str, ns: str) -> tuple[str, str]:
        looked_up.append(ns)
        return ("builtin:cohere", "")

    async def recording_rerank(*, endpoint: str, **kw: Any) -> Any:
        reranked.append(endpoint)
        return await real_rerank(endpoint=endpoint, **kw)

    monkeypatch.setattr(rerank_mod, "get_namespace_rerank_config", namespace_rerank)
    monkeypatch.setattr(rerank_mod, "rerank", recording_rerank)
    with _serving(_op("memory.search"), monkeypatch):
        await _run(search_env)
        assert looked_up == [] and reranked == []
        await _run(search_env, rerank="builtin:cohere")
    assert reranked == ["builtin:cohere"]
    await _run(search_env)  # off the setting, the namespace's reranker runs
    assert looked_up == ["ns"]


async def test_a_read_embeds_its_query_once(search_env, monkeypatch) -> None:
    # Set expansion embeds a phrase per collective noun ("has a sister", "has a
    # brother"); an LLM-free read embeds the query alone, in a search and in a pack.
    import server.embedding.embedder as embedder_mod
    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    embedded: list[str] = []

    class _Counting:
        model_name = "fake-embed"

        async def embed(self, text: str) -> list[float]:
            embedded.append(" ".join(text.split()))
            return [0.1] * 8

    from tests.unit._memory_search_pkg import patch_handler_name

    patch_handler_name(monkeypatch, "get_embedder", lambda: _Counting())
    monkeypatch.setattr(embedder_mod, "get_embedder", lambda: _Counting())
    search_env.hits = [_svc_hit("a", sim=0.80, rank=0.8)]
    query = "how many siblings do I have"
    await _run(search_env, query=query)
    assert len(set(embedded)) > 1  # off the setting: the query and its phrases
    for name, call in (
        ("memory.search", lambda: _run(search_env, query=query)),
        (
            "context.pack",
            lambda: handle_context_pack(
                ContextPackInput(org="o", namespace="ns", agent="a", query=query)
            ),
        ),
    ):
        embedded.clear()
        with _serving(_op(name), monkeypatch):
            await call()
        assert set(embedded) == {query}, name


def test_the_other_planes_read_the_mark(monkeypatch) -> None:
    from server.code.app.search_legs import llm_legs_off
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.query_spelling import _lookup_key

    search = MemorySearchInput(org="o", namespace="ns", query="DataLakeCatalog sizes")
    assert llm_legs_off("auto") is False and _lookup_key(search, "m") is not None
    with _serving(_op("code.search"), monkeypatch):
        assert llm_legs_off("auto") is True  # HyDE and model-named concept expansion
        assert _lookup_key(search, "m") is None  # no spelling-fix embedding


async def test_the_tracker_and_bisect_model_steps_keep_their_deterministic_answer(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from server.code.app import bisect_fusion
    from server.tracker.app import dupcheck, dupcheck_answer

    out = SimpleNamespace(items=[1], verdict="related")
    pool = [SimpleNamespace(pr_number=1)]
    monkeypatch.setattr(dupcheck_answer.settings, "tracker_dupcheck_judge_enabled", True)
    with _serving(_op("tracker.duplicate_check"), monkeypatch):
        assert await dupcheck._rerank_call(SimpleNamespace(title="t", body="b"), [{"k": 1}]) is None
        assert await dupcheck_answer.judge_uncertain(None, None, {}, out, object()) is out
        ranked, info = await bisect_fusion.rerank_stage(pool, text="crash", force=True)
        assert ranked is pool and info == {}
