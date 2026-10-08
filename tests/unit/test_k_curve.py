"""bench/k_curve.py — gold-evidence recall and context size by top_k, LLM-free."""

from __future__ import annotations

from bench.k_curve import QuestionAtK, render, score, summarise, tokens_of


def _hit(session: int, text: str = "x" * 400) -> dict:
    return {"session_id": f"ns-0:session_{session}", "content_excerpt": text}


def test_score_counts_gold_sessions_in_the_top_k_only() -> None:
    hits = [_hit(3), _hit(7), _hit(3), _hit(9), _hit(12)]
    gold = {7, 12}
    r3 = score(hits, "ns-0", gold, 3, "multi_hop")
    r5 = score(hits, "ns-0", gold, 5, "multi_hop")
    assert r3.gold_recall == 0.5 and r3.all_gold is False and r3.hits == 3
    assert r5.gold_recall == 1.0 and r5.all_gold is True and r5.hits == 5
    assert r3.tokens == 300 and r5.tokens == 500  # 400 chars / 4 per hit


def test_tokens_prefer_the_servers_estimate() -> None:
    hits = [_hit(1)]
    assert tokens_of(hits, {"context_tokens_est": 1234}) == 1234
    assert tokens_of(hits, {}) == 100


def test_summarise_and_render_per_category_and_k() -> None:
    rows = [
        QuestionAtK("temporal", 3, 2, 0.5, False, 300, 3, latency_ms=120),
        QuestionAtK("temporal", 3, 1, 1.0, True, 200, 3, latency_ms=80),
        QuestionAtK("temporal", 10, 2, 1.0, True, 900, 10, latency_ms=400),
        QuestionAtK("multi_hop", 3, 2, 0.0, False, 250, 3),
    ]
    pts = summarise(rows)
    assert [(p.category, p.k, p.n) for p in pts] == [
        ("multi_hop", 3, 1),
        ("temporal", 3, 2),
        ("temporal", 10, 1),
    ]
    t3 = next(p for p in pts if p.category == "temporal" and p.k == 3)
    assert t3.gold_recall_mean == 0.75 and t3.all_gold_share == 0.5 and t3.tokens_median == 250
    assert t3.latency_p50_ms == 100 and t3.latency_p90_ms == 120
    assert next(p for p in pts if p.category == "multi_hop").latency_p50_ms == 0  # unmeasured → 0
    table = render(pts)
    assert table.splitlines()[0].startswith("| category | k | n |")
    assert "| temporal | 10 | 1 | 1.000 | 1.000 | 900 |" in table
    assert "| 400 | 400 |" in table and "latency p50 | p90 |" in table.splitlines()[0]


def test_percentile_is_nearest_rank_for_any_count() -> None:
    from bench.k_curve import _p

    assert _p([], 0.9) == 0
    assert _p([5], 0.9) == 5
    assert _p([1000, 3000, 2000], 0.9) == 3000
    assert _p(list(range(1, 11)), 0.9) == 9
    assert _p(list(range(1, 8)), 0.5) == 4


def test_request_body_search_and_retrieval_only_ask() -> None:
    """--reader ask posts a retrieval-only memory.ask under the
    ingest identity with the arm's fields merged last; search is unchanged."""
    from types import SimpleNamespace

    from bench.k_curve import request_body

    a = SimpleNamespace(org="o", reader="search", search_json={"search_mode": "rrf"}, ask_json=None)
    body = request_body(a, "kcurve-0", "what?", 15)
    assert body == {
        "org": "o",
        "namespace": "kcurve-0",
        "query": "what?",
        "top_k": 15,
        "namespace_scope": "this",
        "search_mode": "rrf",
    }
    a = SimpleNamespace(
        org="o",
        reader="ask",
        agent="kcurve",
        ask_json={"deep_first": False},
        search_json={"search_mode": "rrf"},
    )
    body = request_body(a, "kcurve-0", "what?", 15)
    assert body["allow_synthesis"] is False
    assert body["agent"] == "kcurve"
    assert body["deep_first"] is False
    assert "search_mode" not in body


def test_write_path_cost_reads_cost_summary_per_namespace_and_renders(monkeypatch) -> None:
    """The write path is reported beside the recall curve. One cost.summary call per namespace, fail-open, rendered as a
    table with a total; --json output and the points file are unchanged."""
    import asyncio

    from bench import k_curve as kc

    calls: list[dict] = []

    async def fake_post(cl, url, body, tok):
        calls.append(body)
        assert url.endswith("/v1/cost.summary") and tok == "t"
        if body["namespace"] == "kc-1":
            return {"_err": "boom"}
        if body["namespace"] == "kc-2":
            return {"rows": []}
        return {
            "rows": [
                {
                    "namespace": "kc-0",
                    "ops": 26,
                    "tokens": 785304,
                    "embed_tokens": 900,
                    "cost_usd": 0.13,
                }
            ]
        }

    monkeypatch.setattr(kc, "_post", fake_post)
    rows = asyncio.run(
        kc.write_path_cost(None, "http://x", "t", "clickhouse", ["kc-0", "kc-1", "kc-2"])
    )
    assert [r.namespace for r in rows] == [
        "kc-0",
        "kc-2",
    ]  # the failed one is skipped, the empty one is 0
    assert rows[0].ops == 26 and rows[0].cost_usd == 0.13 and rows[1].tokens == 0
    assert calls[0]["operation"] == "memory.set_from_messages" and calls[0]["group_by"] == [
        "namespace"
    ]
    assert (
        calls[0]["from_date"] < calls[0]["to_date"] or calls[0]["from_date"] <= calls[0]["to_date"]
    )
    md = kc.render_write_path(rows)
    assert (
        "| kc-0 | 26 | 785304 | 900 | 0.130 |" in md
        and "| total | 26 | 785304 | 900 | 0.130 |" in md
    )
    assert kc.render_write_path([]) == ""
