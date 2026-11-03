"""bench/knowledge_retrieval.py — the scoring counts a passage once, from its own
document, and every gold passage of the fixture is in its document."""

from __future__ import annotations

import math

import pytest

from bench.knowledge_retrieval import (
    FIXTURE,
    load_fixture,
    parse_params,
    run_namespace,
    score_query,
)

GOLD = [["a.md", "answer one"], ["b.md", "answer two"]]


def _hit(uri: str, content: str) -> dict:
    return {"source_uri": uri, "content": content}


def test_a_hit_counts_only_from_its_document_and_with_its_text() -> None:
    hits = [_hit("b.md", "answer one"), _hit("a.md", "other"), _hit("a.md", "x answer one y")]
    m = score_query(hits, GOLD[:1])
    assert m["recall@1"] == 0.0
    assert m["recall@5"] == 1.0
    assert m["mrr"] == 1 / 3


def test_all_gold_needs_every_passage_and_counts_each_once() -> None:
    hits = [_hit("a.md", "answer one"), _hit("a.md", "answer one again")]
    m = score_query(hits, GOLD)
    assert m["all_gold@10"] == 0.0
    assert m["ndcg@10"] == 1.0 / (1.0 + 1.0 / math.log2(3))
    m = score_query([*hits, _hit("b.md", "answer two")], GOLD)
    assert m["all_gold@10"] == 1.0


def test_one_hit_holding_two_gold_passages_credits_both() -> None:
    gold = [["a.md", "first"], ["a.md", "second"]]
    m = score_query([_hit("a.md", "first then second")], gold)
    assert m["all_gold@10"] == 1.0
    assert m["ndcg@10"] == 1.0


def test_a_miss_scores_zero() -> None:
    m = score_query([_hit("c.md", "nothing")], GOLD)
    assert m == {
        "recall@1": 0.0,
        "recall@5": 0.0,
        "recall@10": 0.0,
        "all_gold@10": 0.0,
        "mrr": 0.0,
        "ndcg@10": 0.0,
    }


def test_params_take_json_values_or_plain_strings() -> None:
    assert parse_params(["lexical_weight=0.5", "search_mode=semantic", "narrow_spans=true"]) == {
        "lexical_weight": 0.5,
        "search_mode": "semantic",
        "narrow_spans": True,
    }


@pytest.mark.parametrize("field", ["top_k", "query", "namespace", "namespaces", "namespace_scope"])
def test_the_fields_that_define_the_measurement_are_refused(field: str) -> None:
    with pytest.raises(SystemExit, match=field):
        parse_params([f"{field}=x"])


def test_only_a_namespace_the_harness_made_up_is_erased() -> None:
    assert run_namespace("docs", "both", False) == ("docs", True)
    assert run_namespace("docs", "search", False) == ("docs", True)
    fresh, keep = run_namespace(None, "both", False)
    assert fresh.startswith("knowledge-retrieval-") and keep is False
    assert run_namespace(None, "both", False)[0] != fresh
    assert run_namespace(None, "ingest", False)[1] is True
    assert run_namespace(None, "both", True)[1] is True


def test_every_gold_passage_is_in_its_document() -> None:
    docs, queries = load_fixture(FIXTURE)
    content = {d["source_uri"]: d["content"] for d in docs}
    assert len(content) == len(docs)
    for q in queries:
        for uri, text in q["gold"]:
            assert content[uri].count(text) == 1, q["id"]
    assert len({q["id"] for q in queries}) == len(queries)
