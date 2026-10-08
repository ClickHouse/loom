"""bench/route_replay.py — the pure parts: feature extraction from a frozen
retrieval, the routing distribution, and the summary arithmetic. The LLM /
HTTP sub-commands are exercised by hand against a live server."""

from __future__ import annotations

from bench.route_replay import (
    ArmResult,
    Frozen,
    Question,
    features_of,
    route_distribution,
    summarize,
)


def _fz(
    qid: str,
    query: str,
    n: int = 3,
    risk: str = "low",
    agree: float = 0.8,
    abstain: bool = False,
    code: str = "",
) -> Frozen:
    return Frozen(
        q=Question(qid=qid, query=query, org="o", namespace="n", reference="ref"),
        search={
            "results": [{"memory_key": f"m{i}", "similarity": 0.8 - 0.1 * i} for i in range(n)],
            "retrieval_risk": risk,
            "route_agreement": agree,
            "retrieval_confidence": 0.7,
            "abstain": abstain,
            "abstain_code": code,
        },
    )


def test_features_mirror_the_ask_pipeline() -> None:
    f = features_of(_fz("a", "what is x", n=20), window=15)
    assert f.n_memories == 15 and f.retrieval_risk == "low" and not f.hedged
    assert f.top_similarity == 0.8 and round(f.similarity_margin, 3) == 0.1
    f2 = features_of(_fz("b", "what about PR 1", code="entity_absent"))
    assert f2.hedged is True


def test_route_distribution_counts_tiers_and_reasons() -> None:
    frozen = [
        _fz("a", "what is my favorite color"),
        _fz("b", "which PRs did we review"),
        _fz("c", "what is x", risk="high"),
        _fz("d", "anything", n=0, abstain=True, code="no_hits"),
    ]
    tiers, reasons = route_distribution(frozen)
    assert tiers == {"cheap": 1, "strong": 2, "(abstained)": 1}
    assert reasons == {"easy_question": 1, "breadth_query": 1, "retrieval_risk_high": 1}
    tiers2, _ = route_distribution(frozen, min_cheap_confidence=0.9)
    assert tiers2["cheap"] == 0


def _r(
    arm: str,
    qid: str,
    verdict: str,
    cost: float,
    tier: str = "",
    reason: str = "",
    escalated: bool = False,
) -> ArmResult:
    return ArmResult(
        arm=arm,
        qid=qid,
        verdict=verdict,
        cost_usd=cost,
        latency_ms=100,
        route_tier=tier,
        route_reason=reason,
        escalated=escalated,
    )


def test_summary_arithmetic() -> None:
    results = {
        "strong": [
            _r("strong", "a", "correct", 0.04),
            _r("strong", "b", "correct", 0.04),
            _r("strong", "c", "wrong", 0.04),
        ],
        "cheap": [
            _r("cheap", "a", "correct", 0.001),
            _r("cheap", "b", "wrong", 0.001),
            _r("cheap", "c", "wrong", 0.001),
        ],
        "cascade": [
            _r("cascade", "a", "correct", 0.001, "cheap", "easy_question"),
            _r("cascade", "b", "correct", 0.041, "strong", "easy_question:cheap_uncited", True),
            _r("cascade", "c", "wrong", 0.04, "strong", "retrieval_risk_high"),
        ],
    }
    s = summarize(results)
    assert s["arms"]["strong"]["accuracy"] == round(2 / 3, 3)
    assert s["arms"]["cheap"]["accuracy"] == round(1 / 3, 3)
    assert s["arms"]["cascade"]["cost_usd"] == 0.082
    assert s["cascade_vs_strong"]["cost_saved_pct"] == round(100 * (1 - 0.082 / 0.12), 1)
    assert s["cascade_vs_strong"]["accuracy_delta_pp"] == 0.0
    assert s["cascade"]["tiers"] == {"cheap": 1, "strong": 2}
    assert s["cascade"]["escalated"] == 1 and s["cascade"]["cheap_attempts"] == 2
    assert s["cascade"]["by_reason"]["easy_question"]["n"] == 2
    # b is the one question cheap got wrong and strong got right; the cascade
    # caught it via escalation.
    h = s["router_recall_on_hard"]
    assert h["hard_questions"] == 1 and h["recall"] == 1.0 and h["missed_qids"] == []


def test_summary_carries_paired_bootstrap_bounds() -> None:
    results = {
        "strong": [
            _r("strong", f"q{i}", "correct" if i < 45 else "wrong", 0.02) for i in range(60)
        ],
        "cascade": [
            _r("cascade", f"q{i}", "correct" if i < 46 else "wrong", 0.01, "cheap", "easy_question")
            for i in range(60)
        ],
    }
    s = summarize(results)
    cv = s["cascade_vs_strong"]
    # one question in sixty: the bound spans zero, and says so
    assert cv["accuracy_delta_pp"] == 1.7
    lb, ub = cv["accuracy_delta_95_pp"]
    assert lb <= 0.0 <= ub and cv["accuracy_wtl"] == [1, 59, 0]
    lo, hi = cv["cost_saved_usd_95"]
    assert 0 < lo <= hi and abs(hi - 0.6) < 0.05  # every question saved $0.01


def test_cite_metrics_follow_the_homer_contract() -> None:
    """Cite Score: +1 per gold cited, -1 per other key
    cited, over the gold count; only the answer text's markers count."""
    from types import SimpleNamespace

    from bench.route_replay import cite_metrics

    hits = [SimpleNamespace(memory_key=k) for k in ("g1", "d1", "d2")]
    assert cite_metrics("Answer [g1].", hits, "g1", "synthesized") == (["g1"], 1.0, 1.0, True)
    assert cite_metrics("Answer [g1] and [d1].", hits, "g1", "synthesized") == (
        ["g1", "d1"],
        0.0,
        0.5,
        True,
    )
    assert cite_metrics("Answer [d1] [d2].", hits, "g1", "synthesized") == (
        ["d1", "d2"],
        -2.0,
        0.0,
        False,
    )
    assert cite_metrics("No markers at all.", hits, "g1", "synthesized") == ([], 0.0, None, False)
    assert cite_metrics("Answer [g1].", hits, "g1", "retrieval_only") == ([], None, None, None)
    assert cite_metrics("Answer [g1].", hits, "", "synthesized") == ([], None, None, None)


def test_summary_carries_citation_metrics() -> None:
    from bench.route_replay import ArmResult, summarize

    rows = [
        ArmResult(
            arm="strong",
            qid="1",
            verdict="correct",
            cited_keys=["g"],
            cite_score=1.0,
            cite_precision=1.0,
            gold_cited=True,
        ),
        ArmResult(
            arm="strong",
            qid="2",
            verdict="correct",
            cited_keys=["g", "x"],
            cite_score=0.0,
            cite_precision=0.5,
            gold_cited=True,
        ),
        ArmResult(
            arm="strong",
            qid="3",
            verdict="wrong",
            cited_keys=[],
            cite_score=0.0,
            cite_precision=None,
            gold_cited=False,
        ),
        ArmResult(arm="strong", qid="4", verdict="abstained"),  # no score: not counted
    ]
    s = summarize({"strong": rows})["arms"]["strong"]
    assert s["cite_score_mean"] == round(1 / 3, 3)
    assert s["cite_precision_mean"] == 0.75
    assert s["gold_cited_share"] == round(2 / 3, 3)


def test_summary_reports_false_memory_rate_and_context_tokens() -> None:
    from bench.route_replay import ArmResult, summarize

    rows = [
        ArmResult(
            arm="a",
            qid="1",
            source="synthesized",
            verdict="correct",
            consistency="entailed",
            context_tokens=1000,
        ),
        ArmResult(
            arm="a",
            qid="2",
            source="synthesized",
            verdict="wrong",
            consistency="contradicted",
            context_tokens=3000,
        ),
        ArmResult(arm="a", qid="3", source="abstained", verdict="abstained"),
        ArmResult(
            arm="a",
            qid="4",
            source="precedent",
            verdict="correct",
            consistency="entailed",
            context_tokens=2000,
        ),
    ]
    arm = summarize({"a": rows})["arms"]["a"]
    assert arm["answered"] == 3 and arm["false_memory_rate"] == round(1 / 3, 3)
    assert arm["context_tokens_mean"] == 2000 and arm["context_tokens_p90"] == 3000
    assert arm["consistency_checked"] == sum(1 for r in rows if r.consistency)
    empty = summarize(
        {"b": [ArmResult(arm="b", qid="1", source="abstained", verdict="abstained")]}
    )["arms"]["b"]
    assert empty["false_memory_rate"] is None and empty["context_tokens_mean"] is None


def test_summary_reports_abstentions_over_the_gate_denominator_too() -> None:
    from bench._gate import normalize_label
    from bench.route_replay import ArmResult, summarize

    rows = [
        ArmResult(arm="a", qid="1", verdict="correct"),
        ArmResult(arm="a", qid="2", verdict="wrong"),
        ArmResult(arm="a", qid="3", verdict="abstained"),
        ArmResult(arm="a", qid="4", verdict="abstained"),
        ArmResult(arm="a", qid="5", verdict="error"),
    ]
    arm = summarize({"a": rows})["arms"]["a"]
    assert arm["accuracy"] == 0.5 and arm["graded"] == 2
    assert arm["abstained"] == 2 and arm["errors"] == 1
    # The gate reads an abstention as WRONG and leaves an error out.
    gate = [normalize_label(r.verdict) for r in rows]
    assert gate.count("CORRECT") / (gate.count("CORRECT") + gate.count("WRONG")) == 0.25
    assert arm["accuracy_abstain_wrong"] == 0.25
    assert "abstained" in arm["accuracy_denominator"]
    assert "abstain" in arm["accuracy_abstain_wrong_denominator"]


def test_replay_ask_pins_the_judge_inline_because_it_reads_the_label() -> None:
    # The deployment default is `deferred`, under
    # which the wire label is empty. The replay reports false_memory_rate from
    # that label, so its request pins the inline mode whatever the setting.
    from bench.route_replay import ask_input_for

    inp = ask_input_for(_fz("a", "what is x"), top_k=7)
    assert inp.consistency == "inline"
    assert inp.query == "what is x" and inp.top_k == 7 and inp.namespace == "n"


def test_summary_carries_the_mid_arm_and_its_routed_bucket() -> None:
    """`cascade_mid` reports the paired delta vs strong like the
    cascade does, and the routed bucket — the asks the mid routes moved —
    against the strong arm on exactly those asks."""
    from bench.route_replay import mcnemar_p

    strong = [
        _r("strong", "a", "correct", 0.05, "strong", "weak_top_hit"),
        _r("strong", "b", "correct", 0.05, "strong", "low_confidence"),
        _r("strong", "c", "wrong", 0.05, "strong", "retrieval_risk_high"),
        _r("strong", "d", "correct", 0.05, "strong", "identifier_question"),
    ]
    mid = [
        _r("cascade_mid", "a", "correct", 0.02, "mid", "weak_top_hit:mid"),
        _r("cascade_mid", "b", "wrong", 0.02, "mid", "low_confidence:mid"),
        _r(
            "cascade_mid",
            "c",
            "correct",
            0.07,
            "strong",
            "retrieval_risk_high:mid:mid_uncited",
            True,
        ),
        _r("cascade_mid", "d", "correct", 0.05, "strong", "identifier_question"),
    ]
    s = summarize({"strong": strong, "cascade_mid": mid})
    cv = s["cascade_mid_vs_strong"]
    assert cv["accuracy_delta_pp"] == 0.0 and cv["accuracy_wtl"] == [1, 2, 1]
    assert cv["mcnemar_p"] == 1.0
    assert s["cascade_mid"]["tiers"] == {"mid": 2, "strong": 2}
    rb = s["cascade_mid"]["routed_bucket"]
    assert rb["n"] == 3 and rb["escalated"] == 1
    assert rb["strong_cost_per_ask"] == 0.05 and rb["arm_cost_per_ask"] == round(0.11 / 3, 5)
    assert rb["wtl"] == [1, 1, 1] and rb["strong_accuracy"] == round(2 / 3, 3)
    assert "cascade_vs_strong" not in s  # no cascade arm ran
    assert mcnemar_p(0, 0) == 1.0 and round(mcnemar_p(9, 1), 4) == 0.0215
