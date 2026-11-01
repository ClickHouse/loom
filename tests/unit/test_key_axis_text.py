"""The key-axis text: one builder for every write path and the re-embed, none
for an imported item's key (which the re-embed never sends), a key-only
re-embed that leaves value vectors alone, and the bench's instance scoring."""

from __future__ import annotations

from typing import Any

import pytest

from bench.k_curve import QuestionAtK, gold_instances, instance_in, score, summarise
from server.memory.app.reembed import _TABLES, _key_input, rewrite_row, where_sql
from server.memory.domain.embed_text import key_axis_text
from server.memory.domain.import_keys import import_key


def test_key_axis_text_is_the_humanized_key() -> None:
    assert key_axis_text("melanie_me_time_activities") == "melanie me time activities"
    assert key_axis_text("") == ""


def test_an_import_key_has_no_key_axis_text() -> None:
    digest = import_key("Prefers dark roast.")
    assert key_axis_text(digest) == "" and _key_input({"memory_key": digest}) == ""
    assert key_axis_text("import:dark_roast") == "import:dark roast"


def test_reembed_key_input_uses_the_same_builder() -> None:
    row = {
        "memory_key": "john_yoga_experience",
        "tags": ["health", "fitness"],
        "entities": ["john"],
    }
    assert _key_input(row) == key_axis_text("john_yoga_experience") == "john yoga experience"


def test_key_only_where_selects_every_live_row_but_an_import_key_without_a_vector() -> None:
    cols = ["memory_key", "tombstoned_at"]
    sql, params = where_sql(cols, "m", org="o", namespace="n", key_only=True)
    assert "embedding_model" not in sql
    assert sql == (
        "NOT (match(memory_key, '^import:[0-9a-f]{24}$') AND memory_key_embedding_dim = 0)"
        " AND embedding_dim > 0 AND tombstoned_at IS NULL AND org = {org:String}"
        " AND namespace = {ns:String}"
    )
    assert params == {"org": "o", "ns": "n"}
    sql_default, _ = where_sql(cols, "m", org=None, namespace=None)
    assert sql_default.startswith("embedding_model != 'm' AND embedding_dim > 0")


def test_key_only_rewrite_keeps_the_value_vector() -> None:
    cfg = _TABLES["memory_embeddings"]
    row = {
        "memory_key": "k",
        "embedding": [1.0, 2.0],
        "memory_key_embedding": [0.0, 0.0],
        "memory_key_embedding_dim": 0,
        "embedding_model": "m",
        "embedding_dim": 2,
        "version": 1,
    }
    out = rewrite_row(row, cfg, "m", 2, None, [0.5, 0.5])
    assert out["embedding"] == [1.0, 2.0]
    assert out["memory_key_embedding"] == [0.5, 0.5] and out["memory_key_embedding_dim"] == 2
    assert out["version"] > 1


def test_a_rewrite_without_a_key_vector_leaves_the_key_dim_at_zero() -> None:
    cfg = _TABLES["memory_embeddings"]
    row = {
        "memory_key": import_key("Prefers dark roast."),
        "embedding": [1.0, 2.0],
        "memory_key_embedding": [0.5, 0.5],
        "memory_key_embedding_dim": 2,
        "embedding_model": "m",
        "embedding_dim": 2,
        "version": 1,
    }
    out = rewrite_row(row, cfg, "m2", 2, [3.0, 4.0], [])
    assert out["memory_key_embedding"] == [] and out["memory_key_embedding_dim"] == 0


@pytest.mark.asyncio
async def test_a_reembed_never_sends_an_import_key_to_the_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.llm import customer
    from server.memory.app import reembed

    class _Emb:
        model_name, dim = "m2", 2

        def __init__(self) -> None:
            self.batches: list[list[str]] = []

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            self.batches.append(list(texts))
            return [[0.1, 0.2] for _ in texts]

    class _CH:
        rows: list[dict[str, Any]] = []

        async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
            self.rows = rows

    async def _no_writers(_rows: Any) -> dict[tuple[str, ...], str]:
        return {}

    async def _one_group(rows: Any) -> dict[Any, list[int]]:
        return {customer.Attribution(): list(range(len(list(rows))))}

    monkeypatch.setattr(reembed, "memory_writers", _no_writers)
    monkeypatch.setattr(customer, "groups", _one_group)
    digest = import_key("Prefers dark roast.")
    stored = {
        "org": "o",
        "namespace": "n",
        "embedding_model": "m1",
        "embedding_dim": 2,
        "memory_key_embedding": [0.0, 0.0],
        "memory_key_embedding_dim": 0,
        "version": 1,
    }
    chunk = [
        {**stored, "memory_key": digest, "content_excerpt": '"Prefers dark roast."'},
        {**stored, "memory_key": "coffee_order", "content_excerpt": '"Dark." (#coffee_order)'},
    ]
    emb, ch = _Emb(), _CH()
    await reembed._rewrite_page(ch, emb, "memory_embeddings", chunk, key_only=False)
    assert emb.batches == [['"Prefers dark roast."', '"Dark." (#coffee_order)'], ["coffee order"]]
    dims = {r["memory_key"]: r["memory_key_embedding_dim"] for r in ch.rows}
    assert dims == {digest: 0, "coffee_order": 2}
    # A key-only pass over an import key with a stale vector clears it, calling no embedder.
    emb = _Emb()
    stale = {**chunk[0], "memory_key_embedding": [0.3, 0.4], "memory_key_embedding_dim": 2}
    await reembed._rewrite_page(ch, emb, "memory_embeddings", [stale], key_only=True)
    assert emb.batches == []
    assert [(r["memory_key_embedding"], r["memory_key_embedding_dim"]) for r in ch.rows] == [
        ([], 0)
    ]


def test_gold_instances_split_set_answers_and_drop_counts() -> None:
    assert gold_instances("clarinet and violin") == ["clarinet", "violin"]
    assert gold_instances("\"Little Women\",'A Court of Thorns andRoses'.") == [
        "little women",
        "a court of thorns androses",
    ]
    assert gold_instances("Seattle, Chicago, New York, and Paris.") == [
        "seattle",
        "chicago",
        "new york",
        "paris",
    ]
    assert gold_instances("2") == [] and gold_instances("two") == []
    assert gold_instances("Sweden") == ["sweden"]


def test_instance_in_is_lexical_with_a_prefix_allowance() -> None:
    text = "Melanie took her family camping in the mountains last week, roasted marshmallows"
    assert instance_in("mountains", text) and instance_in("roast marshmallows", text)
    assert not instance_in("beach", text)
    assert instance_in("lord of the rings", "Joanna watched 'The Lord of the Rings' Trilogy")


def test_score_and_summarise_carry_instance_recall() -> None:
    hits = [
        {"session_id": "ns:session_1", "content_excerpt": "Melanie plays the clarinet"},
        {"session_id": "ns:session_2", "content_excerpt": "me-time: running or playing her violin"},
    ]
    r2 = score(hits, "ns", {1, 2}, 2, "multi_hop", answer="clarinet and violin")
    r1 = score(hits, "ns", {1, 2}, 1, "multi_hop", answer="clarinet and violin")
    single = score(hits, "ns", {1}, 2, "multi_hop", answer="Sweden")
    assert (r2.instances, r2.instances_found) == (2, 2)
    assert (r1.instances, r1.instances_found) == (2, 1)
    assert single.instances == 0  # one instance is not a set
    pts = summarise([r1, r2, QuestionAtK("multi_hop", 2, 1, 1.0, True, 10, 2)])
    p2 = next(p for p in pts if p.k == 2)
    assert p2.n == 2 and p2.n_set == 1 and p2.inst_recall_mean == 1.0 and p2.all_inst_share == 1.0
    p1 = next(p for p in pts if p.k == 1)
    assert p1.n_set == 1 and p1.inst_recall_mean == 0.5 and p1.all_inst_share == 0.0
