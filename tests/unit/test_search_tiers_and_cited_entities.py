"""Cited entities, anchor tiers and the anchor pool on the memory.search read path."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


async def test_unit_budget_buys_one_body_per_cited_entity_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.store as repo
    from server.memory.app.search import payload as h

    def hit(key: str, ents: list[str]) -> SimpleNamespace:
        return SimpleNamespace(
            memory_key=key,
            source="fact",
            namespace="ns",
            session_id="-",
            entities=ents,
            content_excerpt="x" * 1200,
            body_truncated=False,
            next="",
        )

    # rank order: two bodies about eric, then zoey's; each body ~1,000 tokens
    hits = [hit("eric_1", ["eric"]), hit("eric_2", ["eric"]), hit("zoey_1", ["zoey"])]

    async def fake_get_many(
        identity: Any, keys: list[str], *, scope: Any, exclude_high_sensitivity: bool
    ) -> list[Any]:
        assert exclude_high_sensitivity
        return [SimpleNamespace(memory_key=k, value="v" * 4000, sensitivity="") for k in keys]

    monkeypatch.setattr(repo, "get_many_by_keys", fake_get_many)
    inp = SimpleNamespace(
        bodies="unit",
        body_budget_tokens=2000,
        org="o",
        namespace="ns",
        agent="*",
        user_id="-",
        session_id="-",
    )
    returned, elided, _ = await h._expand_unit_bodies(hits, inp, prefer_entities=["eric", "zoey"])
    assert (returned, elided) == (2, 1)
    whole = {x.memory_key for x in hits if not x.body_truncated}
    assert whole == {"eric_1", "zoey_1"}  # not eric_2: one body per cited entity first


async def test_anchor_first_places_the_oldest_run_memory_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The run's first memory is a tier, ahead of similarity.
    import server.memory.store as repo
    from server.memory.app.search import tiers as h

    calls: list[dict[str, Any]] = []

    async def fake_list(
        ident: Any,
        *,
        order_by: str,
        limit: int,
        scope: Any,
        exclude_high_sensitivity: bool,
        **_: Any,
    ) -> list[Any]:
        assert exclude_high_sensitivity  # a 'high' row is never in a search leg
        calls.append({"order_by": order_by, "limit": limit})
        return [
            SimpleNamespace(
                memory_key="base",
                value="the base plan",
                memory_type="semantic",
                run_id="r1",
                created_at="2026-01-01",
            ),
            SimpleNamespace(
                memory_key="later",
                value="later",
                memory_type="semantic",
                run_id="r1",
                created_at="2026-01-02",
            ),
        ]

    monkeypatch.setattr(repo, "list_by_namespace", fake_list)
    sim = SimpleNamespace(memory_key="sim", rank_score=0.9)
    inp = SimpleNamespace(
        anchor_first=1,
        org="o",
        namespace="ns",
        agent="*",
        user_id="-",
        session_id="-",
        run_id="r1",
        top_k=3,
    )
    out, _ = await h._prepend_anchor([sim], inp, {})
    assert [x.memory_key for x in out] == ["base", "sim"]
    assert out[0].score_components == {"anchor_first": 1.0}
    assert calls[0]["order_by"] == "created_at_asc" and calls[0]["limit"] == 4


async def test_canonical_lookup_keeps_the_cited_order() -> None:
    # The bodies="unit" budget reads the list in order, so the lookup
    # must not re-sort a cited-first list; unseen aliases follow, sorted.
    from server.memory.app.entity_resolution import lookup_canonical_entities

    class _CH:
        async def query_all(self, sql: str, params: dict | None = None) -> list[dict]:
            return [{"canonical_text": "zoey"}, {"canonical_text": "aaron"}]

    out = await lookup_canonical_entities(
        _CH(), org="o", namespaces=["ns"], entity_texts=["noah", "aurora", "bart"]
    )
    assert out == ["noah", "aurora", "bart", "aaron", "zoey"]


def test_tier_displaces_the_window_tail() -> None:
    from server.memory.app.search import tiers as h

    tier = [SimpleNamespace(memory_key="base")]
    hits = [SimpleNamespace(memory_key=f"s{i}") for i in range(3)]
    inp = SimpleNamespace(top_k=3)
    assert [x.memory_key for x in h._tier_window(tier, hits, inp)] == ["base", "s0", "s1"]


def test_anchor_pool_where_carries_the_run() -> None:
    # The entity/temporal anchor pool read past the run.
    from server.memory.app.search import sql as h

    params: dict[str, Any] = {}
    parts = h._anchor_pool_where(SimpleNamespace(run_id="r1"), params)
    assert "e.run_id = {run_id:String}" in parts and params["run_id"] == "r1"
    params = {}
    parts = h._anchor_pool_where(SimpleNamespace(run_id=""), params)
    assert not any("run_id" in p for p in parts) and "run_id" not in params
