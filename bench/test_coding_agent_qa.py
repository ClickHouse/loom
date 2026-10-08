"""CH-free, LLM-free unit tests for the coding-agent QA + fix-reuse bench.

Lives under bench/ (not tests/) so it travels with the harness it guards —
same rationale as test_temporal_assertions.py: the bench needs a live control
plane, so only its PURE core (the scorer + corpus loader + dry-mode gate) is
unit-tested, here, decoupled from the integration suite. `testpaths =
["tests"]` means the repo's default pytest run does NOT collect this file.

Runnable two ways:
    python bench/test_coding_agent_qa.py     # standalone, exit 0/1
    pytest bench/test_coding_agent_qa.py      # if pointed at bench/
"""

from __future__ import annotations

from pathlib import Path

from bench.coding_agent_qa import (
    _DEFAULT_FIXTURE,
    _run_dry,
    build_synthetic_retrieval,
    fix_reuse_hit,
    load_corpus,
    mrr,
    recall_at_k,
    score_question,
    summarize,
)


def test_recall_at_k_basic() -> None:
    gold = ["a", "b"]
    retrieved = ["x", "a", "y", "b", "z"]
    assert recall_at_k(gold, retrieved, 2) == 0.5  # only "a" in top-2
    assert recall_at_k(gold, retrieved, 5) == 1.0  # both in top-5
    assert recall_at_k(gold, retrieved, 1) == 0.0  # "x" only


def test_recall_at_k_empty_gold_is_zero_not_crash() -> None:
    assert recall_at_k([], ["a", "b"], 5) == 0.0


def test_recall_at_k_no_retrieval() -> None:
    assert recall_at_k(["a"], [], 5) == 0.0


def test_mrr_reciprocal_of_first_gold_rank() -> None:
    assert mrr(["a"], ["a", "b", "c"]) == 1.0
    assert mrr(["b"], ["a", "b", "c"]) == 0.5
    assert mrr(["c"], ["a", "b", "c"]) == 1.0 / 3.0
    assert mrr(["z"], ["a", "b", "c"]) == 0.0  # not retrieved


def test_mrr_uses_first_of_multiple_gold() -> None:
    # Two gold keys; reciprocal rank is of whichever appears first.
    assert mrr(["b", "c"], ["a", "b", "c"]) == 0.5


def test_fix_reuse_hit_small_k_gate() -> None:
    retrieved = ["other", "prior_fix", "noise"]
    assert fix_reuse_hit("prior_fix", retrieved, 5) is True
    assert fix_reuse_hit("prior_fix", retrieved, 1) is False  # past the small-k window
    assert fix_reuse_hit("absent", retrieved, 5) is False


def test_score_question_populates_fix_reuse_only_when_keyed() -> None:
    # No fix_reuse key -> the field is None (excluded from the rollup).
    r1 = score_question(["g"], ["g", "x"])
    assert r1.fix_reuse_hit_at_5 is None
    assert r1.r_at_5 == 1.0 and r1.mrr == 1.0

    # With a fix_reuse key that surfaces in top-5 -> True.
    r2 = score_question(["g"], ["g", "x"], fix_reuse_key="g")
    assert r2.fix_reuse_hit_at_5 is True

    # Keyed but the prior fix did NOT surface -> False (an actionable miss).
    r3 = score_question(["g"], ["x", "y", "z"], fix_reuse_key="g")
    assert r3.fix_reuse_hit_at_5 is False


def test_summarize_aggregates_and_rolls_up_fix_reuse() -> None:
    results = [
        score_question(["a"], ["a", "x"], question_type="bug-fix-landing"),
        score_question(["b"], ["x", "b"], question_type="infra-fix", fix_reuse_key="b"),
        score_question(["c"], ["x", "y"], question_type="infra-fix", fix_reuse_key="c"),
    ]
    s = summarize(results)
    assert s.n == 3
    # Per-type buckets present and counted.
    assert s.per_type["bug-fix-landing"]["n"] == 1.0
    assert s.per_type["infra-fix"]["n"] == 2.0
    # Fix-reuse rollup only over the two keyed questions: one hit@5, one miss.
    assert s.n_fix_reuse == 2
    assert s.fix_reuse_rate_at_5 == 0.5
    # No judge labels set -> qa_accuracy stays None.
    assert s.qa_accuracy is None


def test_summarize_qa_accuracy_when_judged() -> None:
    r1 = score_question(["a"], ["a"], question_type="t")
    r1.judge_label = "CORRECT"
    r2 = score_question(["b"], ["b"], question_type="t")
    r2.judge_label = "WRONG"
    s = summarize([r1, r2])
    assert s.n_judged == 2
    assert s.qa_accuracy == 0.5


def test_summarize_empty() -> None:
    s = summarize([])
    assert s.n == 0 and s.qa_accuracy is None and s.fix_reuse_rate_at_5 is None


def test_load_corpus_validates_gold_keys() -> None:
    lessons, questions = load_corpus(Path(_DEFAULT_FIXTURE))
    assert lessons and questions
    keys = {ln["memory_key"] for ln in lessons}
    for q in questions:
        for gk in q["gold_keys"]:
            assert gk in keys
        fr = q.get("fix_reuse") or {}
        if fr.get("prior_fix_key"):
            assert fr["prior_fix_key"] in keys
    # The taxonomy must cover the core coding question types.
    types = {q["question_type"] for q in questions}
    for required in (
        "bug-fix-landing",
        "infra-fix",
        "refactor-location",
        "flaky-test",
        "perf-leak",
        "schema-migration",
        "multi-session-causal",
        "temporal",
        "team-convention-preference",
    ):
        assert required in types, f"taxonomy missing {required}"


def test_load_corpus_rejects_dangling_gold_key(tmp_path: Path) -> None:
    import json

    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "lessons": [{"memory_key": "real", "memory_type": "lesson", "value": {}}],
                "questions": [{"question": "q", "question_type": "t", "gold_keys": ["ghost"]}],
            }
        )
    )
    raised = False
    try:
        load_corpus(bad)
    except ValueError:
        raised = True
    assert raised, "dangling gold_key should raise"


def test_build_synthetic_retrieval_puts_gold_first() -> None:
    _lessons, questions = load_corpus(Path(_DEFAULT_FIXTURE))
    synth = build_synthetic_retrieval(questions)
    for q in questions:
        retrieved = synth[q["question"]]
        gold = q["gold_keys"]
        if gold:
            assert retrieved[0] == gold[0], "first gold must rank #1 in synthetic fixture"
        # All gold present somewhere (so R@10 is perfect on the fixture).
        assert set(gold) <= set(retrieved)


def test_dry_mode_gate_passes_on_real_fixture() -> None:
    # The end-to-end offline gate: the same path `--dry` runs. Returns 0 and
    # asserts internally that R@5/R@10/MRR + fix-reuse compute. No API spend.
    _lessons, questions = load_corpus(Path(_DEFAULT_FIXTURE))
    assert _run_dry(questions) == 0


def _main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            # Skip the tmp_path fixture test in standalone mode (needs pytest).
            try:
                params = fn.__code__.co_varnames[: fn.__code__.co_argcount]
            except AttributeError:  # pragma: no cover
                params = ()
            if "tmp_path" in params:
                print(f"  SKIP {name} (needs pytest tmp_path)")
                continue
            try:
                fn()
                print(f"  PASS {name}")
            except AssertionError as e:  # pragma: no cover - smoke path
                failures += 1
                print(f"  FAIL {name}: {e}")
    print(f"{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())


def test_live_run_counts_failed_searches_and_sends_the_bearer(monkeypatch) -> None:
    import asyncio
    import functools

    import httpx

    from bench import coding_agent_qa as M

    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization", ""))
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={})
        return httpx.Response(503, json={"detail": "down"})

    monkeypatch.setattr(
        M.httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )
    questions = [{"question": "q1", "gold_keys": ["k"]}, {"question": "q2", "gold_keys": ["k"]}]
    results, failures = asyncio.run(
        M._run_live(
            "http://bench.invalid",
            "ns",
            [],
            questions,
            do_ingest=False,
            do_search=True,
            answer_judge=False,
            search_mode="rrf",
            alpha=0.5,
            top_k=10,
            reader_model="r",
            judge_model=None,
            judge_api_key="",
            token="tok",
        )
    )
    assert results == [] and failures == 2
    assert set(seen_auth) == {"Bearer tok"}
