"""Under a code scope, loom_search's memory section also reads the decisions
recorded in the repository's trunk code namespace. The leg runs only for a
namespace that passed the caller's gate, never fails the search, carries the
caller's read scope, and merges its hits into the one memory section by
similarity."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Literal

import pytest

import server.memory.app.loom_search.fanout as fanout
import server.memory.app.loom_search.repo_decisions as repo_decisions
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.code.domain.index import repos
from server.memory.api.models_search import (
    MemorySearchInput,
    MemorySearchOutput,
    _MemorySearchHit,
)
from server.memory.app.ask.reader_render import render_reader_block
from server.memory.app.search.scoring import per_key_fetch
from server.memory.app.search.telemetry import credit_merged_hits
from server.memory.domain.search_types import SearchHit
from tests.unit.memory_search_fakes import install_search_fakes

ORG = "acme"
TRUNK = "code-kj"


@pytest.fixture(autouse=True)
def _no_merged_credit(monkeypatch):
    async def credit(*a: Any) -> None:
        return None

    monkeypatch.setattr("server.memory.app.search.telemetry.credit_merged_hits", credit)


@pytest.fixture
def search_env(monkeypatch) -> SimpleNamespace:
    """The real memory.search with its collaborators faked."""
    return install_search_fakes(monkeypatch)


@pytest.fixture(autouse=True)
def _repos(monkeypatch):
    entries = [
        SimpleNamespace(org=ORG, namespace=TRUNK, repo="Acme/kj", git_branch="master"),
        SimpleNamespace(org=ORG, namespace="code-kj-26-7", repo="Acme/kj", git_branch="26.7"),
    ]
    monkeypatch.setattr(repos, "configured_repos", lambda: list(entries))


def _hit(
    key: str, sim: float, ns: str = "", mtype: str = "semantic", rank: float | None = None
) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type=mtype,  # type: ignore[arg-type]
        content_excerpt=f"text of {key}",
        distance=1 - sim,
        similarity=sim,
        rank_score=sim if rank is None else rank,
        namespace=ns,
    )


def _svc_hit(key: str, *, sim: float, mtype: str) -> SearchHit:
    return SearchHit(
        memory_key=key,
        memory_type=mtype,
        content_excerpt=f"text of {key}",
        distance=1.0 - sim,
        rank_score=sim,
    )


async def _no_conversations(*a: Any) -> Any:
    from server.knowledge.api.models_conversations import ConversationSearchOutput

    return ConversationSearchOutput(hits=[])


def _output(hits: list[_MemorySearchHit]) -> MemorySearchOutput:
    """A memory leg's output as the real search leaves it, settled input set."""
    out = MemorySearchOutput(results=hits, embedding_model="fake", reader_instructions="rules")
    out._settled_input = BASE
    return out


def _stub_side_legs(monkeypatch) -> None:
    """Every loom_search leg but memory and knowledge answers nothing."""

    async def nothing(*a: Any, **k: Any) -> Any:
        return []

    async def namespaces(org: str, ns: str, scope: Any) -> tuple[list[str], list[str]]:
        return [ns], []

    async def all_hits(hits: Any, *a: Any) -> Any:
        return hits

    monkeypatch.setattr(fanout, "search_profile", nothing)
    monkeypatch.setattr(fanout, "_conversation_leg", _no_conversations)
    monkeypatch.setattr(fanout, "tracker_leg", nothing)
    monkeypatch.setattr(fanout, "served_hits", all_hits)
    monkeypatch.setattr(fanout, "request_scope", lambda: None)
    monkeypatch.setattr("server.auth.fanout.namespaces_to_search", namespaces)
    monkeypatch.setattr("server.memory.app.loom_search.answer_first.precision_floor", lambda: 0.0)


async def _assemble(
    monkeypatch,
    out: MemorySearchOutput,
    *,
    explain: bool = False,
    namespace: str = "me",
    namespace_scope: Literal["this", "all_mine"] = "all_mine",
) -> dict[str, Any]:
    """The loom_search payload search_sections assembles around a memory
    leg that answers `out` (no knowledge hits) for a search addressed to
    `namespace`; `explain` keeps every row."""
    from server.knowledge.api.models_knowledge import KnowledgeSearchOutput

    async def memory_leg(inp: MemorySearchInput, *, repo_namespace: str) -> MemorySearchOutput:
        return out

    async def knowledge_leg(inp: Any, *, repo_namespace: str) -> Any:
        return KnowledgeSearchOutput(results=[], embedding_model="fake")

    _stub_side_legs(monkeypatch)
    monkeypatch.setattr(fanout, "memory_with_repo_decisions", memory_leg)
    monkeypatch.setattr(fanout, "knowledge_with_repo_docs", knowledge_leg)
    return await fanout.search_sections(
        org=ORG,
        namespace=namespace,
        agent="a",
        query=BASE.query,
        top_k=5,
        tags=[],
        namespace_scope=namespace_scope,
        recipe="",
        bodies="excerpt",
        page="",
        explain=explain,
        repo_namespace=TRUNK,
    )


def _gate(allowed: set[str]):
    async def row_in_scope(org, ns, role, *, hides_answer=True):
        return ns in allowed

    return row_in_scope


def _search(results_by_ns: dict[str, list[_MemorySearchHit]], calls: list[Any], delay=0.0):
    async def handle_memory_search(inp: MemorySearchInput) -> MemorySearchOutput:
        calls.append(inp)
        if delay:
            await asyncio.sleep(delay)
        hits = [h.model_copy() for h in results_by_ns.get(inp.namespace, [])]
        out = MemorySearchOutput(
            results=hits,
            embedding_model="fake",
            reader_block="old block" if inp.render == "reader" and hits else "",
            reader_instructions="rules" if inp.render == "reader" else "",
            namespaces_searched=[inp.namespace],
            candidates_total=len(hits),
            abstain=not hits,
            abstain_code="" if hits else "no_match",
            abstain_reason="" if hits else "no results",
            retrieval_risk="medium" if hits else "abstain",
            retrieval_confidence=0.6 if hits else 0.0,
        )
        out._settled_input = inp  # as _finish_search_output keeps it
        return out

    return handle_memory_search


BASE = MemorySearchInput(
    org=ORG,
    namespace="me",
    agent="*",
    query="why does the kj cache evict early",
    top_k=3,
    tags=["repo:kj"],
    namespace_scope="all_mine",
    render="reader",
)
PLANES = {
    # rank_score disagrees with similarity: the decision search's scale is its own
    "me": [_hit("note-a", 0.6, "me", rank=0.03), _hit("note-b", 0.2, "me", rank=0.01)],
    TRUNK: [_hit("decision:evict", 0.5, mtype="decision", rank=0.9)],
}


async def _leg(monkeypatch, repo_ns: str, calls: list[Any], planes=PLANES, inp=BASE):
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search(planes, calls))
    return await repo_decisions.memory_with_repo_decisions(inp, repo_namespace=repo_ns)


@pytest.mark.asyncio
async def test_the_repositorys_decisions_join_the_memory_section_by_similarity(
    monkeypatch,
) -> None:
    calls: list[MemorySearchInput] = []
    out = await _leg(monkeypatch, TRUNK, calls)
    assert [(h.memory_key, h.namespace) for h in out.results] == [
        ("note-a", "me"),
        ("decision:evict", TRUNK),  # by similarity 0.5, not its rank_score 0.9
        ("note-b", "me"),
    ]
    extra = next(c for c in calls if c.namespace == TRUNK)
    assert (extra.namespace_scope, extra.memory_type, extra.agent, extra.tags) == (
        "this",
        "decision",
        "*",
        ["repo:kj"],
    )
    assert extra.retrieval_budget == "fast" and extra._merged_into_parent
    # the stored clip, so the reason the statement ends with is served
    assert extra.bodies == "full" and extra.quality_filter is False
    decision = next(h for h in out.results if h.namespace == TRUNK)
    assert decision.score_components["foreign"] == 1.0
    assert out.namespaces_searched == ["me", TRUNK]
    assert len(out.results) == 3
    # search_sections renders the block from the final section; the decision names its namespace
    block = (await _assemble(monkeypatch, out, explain=True))["memory"]["reader_block"]
    assert "[decision:evict] (decision, code-kj) text of decision:evict" in block
    assert "[note-a] (semantic) text of note-a" in block


@pytest.mark.asyncio
async def test_an_empty_home_namespace_serves_the_decision_without_abstaining(
    monkeypatch,
) -> None:
    out = await _leg(monkeypatch, TRUNK, [], planes={TRUNK: PLANES[TRUNK]})
    assert [h.memory_key for h in out.results] == ["decision:evict"]
    assert (out.abstain, out.abstain_code, out.abstain_reason) == (False, "", "")
    assert (out.retrieval_risk, out.retrieval_confidence) == ("medium", 0.6)
    block = (await _assemble(monkeypatch, out))["memory"]["reader_block"]
    assert block.startswith("[undated] [decision:evict] (decision, code-kj)")


def test_the_merge_keeps_one_hit_per_namespace_and_key() -> None:
    base = [_hit("a", 0.8, "me"), _hit("same", 0.5, TRUNK), _hit("b", 0.3, "")]
    extra = [_hit("same", 0.9, TRUNK), _hit("d", 0.6, TRUNK), _hit("b", 0.7, TRUNK)]
    out, added = repo_decisions.merge_memory_hits(base, extra, "me")
    assert [h.memory_key for h in added] == ["b", "d"]
    assert [(h.memory_key, h.namespace, h.similarity) for h in out] == [
        ("a", "me", 0.8),
        ("b", TRUNK, 0.7),  # another namespace's `b` is another row
        ("d", TRUNK, 0.6),
        ("same", TRUNK, 0.5),  # the base copy stays, the extra one is dropped
        ("b", "", 0.3),
    ]


@pytest.mark.asyncio
async def test_no_repository_runs_only_the_base_search(monkeypatch) -> None:
    calls: list[MemorySearchInput] = []
    out = await _leg(monkeypatch, "", calls)
    assert [h.memory_key for h in out.results] == ["note-a", "note-b"]
    assert out.reader_block == "old block"
    assert [c.namespace for c in calls] == ["me"]


@pytest.mark.asyncio
async def test_the_decision_pass_serves_keys_only_when_the_caller_asks_for_none(
    monkeypatch,
) -> None:
    calls: list[MemorySearchInput] = []
    await _leg(monkeypatch, TRUNK, calls, inp=BASE.model_copy(update={"bodies": "none"}))
    assert next(c for c in calls if c.namespace == TRUNK).bodies == "none"


@pytest.mark.asyncio
async def test_the_junk_filter_does_not_run_over_the_repositorys_decisions(monkeypatch) -> None:
    calls: list[MemorySearchInput] = []
    await _leg(monkeypatch, TRUNK, calls, inp=BASE.model_copy(update={"quality_filter": True}))
    by_ns = {c.namespace: c.quality_filter for c in calls}
    assert by_ns == {"me": True, TRUNK: False}


@pytest.mark.asyncio
async def test_a_failing_base_search_cancels_the_decision_pass(monkeypatch) -> None:
    cancelled = asyncio.Event()

    async def handle_memory_search(inp: MemorySearchInput) -> MemorySearchOutput:
        if inp.namespace != TRUNK:
            await asyncio.sleep(0)
            raise RuntimeError("base failed")
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("not cancelled")

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", handle_memory_search)
    with pytest.raises(RuntimeError, match="base failed"):
        await repo_decisions.memory_with_repo_decisions(BASE, repo_namespace=TRUNK)
    await asyncio.wait_for(cancelled.wait(), 1.0)


@pytest.mark.asyncio
async def test_a_failing_slow_or_abstaining_decision_search_leaves_the_base(
    monkeypatch,
) -> None:
    base_search = _search(PLANES, [])

    async def trunk_fails(inp: MemorySearchInput) -> MemorySearchOutput:
        if inp.namespace == TRUNK:
            raise RuntimeError("search failed")
        return await base_search(inp)

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", trunk_fails)
    out = await repo_decisions.memory_with_repo_decisions(BASE, repo_namespace=TRUNK)
    assert [h.memory_key for h in out.results] == ["note-a", "note-b"]
    assert out.reader_block == "old block"
    # the decision search does not answer in time
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_repo_decisions_timeout_s", 0.01)
    monkeypatch.setattr(
        "server.memory.app.search.handle_memory_search", _search(PLANES, [], delay=0.05)
    )
    assert await repo_decisions.search_repo_decisions(BASE, TRUNK) is None
    # the decision search abstains: its hits are not an answer
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search({}, []))
    assert await repo_decisions.search_repo_decisions(BASE, TRUNK) is None


@pytest.mark.asyncio
async def test_the_decision_search_is_merged_llm_free_and_carries_the_tokens_read_scope(
    monkeypatch,
) -> None:
    """The input the store would receive, through the real request resolver:
    the token's user pin applies, the namespace's retrieval defaults cannot
    turn a model call back on, and the pass is marked as merged."""
    from server.memory.app import namespace_retrieval_defaults as ns_defaults
    from server.memory.app.search.query_plan import _is_llm_free
    from server.memory.app.search.request import _resolve_search_input

    async def code_ns_defaults(org: str, namespace: str) -> dict[str, Any]:
        # top_k is a namespace-default knob; legs and memory_type are not
        return {"search_mode": "hybrid", "auto_touch": True, "top_k": 50}

    monkeypatch.setattr(ns_defaults, "resolve", code_ns_defaults)
    resolved: list[MemorySearchInput] = []

    async def handle_memory_search(inp: MemorySearchInput) -> MemorySearchOutput:
        settled, *_ = await _resolve_search_input(inp)
        resolved.append(settled)
        return await _search(PLANES, [])(inp)

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", handle_memory_search)
    pinned = AuthInfo(
        subject="sa:bot",
        org=ORG,
        namespaces=("me",),
        roles=(),
        is_service_account=True,
        read_user_id="alice",
    )
    unresolvable = AuthInfo(
        subject="sa:bot",
        org=ORG,
        namespaces=("me",),
        roles=(),
        is_service_account=True,
        read_scope="own_user",
    )
    try:
        set_current_auth(pinned)
        readmit = BASE.model_copy(update={"floor_mode": "readmit"})
        assert await repo_decisions.search_repo_decisions(readmit, TRUNK) is not None
        (settled,) = resolved
        assert (settled.user_id, settled.read_scope) == ("alice", "own_user")
        assert (settled.search_mode, settled.hyde, settled.rerank) == ("semantic", False, "none")
        # the defaults fill only what the pass left unset
        fetch = per_key_fetch(BASE.top_k)
        assert (settled.top_k, settled.legs, settled.memory_type) == (fetch, ["memory"], "decision")
        assert settled.floor_mode == "drop"
        assert _is_llm_free(settled) and settled._merged_into_parent
        # an own_user token that names no user is refused, and the leg answers nothing
        set_current_auth(unresolvable)
        assert await repo_decisions.search_repo_decisions(BASE, TRUNK) is None
    finally:
        set_current_auth(None)


@pytest.mark.asyncio
async def test_only_the_merged_decisions_are_credited_once(monkeypatch) -> None:
    credited: list[tuple[str, int, list[str]]] = []

    async def credit(inp: MemorySearchInput, hits: list[_MemorySearchHit]) -> None:
        credited.append((inp.namespace, inp.top_k, [h.memory_key for h in hits]))

    monkeypatch.setattr("server.memory.app.search.telemetry.credit_merged_hits", credit)
    planes = {
        # the home fan-out already read decision:seen; its own search credits it
        "me": [_hit("note-a", 0.6, "me"), _hit("decision:seen", 0.55, TRUNK)],
        TRUNK: [
            _hit("decision:kept", 0.58, mtype="decision"),
            _hit("decision:seen", 0.55, mtype="decision"),
        ],
    }
    out = await _leg(monkeypatch, TRUNK, [], planes=planes)
    assert [h.memory_key for h in out.results] == ["note-a", "decision:kept", "decision:seen"]
    # with the caller's top_k, not the pass's over-fetch
    assert credited == [(TRUNK, BASE.top_k, ["decision:kept"])]


@pytest.mark.asyncio
async def test_candidates_total_counts_the_merged_decisions(monkeypatch) -> None:
    async def search(inp: MemorySearchInput) -> MemorySearchOutput:
        out = await _search(PLANES, [])(inp)
        if inp.namespace == "me":
            out.candidates_total = 10
        return out

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", search)
    out = await repo_decisions.memory_with_repo_decisions(BASE, repo_namespace=TRUNK)
    assert out.candidates_total == 11  # the home pool's count plus the decision


@pytest.mark.asyncio
async def test_merged_hits_get_exposure_and_recall_credit_from_the_existing_writers(
    monkeypatch,
) -> None:
    from server.memory.app import after_write
    from server.memory.app.search import telemetry

    exposed: list[list[str]] = []
    usage: list[dict[str, list[str]]] = []
    monkeypatch.setattr(
        telemetry,
        "_schedule_exposure",
        lambda inp, hits: exposed.append([h.memory_key for h in hits]),
    )

    async def credited(inp: Any) -> Any:
        return SimpleNamespace(credited=True, disclosure=None)

    monkeypatch.setattr(telemetry, "_recall_credit_decision", credited)

    async def record(org, ns_keys, **kw):
        usage.append(ns_keys)

    monkeypatch.setattr("server.memory.store.usage.record_usage_for_keys", record)
    pending: list[Any] = []
    monkeypatch.setattr(after_write, "schedule", lambda coro, name="": pending.append(coro))
    inp = BASE.model_copy(update={"namespace": TRUNK})
    await credit_merged_hits(inp, [_hit("decision:evict", 0.5, TRUNK)])
    for coro in pending:
        await coro
    assert exposed == [["decision:evict"]]
    assert usage == [{TRUNK: ["decision:evict"]}]


@pytest.mark.asyncio
async def test_capture_consent_is_narrowed_to_the_repository_before_any_leg(
    monkeypatch,
) -> None:
    import server.memory.app.loom_search.handler as handler
    from server.memory.api.models_loom_search import SearchInput

    order: list[str] = []

    async def namespaces_to_search(org, ns, scope):
        return ["me", "mine-2"], []

    async def repo_ns(org):
        return TRUNK

    async def narrow_to(org, namespaces):
        order.append(f"narrow:{','.join(namespaces)}")

    async def search_sections(**kw):
        order.append(f"search:{kw['repo_namespace']}")
        return {
            "namespaces_skipped": [],
            "memory": {"results": []},
            "knowledge": {"results": []},
            "profile": {"results": []},
            "conversations": {"results": []},
        }

    monkeypatch.setattr("server.auth.fanout.namespaces_to_search", namespaces_to_search)
    monkeypatch.setattr(handler, "repo_namespace_in_scope", repo_ns)
    monkeypatch.setattr("server.observability.capture_consent.narrow_to", narrow_to)
    monkeypatch.setattr(handler, "search_sections", search_sections)
    await handler.handle_search(SearchInput(org=ORG, namespace="me", agent="a", query="why"))
    # in the request's own context, so every leg's task inherits the level
    assert order == [f"narrow:me,mine-2,{TRUNK}", f"search:{TRUNK}"]


@pytest.mark.asyncio
async def test_decisions_join_above_the_ceiling_the_base_search_applied(monkeypatch) -> None:
    """The base search's own cut stands and nothing is cut after the merge."""

    async def ceiling_search(inp: MemorySearchInput) -> MemorySearchOutput:
        out = await _search(PLANES, [])(inp)
        if inp.namespace == "me":
            out.top_k_ceiling = 1
            out.results = out.results[:1]
            out.candidates_total = 2
        return out

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", ceiling_search)
    out = await repo_decisions.memory_with_repo_decisions(BASE, repo_namespace=TRUNK)
    assert [h.memory_key for h in out.results] == ["note-a", "decision:evict"]
    assert out.candidates_total == 3


def test_a_decision_below_every_home_hit_goes_last() -> None:
    base = [_hit("a", 0.8, "me", rank=0.01), _hit("b", 0.7, "me", rank=0.01)]
    extra = [_hit("d", 0.69, TRUNK, rank=0.99)]
    out, added = repo_decisions.merge_memory_hits(base, extra, "me")
    assert [h.memory_key for h in out] == ["a", "b", "d"] and added == extra


@pytest.mark.asyncio
async def test_a_decision_the_home_fan_out_already_read_returns_the_base_unchanged(
    monkeypatch,
) -> None:
    """The home fan-out may read the trunk namespace itself (a deployment
    that fans out over code namespaces): the decision search finds the same
    row, the dedupe drops it, and nothing else changes."""
    planes = {
        "me": [_hit("note-a", 0.6, "me"), _hit("decision:evict", 0.5, TRUNK, "decision")],
        TRUNK: [_hit("decision:evict", 0.5, mtype="decision")],
    }
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search(planes, []))
    base_alone = await _search(planes, [])(BASE)
    out = await repo_decisions.memory_with_repo_decisions(BASE, repo_namespace=TRUNK)
    assert out.model_dump() == base_alone.model_dump()


@pytest.mark.asyncio
async def test_merged_decisions_are_credited_with_the_input_the_pass_ran(monkeypatch) -> None:
    """Exposure carries the token's user pin, and a namespace default that
    turns auto_touch off writes no recall credit."""
    from server.memory.app import after_write
    from server.memory.app import namespace_retrieval_defaults as ns_defaults
    from server.memory.app.search import telemetry
    from server.memory.app.search.request import _resolve_search_input

    monkeypatch.setattr(telemetry, "credit_merged_hits", credit_merged_hits)

    async def code_ns_defaults(org: str, namespace: str) -> dict[str, Any]:
        return {"auto_touch": False} if namespace == TRUNK else {}

    monkeypatch.setattr(ns_defaults, "resolve", code_ns_defaults)

    async def handle_memory_search(inp: MemorySearchInput) -> MemorySearchOutput:
        settled, *_ = await _resolve_search_input(inp)
        return await _search(PLANES, [])(settled)

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", handle_memory_search)
    exposures: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "server.memory.app.exposure_ledger.record_exposure", lambda **kw: exposures.append(kw)
    )

    async def credited(inp: Any) -> Any:
        return SimpleNamespace(credited=True, disclosure=None)

    monkeypatch.setattr(telemetry, "_recall_credit_decision", credited)
    scheduled: list[Any] = []
    monkeypatch.setattr(after_write, "schedule", lambda coro, name="": scheduled.append(coro))
    set_current_auth(
        AuthInfo(
            subject="sa:bot",
            org=ORG,
            namespaces=("me",),
            roles=(),
            is_service_account=True,
            read_user_id="alice",
        )
    )
    try:
        out = await repo_decisions.memory_with_repo_decisions(BASE, repo_namespace=TRUNK)
    finally:
        set_current_auth(None)
    assert "decision:evict" in [h.memory_key for h in out.results]
    (row,) = exposures
    assert (row["namespace"], row["keys"]) == (TRUNK, ["decision:evict"])
    assert (row["user_id"], row["read_scope"]) == ("alice", "own_user")
    assert scheduled == []  # the code namespace turned auto_touch off


@pytest.mark.asyncio
async def test_the_real_search_serves_only_decision_rows_from_the_repository(
    search_env, monkeypatch
) -> None:
    """Through the real memory.search: the session-digest leg does not run,
    and a non-decision row or a computed aggregate from the pass is dropped."""
    import server.memory.app.retrieval as retrieval
    from server.memory.app.search import computed

    search_env.hits_by_ns[TRUNK] = [
        _svc_hit("decision:evict", sim=0.9, mtype="decision"),
        _svc_hit("note", sim=0.8, mtype="semantic"),  # the fake leg ignores memory_type
    ]
    digests: list[str] = []

    async def fetch_session_summaries(ch: Any, *, namespace: str, **kw: Any) -> list[Any]:
        digests.append(namespace)
        return []

    async def aggregate(ctx: Any) -> dict[str, Any]:
        return {"op": "count", "count": 2, "items": []}

    monkeypatch.setattr(retrieval, "fetch_session_summaries", fetch_session_summaries)
    monkeypatch.setattr(computed, "_maybe_derived_aggregate", aggregate)
    inp = BASE.model_copy(update={"org": "o", "render": "", "tags": []})
    found = await repo_decisions.search_repo_decisions(inp, TRUNK)
    assert found is not None
    out, settled = found
    assert [(h.memory_key, h.memory_type, h.namespace) for h in out.results] == [
        ("decision:evict", "decision", TRUNK)
    ]
    assert digests == []
    assert settled.legs == ["memory"]


@pytest.mark.asyncio
async def test_an_empty_repository_is_searched_once(search_env, monkeypatch) -> None:
    """The merged pass runs no read-your-writes re-read: an empty trunk is
    not searched again with sequential consistency. The trade-off: a decision
    written moments ago through another replica can be missed until
    replication catches up (only the up-front write-mark check applies)."""
    from server.settings import settings

    monkeypatch.setattr(settings, "ch_read_your_writes", True)
    inp = BASE.model_copy(update={"org": "o", "render": "", "tags": []})
    assert await repo_decisions.search_repo_decisions(inp, TRUNK) is None
    assert [q.namespace for q in search_env.search_queries] == [TRUNK]


@pytest.mark.asyncio
async def test_a_later_page_keeps_the_repository_docs_and_skips_the_decisions(
    monkeypatch,
) -> None:
    """The knowledge leg re-runs on every page and keeps the repository's
    documents, so the gate and the consent narrowing run on a page too; the
    decision leg does not run, a later page continuing the home search."""
    import server.memory.app.loom_search.handler as handler
    from server.knowledge.api.models_knowledge import KnowledgeSearchOutput, _SearchHit
    from server.memory.api.models_loom_search import SearchInput

    narrowed: list[list[str]] = []
    legs: dict[str, str] = {}

    async def namespaces_to_search(org, ns, scope):
        return ["me"], []

    async def repo_ns(org):
        return TRUNK

    async def narrow_to(org, namespaces):
        narrowed.append(list(namespaces))

    async def memory_leg(inp: MemorySearchInput, *, repo_namespace: str) -> MemorySearchOutput:
        legs["memory"] = repo_namespace
        return MemorySearchOutput(results=[], embedding_model="fake")  # a parked page

    async def knowledge_leg(inp: Any, *, repo_namespace: str) -> Any:
        legs["knowledge"] = repo_namespace
        doc = _SearchHit(
            source_uri="doc://eviction",
            chunk_idx=0,
            content="why the cache evicts",
            tags=[],
            distance=0.1,
            similarity=0.9,
            namespace=repo_namespace,
            score=0.9,
        )
        return KnowledgeSearchOutput(results=[doc] if repo_namespace else [], embedding_model="f")

    monkeypatch.setattr("server.auth.fanout.namespaces_to_search", namespaces_to_search)
    monkeypatch.setattr(handler, "repo_namespace_in_scope", repo_ns)
    monkeypatch.setattr("server.observability.capture_consent.narrow_to", narrow_to)
    _stub_side_legs(monkeypatch)
    monkeypatch.setattr(fanout, "memory_with_repo_decisions", memory_leg)
    monkeypatch.setattr(fanout, "knowledge_with_repo_docs", knowledge_leg)
    out = await handler.handle_search(
        SearchInput(org=ORG, namespace="me", agent="a", query="why", page="p2")
    )
    assert narrowed == [["me", TRUNK]]
    assert legs == {"memory": "", "knowledge": TRUNK}
    assert [h["source_uri"] for h in out.knowledge["results"]] == ["doc://eviction"]


@pytest.mark.asyncio
async def test_a_flagged_home_row_leaves_the_block_and_the_decision_label_stays(
    monkeypatch,
) -> None:
    flagged = _hit("note-b", 0.2, "me")
    flagged.below_threshold = True
    planes = {"me": [_hit("note-a", 0.6, "me"), flagged], TRUNK: PLANES[TRUNK]}
    out = await _leg(monkeypatch, TRUNK, [], planes=planes)
    section = (await _assemble(monkeypatch, out, explain=True))["memory"]
    assert [h["memory_key"] for h in section["results"]] == ["note-a", "decision:evict"]
    assert "[decision:evict] (decision, code-kj) text of decision:evict" in section["reader_block"]
    assert "[note-b]" not in section["reader_block"]


@pytest.mark.asyncio
async def test_a_decision_answer_first_withholds_leaves_the_mcp_text(monkeypatch) -> None:
    """A home fact leads and the decision sits outside the foreign margin:
    answer_first withholds it, and the reader block the agent reads, rendered
    from the final rows, does not carry it."""
    from server.transport import mcp_tools

    out = _output([_hit("home_fact", 0.8, "me"), _hit("decision:evict", 0.5, TRUNK, "decision")])
    payload = await _assemble(monkeypatch, out)
    assert payload["withheld"]["memory"] == 1
    text = mcp_tools.search_answer(payload, {"query": BASE.query}, "me", "s1")["content"][0]["text"]
    assert "[home_fact]" in text and "decision:evict" not in text


@pytest.mark.asyncio
async def test_an_excerpt_quoting_another_rows_head_cannot_carry_that_row(monkeypatch) -> None:
    """The block is rendered from the served rows, never parsed back: a
    served row whose excerpt quotes a withheld row's exact paragraph start
    does not bring the withheld row back, and keeps its own text whole."""
    withheld = _hit("decision:evict", 0.5, TRUNK, "decision")
    quoted = render_reader_block([withheld], BASE.query, label_namespace=TRUNK)
    lead = _hit("home_fact", 0.8, "me")
    lead.content_excerpt = f"see below\n\n{quoted}"
    payload = await _assemble(monkeypatch, _output([lead, withheld]))
    assert [h["memory_key"] for h in payload["memory"]["results"]] == ["home_fact"]
    assert payload["memory"]["reader_block"] == render_reader_block(
        [lead], BASE.query, label_namespace=TRUNK
    )


@pytest.mark.asyncio
# production stores cites_fact (a key, not a float) in score_components; pydantic warns on the dump
@pytest.mark.filterwarnings("ignore:Pydantic serializer warnings")
async def test_restatement_notes_are_recomputed_against_the_served_rows(monkeypatch) -> None:
    """A chunk that restates a fact is noted only while that fact is served;
    the note reads score_components, which the search models keep even when
    the wire elides them."""
    fact = _hit("where_i_live", 0.8, "me")
    chunk = _hit("chunk:s1:0", 0.79, "me")
    chunk.source = "chunk"
    chunk.score_components = {"cites_fact": "where_i_live"}
    chunk._elide = frozenset({"score_components"})  # explain elision is the wire's only
    both = (await _assemble(monkeypatch, _output([fact, chunk])))["memory"]["reader_block"]
    assert "(restates [where_i_live]" in both
    flagged = fact.model_copy()
    flagged.below_threshold = True  # the calibration flags the fact; the chunk stays
    alone = (await _assemble(monkeypatch, _output([flagged, chunk])))["memory"]["reader_block"]
    assert "[chunk:s1:0]" in alone and "restates" not in alone


@pytest.mark.asyncio
async def test_a_decision_pushes_no_home_match_out(monkeypatch) -> None:
    """A home row pushed past top_k would be served on no page (the parked
    tail starts after the home search's own rows): decisions ride above it."""
    from server.transport import mcp_tools

    planes = {
        "me": [_hit("note-a", 0.6, "me"), _hit("note-b", 0.2, "me")],
        TRUNK: [_hit("decision:evict", 0.5, mtype="decision")],
    }
    out = await _leg(
        monkeypatch, TRUNK, [], planes=planes, inp=BASE.model_copy(update={"top_k": 2})
    )
    assert [h.memory_key for h in out.results] == ["note-a", "decision:evict", "note-b"]
    assert out.candidates_total == 3 and out.next_page == ""
    section = out.model_dump()
    text = "\n".join(mcp_tools._memory_section_lines(section, section["results"], top_k=2))
    assert "next_page serves the rest" not in text


def test_a_decision_goes_after_the_last_home_hit_that_matched_better() -> None:
    """The home list is in rank order, not falling similarity: a decision
    goes after the last home hit at least as similar, never before one."""
    base = [_hit("a", 0.8, "me"), _hit("b", 0.3, "me"), _hit("c", 0.6, "me")]
    out, added = repo_decisions.merge_memory_hits(base, [_hit("d", 0.5, TRUNK)], "me")
    assert [h.memory_key for h in out] == ["a", "b", "c", "d"]
    out, _ = repo_decisions.merge_memory_hits(base, [_hit("e", 0.7, TRUNK)], "me")
    assert [h.memory_key for h in out] == ["a", "e", "b", "c"]


def test_answer_first_keeps_the_row_dicts_it_is_given(monkeypatch) -> None:
    """The block is re-rendered from each served row's model, found by the
    row's id: every trimming step must hand back the dicts it was given."""
    from server.memory.app.loom_search import answer_first as af
    from server.tracker.domain.defect_answer import VERDICT_MEMORY_MAX

    monkeypatch.setattr(af, "precision_floor", lambda: 0.3)

    def served_are_given(payload: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
        served = af.answer_first(payload, "me", explain=False)["memory"]["results"]
        assert served and all(any(h is r for r in rows) for h in served)
        return [h["memory_key"] for h in served]

    # a home fact leads: foreign rows outside the margin go, one chunk stays
    # behind it (cap_chunks_behind_fact), a row under the floor goes
    chunks = [_hit(f"chunk:{i}", 0.78, "me") for i in range(3)]
    for c in chunks:
        c.source = "chunk"
    rows = [
        h.model_dump()
        for h in [
            _hit("home_fact", 0.8, "me"),
            _hit("decision:evict", 0.5, TRUNK, "decision"),
            *chunks,
            _hit("faint", 0.1, "me"),  # apply_precision_floor
        ]
    ]
    payload = {"memory": {"results": list(rows)}, "knowledge": {"results": []}}
    assert served_are_given(payload, rows) == ["home_fact", "chunk:0"]

    # a tracker verdict leads: Loom's own output is dropped (drop_loom_output)
    # and the memory section shrinks to VERDICT_MEMORY_MAX rows (verdict_leads)
    quoting = _hit("echo", 0.9, "me")
    quoting.content_excerpt = "Loom recall for this prompt: the cache evicts early"
    many = [_hit(f"row:{i}", 0.9 - i / 100, "me") for i in range(VERDICT_MEMORY_MAX + 2)]
    rows = [h.model_dump() for h in [quoting, *many]]
    payload = {
        "tracker": {"verdict": "likely_duplicate"},
        "memory": {"results": list(rows)},
        "knowledge": {"results": []},
        "conversations": {"results": []},
    }
    kept = served_are_given(payload, rows)
    assert "echo" not in kept and len(kept) == VERDICT_MEMORY_MAX


@pytest.mark.asyncio
async def test_a_search_addressed_to_the_repository_labels_nothing(monkeypatch) -> None:
    out = _output([_hit("decision:evict", 0.8, TRUNK, "decision")])
    payload = await _assemble(
        monkeypatch, out, explain=True, namespace=TRUNK, namespace_scope="this"
    )
    assert payload["memory"]["reader_block"].startswith("[undated] [decision:evict] (decision) ")


def test_every_served_row_gets_a_why_line_above_top_k() -> None:
    """Decisions ride above top_k, so the explain lines cover the whole section."""
    from server.transport import mcp_tools

    rows = [
        {"memory_key": "note-a", "explanation": "home"},
        {"memory_key": "decision:evict", "explanation": "repository decision"},
    ]
    section = {"results": rows, "reader_block": "[d] [note-a] a\n\n[d] [decision:evict] b"}
    text = "\n".join(mcp_tools._memory_section_lines(section, rows, top_k=1, explain=True))
    assert "[decision:evict] why: repository decision" in text


@pytest.mark.asyncio
async def test_two_rows_of_one_decision_key_count_as_one_decision(monkeypatch) -> None:
    from server.settings import settings

    # the search serves a row per writer and session: one key can fill two places
    monkeypatch.setattr(settings, "memory_search_per_key_cap", 2)
    rows = [
        _hit("decision:evict", 0.9, mtype="decision"),
        _hit("decision:evict", 0.8, mtype="decision"),
        _hit("decision:shard", 0.7, mtype="decision"),
        _hit("decision:ttl", 0.6, mtype="decision"),
    ]
    calls: list[Any] = []
    monkeypatch.setattr(
        "server.memory.app.search.handle_memory_search", _search({TRUNK: rows}, calls)
    )
    got = await repo_decisions.search_repo_decisions(BASE.model_copy(update={"top_k": 2}), TRUNK)
    assert got is not None
    assert [h.memory_key for h in got[0].results] == ["decision:evict", "decision:shard"]
    assert calls[0].top_k == 4
