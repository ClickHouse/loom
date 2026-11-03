"""bench/knowledge_retrieval.py — the scoring counts a passage once, from its own
document, and every gold passage of the fixture is in its document."""

from __future__ import annotations

import json
import math

import pytest

from bench.bench_namespaces import erase_on_exit_sync
from bench.knowledge_retrieval import (
    FIXED_FIELDS,
    FIXTURE,
    check_corpus,
    cluster_of,
    fixture_tag,
    ingest,
    load_fixture,
    mean_by_kind,
    parse_params,
    require_real_embedder,
    run_namespace,
    score_query,
    search,
)
from server.knowledge.domain.chunking import chunk_document
from server.settings import settings

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


def test_one_hit_holding_two_gold_passages_credits_both_at_its_rank() -> None:
    gold = [["a.md", "first"], ["a.md", "second"]]
    m = score_query([_hit("a.md", "first then second")], gold)
    assert m["all_gold@10"] == 1.0
    assert m["ndcg@10"] == 1.0
    # finding both lower never scores above finding both in one chunk at the same rank
    noise = _hit("x.md", "noise")
    together = score_query([noise, _hit("a.md", "first then second")], gold)["ndcg@10"]
    apart = score_query([noise, _hit("a.md", "first"), _hit("a.md", "second")], gold)["ndcg@10"]
    assert together > apart


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


@pytest.mark.parametrize("field", FIXED_FIELDS)
def test_the_fields_that_define_the_measurement_are_refused(field: str) -> None:
    with pytest.raises(SystemExit, match=f"--param {field} is refused: the harness fixes"):
        parse_params([f"{field}=x"])


@pytest.mark.parametrize("field", ["session_id", "explain", "traffic_class"])
def test_a_key_that_is_no_search_field_is_refused(field: str) -> None:
    with pytest.raises(SystemExit, match="not a knowledge.search field"):
        parse_params([f"{field}=x"])


def test_a_named_namespace_is_kept_and_a_made_up_one_is_not() -> None:
    assert run_namespace("knowledge-retrieval-eval") == ("knowledge-retrieval-eval", True)
    fresh, keep = run_namespace(None)
    assert fresh.startswith("knowledge-retrieval-run-") and keep is False
    assert run_namespace(None)[0] != fresh


def test_a_fresh_ingest_is_erased_when_the_run_ends() -> None:
    sent: list[tuple[str, dict]] = []

    def post(path: str, body: dict) -> dict:
        sent.append((path, body))
        return {}

    docs = [{"source_uri": "a.md", "content": "x", "tags": []}]
    fresh, keep = run_namespace(None)
    with erase_on_exit_sync(post, org="o", keep=keep):
        ingest(post, "o", fresh, docs, keep)
    erasures = [body for path, body in sent if path == "/v1/erasure.request"]
    assert len(erasures) == 1 and fresh in json.dumps(erasures[0])


def test_a_named_namespace_needs_the_prefix_and_cannot_be_a_runs_own() -> None:
    with pytest.raises(SystemExit, match="must start with"):
        run_namespace("docs")
    with pytest.raises(SystemExit, match="must start with"):
        run_namespace("knowledge-retrieval-run-1a2b3c4d")


@pytest.mark.parametrize("model", ["", "fake-ngram-1024"])
def test_a_run_without_a_real_embedder_is_refused(model: str) -> None:
    with pytest.raises(SystemExit, match="unknown or fake"):
        require_real_embedder({"embedding_model": model})
    require_real_embedder({"embedding_model": "text-embedding-3-large"})


def test_a_namespace_holding_other_documents_than_the_fixture_is_refused() -> None:
    docs = [{"source_uri": "a.md", "content": "x"}, {"source_uri": "b.md", "content": "y"}]
    tag = fixture_tag(docs)
    pages = {None: {"rows": [{"source_uri": "a.md", "tags": [tag]}], "next_cursor": "c"}}
    pages["c"] = {"rows": []}
    with pytest.raises(SystemExit, match="missing 1, extra 0, from another version 0"):
        check_corpus(lambda path, body: pages[body["cursor"]], "o", "n", docs)
    pages["c"] = {"rows": [{"source_uri": "b.md", "tags": [tag]}]}
    check_corpus(lambda path, body: pages[body["cursor"]], "o", "n", docs)
    # the same documents from an edited corpus carry another tag
    pages["c"] = {"rows": [{"source_uri": "b.md", "tags": ["fixture:000000000000"]}]}
    with pytest.raises(SystemExit, match="from another version 1"):
        check_corpus(lambda path, body: pages[body["cursor"]], "o", "n", docs)
    assert fixture_tag([*docs[:1], {**docs[1], "content": "z"}]) != tag


def test_the_top_similarity_is_reported_per_answerability() -> None:
    queries = [
        {"id": "a", "query": "x", "gold": [["a.md", "y"]], "kind": "context"},
        {"id": "b", "query": "z", "gold": [], "kind": "unanswerable"},
    ]
    hit = {"source_uri": "a.md", "content": "y", "similarity": 0.25, "lexical_score": 1.0}
    report = search(lambda path, body: {"results": [hit]}, "o", "n", queries, {})
    assert report["top_similarity"] == {
        "answerable": 0.25,
        "unanswerable": 0.25,
        "no_hits": {"answerable": 0, "unanswerable": 0},
    }
    # an unanswerable query with no hit is counted apart, not averaged as 0
    queries.append({"id": "c", "query": "w", "gold": [], "kind": "unanswerable"})
    report = search(
        lambda path, body: {"results": [] if body["query"] == "w" else [hit]}, "o", "n", queries, {}
    )
    assert report["top_similarity"] == {
        "answerable": 0.25,
        "unanswerable": 0.25,
        "no_hits": {"answerable": 0, "unanswerable": 1},
    }
    assert report["metrics"]["all"]["n"] == 1 and report["metrics"]["context"]["mrr"] == 1.0


def test_lexical_fired_counts_the_queries_whose_hits_the_lexical_leg_scored() -> None:
    queries = [{"id": i, "query": i, "gold": [["a.md", "y"]], "kind": "context"} for i in "pq"]
    scored = {"source_uri": "a.md", "content": "y", "similarity": 0.5, "lexical_score": 1.0}
    vector_only = {**scored, "lexical_score": 0.0}
    report = search(
        lambda path, body: {"results": [scored if body["query"] == "p" else vector_only]},
        "o",
        "n",
        queries,
        {},
    )
    assert report["lexical_fired"] == 1


def test_kinds_average_apart_and_together() -> None:
    rows = [
        {"kind": "context", "metrics": {"mrr": 1.0}},
        {"kind": "paraphrase", "metrics": {"mrr": 0.0}},
    ]
    assert mean_by_kind(rows) == {
        "all": {"mrr": 0.5, "n": 2},
        "context": {"mrr": 1.0, "n": 1},
        "paraphrase": {"mrr": 0.0, "n": 1},
    }


def test_a_kept_ingest_registers_as_memory_and_tags_each_upsert() -> None:
    docs = [{"source_uri": "a.md", "content": "x", "tags": ["t"]}]
    sent: list[tuple[str, dict]] = []

    def post(path: str, body: dict) -> dict:
        sent.append((path, body))
        return {}

    ingest(post, "o", "knowledge-retrieval-x", docs, keep=True)
    # a kept namespace registers as memory, so the idle-namespace gc leaves it
    assert sent[0][0] == "/v1/namespace.register" and sent[0][1]["kind"] == "memory"
    upserts = [body for path, body in sent if path == "/v1/knowledge.upsert"]
    assert [u["tags"] for u in upserts] == [["t", fixture_tag(docs)]]


def test_a_lexical_arm_whose_lexical_leg_never_fired_fails() -> None:
    queries = [{"id": "q", "query": "x", "gold": [["a.md", "y"]], "kind": "context"}]
    vector_only = {"source_uri": "a.md", "content": "y", "similarity": 0.5}
    with pytest.raises(SystemExit, match="lexical leg never fired"):
        search(lambda path, body: {"results": [vector_only]}, "o", "n", queries, {})
    semantic = {"search_mode": "semantic"}
    report = search(lambda path, body: {"results": [vector_only]}, "o", "n", queries, semantic)
    assert report["metrics"]["all"]["mrr"] == 1.0


def test_a_search_that_finds_nothing_or_ignores_a_param_fails() -> None:
    queries = [{"id": "q", "query": "x", "gold": [["a.md", "y"]], "kind": "context"}]
    with pytest.raises(SystemExit, match="was it ingested"):
        search(lambda path, body: {"results": []}, "o", "n", queries, {})
    with pytest.raises(SystemExit, match="ignored"):
        search(
            lambda path, body: {"results": [], "ignored_args": ["serch_mode"]},
            "o",
            "n",
            queries,
            {},
        )


def test_every_gold_passage_is_in_its_document() -> None:
    docs, queries = load_fixture(FIXTURE)
    content = {d["source_uri"]: d["content"] for d in docs}
    assert len(content) == len(docs)
    for q in queries:
        for uri, text in q["gold"]:
            assert content[uri].count(text) == 1, q["id"]
    assert len({q["id"] for q in queries}) == len(queries)


def test_every_gold_passage_lies_inside_one_chunk_at_the_default_size() -> None:
    docs, queries = load_fixture(FIXTURE)
    size, stride = settings.knowledge_chunk_size, settings.knowledge_chunk_stride
    chunks = {d["source_uri"]: chunk_document(d["content"], size=size, stride=stride) for d in docs}
    for q in queries:
        holders = []
        for uri, text in q["gold"]:
            held = [c.idx for c in chunks[uri] if text in c.text]
            assert held, q["id"]
            holders.append((uri, held[0]))
        # each passage of a query in a chunk of its own: nDCG's cap never applies here
        assert len(set(holders)) == len(holders), q["id"]
    assert sum(len(c) for c in chunks.values()) == 97  # one chunk per non-empty section


@pytest.mark.parametrize("phase", ["ingest", "search"])
def test_one_phase_alone_needs_a_named_namespace(monkeypatch, phase: str) -> None:
    from bench import knowledge_retrieval

    monkeypatch.setattr("sys.argv", ["knowledge_retrieval.py", "--phase", phase])
    with pytest.raises(SystemExit) as exit_info:
        knowledge_retrieval.main()
    assert exit_info.value.code == 2  # argparse's usage error, before any request


def test_the_handbook_questions_cluster_by_template_and_the_rest_stand_alone() -> None:
    assert cluster_of("harbor-retention") == cluster_of("lantern-retention") == "retention"
    assert cluster_of("rb-E4031") == "rb-E4031" and cluster_of("all-sla") == "all-sla"


def test_the_corpus_check_returns_the_chunk_count_the_run_measured() -> None:
    docs = [{"source_uri": "a.md", "content": "x"}]
    row = {"source_uri": "a.md", "tags": [fixture_tag(docs)], "chunk_count": 3}
    assert check_corpus(lambda path, body: {"rows": [row]}, "o", "n", docs) == 3
