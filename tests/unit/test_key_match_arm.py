"""The memory key names the attribute a question asks for, and
the key-match arm reads it whatever the search mode. Code facets are mined
from prose only when the surface carries a code signal."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.code.domain.facets import extract_code_facets, has_code_signal
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search.key_match import (
    _run_key_match_pool,
    key_match_enabled,
    key_match_gate,
    key_match_key_qualifies,
    key_match_mode,
    key_match_query_stems,
)
from server.memory.app.search.ranking import _apply_global_fusion
from server.memory.app.search.scoring import _effective_arm_boosts
from server.memory.app.search.sql import (
    key_match_component_sql,
    key_match_precheck_sql,
    key_match_sql,
)


def test_query_stems_drop_stop_words_and_stem() -> None:
    stems = key_match_query_stems("What is the user's task worktree path?")
    assert "task" in stems and "worktre" in stems and "path" in stems
    assert not ({"what", "is", "the", "user", "s"} & set(stems))
    assert key_match_query_stems("what?") == []


def test_query_stems_add_the_singular_of_acronym_plurals() -> None:
    # Snowball leaves "prs" alone while the key part reads "pr"; both forms
    # are in the stem set so `user_pr_status` still matches. Question words
    # and auxiliaries ("how many", "does", "have", "non") never reach the
    # key comparison.
    stems = key_match_query_stems("How many non-Acme PRs does the user have?")
    assert "pr" in stems and "prs" in stems
    assert not {"how", "mani", "doe", "have", "non", "user"} & set(stems)
    # A stem already ending in a double s is left alone.
    assert "clas" not in key_match_query_stems("which class is it")


def test_key_match_switch_is_request_first_then_setting(monkeypatch) -> None:
    from server.settings import settings

    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q")
    monkeypatch.setattr(settings, "memory_search_key_match_mode", "off")
    assert key_match_enabled(inp) is False
    assert key_match_enabled(inp.model_copy(update={"key_match": True})) is True
    monkeypatch.setattr(settings, "memory_search_key_match_mode", "gated")
    assert key_match_enabled(inp) is True
    assert key_match_enabled(inp.model_copy(update={"key_match": False})) is False
    assert inp.key_match is None


def _hit(key, raw, **comps):
    return SimpleNamespace(
        memory_key=key, rank_score=raw, score_components={"rank_score_raw": raw, **comps}
    )


def test_arm_boost_is_an_rrf_arm_in_rrf_pools_and_capped_additive_otherwise() -> None:
    hits = [_hit("a", 0.030), _hit("b", 0.020, key_match=1.0), _hit("c", 0.010, key_match=0.4)]
    rrf = _effective_arm_boosts(hits, component="key_match", rrf_pool=True)
    assert set(rrf) == {1, 2} and rrf[1] == 1 / 61 and rrf[2] == 1 / 62
    add = _effective_arm_boosts(hits, component="key_match", rrf_pool=False, scale=0.5)
    assert add[1] == 0.030 and add[2] == 0.030  # 0.5 and 0.2, both clamped to the pool max
    wide = _effective_arm_boosts(
        [_hit("a", 0.9), _hit("b", 0.5, key_match=0.4)],
        component="key_match",
        rrf_pool=False,
        scale=0.5,
    )
    assert abs(wide[1] - 0.2) < 1e-9  # under the cap the strength scales by `scale`
    # The handler passes the setting (0.03 by default): a tie-break in cosine
    # units, so a perfect key match lifts a row past near-equal cosines only.
    tie = _effective_arm_boosts(
        [_hit("a", 0.72), _hit("b", 0.69, key_match=1.0)],
        component="key_match",
        rrf_pool=False,
        scale=0.03,
    )
    assert abs(tie[1] - 0.03) < 1e-9
    assert _effective_arm_boosts([_hit("a", 0.1)], component="key_match", rrf_pool=False) == {}


def test_fusion_adds_the_key_match_boost_to_the_raw_score() -> None:
    ctx = SimpleNamespace(
        input=SimpleNamespace(search_mode="semantic"),
        all_hits=[_hit("a", 0.80), _hit("b", 0.79, key_match=1.0), _hit("c", 0.60, key_match=1.0)],
        entity_boost_by_key={},
        resolved_dates=[],
    )
    _apply_global_fusion(ctx)
    by = {h.memory_key: h for h in ctx.all_hits}
    assert abs(by["b"].score_components["key_match_boost_effective"] - 0.03) < 1e-9
    assert abs(by["b"].rank_score - 0.82) < 1e-9 and by["a"].rank_score == 0.80
    # A near-tie flips on the key; a 0.2 cosine gap does not.
    assert [h.memory_key for h in ctx.all_hits] == ["b", "a", "c"]


# --- code facets from prose ---------------------------------------------------------


def test_prose_abbreviations_and_bare_letters_are_not_code() -> None:
    for text in (
        "Pedro Pierluisi worked in the city of Washington, D.C.",
        "Robert A. Heinlein is a citizen of the U.S.A.",
        "Pakistan International Airlines was founded in Karachi.",
    ):
        assert has_code_signal(text) is False
        assert extract_code_facets(text) == ([], [])


def test_code_surfaces_still_yield_files_and_symbols() -> None:
    files, concepts = extract_code_facets(
        "Fixed the flaky test in `server/memory/domain/search_sql.py:142` by calling "
        "get_client() before the os.environ read."
    )
    assert files == ["server/memory/domain/search_sql.py"]
    assert "get_client" in concepts and "os.environ" in concepts
    files, concepts = extract_code_facets(
        "The KeyError came from settings.py when np.array was called."
    )
    assert files == ["settings.py"] and "KeyError" in concepts and "np.array" in concepts


def test_key_match_statement_filters_keys_in_prewhere_before_the_wide_read() -> None:
    """The wide pass reads a row's vectors only when its key
    shares a stem with the query; every pass merges under FINAL."""
    from server.memory.app.search.sql import key_match_sql

    sql = key_match_sql("e.org = {org:String} AND e.tombstoned_at IS NULL")
    inner = sql[
        sql.index("FROM loom.memory_embeddings FINAL\n            PREWHERE") : sql.index(") AS e")
    ]
    assert (
        "PREWHERE org = {org:String} AND namespace = {ns:String}\n"
        "              AND arrayExists(t -> has({stems:Array(String)}, stem(t, 'en')),\n"
        "                              splitByChar('_', lower(memory_key)))" in inner
    )
    assert "\n            WHERE" not in inner and "SELECT *" in sql  # no version rule
    assert sql.count("FROM loom.memory_embeddings FINAL") == 3  # two statistics scans + wide pass
    assert "WHERE e.org = {org:String} AND e.tombstoned_at IS NULL" in sql


# --- the arm runs only when it could change the served window -----------------


def test_key_match_mode_precedence(monkeypatch) -> None:
    from server.settings import settings

    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q")
    monkeypatch.setattr(settings, "memory_search_key_match_mode", "gated")
    assert key_match_mode(inp) == "gated"
    assert key_match_mode(inp.model_copy(update={"key_match_mode": "always"})) == "always"
    # The bool still wins over the mode, both ways.
    assert (
        key_match_mode(inp.model_copy(update={"key_match": True, "key_match_mode": "off"}))
        == "always"
    )
    assert (
        key_match_mode(inp.model_copy(update={"key_match": False, "key_match_mode": "always"}))
        == "off"
    )
    # The deployment mode `off` holds unless the request names a mode.
    monkeypatch.setattr(settings, "memory_search_key_match_mode", "off")
    assert key_match_mode(inp) == "off" and key_match_enabled(inp) is False
    assert key_match_enabled(inp.model_copy(update={"key_match_mode": "gated"})) is True


def test_key_qualifies_mirrors_the_statement_predicate() -> None:
    stems = set(key_match_query_stems("What is the user's task worktree path?"))
    assert key_match_key_qualifies("user_task_worktree", stems)  # two stems shared
    assert key_match_key_qualifies("worktree", stems)  # one part, all of it matched
    assert not key_match_key_qualifies("user_task_status", stems)  # one of three
    assert not key_match_key_qualifies("melanie_instrument", stems)
    assert not key_match_key_qualifies("", stems)


def _gate(**kw):
    base = dict(
        query="what is the user's task worktree path",
        stems=key_match_query_stems("what is the user's task worktree path"),
        search_mode="semantic",
        recipe="",
        external_rerank=False,
        pool=[(f"k{i}", f"text {i}", "fact") for i in range(20)],
        per_key_cap=3,
        top_k=10,
    )
    base.update(kw)
    return key_match_gate(**base)


def test_gate_skips_a_full_additive_pool_with_no_matching_key() -> None:
    # Twenty distinct keys, none sharing two stems with the query, no
    # identifier, no quote, no reranker: an added row would enter below the
    # floor and no pooled row would gain the component.
    assert _gate() == ""


def test_gate_runs_where_a_tail_row_or_a_boost_could_be_served() -> None:
    assert _gate(search_mode="rrf") == "rrf"
    assert _gate(recipe="quality") == "rerank"
    assert _gate(external_rerank=True) == "rerank"
    # Thin after the per-key cap: 12 rows of one key are 3 rows served.
    assert _gate(pool=[("same_key", "t", "fact")] * 12) == "thin_pool"
    assert _gate(pool=[(f"k{i}", "t", "fact") for i in range(9)]) == "thin_pool"
    # Banded planes do not fill the window: nine facts and twenty summaries
    # are still a thin pool for top_k 10 (an added fact is served past them).
    assert (
        _gate(
            pool=[(f"k{i}", "t", "fact") for i in range(9)]
            + [(f"s{i}", "t", "summary") for i in range(20)]
        )
        == "thin_pool"
    )
    # A quoted phrase is not a reason: no stage weighs a quoted match, its
    # words are stems like any other, so the pre-check decides.
    assert _gate(query='what did the user say about "worktree path"') == ""
    # A pooled key the statement could return.
    assert (
        _gate(
            pool=[(f"k{i}", "t", "fact") for i in range(19)] + [("user_task_worktree", "t", "fact")]
        )
        == "pool_key"
    )


def test_gate_identifier_reason_reads_how_many_pooled_rows_carry_it() -> None:
    q = "what is the state of PR 117947 in the user's worktree"
    stems = key_match_query_stems(q)
    # Nothing in the pool carries the identifier: a pinned added row would be served.
    assert _gate(query=q, stems=stems) == "identifier"
    # Ten distinct pooled rows carry it: they fill the pinned band first.
    carrying = [(f"k{i}", "about 117947", "fact") for i in range(10)] + [
        (f"z{i}", "t", "fact") for i in range(10)
    ]
    assert _gate(query=q, stems=stems, pool=carrying) == ""
    # Ten rows of one key carry it: three after the cap, so the arm runs.
    one_key = [("pr_status", "about 117947", "fact")] * 10 + [
        (f"z{i}", "t", "fact") for i in range(10)
    ]
    assert _gate(query=q, stems=stems, pool=one_key) == "identifier"


async def _ctx_for(
    monkeypatch, *, mode: str, pool_keys: list[str], top_k: int = 5, query_shape: str = ""
):
    import server.core.ch as ch_mod

    calls: list[str] = []

    class _CH:
        async def query_all(self, sql, params=None):
            calls.append(sql)
            return []

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    hits = [
        SimpleNamespace(
            namespace="n",
            memory_key=k,
            content_excerpt="t",
            rank_score=0.5,
            score_components={"rank_score_raw": 0.5},
        )
        for k in pool_keys
    ]
    inp = MemorySearchInput(
        org="o",
        namespace="n",
        agent="a",
        query="what is the user's task worktree path",
        top_k=top_k,
        key_match_mode=mode,
        search_mode="semantic",
    )
    ctx = SimpleNamespace(
        input=inp,
        all_hits=hits,
        actual_work={},
        namespaces=["n"],
        query_vec=[0.1, 0.2],
        embedder=SimpleNamespace(model_name="m"),
        endpoint=None,
        query_shape=query_shape,
    )
    await _run_key_match_pool(ctx)
    return ctx, calls


async def test_gated_pool_skips_the_read_and_says_so(monkeypatch) -> None:
    ctx, calls = await _ctx_for(monkeypatch, mode="gated", pool_keys=[f"k{i}" for i in range(20)])
    assert calls == [] and ctx.actual_work["key_match_gate"] == "skip"
    assert ctx.actual_work["key_match_mode"] == "gated" and "key_match_added" not in ctx.actual_work


async def test_gated_pool_reads_when_the_pool_is_thin_and_always_reads(monkeypatch) -> None:
    ctx, calls = await _ctx_for(monkeypatch, mode="gated", pool_keys=["k1", "k2"])
    assert len(calls) == 1 and ctx.actual_work["key_match_gate"] == "thin_pool"
    assert ctx.actual_work["key_match_added"] == 0
    ctx, calls = await _ctx_for(monkeypatch, mode="always", pool_keys=[f"k{i}" for i in range(20)])
    assert len(calls) == 1 and "key_match_gate" not in ctx.actual_work
    ctx, calls = await _ctx_for(monkeypatch, mode="off", pool_keys=["k1"])
    assert calls == [] and ctx.actual_work == {}


def test_component_statement_reads_no_wide_column_and_is_keyed_to_the_pool() -> None:
    sql = key_match_component_sql("e.org = {org:String}")
    assert "memory_key IN {pool_keys:Array(String)}" in sql
    assert "embedding," not in sql and "e.embedding" not in sql and "content_excerpt" not in sql
    assert "cosineDistance" not in sql and "L2Distance" not in sql
    assert "strength >= {min_strength:Float64}" in sql and "matched >= 2" in sql
    assert sql.count("FINAL") == 2  # the one statistics pass and the keyed pass
    assert "sumMap(" in sql  # n_keys and the per-stem frequencies from one scan


async def test_gated_pool_key_reason_reads_components_only(monkeypatch) -> None:
    import server.core.ch as ch_mod

    calls: list[tuple[str, dict]] = []

    class _CH:
        async def query_all(self, sql, params=None):
            calls.append((sql, params or {}))
            return [
                {"memory_key": "user_task_worktree", "strength": 0.9},
                {"memory_key": "not_in_pool_key", "strength": 1.0},
            ]

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    keys = [f"k{i}" for i in range(19)] + ["user_task_worktree"]
    hits = [
        SimpleNamespace(
            namespace="n",
            memory_key=k,
            content_excerpt="t",
            rank_score=0.5,
            score_components={"rank_score_raw": 0.5},
        )
        for k in keys
    ]
    inp = MemorySearchInput(
        org="o",
        namespace="n",
        agent="a",
        top_k=5,
        search_mode="semantic",
        query="what is the user's task worktree path",
        key_match_mode="gated",
    )
    ctx = SimpleNamespace(
        input=inp,
        all_hits=hits,
        actual_work={},
        namespaces=["n"],
        query_vec=[0.1],
        embedder=SimpleNamespace(model_name="m"),
        endpoint=None,
        query_shape="",
    )
    await _run_key_match_pool(ctx)
    assert ctx.actual_work["key_match_gate"] == "pool_key"
    assert len(calls) == 1 and "pool_keys" in calls[0][1] and "cosineDistance" not in calls[0][0]
    assert calls[0][1]["pool_keys"] == sorted(keys) and calls[0][1]["limit"] == 20
    assert "qv" not in calls[0][1]  # no vector is read, so none is sent
    assert hits[-1].score_components["key_match"] == 0.9
    assert len(ctx.all_hits) == 20 and ctx.actual_work["key_match_added"] == 0
    assert ctx.actual_work["key_match_component_only"] is True


# --- a keys-only pre-check before the wide statement ---------------------------


def test_precheck_statement_reads_keys_only_without_final() -> None:
    sql = key_match_precheck_sql()
    assert "FROM loom.memory_embeddings\n" in sql and "FINAL" not in sql
    assert "embedding" not in sql.replace("memory_embeddings", "")  # no vector column
    assert "WHERE matched >= 2 OR (matched >= 1 AND matched = key_parts)" in sql
    assert sql.strip().endswith("LIMIT 1")
    # The wide statement's own qualification, nothing narrower: a raw miss here is a FINAL miss there.
    assert "(matched >= 2 OR (matched >= 1 AND matched = key_parts))" in key_match_sql("1")


class _RecordingCH:
    def __init__(self, answers):
        self.answers = answers
        self.statements: list[str] = []
        self.params: list[dict] = []

    async def query_all(self, sql, params=None):
        self.statements.append(sql)
        self.params.append(params or {})
        for needle, rows in self.answers:
            if needle in sql:
                return rows
        return []


def _pool_ctx():
    inp = MemorySearchInput(
        org="o", namespace="n", agent="a", query="what is the user's task worktree path", top_k=10
    )
    return SimpleNamespace(
        input=inp,
        actual_work={},
        all_hits=[],
        namespaces=["n"],
        query_vec=[0.0] * 8,
        embedder=SimpleNamespace(model_name="m"),
        endpoint=None,
        query_shape="",
    )


def _run_pool(monkeypatch, ch, shape=""):
    import asyncio

    import server.core.ch as ch_mod
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_key_match_mode", "always")
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    ctx = _pool_ctx()
    ctx.query_shape = shape
    asyncio.run(_run_key_match_pool(ctx))
    return ctx


def test_precheck_skips_the_wide_statement_when_no_key_can_qualify(monkeypatch) -> None:
    ch = _RecordingCH([])
    ctx = _run_pool(monkeypatch, ch)
    assert len(ch.statements) == 1 and "SELECT 1 AS any_key" in ch.statements[0]
    assert ctx.actual_work["key_match_precheck_skipped"] == 1
    assert ctx.actual_work["key_match_added"] == 0 and ctx.all_hits == []


def test_precheck_hands_over_to_the_wide_statement_when_a_key_qualifies(monkeypatch) -> None:
    wide_row = {
        "memory_key": "user_task_worktree",
        "similarity": 0.7,
        "strength": 0.9,
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
    }
    ch = _RecordingCH([("SELECT 1 AS any_key", [{"any_key": 1}]), ("AS n_keys", [wide_row])])
    ctx = _run_pool(monkeypatch, ch)
    assert len(ch.statements) == 2
    assert "SELECT 1 AS any_key" in ch.statements[0] and "AS n_keys" in ch.statements[1]
    assert ctx.actual_work["key_match_precheck_skipped"] == 0
    assert ctx.actual_work["key_match_added"] == 1
    assert [h.memory_key for h in ctx.all_hits] == ["user_task_worktree"]


@pytest.mark.parametrize(
    ("cap", "shape", "per_key"),
    [(3, "", 3), (0, "", 20), (3, "aggregational", 20), (3, "temporal_diff", 20)],
)
def test_the_wide_statement_keeps_as_many_rows_of_a_key_as_the_window(
    monkeypatch, cap, shape, per_key
) -> None:
    # 0 is no cap: the statement then keeps a key's rows up to its own limit (20 here).
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_per_key_cap", cap)
    ch = _RecordingCH([("SELECT 1 AS any_key", [{"any_key": 1}])])
    _run_pool(monkeypatch, ch, shape)
    (wide,) = [p for sql, p in zip(ch.statements, ch.params, strict=True) if "AS n_keys" in sql]
    assert wide["per_key"] == per_key


async def test_a_key_match_row_carries_its_version(monkeypatch) -> None:
    # the folds pick a writer's newest row of a key by version
    import server.core.ch as ch_mod

    row = {"memory_key": "task_worktree", "agent": "a", "user_id": "u", "session_id": "s1"}

    class _CH:
        async def query_all(self, sql, params=None):
            return [{"any_key": 1}] if "any_key" in sql else [{**row, "version": 9}]

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    inp = MemorySearchInput(
        org="o", namespace="n", agent="a", query="task worktree", key_match_mode="always"
    )
    ctx = SimpleNamespace(
        input=inp,
        all_hits=[],
        actual_work={},
        namespaces=["n"],
        query_vec=[0.1],
        embedder=SimpleNamespace(model_name="m"),
        endpoint=None,
        query_shape="",
    )
    await _run_key_match_pool(ctx)
    assert [h.version for h in ctx.all_hits] == [9]


@pytest.mark.parametrize("shape", ["aggregational", "temporal_diff"])
async def test_the_gate_counts_rows_per_key_as_the_final_block_keeps_them(
    monkeypatch, shape: str
) -> None:
    # Six rows of one key: three served under the per-key cap, all six for a query
    # shape that keeps every row of the key.
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_per_key_cap", 3)
    one_key = ["same_key"] * 6
    ctx, _ = await _ctx_for(monkeypatch, mode="gated", pool_keys=one_key)
    assert ctx.actual_work["key_match_gate"] == "thin_pool"
    ctx, calls = await _ctx_for(monkeypatch, mode="gated", pool_keys=one_key, query_shape=shape)
    assert calls == [] and ctx.actual_work["key_match_gate"] == "skip"
