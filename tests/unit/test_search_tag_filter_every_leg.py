"""The hard tag filter holds on every fused leg.

Under `search_mode=rrf` the anchor pool, the key-match arm, the digest
descent and the query-neighbour leg carry the fact leg's `hasAny(e.tags, …)`
too: every fact-table leg spells the same predicate from one helper and selects the row's tags; a post-fusion belt
drops any memory row that still lacks the filter and says so on
`retrieval_work.tag_filter_dropped`. A request with no hard filter — or a
soft one — issues the statements it always did and gets the payload it
always got.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import server.memory.app.search_tag_filter as tag_belt
from server.core.tag_sql import TagFilter, tag_predicate
from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search import assemble, payload
from server.memory.app.search.assemble import _drop_excluded_and_duplicates
from server.memory.app.search.payload import _fill_hit_tags
from server.memory.app.search.sql import (
    _anchor_pool_where,
    _digest_descent_where,
    _key_match_where,
    anchor_pool_sql,
    digest_descent_sql,
    key_match_component_sql,
    key_match_sql,
)
from server.memory.app.search.tiers import _prepend_anchor, _prepend_recent
from server.memory.app.search.vector_legs import _stamp_leg_tags
from server.memory.app.search_tag_filter import stamp_row_tags, tagged_row_hit
from server.memory.domain.search_sql import _build_where, hard_tag_filter
from server.memory.domain.search_types import SearchQuery

TAG = "hasAny(e.tags, {tags:Array(String)})"


def _inp(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "agent": "*",
        "run_id": "",
        "tags": [],
        "tags_soft": False,
        "include_duplicates": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------- helper


def test_hard_tag_filter_is_the_request_tags_unless_soft() -> None:
    assert hard_tag_filter(["a", "", "b"], False) == ["a", "b"]
    assert hard_tag_filter(["a"], True) == []
    assert hard_tag_filter(None, False) == []
    assert hard_tag_filter([], False) == []


def test_tag_predicate_spells_the_fact_leg_clause() -> None:
    assert tag_predicate("e") == TAG
    assert tag_predicate("") == "hasAny(tags, {tags:Array(String)})"
    # the fact leg's own WHERE renders the identical text (shapes unchanged)
    where, params = _build_where(
        SearchQuery(
            org="o",
            namespace="n",
            scope="private",
            agent="a",
            embedding_model="m",
            query_vec=[0.0],
            top_k=5,
            tags=["x"],
        )
    )
    assert TAG in where and params["tags"] == ["x"]


# ----------------------------------------------------------------- the legs


def test_anchor_pool_where_carries_the_hard_tag_filter() -> None:
    params: dict[str, Any] = {}
    parts = _anchor_pool_where(_inp(tags=["subsystem:Storages"]), params)
    assert TAG in parts and params["tags"] == ["subsystem:Storages"]
    params = {}
    parts = _anchor_pool_where(_inp(tags=["subsystem:Storages"], tags_soft=True), params)
    assert TAG not in parts and "tags" not in params
    params = {}
    parts = _anchor_pool_where(_inp(), params)
    assert TAG not in parts and "tags" not in params
    # the statement selects the row's tags so the belt needs no second read
    assert "e.tags AS tags" in anchor_pool_sql(" AND ".join(parts))


def test_key_match_where_carries_the_hard_tag_filter_and_the_statements_select_tags() -> None:
    params: dict[str, Any] = {}
    where = _key_match_where(_inp(tags=["kind:lesson"], agent="bot", run_id="r1"), params)
    assert TAG in where and params["tags"] == ["kind:lesson"]
    assert "(e.agent = {agent:String} OR e.scope = 'shared')" in where and params["agent"] == "bot"
    assert "e.run_id = {run_id:String}" in where and params["run_id"] == "r1"
    params = {}
    assert TAG not in _key_match_where(_inp(tags=["kind:lesson"], tags_soft=True), params)
    assert TAG not in _key_match_where(_inp(), params)
    where_sql = " AND ".join(where)
    assert "e.tags AS tags" in key_match_sql(where_sql)
    # the component statement's inner projection must expose `tags` for the
    # outer predicate to read
    comp = key_match_component_sql(where_sql)
    assert "tombstoned_at, status, tags," in comp and TAG in comp


def test_digest_descent_where_carries_the_hard_tag_filter() -> None:
    params: dict[str, Any] = {}
    where = _digest_descent_where(_inp(tags=["t1", "t2"], agent="bot"), params)
    assert "hasAny(tags, {tags:Array(String)})" in where and params["tags"] == ["t1", "t2"]
    assert "(agent = {agent:String} OR scope = 'shared')" in where
    params = {}
    where = _digest_descent_where(_inp(), params)
    assert not any("hasAny(tags" in w for w in where) and "tags" not in params
    sql = digest_descent_sql(" AND ".join(where))
    assert "toString(created_at) AS created_at,\n                       tags" in sql


@pytest.mark.asyncio
async def test_lexical_union_pool_selects_the_rows_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app.retrieval import side_legs as ms

    class _CH:
        def __init__(self) -> None:
            self.sql = ""

        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            self.sql = sql
            return [
                {
                    "memory_key": "k1",
                    "memory_type": "semantic",
                    "content_excerpt": "grace hash join",
                    "status": "active",
                    "version": 1,
                    "distance": 0.3,
                    "lex_score": 2.0,
                    "temporal_anchor": "",
                    "temporal_confidence": 0.0,
                    "valid_at": "",
                    "source_resource_id": "",
                    "source_class": "",
                    "verified": 0,
                    "session_id": "",
                    "tags": ["subsystem:SQL"],
                }
            ]

    ch = _CH()
    q = SearchQuery(
        org="o",
        namespace="n",
        scope="private",
        agent="a",
        embedding_model="m",
        query_vec=[0.0],
        top_k=5,
        query_text="grace hash join",
        tags=["subsystem:SQL"],
    )
    hits = await ms._lexical_recall_fallback(ch, q, limit=50, max_distance=0.9)
    assert "e.tags AS tags" in ch.sql and TAG in ch.sql
    assert hits[0].tags == ["subsystem:SQL"]


# ------------------------------------------------------------------ the belt


def _hit(key: str, source: str = "fact", row_tags: list[str] | None = None) -> _MemorySearchHit:
    hit = _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=key,
        distance=0.2,
        similarity=0.8,
        rank_score=0.8,
        source=source,
        namespace="ns",
    )
    if row_tags is not None:
        stamp_row_tags(hit, row_tags)
    return hit


def test_row_tags_ride_privately_and_never_on_the_wire() -> None:
    hit = _hit("k", row_tags=["a"])
    assert hit._row_tags == ("a",)
    assert "tags" not in hit.model_dump() and "_row_tags" not in hit.model_dump()
    assert tagged_row_hit(_hit("k", "fact")) and tagged_row_hit(_hit("k", "anchor"))
    for source in (
        "chunk",
        "summary",
        "entity_summary",
        "observation",
        "derived",
        "computed",
        "record",
    ):
        assert not tagged_row_hit(_hit("k", source))
    assert not tagged_row_hit(_hit("chunk:s:1", "fact"))


@pytest.mark.asyncio
async def test_belt_is_a_no_op_without_a_hard_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(*a: Any, **k: Any) -> dict[Any, Any]:
        raise AssertionError("no read without a hard filter")

    monkeypatch.setattr(tag_belt, "read_row_tags", _boom)
    hits = [_hit("k1"), _hit("k2", row_tags=["x"])]
    work: dict[str, Any] = {}
    assert await tag_belt.enforce_hard_tag_filter(hits, _inp(), work, ["ns"]) is hits
    assert work == {}
    assert (
        await tag_belt.enforce_hard_tag_filter(hits, _inp(tags=["x"], tags_soft=True), work, ["ns"])
        is hits
    )
    assert work == {}


@pytest.mark.asyncio
async def test_belt_drops_a_fused_row_lacking_the_tag_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[list[str]] = []

    async def _read(org: str, nss: list[str], keys: list[str]) -> dict[tuple[str, str], list[str]]:
        reads.append(sorted(keys))
        return {
            ("ns", "unknown_ok"): ["subsystem:Storages"],
            ("ns", "unknown_leak"): ["subsystem:General"],
        }

    monkeypatch.setattr(tag_belt, "read_row_tags", _read)
    hits = [
        _hit("fact_ok", row_tags=["subsystem:Storages", "kind:finding"]),
        _hit("anchor_leak", "anchor", row_tags=["subsystem:General"]),
        _hit("unknown_ok"),  # no leg stamped it: point-read
        _hit("unknown_leak"),
        _hit("chunk:s:1", "chunk"),  # not a tagged row: never judged
        _hit("summary:s", "summary"),
    ]
    work: dict[str, Any] = {}
    kept = await tag_belt.enforce_hard_tag_filter(
        hits, _inp(tags=["subsystem:Storages"]), work, ["ns"]
    )
    assert [x.memory_key for x in kept] == ["fact_ok", "unknown_ok", "chunk:s:1", "summary:s"]
    assert work == {"tag_filter_dropped": 2}
    # only the unstamped rows were read, once
    assert reads == [["unknown_leak", "unknown_ok"]]
    # the read stamped them, so a second pass reads nothing
    kept2 = await tag_belt.enforce_hard_tag_filter(
        kept, _inp(tags=["subsystem:Storages"]), work, ["ns"]
    )
    assert len(reads) == 1 and kept2 == kept and work == {"tag_filter_dropped": 2}


@pytest.mark.asyncio
async def test_belt_fails_open_on_the_read_and_counts_the_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _read(*a: Any, **k: Any) -> dict[tuple[str, str], list[str]]:
        raise RuntimeError("ch down")

    monkeypatch.setattr(tag_belt, "read_row_tags", _read)
    hits = [_hit("stamped_leak", row_tags=["other"]), _hit("unknown")]
    work: dict[str, Any] = {}
    kept = await tag_belt.enforce_hard_tag_filter(hits, _inp(tags=["x"]), work, ["ns"])
    assert [x.memory_key for x in kept] == ["unknown"]
    assert work == {"tag_filter_dropped": 1, "tag_filter_unverified": 1}


# -------------------------------------------------- the wire hits are stamped


def test_leg_hits_are_stamped_with_the_rows_tags() -> None:
    leg = SimpleNamespace(tags=["a", "b"])
    wire = _stamp_leg_tags(leg, _hit("k", "fact"))
    assert wire._row_tags == ("a", "b")
    # a chunk hit reads no tags and stays unstamped
    assert _stamp_leg_tags(leg, _hit("chunk:s:1", "chunk"))._row_tags is None
    # a leg that read an untagged row stamps the empty tuple (known, empty)
    assert _stamp_leg_tags(SimpleNamespace(tags=[]), _hit("k", "anchor"))._row_tags == ()


@pytest.mark.asyncio
async def test_include_tags_fill_uses_the_stamped_tags_without_a_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(*a: Any, **k: Any) -> dict[Any, Any]:
        raise AssertionError("stamped rows need no point read")

    monkeypatch.setattr(payload, "_read_row_tags", _boom)
    hits = [_hit("k1", row_tags=["kind:lesson"]), _hit("k2", row_tags=[])]
    filled = await _fill_hit_tags(hits, _inp(include_tags=True), ["ns"])
    assert filled == 1 and hits[0].tags == ["kind:lesson"] and hits[1].tags == []


@pytest.mark.asyncio
async def test_tiers_pass_the_hard_filter_only_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.store as repo

    calls: list[dict[str, Any]] = []

    async def fake_list(ident: Any, **kw: Any) -> list[Any]:
        calls.append(dict(kw))
        return []

    monkeypatch.setattr(repo, "list_by_namespace", fake_list)
    base = dict(org="o", namespace="ns", agent="*", user_id="-", session_id="-", run_id="", top_k=3)
    await _prepend_recent([], SimpleNamespace(recent_first=1, **base))
    assert calls[-1]["tag_filter"] is None
    await _prepend_recent([], SimpleNamespace(recent_first=1, tags=["x"], tags_soft=False, **base))
    # memory.search's exact match, not memory.list's any-case one.
    assert calls[-1]["tag_filter"] == TagFilter(("x",), "exact_any")
    await _prepend_anchor([], SimpleNamespace(anchor_first=1, tags=["x"], tags_soft=True, **base))
    assert calls[-1]["tag_filter"] is None


# ------------------------------------------ the two paths the replay found


def test_rrf_rebuild_keeps_the_rows_tags() -> None:
    """Under rrf the fused hits are rebuilt; a rebuild without `tags` would read
    as "no tags" and the belt would drop every rrf row."""
    from server.memory.app.retrieval.fusion import rrf_scores
    from server.memory.domain.search_types import SearchHit

    hit = SearchHit(
        memory_key="k",
        memory_type="semantic",
        content_excerpt="x",
        distance=0.2,
        rank_score=0.8,
        tags=["subsystem:Storages"],
        score_components={"lexical": 0.5, "semantic": 0.8},
    )
    assert rrf_scores([hit], w_lex=1.0) == [pytest.approx(2.0 / 61.0)]
    from server.memory.app.retrieval.fusion import _rerank_fact_pool
    from tests.unit.test_memory_search_service import _q

    (fused,) = _rerank_fact_pool([hit], _q(search_mode="rrf", query_text="x"))
    assert fused.tags == ["subsystem:Storages"]


def test_canonical_substitute_is_stamped_with_its_own_tags() -> None:
    """A duplicate's canonical is seated in its slot; that row came through
    no leg's WHERE (a `resolved_from` row), so the belt must judge the
    canonical's tags, not the duplicate's."""
    from server.memory.app.served_duplicates import _substitute
    from server.memory.store.dedup import CanonicalRow

    dup = _hit("lesson_dup", row_tags=["subsystem:SQL"])
    canon = _substitute(
        dup,
        CanonicalRow.from_row(
            {
                "memory_key": "finding_canon",
                "memory_type": "semantic",
                "value": "v",
                "version": 3,
                "tags": ["subsystem:General", "plane:finding"],
            }
        ),
        "ns",
    )
    assert canon.memory_key == "finding_canon" and canon.resolved_from == "lesson_dup"
    assert canon._row_tags == ("subsystem:General", "plane:finding")
    assert dup._row_tags == ("subsystem:SQL",)


@pytest.mark.asyncio
async def test_belt_runs_after_the_duplicate_substitution(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    async def fake_hide(ctx: Any) -> None:
        order.append("hide")
        ctx.all_hits = [_hit("finding_canon", row_tags=["subsystem:General"])] + ctx.all_hits

    async def fake_belt(
        hits: list[Any], input: Any, work: dict[str, Any], nss: list[str]
    ) -> list[Any]:
        order.append("belt")
        assert [x.memory_key for x in hits] == ["finding_canon", "k1"]
        return [x for x in hits if x.memory_key != "finding_canon"]

    monkeypatch.setattr(assemble, "hide_pool_duplicates", fake_hide)
    monkeypatch.setattr(assemble, "_enforce_hard_tag_filter", fake_belt)
    monkeypatch.setattr(assemble, "_drop_excluded_keys", lambda ctx: order.append("exclude"))
    ctx = SimpleNamespace(
        all_hits=[_hit("k1", row_tags=["subsystem:SQL"])],
        input=_inp(tags=["subsystem:SQL"]),
        actual_work={},
        namespaces=["ns"],
    )
    await _drop_excluded_and_duplicates(ctx)
    assert order == ["exclude", "hide", "belt"]
    assert [x.memory_key for x in ctx.all_hits] == ["k1"]


@pytest.mark.asyncio
async def test_ask_injection_legs_are_held_to_the_hard_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ask's own legs (identifier injection, bridge completion, entity
    follow, mention chunks, time-window turns) prepend rows read by facet,
    substring or session, never by the request's tags; the belt runs after
    the last of them (one stage earlier lets untagged rows through)."""
    from server.memory.app import ask as ma
    from server.memory.app.ask import abstain, pool

    reads: list[list[str]] = []

    async def _read(org: str, nss: list[str], keys: list[str]) -> dict[tuple[str, str], list[str]]:
        reads.append(sorted(keys))
        return {
            ("ns", "injected_ok"): ["subsystem:MergeTree"],
            ("ns", "injected_leak"): ["kind:observation"],
        }

    monkeypatch.setattr(tag_belt, "read_row_tags", _read)
    ranked = [
        _hit("injected_ok"),
        _hit("injected_leak"),
        _hit("fact_ok", row_tags=["subsystem:MergeTree"]),
    ]
    out = await pool._tag_belt(ranked, _inp(tags=["subsystem:MergeTree"]))
    assert [x.memory_key for x in out] == ["injected_ok", "fact_ok"]
    assert reads == [["injected_leak", "injected_ok"]]
    # no hard filter: nothing is read or dropped
    out = await pool._tag_belt(list(ranked), _inp())
    assert [x.memory_key for x in out] == ["injected_ok", "injected_leak", "fact_ok"]
    assert len(reads) == 1
    # the belt sits after the last row-adding step of the ask
    import inspect

    from server.memory.app.ask import pipeline

    src = inspect.getsource(ma.ask)
    fill = inspect.getsource(pipeline._fill_pool)
    # the mention-time turns are the last rows the injection phase adds
    assert "_mention_window_chunks(" in inspect.getsource(pool._inject_named_evidence)
    assert fill.index("_tag_belt(") > fill.index("_inject_named_evidence(")
    assert "abstention flows through" in inspect.getsource(abstain._early_answer)
    assert src.index("_fill_pool(") < src.index("_early_answer(")


@pytest.mark.asyncio
async def test_a_hit_made_from_a_set_chunk_is_judged_by_its_own_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # fold -> stamp -> belt: the row's tags ride from the set-chunk read, so no key read
    from server.memory.app.retrieval.pool import ChunkId, SetChunkRow, fold_set_chunks
    from server.memory.domain.search_types import SearchHit

    async def _boom(*a: Any, **k: Any) -> dict[Any, Any]:
        raise AssertionError("the row's tags were read with it")

    monkeypatch.setattr(tag_belt, "read_row_tags", _boom)
    chunk = SearchHit(
        memory_key="chunk:memory.set:k:0",
        memory_type="episodic",
        content_excerpt="a passage",
        distance=0.2,
        source="chunk",
        parent_memory_key="k",
        identity=("a", "u", "memory.set:k"),
        chunk_sha256="sha",
    )
    rows = {ChunkId("a", "u", "k", 0, "sha"): SetChunkRow(("a", "u", "s1"), 3, ("kind:lesson",))}
    (made,) = fold_set_chunks([chunk], rows)
    wire = _stamp_leg_tags(made, _hit("k"))
    kept = await tag_belt.enforce_hard_tag_filter([wire], _inp(tags=["kind:lesson"]), {}, ["ns"])
    assert kept == [wire]
