"""bench/beir_knowledge.py: loading a dataset, scoring by document, the namespace
rules and the search loop, with no server or network."""

from __future__ import annotations

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


def test_a_documents_chunks_count_once_at_the_first_rank() -> None:
    hits = [{"source_uri": u} for u in ("beir:a", "beir:b", "beir:a", "beir:c")]
    assert bk.ranked_docs(hits) == ["a", "b", "c"]


def test_scores_use_the_graded_gain_and_count_only_relevant_documents() -> None:
    qrels = {"a": 2, "b": 1, "z": 0}
    out = bk.score(["z", "b", "x", "a"], qrels)
    dcg = 1 / math.log2(3) + 2 / math.log2(5)
    ideal = 2 / math.log2(2) + 1 / math.log2(3)
    assert out["ndcg@10"] == pytest.approx(dcg / ideal)
    assert out["mrr@10"] == pytest.approx(1 / 2)  # a judgement of 0 is not relevant
    assert out["recall@10"] == 1.0 and out["recall@100"] == 1.0
    assert bk.score([], qrels) == {
        "ndcg@10": 0.0,
        "recall@10": 0.0,
        "recall@100": 0.0,
        "mrr@10": 0.0,
    }
    # a relevant document past rank 10 counts for recall@100 only
    deep = [f"x{i}" for i in range(10)] + ["a"]
    assert bk.score(deep, {"a": 1})["recall@10"] == 0.0
    assert bk.score(deep, {"a": 1})["recall@100"] == 1.0


def test_a_named_namespace_carries_the_prefix_and_is_kept() -> None:
    assert bk.run_namespace("beir-scifact") == ("beir-scifact", True)
    fresh, keep = bk.run_namespace(None)
    assert fresh.startswith("beir-run-") and not keep
    for bad in ("scifact", "beir-run-x"):
        with pytest.raises(SystemExit):
            bk.run_namespace(bad)


def test_a_fetch_writes_only_the_kept_members(tmp_path: Path, monkeypatch) -> None:
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
            return type(
                "R", (), {"content": buf.getvalue(), "raise_for_status": lambda self: None}
            )()

    monkeypatch.setattr(bk.httpx, "Client", _Client)
    out = bk.fetch("scifact", tmp_path / "data")
    written = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert written == sorted(f"data/scifact/{m}" for m in bk.KEPT)
    assert (out / "qrels/test.tsv").read_text() == "body of qrels/test.tsv"
    with pytest.raises(SystemExit):
        bk.fetch("msmarco", tmp_path)


def test_the_search_scores_each_query_and_counts_reranked_answers() -> None:
    sent: list[dict] = []

    def post(path: str, body: dict) -> Any:
        sent.append(body)
        return {"results": [{"source_uri": "beir:d1"}], "reranked_by": "builtin:openai"}

    report = bk.search(
        post, "o", "beir-x", {"q1": "refund"}, {"q1": {"d1": 1}}, {"rerank": "builtin:openai"}
    )
    assert report["metrics"] == {"ndcg@10": 1.0, "recall@10": 1.0, "recall@100": 1.0, "mrr@10": 1.0}
    assert report["reranked"] == 1 and report["queries"][0]["id"] == "q1"
    assert sent[0]["top_k"] == bk.DEPTH and sent[0]["collapse_sections"] is False
    assert sent[0]["rerank"] == "builtin:openai"


def test_an_ignored_search_field_fails_the_run() -> None:
    def post(path: str, body: dict) -> Any:
        return {"results": [], "ignored_args": ["rerank"]}

    with pytest.raises(SystemExit):
        bk.search(post, "o", "beir-x", {"q1": "x"}, {"q1": {"d1": 1}}, {"rerank": "x"})


def test_a_namespace_holding_another_corpus_is_refused(monkeypatch) -> None:
    docs = [{"source_uri": "beir:d1", "title": "", "content": "x"}]
    tag = bk.corpus_tag(docs)
    rows = [{"source_uri": "beir:d1", "tags": [tag], "chunk_count": 2}]
    monkeypatch.setattr(bk, "listed_documents", lambda post, org, ns: rows)
    assert bk.check_corpus(None, "o", "beir-x", docs) == 2
    rows[0]["tags"] = ["beir:other"]
    with pytest.raises(SystemExit):
        bk.check_corpus(None, "o", "beir-x", docs)


def test_the_ingest_batches_bulk_adds_with_the_corpus_tag(monkeypatch) -> None:
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(bk, "register_bench_namespace_sync", lambda post, **kw: None)
    monkeypatch.setattr(bk, "BATCH", 2)
    docs = [{"source_uri": f"beir:d{i}", "title": "", "content": "x"} for i in range(5)]
    bk.ingest(lambda path, body: sent.append((path, body)), "o", "beir-x", docs, keep=False)
    assert [len(b["entries"]) for _, b in sent] == [2, 2, 1]
    assert {p for p, _ in sent} == {"/v1/knowledge.bulk_add"}
    assert sent[0][1]["entries"][0]["tags"] == [bk.corpus_tag(docs)]
