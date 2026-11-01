"""The anchor pool honours the agent scope like every other leg.

`_anchor_pool_where` carries org, namespace, tombstone, dim, the run, the
hard tag filter and the agent clause: a private-scope search naming agent A
reads A's own rows plus the shared ones, as the fact leg, the key-match arm
and the digest descent do. The three additive legs spell that clause from
one helper, the pool's statement selects the row's agent and scope, and a
post-fusion belt drops any stamped anchor row that still belongs to another
agent's private scope, disclosing the count only when it dropped one. A
cross-agent request (`*`, `-`, none) issues the statement without the agent
clause.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from server.core.tag_sql import tag_predicate
from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search.anchor_pool import _enforce_agent_scope, _stamp_row_owner
from server.memory.app.search.sql import (
    _anchor_pool_where,
    _digest_descent_where,
    _key_match_where,
    anchor_pool_sql,
)
from server.memory.domain.search_sql import agent_filter, agent_or_shared_predicate

AGENT = "(e.agent = {agent:String} OR e.scope = 'shared')"


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


def _hit(key: str, source: str, agent: str | None = None, scope: str = "private") -> Any:
    hit = _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt="x",
        distance=0.2,
        similarity=0.8,
        source=source,
    )
    if agent is not None:
        _stamp_row_owner(hit, agent, scope)
    return hit


# --------------------------------------------------------------------- helper


def test_agent_filter_skips_the_cross_agent_sentinels() -> None:
    assert agent_filter("bot") == "bot"
    assert agent_filter("*") == "" and agent_filter("-") == "" and agent_filter("") == ""
    assert agent_filter(None) == ""


def test_agent_or_shared_predicate_spells_the_additive_legs_clause() -> None:
    assert agent_or_shared_predicate("e") == AGENT
    assert agent_or_shared_predicate("") == "(agent = {agent:String} OR scope = 'shared')"


# ----------------------------------------------------------------- the legs


def test_anchor_pool_where_carries_the_agent_predicate() -> None:
    params: dict[str, Any] = {}
    parts = _anchor_pool_where(_inp(agent="bot"), params)
    assert AGENT in parts and params["agent"] == "bot"
    # the pool's identity predicates come first, the agent before the run and the tags
    params = {}
    parts = _anchor_pool_where(_inp(agent="bot", run_id="r", tags=["k"]), params)
    assert parts.index(AGENT) < parts.index("e.run_id = {run_id:String}")
    assert parts.index("e.run_id = {run_id:String}") < parts.index(tag_predicate("e"))


def test_anchor_pool_where_is_unchanged_for_a_cross_agent_request() -> None:
    for agent in ("*", "-", ""):
        params: dict[str, Any] = {}
        parts = _anchor_pool_where(_inp(agent=agent), params)
        assert AGENT not in parts and "agent" not in params
        assert parts == [
            "e.org = {org:String}",
            "e.namespace = {ns:String}",
            "e.embedding_model = {model:String}",
            "e.tombstoned_at IS NULL",
            "e.embedding_dim > 0",
            "e.status IN ('active', 'conflicted')",
            "(e.expires_at IS NULL OR e.expires_at > now())",
            "(e.valid_to IS NULL OR e.valid_to > now())",
        ]


def test_anchor_pool_serves_what_the_key_match_arm_serves() -> None:
    """The query's model and the served statuses, as the key-match arm reads them: no
    superseded version, no unapproved proposal, no cosine against another model's vectors."""
    for inp in (_inp(), _inp(include_proposed=True, include_superseded=True)):
        anchor = _anchor_pool_where(inp, {})
        key_match = _key_match_where(inp, {})
        assert "e.embedding_model = {model:String}" in anchor
        assert [p for p in anchor if "status IN" in p] == [p for p in key_match if "status IN" in p]


def test_every_additive_arm_keeps_the_fact_legs_liveness_window() -> None:
    """An expired row, or one whose stated validity has ended, is served by no leg; under
    an as-of replay the validity window is the as-of predicate's, so only expiry stays."""
    from datetime import UTC, datetime

    from server.memory.app.search.sql import _digest_descent_where

    expiry, validity = "expires_at IS NULL OR", "valid_to IS NULL OR"
    for build in (_anchor_pool_where, _key_match_where, _digest_descent_where):
        live = " AND ".join(build(_inp(), {}))
        assert expiry in live and validity in live, build.__name__
        replay = " AND ".join(build(_inp(as_of=datetime(2026, 1, 1, tzinfo=UTC)), {}))
        assert expiry in replay and validity not in replay, build.__name__


def test_anchor_pool_statement_selects_the_rows_agent_and_scope() -> None:
    sql = anchor_pool_sql(" AND ".join(_anchor_pool_where(_inp(agent="bot"), {})))
    assert "e.agent AS agent" in sql and "e.scope AS scope" in sql
    assert AGENT in sql


def test_key_match_and_descent_read_the_same_clause_as_before() -> None:
    params: dict[str, Any] = {}
    assert AGENT in _key_match_where(_inp(agent="bot"), params) and params["agent"] == "bot"
    params = {}
    assert "(agent = {agent:String} OR scope = 'shared')" in _digest_descent_where(
        _inp(agent="bot"), params
    )
    assert AGENT not in _key_match_where(_inp(agent="*"), {})
    assert "(agent = {agent:String} OR scope = 'shared')" not in _digest_descent_where(
        _inp(agent="-"), {}
    )


# ----------------------------------------------------------------- the belt


def test_row_owner_rides_privately_and_never_on_the_wire() -> None:
    hit = _hit("k", "anchor", agent="other")
    assert hit._row_agent == "other" and hit._row_scope == "private"
    assert "_row_agent" not in hit.model_dump() and "agent" not in hit.model_dump()


def test_belt_drops_a_stamped_anchor_row_of_another_agent_and_says_so() -> None:
    hits = [
        _hit("own", "anchor", agent="bot"),
        _hit("shared", "anchor", agent="_shared", scope="shared"),
        _hit("leak", "anchor", agent="other"),
        _hit("unstamped", "anchor"),
        _hit("fact", "fact"),
    ]
    work: dict[str, Any] = {}
    kept = _enforce_agent_scope(hits, _inp(agent="bot"), work)
    assert [x.memory_key for x in kept] == ["own", "shared", "unstamped", "fact"]
    assert work == {"agent_filter_dropped": 1}


def test_belt_is_a_no_op_without_a_named_agent_and_adds_no_key_when_nothing_fell() -> None:
    hits = [_hit("a", "anchor", agent="other"), _hit("b", "anchor", agent="bot")]
    for agent in ("*", "-", ""):
        work: dict[str, Any] = {}
        assert _enforce_agent_scope(hits, _inp(agent=agent), work) is hits
        assert work == {}
    work = {}
    kept = _enforce_agent_scope([_hit("b", "anchor", agent="bot")], _inp(agent="bot"), work)
    assert len(kept) == 1 and work == {}
    assert _enforce_agent_scope([], _inp(agent="bot"), work) == [] and work == {}


async def test_an_anchor_row_carries_its_version(monkeypatch) -> None:
    # the folds pick a writer's newest row of a key by version
    from types import SimpleNamespace

    import server.core.ch as ch_mod
    from server.memory.app.search.anchor_pool import _run_anchor_pool

    row = {"memory_key": "k", "agent": "a", "user_id": "u", "session_id": "s2", "version": 9}

    class _CH:
        async def query_all(self, sql, params=None):
            return [{**row, "similarity": 0.5, "content_excerpt": "t"}]

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    ctx = SimpleNamespace(
        input=_inp(top_k=5),
        all_hits=[],
        actual_work={},
        namespaces=["n"],
        effective_entity_filter=["alice"],
        resolved_dates=[],
        query_vec=[0.1],
        embedder=SimpleNamespace(model_name="m"),
    )
    await _run_anchor_pool(ctx)
    assert [(h._row_identity, h.version) for h in ctx.all_hits] == [(("a", "u", "s2"), 9)]


async def test_an_anchor_row_of_another_writer_joins_though_its_excerpt_matches(
    monkeypatch,
) -> None:
    import server.core.ch as ch_mod
    from server.memory.app.search.anchor_pool import _run_anchor_pool

    row = {"memory_key": "k", "agent": "a", "session_id": "s1", "content_excerpt": "t"}

    class _CH:
        async def query_all(self, sql, params=None):
            return [
                {**row, "user_id": "u1", "similarity": 0.5},
                {**row, "user_id": "u2", "similarity": 0.5},
            ]

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    pooled = _MemorySearchHit(
        memory_key="k",
        memory_type="semantic",
        content_excerpt="t",
        distance=0.5,
        similarity=0.5,
        namespace="n",
    )
    pooled._row_identity = ("a", "u1", "s1")
    ctx = SimpleNamespace(
        input=_inp(top_k=5),
        all_hits=[pooled],
        actual_work={},
        namespaces=["n"],
        effective_entity_filter=["alice"],
        resolved_dates=[],
        query_vec=[0.1],
        embedder=SimpleNamespace(model_name="m"),
    )
    await _run_anchor_pool(ctx)
    assert [h._row_identity for h in ctx.all_hits[1:]] == [("a", "u2", "s1")]
