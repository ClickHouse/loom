"""An inference never outranks a statement by its priors:
per-request personalization arm, provenance-gated boosts, the preference
fallback as a setting, and a decomposed score on every hit."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from server.memory.app.retrieval import SearchQuery, vector_search
from server.settings import settings


class StubCH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows or []

    async def query_all(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return list(self.rows)


def _row(**kw) -> dict[str, Any]:
    base = dict(
        memory_key="k1",
        memory_type="preference",
        content_excerpt="likes tea",
        distance=0.2,
        rank_score=0.95,
        temporal_anchor="",
        temporal_confidence=0.0,
        valid_at="",
        created_at="",
        entities=[],
        source_resource_id="",
        source_class="extracted",
        preference_boost_sql=0.15,
        reinforcement_sql=0.0,
        tag_boost_sql=0.0,
        category_boost_sql=0.0,
        prior_group_sql=0.25,
    )
    base.update(kw)
    return base


def _q(**kw) -> SearchQuery:
    base = dict(
        org="o",
        namespace="n",
        scope="private",
        agent="*",
        embedding_model="m",
        query_vec=[0.1, 0.2],
        top_k=5,
        query_text="what tea does the user like",
        search_mode="semantic",
        reinforcement_weight=0.15,
    )
    base.update(kw)
    return SearchQuery(**base)


def _main_sql(ch: StubCH) -> tuple[str, dict[str, Any]]:
    for sql, params in ch.calls:
        if "AS rank_score" in sql and "FROM loom.memory_embeddings" in sql:
            return sql, params
    raise AssertionError("main ranking query not issued")


def test_full_arm_carries_the_preference_fallback_as_a_bound_setting(monkeypatch):

    monkeypatch.setattr(settings, "memory_search_preference_fallback", 0.15)
    ch = StubCH([_row()])
    hits = asyncio.run(vector_search(_q(search_mode="hybrid"), ch=ch))
    sql, params = _main_sql(ch)
    assert "{pref_fallback:Float32}" in sql and params["pref_fallback"] == 0.15
    assert "AS preference_boost_sql" in sql and "AS prior_group_sql" in sql
    assert "e.source_class AS source_class" in sql
    assert hits[0].source_class == "extracted"
    assert hits[0].score_components["preference_boost"] == 0.15
    assert hits[0].score_components["prior_group"] == 0.25
    assert "reinforcement" not in hits[0].score_components  # zero → omitted


def test_fallback_zero_makes_the_boost_intent_gated(monkeypatch):

    monkeypatch.setattr(settings, "memory_search_preference_fallback", 0.0)
    ch = StubCH([_row()])
    asyncio.run(vector_search(_q(search_mode="hybrid", preference_attribute="tea"), ch=ch))
    sql, params = _main_sql(ch)
    assert params["pref_fallback"] == 0.0
    assert (
        "if(has(e.tags, {pref_tag:String}), 0.3," in sql and params["pref_tag"] == "preference:tea"
    )


def test_none_arm_drops_every_user_derived_term_in_every_mode():
    for mode in ("semantic", "rrf", "hybrid", "bm25_hybrid", "lexical", "graph"):
        ch = StubCH([_row()])
        asyncio.run(
            vector_search(
                _q(
                    search_mode=mode,
                    personalization="none",
                    tags=["x"],
                    tags_soft=True,
                    tag_boost=0.3,
                ),
                ch=ch,
            )
        )
        sql, params = _main_sql(ch)
        assert "(0.0) AS preference_boost_sql" in sql, mode
        assert "(0.0) AS reinforcement_sql" in sql, mode
        assert "(0.0) AS tag_boost_sql" in sql, mode
        assert "(0.0) AS prior_group_sql" in sql, mode
        assert "{pref_fallback:Float32}, 0.0)) AS" not in sql, mode
        # Relevance terms survive: cosine still ranks.
        assert "1.0 - distance" in sql or "value_distance" in sql or mode == "lexical", mode


def test_eligibility_gates_priors_on_provenance(monkeypatch):
    ch = StubCH([_row()])
    asyncio.run(
        vector_search(
            _q(
                search_mode="hybrid",
                source_class_boost_eligible=["asserted", "observed"],
                tags=["x"],
                tags_soft=True,
            ),
            ch=ch,
        )
    )
    sql, params = _main_sql(ch)
    assert params["sc_eligible"] == ["asserted", "observed"]
    assert sql.count("has({sc_eligible:Array(String)}, e.source_class)") >= 3
    # The whole prior group (incl. stored confidence) is gated, not just its parts.
    assert (
        "if(has({sc_eligible:Array(String)}, e.source_class), least(" in sql
        or "if(has({sc_eligible:Array(String)}, e.source_class), {confidence_weight:Float32}" in sql
    )


def test_no_eligibility_means_no_gate():
    ch = StubCH([_row()])
    asyncio.run(vector_search(_q(), ch=ch))
    sql, params = _main_sql(ch)
    assert "sc_eligible" not in sql and "sc_eligible" not in params


def test_wire_models_carry_personalization_and_source_class():
    from server.memory.api.models_ask import MemoryAskInput
    from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit

    s = MemorySearchInput(org="o", namespace="n", query="q")
    assert s.personalization == "full"
    assert (
        MemorySearchInput(org="o", namespace="n", query="q", personalization="none").personalization
        == "none"
    )
    a = MemoryAskInput(org="o", namespace="n", agent="*", query="q", personalization="none")
    assert a.personalization == "none"
    h = _MemorySearchHit(
        memory_key="k", memory_type="semantic", content_excerpt="x", distance=0.1, similarity=0.9
    )
    assert h.source_class == ""
    with pytest.raises(ValidationError):
        MemorySearchInput(org="o", namespace="n", query="q", personalization="some")


def test_handler_reads_the_eligible_set_from_settings(monkeypatch):
    from server.memory.app.search.knobs import _source_class_boost_eligible

    monkeypatch.setattr(settings, "memory_search_source_class_boost_eligible", "")
    assert _source_class_boost_eligible() is None
    monkeypatch.setattr(
        settings,
        "memory_search_source_class_boost_eligible",
        "asserted, observed ,imported",
    )
    assert _source_class_boost_eligible() == ["asserted", "observed", "imported"]


def test_profile_fold_is_derived_not_asserted():
    from server.memory.api.models_profile import ProfileSetInput
    from server.memory.app.profile import _to_memory_set_input
    from server.memory.domain.provenance import source_class_for

    inp = ProfileSetInput(
        org="o",
        namespace="n",
        agent="-",
        user_id="u1",
        label="consolidated_profile",
        value="Prefers terse answers.",
        source_trace="profile.fold:consolidated_profile",
    )
    mset = _to_memory_set_input(inp, label=inp.label, value=inp.value, char_limit=8192)
    assert mset.source_trace == "profile.fold:consolidated_profile"
    assert (
        source_class_for(default="asserted", requested=None, source_trace=mset.source_trace)
        == "derived"
    )
    # A client persona with no trace stays a statement.
    plain = _to_memory_set_input(
        ProfileSetInput(org="o", namespace="n", agent="a", label="persona", value="v"),
        label="persona",
        value="v",
        char_limit=8192,
    )
    assert plain.source_trace == ""
    assert source_class_for(default="asserted", requested=None, source_trace="") == "asserted"
