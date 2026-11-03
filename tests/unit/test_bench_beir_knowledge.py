"""bench/beir_knowledge.py: loading a dataset, scoring by document, the namespace
rules and the search loop, with no server or network."""

from __future__ import annotations

import hashlib
import io
import json
import math
import zipfile
from pathlib import Path
from typing import Any

import pytest

from bench import beir_knowledge as bk


def _dataset(root: Path) -> Path:
    (root / "qrels").mkdir(parents=True)
    corpus = [
        {"_id": "d1", "title": "Refunds", "text": "Refunds take 14 days."},
        {"_id": "d2", "title": "", "text": "Regions: eu and us."},
        {"_id": "d3", "title": "Only a title", "text": ""},
        {"_id": "d4", "title": "", "text": ""},
    ]
    (root / "corpus.jsonl").write_text("\n".join(json.dumps(r) for r in corpus) + "\n")
    queries = [{"_id": "q1", "text": "refund time"}, {"_id": "q2", "text": "unjudged"}]
    (root / "queries.jsonl").write_text("\n".join(json.dumps(q) for q in queries) + "\n")
    (root / "qrels" / "test.tsv").write_text("query-id\tcorpus-id\tscore\nq1\td1\t2\nq1\td2\t0\n")
    return root


def test_a_dataset_loads_as_documents_judged_queries_and_qrels(tmp_path: Path) -> None:
    docs, queries, qrels = bk.load(_dataset(tmp_path))
    assert [d["source_uri"] for d in docs] == ["beir:d1", "beir:d2", "beir:d3"]
    # a passage with no text is indexed by its title; one with neither is left out
    assert docs[2]["content"] == "Only a title"
    assert queries == {"q1": "refund time"}  # a query with no judgement is left out
    assert qrels == {"q1": {"d1": 2, "d2": 0}}


def test_a_documents_chunks_count_once_and_the_query_itself_never() -> None:
    hits = [{"source_uri": u} for u in ("beir:a", "beir:q1", "beir:b", "beir:a", "beir:c")]
    # a hit on the query's own id is dropped, as BEIR's evaluation does (ArguAna)
    assert bk.ranked_docs(hits, "q1") == ["a", "b", "c"]


def test_scores_use_the_graded_gain_and_count_only_relevant_documents() -> None:
    qrels = {"a": 2, "b": 1, "z": 0}
    out = bk.score(["z", "b", "x", "a"], qrels)
    dcg = 1 / math.log2(3) + 2 / math.log2(5)
    ideal = 2 / math.log2(2) + 1 / math.log2(3)
    assert out["ndcg@10"] == pytest.approx(dcg / ideal)
    assert out["mrr@10"] == pytest.approx(1 / 2)  # a judgement of 0 is not relevant
    assert out["recall@10"] == 1.0 and out["recall@100"] == 1.0
    empty = {"ndcg@10": 0.0, "recall@10": 0.0, "recall@100": 0.0, "mrr@10": 0.0}
    assert bk.score([], qrels) == empty
    # a relevant document past rank 10 counts for recall@100 only
    deep = [f"x{i}" for i in range(10)] + ["a"]
    assert bk.score(deep, {"a": 1})["recall@10"] == 0.0
    assert bk.score(deep, {"a": 1})["recall@100"] == 1.0


def test_a_named_namespace_carries_the_prefix_and_is_kept() -> None:
    assert bk.run_namespace("beir-scifact", bk.PREFIX) == ("beir-scifact", True)
    fresh, keep = bk.run_namespace(None, bk.PREFIX)
    assert fresh.startswith("beir-run-") and not keep
    for bad in ("scifact", "beir-run-x", "knowledge-retrieval-x"):
        with pytest.raises(SystemExit):
            bk.run_namespace(bad, bk.PREFIX)


def test_a_dataset_with_no_judged_query_is_refused(tmp_path: Path) -> None:
    root = _dataset(tmp_path)
    (root / "qrels" / "test.tsv").write_text("query-id\tcorpus-id\tscore\nq9\td1\t1\n")
    with pytest.raises(SystemExit, match="no query"):
        bk.load(root)


def test_a_fetch_checks_the_archive_and_writes_only_the_kept_members(
    tmp_path: Path, monkeypatch
) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for member in bk.KEPT:
            z.writestr(f"scifact/{member}", f"body of {member}")
        z.writestr("scifact/qrels/train.tsv", "not kept")
        z.writestr("../outside.txt", "never written")

    class _Client:
        def __init__(self, **kw: Any) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *a: Any) -> None:
            pass

        def get(self, url: str) -> Any:
            assert url == bk.RELEASE.format(name="scifact")
            body = {"content": buf.getvalue(), "raise_for_status": lambda self: None}
            return type("R", (), body)()

    monkeypatch.setattr(bk.httpx, "Client", _Client)
    monkeypatch.setitem(bk.SMALL, "scifact", "0" * 32)
    with pytest.raises(SystemExit, match="not the published"):
        bk.fetch("scifact", tmp_path / "data")
    assert not (tmp_path / "data").exists()  # nothing is written from an unchecked archive
    published = hashlib.md5(buf.getvalue(), usedforsecurity=False).hexdigest()
    monkeypatch.setitem(bk.SMALL, "scifact", published)
    out = bk.fetch("scifact", tmp_path / "data")
    written = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert written == sorted(f"data/scifact/{m}" for m in bk.KEPT)
    assert (out / "qrels/test.tsv").read_text() == "body of qrels/test.tsv"
    with pytest.raises(SystemExit):
        bk.fetch("msmarco", tmp_path)


def test_the_search_scores_each_query_and_counts_what_ran() -> None:
    sent: list[dict] = []

    def post(path: str, body: dict) -> Any:
        sent.append(body)
        hits = [{"source_uri": "beir:d1", "lexical_score": 1.0}, {"source_uri": "beir:d1"}]
        return {"results": hits, "reranked_by": "builtin:openai"}

    params = {"rerank": "builtin:openai"}
    report = bk.search(post, "o", "beir-x", {"q1": "refund"}, {"q1": {"d1": 1}}, params)
    plain = {"results": [{"source_uri": "beir:d1", "lexical_score": 1.0}]}
    baseline = bk.search(
        lambda p, b: sent.append(b) or plain, "o", "beir-x", {"q1": "r"}, {"q1": {"d1": 1}}, {}
    )
    # an arm that names no reranker pins none, whatever the deployment default, and
    # its report says so
    assert sent[-1]["rerank"] == "none" and baseline["reranked"] == 0
    assert baseline["params"] == {"rerank": "none"}
    assert report["metrics"] == {
        "ndcg@10": 1.0,
        "recall@10": 1.0,
        "recall@100": 1.0,
        "mrr@10": 1.0,
        "depth": 1.0,  # two chunk hits, one document
    }
    assert report["reranked"] == 1 and report["lexical_fired"] == 1
    assert report["queries"][0]["id"] == "q1"
    assert sent[0]["top_k"] == bk.DEPTH and sent[0]["collapse_sections"] is False
    assert sent[0]["rerank"] == "builtin:openai"


_HIT = [{"source_uri": "beir:d1"}]


@pytest.mark.parametrize(
    ("params", "answer", "why"),
    [
        ({}, {"results": _HIT}, "lexical leg never fired"),  # hybrid, lexical leg silent
        ({"search_mode": "semantic", "rerank": "builtin:openai"}, {"results": _HIT}, "did not run"),
        ({"search_mode": "semantic"}, {"results": _HIT, "reranked_by": "x"}, "names none"),
    ],
    ids=["lexical-never-fired", "no-answer-reranked", "baseline-reranked"],
)
def test_an_arm_whose_treatment_never_ran_fails(params: dict, answer: dict, why: str) -> None:
    with pytest.raises(SystemExit, match=why):
        bk.search(lambda p, b: answer, "o", "beir-x", {"q1": "x"}, {"q1": {"d1": 1}}, params)


def test_a_semantic_arm_needs_no_lexical_leg() -> None:
    answer = {"results": [{"source_uri": "beir:d1"}]}
    params = {"search_mode": "semantic"}
    report = bk.search(lambda p, b: answer, "o", "beir-x", {"q1": "x"}, {"q1": {"d1": 1}}, params)
    assert report["lexical_fired"] == 0


def test_an_ignored_search_field_fails_the_run() -> None:
    answer = {"results": [{"source_uri": "beir:d1"}], "ignored_args": ["rerank"]}
    with pytest.raises(SystemExit, match="ignored"):
        bk.search(lambda p, b: answer, "o", "beir-x", {"q1": "x"}, {"q1": {"d1": 1}}, {})


def test_a_search_refuses_a_namespace_not_holding_exactly_this_corpus() -> None:
    docs = [{"source_uri": "beir:d1", "title": "", "content": "x"}]
    tag = bk.corpus_tag(docs, "m")
    row = {"source_uri": "beir:d1", "tags": [tag], "chunk_count": 2}
    page = {"rows": [row]}
    assert bk.check_corpus(lambda p, b: page, "o", "beir-x", docs, tag) == 2
    for bad in ({"tags": ["beir:other"]}, {"chunk_count": 0}):
        page["rows"] = [{**row, **bad}]
        with pytest.raises(SystemExit):
            bk.check_corpus(lambda p, b: page, "o", "beir-x", docs, tag)


_ONE_DOC = [{"source_uri": "beir:d0", "title": "", "content": "x"}]


@pytest.mark.parametrize(
    "row",
    [
        {"source_uri": "beir:d0", "tags": [bk.corpus_tag(_ONE_DOC, "other-model")]},
        {"source_uri": "beir:d0", "tags": ["beir:other"]},
        {"source_uri": "beir:z", "tags": []},
    ],
    ids=["another-model", "another-version", "another-corpus"],
)
def test_an_ingest_into_a_namespace_of_another_corpus_writes_nothing(monkeypatch, row) -> None:
    registered: list[str] = []
    sent: list[str] = []
    monkeypatch.setattr(
        bk, "register_bench_namespace_sync", lambda post, **kw: registered.append(kw["namespace"])
    )
    monkeypatch.setattr(bk, "listed_documents", lambda post, org, ns: [{**row, "chunk_count": 1}])
    tag = bk.corpus_tag(_ONE_DOC, "m")
    with pytest.raises(SystemExit, match="another corpus"):
        bk.ingest(lambda path, body: sent.append(path), "o", "beir-x", _ONE_DOC, tag, keep=False)
    assert sent == [] and registered == []  # refused before the namespace is registered


def test_the_ingest_adds_only_what_the_namespace_lacks_in_tagged_batches(monkeypatch) -> None:
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(bk, "register_bench_namespace_sync", lambda post, **kw: None)
    monkeypatch.setattr(bk, "BATCH", 2)
    docs = [{"source_uri": f"beir:d{i}", "title": "", "content": "x"} for i in range(6)]
    tag = bk.corpus_tag(docs, "m")
    # a resumed ingest: two passages are there with their chunks, and bulk_add never
    # replaces; one an interrupted write left without chunks is sent again
    held = [
        {"source_uri": "beir:d0", "tags": [tag], "chunk_count": 1},
        {"source_uri": "beir:d3", "tags": [tag], "chunk_count": 1},
        {"source_uri": "beir:d4", "tags": [tag], "chunk_count": 0},
    ]
    monkeypatch.setattr(bk, "listed_documents", lambda post, org, ns: held)
    bk.ingest(lambda path, body: sent.append((path, body)), "o", "beir-x", docs, tag, keep=False)
    uris = [[e["source_uri"] for e in b["entries"]] for _, b in sent]
    assert uris == [["beir:d1", "beir:d2"], ["beir:d4", "beir:d5"]]
    assert {p for p, _ in sent} == {"/v1/knowledge.bulk_add"}
    assert sent[0][1]["entries"][0]["tags"] == [tag]


def test_a_rerank_arm_is_a_search_field_the_harness_takes() -> None:
    assert bk.parse_params(["rerank=builtin:openai"]) == {"rerank": "builtin:openai"}


def test_a_collapse_arm_is_refused() -> None:
    with pytest.raises(SystemExit, match="collapse_sections is refused"):
        bk.arm_params(["collapse_sections=true"])
    assert bk.arm_params(["search_mode=semantic"]) == {"search_mode": "semantic"}


def test_an_arm_that_finds_nothing_fails() -> None:
    # every query empty: a namespace with no chunks of the server's model, or none at all
    with pytest.raises(SystemExit, match="found anything"):
        bk.search(lambda p, b: {"results": []}, "o", "beir-x", {"q1": "x"}, {"q1": {"d1": 1}}, {})
