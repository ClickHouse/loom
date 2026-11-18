"""A lost document proof discards a complete generated answer and its derived state."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
from types import SimpleNamespace

import pytest

from server.core import request_work
from server.knowledge.api.models_knowledge import KnowledgeSearchOutput, _SearchHit
from server.knowledge.app import knowledge_search, source_access
from server.knowledge.app.knowledge_search import CheckedSearch, SearchRequest
from server.knowledge.app.source_access import ReadPlan
from server.knowledge.app.source_corpus import LostCorpus
from server.memory.app.ask import cascade, handler, pipeline
from server.observability import cost_accumulator
from server.observability.genai import current_stage
from tests.unit.test_ask_synth_accounting import _ask, make_env


def _passage(name):
    return _SearchHit(
        namespace="ns",
        source_uri=name,
        chunk_idx=0,
        content=name,
        tags=[],
        distance=0.2,
        similarity=0.8,
    )


def _plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return vars(value) if hasattr(value, "__dict__") else value


def _snapshot(run):
    return copy.deepcopy(
        {
            "rows": {
                name: [_plain(row) for row in getattr(run, name)]
                for name in ("ranked", "memories", "results", "synth_pool")
            },
            "intent": _plain(run.intent),
            "search": _plain(run.search_input),
        }
    )


class _Proof:
    mirrored = True

    def __init__(self, world):
        self.world = world
        self.invalid = False

    def fresh(self):
        return not self.invalid and self.world.allowed

    async def check(self):
        if not self.fresh():
            self.invalid = True
            raise LostCorpus()


class _Renderer:
    marked = True

    def __init__(self, world):
        self.world = world

    async def render(self, _rows, _dropped):
        self.world.renders += 1
        await asyncio.sleep(0)
        return KnowledgeSearchOutput(
            results=[_passage("ordinary:document")], embedding_model="fake-embed"
        )


class _World:
    provider = "fake"
    model = "gpt-4o-mini"

    def __init__(self):
        self.allowed = True
        self.phase = None
        self.fired = False
        self.calls = []
        self.pending_reads = []
        self.searches = []
        self.resolutions = 0
        self.renders = 0
        self.runs = []
        self.deferred = []
        self.live = set()
        self.entered = asyncio.Event()
        self.cleanup_entered = asyncio.Event()
        self.release_cleanup = asyncio.Event()

    async def resolve(self, input):
        self.resolutions += 1
        return SearchRequest(input.model_copy(deep=True), (input.namespace,), ())

    async def prepare(self, org, namespaces):
        assert namespaces == ["ns"]
        assert request_work.mark_mirrored()
        return ReadPlan(org, marked=True)

    async def search(self, request, plan):
        self.searches.append(request)
        proof = _Proof(self) if self.allowed else None
        values = [_passage("PRIVATE-CANARY")] if proof else [_passage("ordinary:document")]
        return CheckedSearch(
            KnowledgeSearchOutput(results=values, embedding_model="fake-embed"),
            proof,
            _Renderer(self),
            [],
            0,
        )

    async def complete(self, system, user, **kwargs):
        stage = str(current_stage() or "")
        selector = "worth opening at full detail" in system
        self.calls.append((stage, system, user))
        self.live.add(asyncio.current_task())
        cost_accumulator.report_llm(self.model, 101, 11, cache_read=17)
        try:
            self.entered.set()
            if self.phase == "cancel":
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cleanup_entered.set()
                    await self.release_cleanup.wait()
            ordinary = not self.allowed
            if ordinary and self.phase == "retry-error":
                raise RuntimeError("ordinary retry refused")
            synthesis_n = sum(
                "worth opening at full detail" not in row[1] and row[0] != "ask_consistency"
                for row in self.calls
            )
            revoke = (
                (self.phase == "excerpt" and not selector)
                or (self.phase == "selector" and selector)
                or (self.phase == "deep" and not selector and synthesis_n == 2)
                or (self.phase == "inline" and stage == "ask_consistency")
                or (self.phase == "mid" and stage == "ask_synthesize_mid")
                or (
                    self.phase in {"cheap-escalation", "mid-verify"}
                    and stage == "ask_synthesize_strong"
                )
                or (self.phase == "reread" and not selector and synthesis_n == 2)
                or (self.phase == "retry-error" and not selector and not ordinary)
            )
            if revoke and not self.fired:
                self.allowed, self.fired = False, True
            await asyncio.sleep(0)
            if selector:
                return SimpleNamespace(text=json.dumps({"picks": [0]}), model=self.model)
            if stage == "ask_consistency":
                return SimpleNamespace(text='{"entailment":1,"neutral":0,"contradiction":0}')
            if "PRIVATE-CANARY" in user and (
                (self.phase in {"reread", "reread-io"} and synthesis_n == 1)
                or (self.phase == "cheap-escalation" and stage == "ask_synthesize_cheap")
                or (self.phase == "mid-verify" and stage != "ask_synthesize_strong")
            ):
                return SimpleNamespace(text="The memories do not record this.", model=self.model)
            answer = "PRIVATE-CANARY" if "PRIVATE-CANARY" in user else "Ordinary answer"
            return SimpleNamespace(text=answer + " [m1].", model=self.model)
        finally:
            self.live.discard(asyncio.current_task())


@pytest.fixture
def world(monkeypatch):
    env = make_env(monkeypatch)
    state = _World()
    env.llm = state
    monkeypatch.setattr(env.settings, "memory_ask_knowledge_top_k", 2)
    monkeypatch.setattr(env.settings, "memory_ask_premise_check", False)
    token = request_work._current.set(None)
    request_work.begin()
    monkeypatch.setattr(knowledge_search, "resolve_search", state.resolve)
    monkeypatch.setattr(source_access, "prepare", state.prepare)
    monkeypatch.setattr(knowledge_search, "run_checked_search", state.search)
    settle = pipeline._settle_window
    audit = cascade._audit_answer

    async def settle_window(run, **kwargs):
        result = await settle(run, **kwargs)
        state.runs.append((run, _snapshot(run)))
        return result

    async def audit_answer(*args, **kwargs):
        result = await audit(*args, **kwargs)
        state.deferred.append(result[3])
        if state.phase == "tail" and state.allowed:
            state.allowed, state.fired = False, True
        return result

    def forbidden(*args, **kwargs):
        pytest.fail("marked generated work reached a deferred or precedent sink")

    async def dated_turns(**kwargs):
        from server.memory.app.ask_when_evidence import DatedTurn

        if state.phase == "reread-io" and state.allowed:
            state.allowed, state.fired = False, True
        return [DatedTurn(day="2023-05-21", role="user", content="Ordinary dated fact")]

    async def pending_extraction(*args, **kwargs):
        state.pending_reads.append(len(state.calls))
        return 0

    monkeypatch.setattr(pipeline, "_settle_window", settle_window)
    monkeypatch.setattr(pipeline, "_audit_answer", audit_answer)
    monkeypatch.setattr("server.memory.app.ask_when_evidence.fetch_dated_turns", dated_turns)
    monkeypatch.setattr(
        "server.memory.app.pending_disclosure.pending_extraction", pending_extraction
    )
    for name in ("_schedule_cited_used", "_record_contradiction", "_schedule_deferred_consistency"):
        monkeypatch.setattr(handler, name, forbidden)
    yield state, env.settings
    request_work._current.reset(token)


@pytest.mark.parametrize(
    "phase",
    [
        "excerpt",
        "selector",
        "deep",
        "inline",
        "tail",
        "reread",
        "reread-io",
        "mid",
        "cheap-escalation",
        "mid-verify",
    ],
)
async def test_public_ask_rebuilds_the_whole_ordinary_answer_after_loss(world, phase, monkeypatch):
    state, settings = world
    state.phase = phase
    deep = phase in {"selector", "deep", "mid-verify"}
    monkeypatch.setattr(settings, "memory_ask_consistency_check", phase == "inline")
    if phase in {"mid", "mid-verify", "cheap-escalation"}:
        import server.memory.app.search as search

        retrieve = search.handle_memory_search

        async def routed_search(input):
            output = await retrieve(input)
            for index, row in enumerate(output.results):
                row.similarity = (
                    (0.9 if index == 0 else 0.5)
                    if phase == "cheap-escalation"
                    else (0.6 if index == 0 else 0.5)
                )
            output.retrieval_confidence = 0.95 if phase == "cheap-escalation" else 0.7
            output.retrieval_risk = "low" if phase == "cheap-escalation" else "medium"
            output.route_agreement = 0.95
            return output

        async def ratio(org: str, ns: str, *, user_id: str = ""):
            return 0.0

        monkeypatch.setattr(search, "handle_memory_search", routed_search)
        monkeypatch.setattr("server.llm.client.get_llm_client_for_model", lambda _model: state)
        monkeypatch.setattr("server.observability.budget.namespace_budget_ratio", ratio)
        if phase == "cheap-escalation":
            monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", state.model)
        else:
            monkeypatch.setattr(settings, "memory_ask_mid_model", state.model)
            monkeypatch.setattr(settings, "memory_ask_synth_mid_routes", "uncertain")
    options = dict(
        depth="raw" if deep else "excerpt",
        consistency="inline",
        knowledge=True,
        deep_first=phase == "mid-verify",
    )
    acc = cost_accumulator.begin_op()
    late = await _ask(**options)
    assert state.fired and not state.live
    assert acc.by_model[state.model][0] == len(state.calls)
    assert all(_snapshot(run) == frozen for run, frozen in state.runs)
    assert state.resolutions == 1 and [r.input.top_k for r in state.searches] == [2, 8]
    assert all(r.input.rerank == "none" and r.namespaces == ("ns",) for r in state.searches)
    assert "PRIVATE-CANARY" not in late.model_dump_json()
    assert late.synth_cost_usd == late.synth_input_tokens == 0
    assert late.synth_cached_input_tokens == late.synth_cache_write_tokens == 0
    charged = len(state.calls)
    state.phase, state.calls = None, []
    initial = await _ask(**options)
    assert late.model_dump() == initial.model_dump()
    assert charged > len(state.calls)
    assert not state.live and all(_snapshot(run) == frozen for run, frozen in state.runs)
    assert state.pending_reads and all(count == 0 for count in state.pending_reads)


async def test_no_admitted_mirrors_uses_one_generation_without_a_retry(world):
    state, _settings = world
    state.allowed = False
    output = await _ask(depth="excerpt", knowledge=True)
    assert output.source == "synthesized" and output.knowledge_hits == 1
    assert len(state.calls) == 1 and not state.live and state.resolutions == 1


async def test_marked_ask_bypasses_response_cache_precedents_and_deferred_judge(world, monkeypatch):
    from server.llm.cache import CachedLLMClient

    state, settings = world
    monkeypatch.setattr(settings, "memory_ask_consistency_check", True)
    monkeypatch.setattr(settings, "memory_ask_precedents", True)

    def forbidden(*args, **kwargs):
        pytest.fail("private work reached a cache or precedent")

    async def require_customer():
        return None

    monkeypatch.setattr("server.llm.customer.require", require_customer)
    for name in ("_get", "_put"):
        monkeypatch.setattr(CachedLLMClient, name, forbidden)
    for name in ("fast_reuse", "reuse", "schedule_store"):
        monkeypatch.setattr("server.memory.app.precedents." + name, forbidden)
    output = await _ask(
        depth="excerpt", knowledge=True, llm_cache=True, precedents=True, consistency="deferred"
    )
    assert output.source == "synthesized" and output.llm_cache_hits == 0
    assert len(state.calls) == 1 and state.deferred == [None]


async def test_ordinary_retry_failure_never_returns_the_private_answer_or_retries_again(world):
    state, _settings = world
    state.phase = "retry-error"
    with pytest.raises(RuntimeError, match="ordinary retry refused"):
        await _ask(depth="excerpt", knowledge=True)
    assert len(state.calls) == 2 and not state.live
    assert state.resolutions == 1 and len(state.searches) == 2


async def test_repeated_request_cancellation_joins_the_provider_cleanup(world):
    state, _settings = world
    state.phase = "cancel"
    request = asyncio.create_task(_ask(depth="excerpt", knowledge=True))
    try:
        await asyncio.wait_for(state.entered.wait(), 1)
        request.cancel()
        await asyncio.wait_for(state.cleanup_entered.wait(), 1)
        request.cancel()
        await asyncio.sleep(0)
        assert not request.done() and state.live
        state.release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not state.live
    finally:
        state.release_cleanup.set()
        owned = {request, *state.live}
        for task in owned:
            if not task.done():
                task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)
