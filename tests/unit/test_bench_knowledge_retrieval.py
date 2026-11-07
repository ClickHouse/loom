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
    SEARCH_FIELDS,
    check_corpus,
    fixture_tag,
    ingest,
    load_fixture,
    main,
    mean_by_kind,
    parse_params,
    require_real_embedder,
    run_namespace,
    score_query,
    search,
)
from server.knowledge.api.models_knowledge import KnowledgeSearchInput
from server.knowledge.app.writes import _chunk_text


def _ingest_chunks(docs: list[dict]) -> dict[str, list]:
    """Each document's chunks as ingest makes them, at the configured size and stride."""
    return {d["source_uri"]: _chunk_text(d["content"]) for d in docs}


# The handbook products: one question asked of each is one cluster.
PRODUCTS = ("harbor", "lantern", "quill", "meridian")

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


@pytest.mark.parametrize("pair", ['block_types=["paragraph"]', 'formats=["markdown"]'])
def test_a_filter_on_what_is_searched_is_refused(pair: str) -> None:
    with pytest.raises(SystemExit, match="the harness fixes"):
        parse_params([pair])


def test_every_search_field_is_either_a_knob_or_fixed() -> None:
    """A field added to knowledge.search is refused until it is named in one list."""
    assert not SEARCH_FIELDS & set(FIXED_FIELDS)
    assert SEARCH_FIELDS | set(FIXED_FIELDS) == set(KnowledgeSearchInput.model_fields)


def test_a_param_without_a_value_is_refused() -> None:
    with pytest.raises(SystemExit, match="needs KEY=VALUE"):
        parse_params(["narrow_spans"])


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
    first = {"source_uri": "a.md", "tags": [tag], "chunk_count": 1}
    pages = {None: {"rows": [first], "next_cursor": "c"}}
    pages["c"] = {"rows": []}
    with pytest.raises(SystemExit, match="missing 1, extra 0, from another version 0"):
        check_corpus(lambda path, body: pages[body["cursor"]], "o", "n", docs)
    pages["c"] = {"rows": [{"source_uri": "b.md", "tags": [tag], "chunk_count": 2}]}
    assert check_corpus(lambda path, body: pages[body["cursor"]], "o", "n", docs) == 3
    # the same documents from an edited corpus carry another tag
    pages["c"] = {"rows": [{"source_uri": "b.md", "tags": ["fixture:000000000000"]}]}
    with pytest.raises(SystemExit, match="from another version 1"):
        check_corpus(lambda path, body: pages[body["cursor"]], "o", "n", docs)
    # a document an interrupted write left without chunks is not this corpus either
    pages["c"] = {"rows": [{"source_uri": "b.md", "tags": [tag], "chunk_count": 0}]}
    with pytest.raises(SystemExit, match="without chunks 1"):
        check_corpus(lambda path, body: pages[body["cursor"]], "o", "n", docs)
    assert fixture_tag([*docs[:1], {**docs[1], "content": "z"}]) != tag


def test_the_top_similarity_is_reported_per_answerability() -> None:
    queries = [
        {"id": "a", "query": "x", "gold": [["a.md", "y"]], "kind": "context", "cluster": "c"},
        {"id": "b", "query": "z", "gold": [], "kind": "unanswerable", "cluster": "u"},
    ]
    hit = {"source_uri": "a.md", "content": "y", "similarity": 0.25, "lexical_score": 1.0}
    report = search(lambda path, body: {"results": [hit]}, "o", "n", queries, {})
    assert report["top_similarity"] == {
        "answerable": 0.25,
        "unanswerable": 0.25,
        "no_hits": {"answerable": 0, "unanswerable": 0},
    }
    # an unanswerable query with no hit is counted apart, not averaged as 0
    queries.append({"id": "c", "query": "w", "gold": [], "kind": "unanswerable", "cluster": "u"})
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
    queries = [
        {"id": i, "query": i, "gold": [["a.md", "y"]], "kind": "context", "cluster": i}
        for i in "pq"
    ]
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
    queries = [
        {"id": "q", "query": "x", "gold": [["a.md", "y"]], "kind": "context", "cluster": "c"}
    ]
    vector_only = {"source_uri": "a.md", "content": "y", "similarity": 0.5}
    with pytest.raises(SystemExit, match="lexical leg never fired"):
        search(lambda path, body: {"results": [vector_only]}, "o", "n", queries, {})
    semantic = {"search_mode": "semantic"}
    report = search(lambda path, body: {"results": [vector_only]}, "o", "n", queries, semantic)
    assert report["metrics"]["all"]["mrr"] == 1.0


def test_a_search_that_finds_nothing_or_ignores_a_param_fails() -> None:
    queries = [
        {"id": "q", "query": "x", "gold": [["a.md", "y"]], "kind": "context", "cluster": "c"}
    ]
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
    chunks = _ingest_chunks(docs)
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
    monkeypatch.setattr("sys.argv", ["knowledge_retrieval.py", "--phase", phase])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2  # argparse's usage error, before any request


def test_the_clusters_are_exactly_the_groups_whose_results_move_together() -> None:
    """One question asked of each product is one group, every set of queries sharing a
    gold chunk is one group, and the groups merge transitively: the fixture's clusters
    are that partition, no coarser and no finer."""
    docs, queries = load_fixture(FIXTURE)
    chunks = _ingest_chunks(docs)
    group = {q["id"]: {q["id"]} for q in queries}

    def join(a: str, b: str) -> None:
        merged = group[a] | group[b]
        for member in merged:
            group[member] = merged

    first_of: dict[object, str] = {}
    for q in queries:
        product, _, question = q["id"].partition("-")
        keys: list[object] = [("question", question)] if product in PRODUCTS else []
        for uri, text in q["gold"]:
            keys.append((uri, next(c.idx for c in chunks[uri] if text in c.text)))
        for key in keys:
            join(q["id"], first_of.setdefault(key, q["id"]))
    by_cluster: dict[str, set[str]] = {}
    for q in queries:
        by_cluster.setdefault(q["cluster"], set()).add(q["id"])
    assert sorted(map(sorted, by_cluster.values())) == sorted(
        {frozenset(g): sorted(g) for g in group.values()}.values()
    )


def test_the_corpus_check_returns_the_chunk_count_the_run_measured() -> None:
    docs = [{"source_uri": "a.md", "content": "x"}]
    row = {"source_uri": "a.md", "tags": [fixture_tag(docs)], "chunk_count": 3}
    assert check_corpus(lambda path, body: {"rows": [row]}, "o", "n", docs) == 3


@pytest.mark.parametrize(
    ("rerank", "reranked", "fails"),
    [
        ("none", 0, False),
        ("none", 3, True),
        ("builtin:openai", 0, True),
        ("builtin:openai", 1, False),
    ],
    ids=["baseline", "baseline-reranked", "rerank-never-ran", "rerank-ran"],
)
def test_an_arm_whose_reranker_did_not_run_as_it_says_fails(rerank, reranked, fails) -> None:
    from bench.knowledge_retrieval import check_reranked

    if fails:
        with pytest.raises(SystemExit):
            check_reranked(rerank, reranked)
    else:
        check_reranked(rerank, reranked)


def test_an_arm_naming_no_reranker_pins_none() -> None:
    queries = [
        {"id": "a", "query": "x", "gold": [["a.md", "y"]], "kind": "context", "cluster": "c"}
    ]
    hit = {"source_uri": "a.md", "content": "y", "similarity": 0.5, "lexical_score": 1.0}
    sent: list[dict] = []
    report = search(
        lambda path, body: sent.append(body) or {"results": [hit]}, "o", "n", queries, {}
    )
    assert sent[0]["rerank"] == "none" and report["params"] == {"rerank": "none"}
    # a deployment default that reranks anyway fails the baseline
    answer = {"results": [hit], "reranked_by": "builtin:openai"}
    with pytest.raises(SystemExit, match="names none"):
        search(lambda path, body: answer, "o", "n", queries, {})
