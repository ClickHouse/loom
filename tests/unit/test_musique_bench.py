"""MuSiQue bench: the pure parts.

The harness talks to a live server; these tests pin the scoring that makes
its numbers comparable with the published MuSiQue/AriGraph table, the
reader-independent retrieval metric, deterministic sampling, and the
summary shape.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench.musique import (  # noqa: E402
    exact_match,
    f1_score,
    golds_of,
    hops_of,
    load_questions,
    normalize_answer,
    paragraph_value,
    summarize,
    supporting_recall,
)


def test_normalisation_is_the_official_squad_musique_form() -> None:
    assert normalize_answer("The  Beatles!") == "beatles"
    assert normalize_answer("An apple, a day.") == "apple day"
    assert exact_match("the Beatles", ["Beatles"]) == 1.0
    assert exact_match("Beatles", ["Rolling Stones"]) == 0.0


def test_f1_takes_the_best_gold_and_handles_no_overlap() -> None:
    assert f1_score("Miquette Giraudy", ["Miquette Giraudy"]) == 1.0
    assert abs(f1_score("Miquette", ["Miquette Giraudy"]) - (2 * 1.0 * 0.5 / 1.5)) < 1e-9
    assert f1_score("nobody", ["Miquette Giraudy", "nobody at all"]) > 0.0
    assert f1_score("x", ["y"]) == 0.0 and f1_score("x", [""]) == 0.0


def test_supporting_recall_reads_paragraph_keys_and_respects_k() -> None:
    keys = ["p3", "p7", "junk", "p1", "p12"]
    assert supporting_recall(keys, {3, 12}, 5) == 1.0
    assert supporting_recall(keys, {3, 12}, 2) == 0.5
    assert supporting_recall(keys, {8}, 10) == 0.0
    assert supporting_recall(keys, set(), 10) == 0.0


def test_loader_samples_deterministically_and_skips_unanswerable(tmp_path: Path) -> None:
    rows = [
        {
            "id": f"2hop__{i}",
            "answerable": i % 5 != 0,
            "question": f"q{i}",
            "answer": "a",
            "answer_aliases": [],
            "paragraphs": [],
            "question_decomposition": [{}, {}],
        }
        for i in range(30)
    ]
    p = tmp_path / "musique.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    a = load_questions(p, 10, seed=0)
    b = load_questions(p, 10, seed=0)
    c = load_questions(p, 10, seed=1)
    assert [r["id"] for r in a] == [r["id"] for r in b] and len(a) == 10
    assert [r["id"] for r in a] != [r["id"] for r in c]
    assert all(r["answerable"] for r in a)
    assert hops_of(a[0]) == 2 and hops_of({"id": "x", "question_decomposition": [1, 2, 3]}) == 3


def test_paragraph_value_and_golds() -> None:
    assert paragraph_value({"title": "Green", "paragraph_text": "An album."}) == "Green. An album."
    assert paragraph_value({"title": "", "paragraph_text": "Text"}) == "Text"
    assert golds_of({"answer": "A", "answer_aliases": ["B", "C"]}) == ["A", "B", "C"]


def test_summary_reports_retrieval_separately_from_the_reader() -> None:
    rows = [
        {
            "id": "2hop__1",
            "hops": 2,
            "recall@2": 0.5,
            "recall@5": 1.0,
            "recall@10": 1.0,
            "search_ms": 40,
            "write_ms": 900,
            "hits": 10,
            "n_supporting": 2,
        },
        {
            "id": "3hop__2",
            "hops": 3,
            "recall@2": 0.0,
            "recall@5": 0.5,
            "recall@10": 1.0,
            "search_ms": 60,
            "write_ms": 1100,
            "hits": 10,
            "n_supporting": 3,
        },
    ]
    s = summarize(rows)
    assert s["n"] == 2 and s["recall@5"] == 0.75 and "em" not in s and "f1" not in s
    assert s["by_hops"]["2"]["recall@5"] == 1.0 and s["by_hops"]["3"]["recall@5"] == 0.5
    rows[0]["em"], rows[0]["f1"] = 1.0, 1.0
    rows[1]["em"], rows[1]["f1"] = 0.0, 0.5
    s = summarize(rows)
    assert s["em"] == 0.5 and s["f1"] == 0.75 and s["by_hops"]["3"]["f1"] == 0.5
    assert summarize([]) == {"n": 0}


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _run_main(monkeypatch, tmp_path, argv, *, fail_search=False):
    """Drive main() against a mock server; returns the (op, body) calls."""
    import bench.bench_namespaces as bn
    import bench.musique as m

    monkeypatch.setattr(bn, "_MINTED", [])
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        op = request.url.path.rsplit("/", 1)[-1]
        calls.append((op, json.loads(request.content or b"{}")))
        if op == "memory.search" and fail_search:
            raise KeyboardInterrupt
        return httpx.Response(200, json={"results": []})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kw),
    )
    data = tmp_path / "d.jsonl"
    item = {"id": "q1", "question": "q", "answer": "a", "paragraphs": [{"idx": 0}]}
    data.write_text(json.dumps(item) + "\n")
    monkeypatch.setattr(sys, "argv", ["musique.py", "--data", str(data), "--n", "1", *argv])
    try:
        m.main()
    except KeyboardInterrupt:
        pass
    return calls


def test_a_run_registers_its_namespace_as_bench_and_erases_it_even_on_interrupt(
    monkeypatch, tmp_path
) -> None:
    for fail in (False, True):
        calls = _run_main(monkeypatch, tmp_path, [], fail_search=fail)
        reg = [b for op, b in calls if op == "namespace.register"]
        assert len(reg) == 1 and reg[0]["kind"] == "bench"
        erased = [b for op, b in calls if op == "erasure.request"]
        assert erased and reg[0]["namespace"] in erased[0]["scope_value"]


def test_keep_namespaces_registers_them_out_of_the_idle_gc_and_erases_nothing(
    monkeypatch, tmp_path
) -> None:
    calls = _run_main(monkeypatch, tmp_path, ["--keep-namespaces"])
    reg = [b for op, b in calls if op == "namespace.register"]
    assert reg and reg[0]["kind"] == "memory" and reg[0]["traffic_class"] == "bench"
    assert not [op for op, _ in calls if op == "erasure.request"]
