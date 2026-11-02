"""Behavior-preserving parallelization of the retrieval legs.

The handler issues the result-independent retrieval legs concurrently:
  * within ``vector_search``: the primary vector facts, verbatim chunks, the
    BM25-only lexical union, and schema records run in one ``asyncio.gather``;
  * in the handler: the session-summary, current-state-observation, and
    entity-store-ANN legs launch as tasks BEFORE the vector legs and are
    joined just before the assembler;
  * graph mode's per-namespace neighbor expansion
    runs as one ``asyncio.gather`` over namespaces.

This is pure scheduling: the FUSED result must be identical to a sequential
issuance, independent of the order in which the legs' CH reads complete. These
tests force out-of-order completion (reversed / shuffled delays) and assert the
fused candidate set + scores are unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.domain.search_types import SearchHit
from tests.unit.memory_search_fakes import install_search_fakes


def _svc_hit(key: str, *, sim: float, rank: float | None = None) -> SearchHit:
    # The service SearchHit carries no namespace — the handler tags each hit
    # with the namespace of the SearchQuery it was returned for during the
    # cross-namespace merge.
    return SearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=f"fact about {key}",
        distance=1.0 - sim,
        rank_score=rank if rank is not None else sim,
    )


def _input(**overrides: Any) -> MemorySearchInput:
    base: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "agent": "a",
        "query": "what is my favorite color",
        "namespace_scope": "this",
        "top_k": 5,
        "auto_touch": False,
    }
    base.update(overrides)
    return MemorySearchInput(**base)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    return install_search_fakes(monkeypatch)


async def _run(**overrides: Any):
    from server.memory.app.search import handle_memory_search

    return await handle_memory_search(_input(**overrides))


# ---------------------------------------------------------------------------
# The parallelized default path fuses the SAME candidates/scores
# regardless of leg completion order.
# ---------------------------------------------------------------------------


async def test_default_path_fusion_is_deterministic(env) -> None:
    """The default (rrf) read path runs its legs concurrently. Two identical
    queries must fuse byte-identical candidate keys + rank_scores — the gather
    does not introduce order-dependent nondeterminism."""
    env.hits_by_ns["ns"] = [
        _svc_hit("a", sim=0.90, rank=1.00),
        _svc_hit("b", sim=0.80, rank=0.80),
        _svc_hit("c", sim=0.70, rank=0.60),
    ]
    out1 = await _run()
    out2 = await _run()
    keys1 = [(r.namespace, r.memory_key) for r in out1.results]
    keys2 = [(r.namespace, r.memory_key) for r in out2.results]
    scores1 = [round(r.rank_score, 9) for r in out1.results]
    scores2 = [round(r.rank_score, 9) for r in out2.results]
    assert keys1 == keys2
    assert scores1 == scores2


async def test_graph_neighbor_gather_is_order_independent(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-namespace graph-neighbor expansion
    builds graph_expanded_by_ns from independent CH reads. Forcing the reads
    to complete in REVERSE order (ns_b finishes before ns_a) must yield the
    SAME per-namespace expansion reaching each SearchQuery — the map is keyed
    by namespace, not by completion order."""
    # Two namespaces in the caller's scope; graph mode pre-expands each.
    env.hits_by_ns = {
        "ns_a": [_svc_hit("ka", sim=0.9, rank=1.0)],
        "ns_b": [_svc_hit("kb", sim=0.9, rank=1.0)],
    }
    env.neighbors_by_ns = {
        "ns_a": ["alpha", "beta"],
        "ns_b": ["gamma"],
    }
    # ns_a's read takes longer than ns_b's → ns_b completes first.
    env.neighbor_delays = {"ns_a": 0.02, "ns_b": 0.0}

    async def _run_graph():
        from server.memory.app.search import handle_memory_search

        return await handle_memory_search(
            _input(
                search_mode="graph",
                namespace="ns_a",
                namespace_scope="all_mine",
                entity_filter=["seed"],
            )
        )

    # Patch namespace resolution so the handler fans out to both namespaces.
    import server.auth.context as auth_ctx

    async def fake_my_ns(org: str, subject: str | None = None) -> list[str]:
        return ["ns_a", "ns_b"]

    monkeypatch.setattr(auth_ctx, "resolve_my_namespaces", fake_my_ns)
    env.search_queries.clear()
    await _run_graph()
    # ns_b's neighbor read completed before ns_a's (reverse of issue order)…
    assert env.neighbor_call_order  # both were called
    # …yet each SearchQuery carries the expansion for ITS OWN namespace.
    graph_by_ns = {q.namespace: list(q.graph_expanded) for q in env.search_queries}
    assert graph_by_ns["ns_a"] == ["alpha", "beta"]
    assert graph_by_ns["ns_b"] == ["gamma"]


async def test_summary_leg_runs_concurrently_and_folds_in(env) -> None:
    """The session-summary leg launches as a task before the vector legs and
    is folded into the fused pool additively. With no summaries it is a no-op
    on the candidate set — the fused result is exactly the vector pool. This
    guards that the concurrent launch/join does not drop or reorder hits."""
    env.hits_by_ns["ns"] = [
        _svc_hit("a", sim=0.90, rank=1.0),
        _svc_hit("b", sim=0.80, rank=0.8),
    ]
    out = await _run()
    fused_keys = {(r.namespace, r.memory_key) for r in out.results}
    assert ("ns", "a") in fused_keys
    assert ("ns", "b") in fused_keys


# ---------------------------------------------------------------------------
# Scoped-token tier waterfall — a priority-ordered cross-namespace search
# STOPS at a tier only when its best hit clears
# `settings.memory_search_tier_stop_similarity`. That stop cutoff is decoupled
# from the return noise floor (`memory_search_min_similarity`): a hit that is
# good enough to RETURN is not necessarily good enough to SUPPRESS a
# lower-priority tier. These pin that two-cutoff behavior.
# ---------------------------------------------------------------------------

_TIER_SECRET = "unit-secret-unit-secret-unit-secret-32"


def _scoped_auth(org: str, **ns_roles: str):
    """A verified scoped token: namespaces = ns_roles keys in insertion order
    (= read priority), authoritative per-namespace roles."""
    from server.auth.jwt import mint_token, verify_token

    tok = mint_token(
        "sa:bot",
        org,
        namespaces=list(ns_roles.keys()),
        roles=[],
        ns_roles=dict(ns_roles),
        secret=_TIER_SECRET,
    )
    return verify_token(tok, secret=_TIER_SECRET)


async def _run_scoped(auth, **overrides: Any):
    from server.auth.context import set_current_auth

    set_current_auth(auth)
    try:
        return await _run(**overrides)
    finally:
        set_current_auth(None)


async def test_tier_waterfall_stops_on_confident_hit(env) -> None:
    """A higher-priority hit clearing the stop cutoff (default 0.40) suppresses
    the lower-priority search entirely — even when the lower tier holds an
    objectively stronger memory. This is the intended 'private wins' behavior."""
    auth = _scoped_auth("o", priv="admin", pub="viewer")  # priv-first priority
    env.hits_by_ns["priv"] = [_svc_hit("p1", sim=0.55)]
    env.hits_by_ns["pub"] = [_svc_hit("u1", sim=0.90)]  # stronger, must stay hidden
    await _run_scoped(auth, namespace="priv", namespace_scope="all_mine")
    queried = {q.namespace for q in env.search_queries}
    assert "priv" in queried
    assert "pub" not in queried


async def test_tier_waterfall_falls_through_on_middling_hit(env) -> None:
    """A higher-priority hit that clears the RETURN floor (0.22) but not the
    STOP cutoff (0.40) must NOT trap the read: the lower-priority tier is still
    searched so its stronger memory can surface. A single low cutoff would trap
    it."""
    auth = _scoped_auth("o", priv="admin", pub="viewer")
    env.hits_by_ns["priv"] = [_svc_hit("p1", sim=0.30)]  # returnable, not confident
    env.hits_by_ns["pub"] = [_svc_hit("u1", sim=0.90)]
    await _run_scoped(auth, namespace="priv", namespace_scope="all_mine")
    queried = {q.namespace for q in env.search_queries}
    assert {"priv", "pub"} <= queried


async def test_tier_stop_cutoff_is_setting_driven(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stop cutoff reads `settings.memory_search_tier_stop_similarity` at
    call time (not a hardcoded value): lowering it below a middling hit
    makes that hit suppress the lower tier again."""
    import server.settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "memory_search_tier_stop_similarity", 0.25)
    auth = _scoped_auth("o", priv="admin", pub="viewer")
    env.hits_by_ns["priv"] = [_svc_hit("p1", sim=0.30)]  # now above the 0.25 cutoff
    env.hits_by_ns["pub"] = [_svc_hit("u1", sim=0.90)]
    await _run_scoped(auth, namespace="priv", namespace_scope="all_mine")
    queried = {q.namespace for q in env.search_queries}
    assert "pub" not in queried


async def test_tier_waterfall_reads_an_addressed_namespace_first_when_the_token_lacks_it(
    env,
) -> None:
    """A scoped token's own order is kept, but an addressed namespace its list
    lacks (of a kind the fan-out does not sweep, or reached by name) leads the
    waterfall, so a confident hit further down cannot keep it from being read."""
    auth = _scoped_auth("o", priv="admin", pub="viewer")
    env.hits_by_ns["mine"] = [_svc_hit("m1", sim=0.30)]
    env.hits_by_ns["priv"] = [_svc_hit("p1", sim=0.55)]
    await _run_scoped(auth, namespace="mine", namespace_scope="all_mine")
    queried = list(dict.fromkeys(q.namespace for q in env.search_queries))
    assert queried == ["mine", "priv"]


async def test_the_output_names_the_namespaces_the_cap_left_out(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.auth.context as auth_ctx
    import server.settings as settings_mod

    async def mine(org: str, subject: str | None = None) -> list[str]:
        return ["ns_a", "ns_b", "ns_c"]

    monkeypatch.setattr(auth_ctx, "resolve_my_namespaces", mine)
    monkeypatch.setattr(settings_mod.settings, "memory_search_fanout_max_namespaces", 1)
    out = await _run(namespace="ns_b", namespace_scope="all_mine")
    assert out.namespaces_searched == ["ns_b", "ns_a"]
    assert out.namespaces_skipped == ["ns_c"]


async def test_private_first_reads_an_addressed_public_namespace_in_the_first_pass(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confident private hit skips the public pass, but never the namespace
    the caller addressed: it joins the private pass whatever its visibility."""
    import server.auth.context as auth_ctx
    import server.identity.app.namespace_visibility as nv

    async def mine(org: str, subject: str | None = None) -> list[str]:
        return ["home", "team", "wiki"]

    async def visibility(org: str, namespaces: list[str]) -> dict[str, str]:
        return {"home": "private", "team": "public", "wiki": "public"}

    monkeypatch.setattr(auth_ctx, "resolve_my_namespaces", mine)
    monkeypatch.setattr(nv, "visibility_map", visibility)
    env.hits_by_ns["home"] = [_svc_hit("h1", sim=0.80)]
    env.hits_by_ns["team"] = [_svc_hit("t1", sim=0.45)]
    out = await _run(namespace="team", namespace_scope="all_mine")
    assert {q.namespace for q in env.search_queries} == {"team", "home"}
    assert ("team", "t1") in {(r.namespace, r.memory_key) for r in out.results}


async def test_tier_waterfall_reads_the_addressed_namespace_before_the_tokens_first(env) -> None:
    """The token lists the addressed namespace second; a confident hit in the
    first must not end the walk before the namespace the caller asked for."""
    auth = _scoped_auth("o", team="viewer", mine="writer")
    env.hits_by_ns["team"] = [_svc_hit("t1", sim=0.60)]
    env.hits_by_ns["mine"] = [_svc_hit("m1", sim=0.35)]
    await _run_scoped(auth, namespace="mine", namespace_scope="all_mine")
    queried = list(dict.fromkeys(q.namespace for q in env.search_queries))
    assert queried == ["mine", "team"]
