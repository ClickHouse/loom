from server.memory.domain.query_planner import estimate_memory_search_work, plan_memory_search


def test_planner_promotes_needle_to_bm25_with_pool() -> None:
    plan = plan_memory_search(
        current_search_mode="rrf",
        current_hyde=False,
        current_include_top_n_unmatched=0,
        current_rerank="",
        query_shape="needle",
        resolved_dates=[],
        entity_count=2,
        sub_query_count=0,
    )

    assert plan.search_mode == "bm25_hybrid"
    assert plan.hyde is False
    assert plan.include_top_n_unmatched == 50
    assert "record" in plan.planes
    assert "lexical" in plan.planes
    assert plan.reason == "needle"


def test_planner_allows_deep_budget_needle_hyde() -> None:
    plan = plan_memory_search(
        current_search_mode="rrf",
        current_hyde=False,
        current_include_top_n_unmatched=0,
        current_rerank="",
        query_shape="needle",
        resolved_dates=[],
        entity_count=2,
        sub_query_count=0,
        budget_class="deep",
    )

    assert plan.hyde is True
    assert plan.budget_class == "deep"


def test_planner_preserves_explicit_recipe() -> None:
    plan = plan_memory_search(
        current_search_mode="semantic",
        current_hyde=False,
        current_include_top_n_unmatched=0,
        current_rerank="",
        recipe="fast",
        query_shape="needle",
        resolved_dates=[],
        entity_count=2,
        sub_query_count=1,
    )

    assert plan.search_mode == "semantic"
    assert plan.hyde is False
    assert plan.include_top_n_unmatched == 0
    assert plan.planes == ["fact"]
    assert plan.reason == "recipe:fast"


def test_planner_uses_temporal_mode_for_dated_single_fact() -> None:
    plan = plan_memory_search(
        current_search_mode="rrf",
        current_hyde=True,
        current_include_top_n_unmatched=0,
        current_rerank="",
        query_shape="single_fact",
        resolved_dates=["2026-06-03"],
        entity_count=0,
        sub_query_count=0,
    )

    assert plan.search_mode == "temporal"
    assert "record" in plan.planes
    assert "temporal" in plan.planes
    assert plan.reason == "date_anchor"


def test_default_rrf_plan_marks_lexical_plane_executable() -> None:
    plan = plan_memory_search(
        current_search_mode="rrf",
        current_hyde=True,
        current_include_top_n_unmatched=0,
        current_rerank="",
        query_shape="single_fact",
        resolved_dates=[],
        entity_count=0,
        sub_query_count=0,
    )

    assert plan.search_mode == "rrf"
    assert "lexical" in plan.planes


def test_preference_attribute_enables_record_plane() -> None:
    plan = plan_memory_search(
        current_search_mode="rrf",
        current_hyde=True,
        current_include_top_n_unmatched=0,
        current_rerank="",
        query_shape="single_fact",
        resolved_dates=[],
        preference_attribute="theme",
        entity_count=0,
        sub_query_count=0,
    )

    assert plan.search_mode == "bm25_hybrid"
    assert "record" in plan.planes
    assert "lexical" in plan.planes
    assert plan.reason == "preference_attribute"


def test_explicit_temporal_mode_keeps_record_plane() -> None:
    plan = plan_memory_search(
        current_search_mode="temporal",
        current_hyde=True,
        current_include_top_n_unmatched=0,
        current_rerank="",
        query_shape="single_fact",
        resolved_dates=["2026-06-03"],
        entity_count=0,
        sub_query_count=0,
    )

    assert plan.search_mode == "temporal"
    assert "record" in plan.planes
    assert "temporal" in plan.planes
    assert plan.reason == "caller_temporal"


def test_unmatched_pool_is_explicit_plane() -> None:
    plan = plan_memory_search(
        current_search_mode="rrf",
        current_hyde=True,
        current_include_top_n_unmatched=25,
        current_rerank="",
        query_shape="single_fact",
        resolved_dates=[],
        entity_count=0,
        sub_query_count=0,
    )

    assert "unmatched" in plan.planes


def test_work_estimate_counts_agentic_fanout() -> None:
    plan = plan_memory_search(
        current_search_mode="rrf",
        current_hyde=False,
        current_include_top_n_unmatched=25,
        current_rerank="",
        query_shape="aggregational",
        resolved_dates=[],
        entity_count=2,
        sub_query_count=3,
    )

    est = estimate_memory_search_work(
        plan,
        namespace_count=2,
        top_k=5,
        pool_top_k=15,
        query_enrichment_used=True,
        sub_query_count=3,
        entity_count=2,
        graph_triples_enabled=False,
        external_rerank_enabled=True,
    )

    assert est["namespace_count"] == 2
    assert est["vector_queries"] == 10
    assert est["embedding_calls"] == 6
    assert est["llm_calls"] == 2
    assert est["candidate_pool"] > 30


def test_recipe_does_not_clobber_caller_set_fields() -> None:
    """A recipe supplies defaults; explicit caller values win.

    `{"recipe": "quality", "hyde": false}` runs with hyde=False. An
    unconditional `model_copy(update=...)` would discard half the request with
    no signal to the caller, and the downstream guard in `handle_memory_search`
    (which reads `caller_hyde_explicit`) would pass the request on the
    clobbered True. It would also make the recipe impossible to bisect:
    disabling a leg per request would appear to change nothing.
    """
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.query_plan import _apply_search_recipe

    pinned = MemorySearchInput(
        org="o",
        namespace="n",
        query="q",
        recipe="quality",
        hyde=False,  # explicit, must survive
        search_mode="rrf",  # explicit, must survive
    )
    out = _apply_search_recipe(pinned)
    assert out.hyde is False
    assert out.search_mode == "rrf"
    # A knob the caller left alone still gets the recipe's default.
    assert out.include_top_n_unmatched == 50


def test_recipe_still_applies_unset_fields() -> None:
    """The fix must not turn recipes into no-ops."""
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.query_plan import _apply_search_recipe

    bare = MemorySearchInput(org="o", namespace="n", query="q", recipe="quality")
    out = _apply_search_recipe(bare)
    assert out.search_mode == "bm25_hybrid"
    assert out.hyde is True
    assert out.include_top_n_unmatched == 50


def test_explicit_zero_beats_the_quality_floor() -> None:
    """`quality` raises include_top_n_unmatched to 50 via max(), which would
    otherwise silently override a deliberate 0 — an explicit value equal to the
    field default still counts as set, so it survives."""
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.query_plan import _apply_search_recipe

    out = _apply_search_recipe(
        MemorySearchInput(
            org="o",
            namespace="n",
            query="q",
            recipe="quality",
            include_top_n_unmatched=0,
        )
    )
    assert out.include_top_n_unmatched == 0


def test_every_recipe_respects_a_pinned_search_mode() -> None:
    """Whatever the recipe, a caller-set search_mode is never overwritten."""
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.query_plan import _apply_search_recipe

    for recipe in ("fast", "balanced", "quality", "verbatim", "code", "temporal", "graph"):
        out = _apply_search_recipe(
            MemorySearchInput(
                org="o",
                namespace="n",
                query="q",
                recipe=recipe,
                search_mode="lexical",
            )
        )
        assert out.search_mode == "lexical", f"{recipe} clobbered search_mode"


# --- an identifier in the query is a lexical query ---------


def test_identifier_tokens_catch_uuids_hashes_and_mixed_ids_not_prose() -> None:
    from server.memory.domain.query_planner import identifier_tokens

    assert identifier_tokens("Find the value for key cd613e30-d8f1-4adf-91b7-584a2265b1f5") == [
        "cd613e30-d8f1-4adf-91b7-584a2265b1f5"
    ]
    assert identifier_tokens("what changed in 3650f447a1b2c3d4e5f6?") == ["3650f447a1b2c3d4e5f6"]
    assert identifier_tokens("is r20260825 still the signing kid?") == ["r20260825"]
    assert identifier_tokens("what do we know about PR 109741") == ["109741"]
    assert identifier_tokens("what did I say about the Chicago trip in 2026?") == []
    assert identifier_tokens("released on 2026-09-12 as v2.1") == []
    assert identifier_tokens("") == []


def test_identifier_query_on_an_unpinned_semantic_default_fuses_lexical() -> None:
    plan = plan_memory_search(
        current_search_mode="semantic",
        current_hyde=False,
        current_include_top_n_unmatched=0,
        current_rerank="",
        query_text="value for key cd613e30-d8f1-4adf-91b7-584a2265b1f5",
        mode_pinned=False,
    )
    assert plan.search_mode == "bm25_hybrid"
    assert "lexical" in plan.planes and plan.reason == "identifier"
    assert plan.include_top_n_unmatched == 30


def test_identifier_rule_yields_to_a_pinned_mode_a_recipe_and_prose() -> None:
    pinned = plan_memory_search(
        current_search_mode="semantic",
        current_hyde=False,
        current_include_top_n_unmatched=0,
        current_rerank="",
        query_text="key cd613e30-d8f1-4adf-91b7-584a2265b1f5",
        mode_pinned=True,
    )
    assert pinned.search_mode == "semantic" and pinned.reason == "caller"
    recipe = plan_memory_search(
        current_search_mode="semantic",
        current_hyde=False,
        current_include_top_n_unmatched=0,
        current_rerank="",
        recipe="fast",
        query_text="key cd613e30-d8f1-4adf-91b7-584a2265b1f5",
        mode_pinned=False,
    )
    assert recipe.search_mode == "semantic" and recipe.planes == ["fact"]
    prose = plan_memory_search(
        current_search_mode="semantic",
        current_hyde=False,
        current_include_top_n_unmatched=0,
        current_rerank="",
        query_text="what hobbies do they enjoy",
        mode_pinned=False,
    )
    assert prose.search_mode == "semantic" and prose.reason == "caller"
    # Already lexical: nothing to add, the caller's route stands.
    lex = plan_memory_search(
        current_search_mode="lexical",
        current_hyde=False,
        current_include_top_n_unmatched=0,
        current_rerank="",
        query_text="key cd613e30-d8f1-4adf-91b7-584a2265b1f5",
        mode_pinned=False,
    )
    assert lex.search_mode == "lexical" and lex.reason == "caller_lexical"


def test_pin_identifier_hits_lifts_exact_matches_above_every_neighbour() -> None:
    from types import SimpleNamespace

    from server.memory.domain.query_planner import pin_identifier_hits

    def hit(key: str, text: str, score: float) -> SimpleNamespace:
        return SimpleNamespace(
            memory_key=key,
            content_excerpt=text,
            rank_score=score,
            score_components={"rank_score": score},
        )

    ident = "cd613e30-d8f1-4adf-91b7-584a2265b1f5"
    hits = [
        hit("kv_a", "Key: aaaa, Value: bbbb", 0.9),
        hit("kv_b", "Key: cccc, Value: dddd", 0.8),
        hit("kv_c", f"Key: {ident}, Value: eeee", 0.5),
        hit("kv_d", f"the value of {ident.upper()} is eeee", 0.4),
        hit("kv_e", "Key: ffff, Value: gggg", 0.3),
    ]
    assert pin_identifier_hits(hits, [ident]) == 2
    assert [h.memory_key for h in hits] == ["kv_c", "kv_d", "kv_a", "kv_b", "kv_e"]
    assert hits[0].rank_score > hits[1].rank_score > hits[2].rank_score == 0.9
    assert hits[0].score_components["identifier_match"] == 1.0
    assert "identifier_match" not in hits[2].score_components
    # Nothing to do: no identifiers, no matches, or everything matches.
    assert pin_identifier_hits(hits, []) == 0
    assert pin_identifier_hits([hit("x", "plain", 1.0)], [ident]) == 0
    only = [hit("y", f"{ident}", 1.0)]
    assert pin_identifier_hits(only, [ident]) == 1 and only[0].rank_score == 1.0
