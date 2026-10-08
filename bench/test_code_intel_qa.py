"""Unit tests for the code_intel_qa pure scoring core.

Lives under bench/ (not tests/) on purpose: `testpaths = ["tests"]` means
the repo's default pytest run does NOT collect this file; CI runs it via
the bench lint/unit lane so the scorer is exercised without a server.
Run: pytest bench/test_code_intel_qa.py
"""

from __future__ import annotations

from pathlib import Path

import pytest
from code_intel_qa import (
    Question,
    Retrieval,
    build_synthetic_retrieval,
    load_questions,
    mrr,
    recall_at_k,
    score_callers_f1,
    score_symbol_def,
    summarize,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_FIXTURE = _FIXTURES / "code_intel_qa.jsonl"


def _q(qtype: str, gold: dict) -> Question:
    return Question(qid="t", qtype=qtype, query="q", gold=gold)


def test_fixture_loads_and_validates() -> None:
    qs = load_questions(_FIXTURE)
    assert len(qs) >= 30
    types = {q.qtype for q in qs}
    assert types == {"symbol_def", "callers", "conceptual", "abstain_probe"}


def test_symbol_exact_match() -> None:
    q = _q(
        "symbol_def",
        {"qualified_name": "DB::IColumn::insert", "path_prefix": "src/Columns/"},
    )
    good = Retrieval(top_symbol="DB::IColumn::insert", paths=["src/Columns/IColumn.h"])
    assert score_symbol_def(q, good) == 1.0
    wrong_sym = Retrieval(top_symbol="DB::IColumn::insertFrom", paths=["src/Columns/IColumn.h"])
    assert score_symbol_def(q, wrong_sym) == 0.0
    # wrong location fails even with the right name
    wrong_loc = Retrieval(top_symbol="DB::IColumn::insert", paths=["contrib/x.h"])
    assert score_symbol_def(q, wrong_loc) == 0.0
    # abstaining on an answerable symbol scores zero
    assert score_symbol_def(q, Retrieval(top_symbol="DB::IColumn::insert", abstained=True)) == 0.0


def test_callers_f1() -> None:
    q = _q("callers", {"symbol": "f", "expected_callers": ["A::x", "B::y"]})
    assert score_callers_f1(q, Retrieval(callers=["A::x", "B::y"])) == 1.0
    assert score_callers_f1(q, Retrieval(callers=["A::x"])) == pytest.approx(2 / 3)
    noisy = Retrieval(callers=["A::x", "B::y", "C::z", "D::w"])
    assert score_callers_f1(q, noisy) == pytest.approx(2 / 3)
    assert score_callers_f1(q, Retrieval(callers=[])) == 0.0


def test_recall_is_fraction_of_gold_not_binary() -> None:
    gold = ["src/A.cpp", "src/B.cpp"]
    assert recall_at_k(gold, ["src/A.cpp"], 5) == 0.5
    assert recall_at_k(gold, ["src/A.cpp", "src/B.cpp"], 5) == 1.0
    # directory gold: a file inside the gold dir counts
    assert recall_at_k(["src/Access"], ["src/Access/EnabledQuota.cpp"], 5) == 1.0


def test_mrr_first_gold_position() -> None:
    assert mrr(["src/A.cpp"], ["x", "src/A.cpp"]) == 0.5
    assert mrr(["src/A.cpp"], []) == 0.0


def test_summarize_abstention_confusion() -> None:
    probe = _q("abstain_probe", {})
    concept = _q("conceptual", {"paths": ["src/A.cpp"]})
    pairs = [
        (probe, Retrieval(abstained=True)),  # correct abstain
        (probe, Retrieval(paths=["src/x.cpp"])),  # missed abstain
        (concept, Retrieval(abstained=True)),  # WRONG abstain on answerable
        (concept, Retrieval(paths=["src/A.cpp"], tokens_returned=100)),
    ]
    s = summarize(pairs)
    assert s.abstain_recall == 0.5  # 1 of 2 probes abstained
    assert s.abstain_precision == 0.5  # 1 correct of 2 total abstains
    assert s.by_type["abstain_probe"]["wrong_abstains_on_answerable"] == 1


def test_errors_excluded_not_counted_as_misses() -> None:
    q = _q("conceptual", {"paths": ["src/A.cpp"]})
    pairs = [
        (q, Retrieval(error="boom")),
        (q, Retrieval(paths=["src/A.cpp"], tokens_returned=10)),
    ]
    s = summarize(pairs)
    assert s.errors == 1
    assert s.recall5 == 1.0  # the errored row does not drag the ratio down


def test_dry_synthetic_is_perfect() -> None:
    qs = load_questions(_FIXTURE)
    pairs = [(q, build_synthetic_retrieval(q)) for q in qs]
    s = summarize(pairs)
    assert s.symbol_exact == 1.0
    assert s.callers_f1 == 1.0
    assert s.recall5 == 1.0
    assert s.abstain_precision == 1.0 and s.abstain_recall == 1.0


def test_gate_refuses_runs_with_errors() -> None:
    import argparse

    from code_intel_qa import gate

    q = _q("conceptual", {"paths": ["src/A.cpp"]})
    s = summarize([(q, Retrieval(error="token expired"))])
    args = argparse.Namespace(
        min_symbol_exact=None,
        min_callers_f1=None,
        min_recall5=None,
        min_abstain_precision=None,
        min_abstain_recall=None,
        max_mean_tokens=None,
    )
    rc, problems = gate(s, args)
    # an expired token: every ratio at its excluded-denominator
    # default, zero threshold violations — the errors alone must fail it
    assert rc == 1
    assert any("errored" in p for p in problems)


def test_gate_passes_clean_run() -> None:
    import argparse

    from code_intel_qa import gate

    q = _q("conceptual", {"paths": ["src/A.cpp"]})
    s = summarize([(q, Retrieval(paths=["src/A.cpp"], tokens_returned=10))])
    args = argparse.Namespace(
        min_symbol_exact=None,
        min_callers_f1=None,
        min_recall5=0.75,
        min_abstain_precision=None,
        min_abstain_recall=None,
        max_mean_tokens=None,
    )
    rc, problems = gate(s, args)
    assert rc == 0 and not problems


def test_hub_recall_is_share_of_gold_top5_among_first_five_symbols() -> None:
    from code_intel_qa import score_hub_recall5

    q = _q(
        "hub",
        {"path_prefix": "src/Interpreters/", "symbols": ["A::a", "B::b", "C::c", "D::d", "E::e"]},
    )
    # three of five gold in the first five, a fourth only at rank 6
    r = Retrieval(symbols=["B::b", "X::x", "A::a", "Y::y", "E::e", "C::c"])
    assert score_hub_recall5(q, r) == 0.6
    assert score_hub_recall5(q, Retrieval(symbols=[])) == 0.0
    s = summarize([(q, r)])
    assert s.hub_recall5 == 0.6 and s.by_type["hub"] == {"n": 1, "recall@5": 0.6}


def test_hub_fixture_loads_and_dry_run_is_perfect() -> None:
    from code_intel_qa import build_synthetic_retrieval, load_questions

    qs = load_questions(_FIXTURES / "code_intel_hub.jsonl")
    assert len(qs) == 20 and all(q.qtype == "hub" for q in qs)
    assert all(len(q.gold["symbols"]) == 5 and q.gold["path_prefix"].endswith("/") for q in qs)
    s = summarize([(q, build_synthetic_retrieval(q)) for q in qs])
    assert s.hub_recall5 == 1.0


def test_hub_needs_prefix_and_symbols(tmp_path) -> None:
    import json

    from code_intel_qa import load_questions

    bad = tmp_path / "hub.jsonl"
    bad.write_text(
        json.dumps({"id": "h1", "type": "hub", "query": "q", "gold": {"path_prefix": "src/"}})
        + "\n"
    )
    with pytest.raises(ValueError, match="hub needs"):
        load_questions(bad)


def test_post_waits_out_a_429_and_raises_other_statuses() -> None:
    import asyncio

    from code_intel_qa import _post

    class _Resp:
        def __init__(self, code: int, retry_after: str | None = None) -> None:
            self.status_code = code
            self.headers = {"retry-after": retry_after} if retry_after else {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"status {self.status_code}")

    class _Client:
        def __init__(self, codes: list[int]) -> None:
            self.codes = list(codes)
            self.calls = 0

        async def post(self, path: str, json: dict) -> _Resp:
            self.calls += 1
            return _Resp(self.codes.pop(0), retry_after="0.2")

    c = _Client([429, 429, 200])
    r = asyncio.run(_post(c, "/v1/code.search", {}))
    assert r.status_code == 200 and c.calls == 3
    with pytest.raises(RuntimeError, match="status 500"):
        asyncio.run(_post(_Client([500]), "/v1/code.search", {}))
    with pytest.raises(RuntimeError, match="status 429"):
        asyncio.run(_post(_Client([429] * 3), "/v1/code.search", {}, attempts=2))


def test_gate_placebo_rule_needs_a_pool_and_then_fires() -> None:
    import argparse

    from code_intel_qa import _PLACEBO_MIN_QUESTIONS, gate

    args = argparse.Namespace(
        min_symbol_exact=None,
        min_callers_f1=None,
        min_recall5=0.5,
        min_abstain_precision=None,
        min_abstain_recall=None,
        max_mean_tokens=None,
    )
    # one perfect question: recall 1.0 == placebo 1.0, but the pool is tiny
    q = _q("conceptual", {"paths": ["src/A.cpp"]})
    s = summarize([(q, Retrieval(paths=["src/A.cpp"], tokens_returned=10))])
    assert s.recall5 == s.placebo_recall5 == 1.0
    assert gate(s, args) == (0, [])
    # a real pool where every answer is another question's file: ranking is
    # no better than the shuffled control and the rule refuses the claim
    pairs = []
    for i in range(_PLACEBO_MIN_QUESTIONS):
        qi = Question(qid=f"q{i}", qtype="conceptual", query="q", gold={"paths": [f"src/F{i}.cpp"]})
        wrong = f"src/F{(i + 1) % _PLACEBO_MIN_QUESTIONS}.cpp"
        pairs.append((qi, Retrieval(paths=[wrong], tokens_returned=10)))
    s2 = summarize(pairs)
    args.min_recall5 = 0.0
    rc, problems = gate(s2, args)
    assert rc == 1 and any("placebo" in p for p in problems)


# ---- history lane ------------------------------------------------


def test_history_fixture_loads_with_four_lanes_and_dry_run_is_perfect() -> None:
    qs = load_questions(_FIXTURES / "code_intel_history.jsonl")
    assert len(qs) >= 60 and {q.qtype for q in qs} == {"history"}
    lanes = {q.lane for q in qs}
    assert lanes == {"number_issue", "number_pr", "phrase_issue", "phrase_pr"}
    assert all(q.gold.get("pr_number") and q.gold.get("paths") for q in qs)
    s = summarize([(q, build_synthetic_retrieval(q)) for q in qs])
    ceiling = sum(min(1.0, 5 / len(q.gold["paths"])) for q in qs) / len(qs)
    assert s.history_pr_hit == 1.0 and s.history_fired == 1.0
    assert abs(s.history_recall5 - ceiling) < 1e-9 and ceiling > 0.99
    assert s.by_type["history"]["n"] == len(qs)


def test_history_needs_pr_number_and_paths(tmp_path) -> None:
    bad = tmp_path / "h.jsonl"
    bad.write_text(
        '{"id": "x", "type": "history", "query": "what fixed issue 1", "gold": {"paths": ["a"]}}\n'
    )
    with pytest.raises(ValueError, match="gold.pr_number"):
        load_questions(bad)


def test_history_pr_hit_and_fire_rate_are_read_off_the_route_and_section() -> None:
    from code_intel_qa import history_fired, score_history_pr_hit

    q = Question(
        qid="h",
        qtype="history",
        query="what fixed issue 7",
        gold={"pr_number": 42, "paths": ["src/a", "src/b"]},
        lane="number_issue",
    )
    hit = Retrieval(paths=["src/a.cpp", "src/z.cpp"], route="history_number", history_prs=[42, 43])
    miss = Retrieval(paths=["src/a.cpp"], route="hybrid", history_prs=[])
    assert score_history_pr_hit(q, hit) == 1.0 and score_history_pr_hit(q, miss) == 0.0
    assert history_fired(hit) and not history_fired(miss)
    c = Question(qid="c", qtype="conceptual", query="how does x", gold={"paths": ["src/a"]})
    s = summarize([(q, hit), (c, Retrieval(paths=["src/a.cpp"], route="hybrid"))])
    assert s.by_type["history"] == {"n": 1, "pr_hit": 1.0, "recall@5": 0.5, "fired": 1.0}
    # one of two code.search answers rode a history_* route
    assert s.history_fire_rate == 0.5
    # a guard lane where the intent never fires reads zero
    assert summarize([(c, Retrieval(paths=["src/a.cpp"], route="hybrid"))]).history_fire_rate == 0.0


def test_compare_arms_pairs_history_questions_and_reports_pr_hit() -> None:
    from code_intel_qa import compare_arms

    q = Question(
        qid="h",
        qtype="history",
        query="what fixed issue 7",
        gold={"pr_number": 42, "paths": ["src/x/a", "src/x/b"]},
        lane="number_issue",
    )
    a = [(q, Retrieval(paths=["src/y/c.cpp"], route="hybrid"))]
    b = [
        (
            q,
            Retrieval(
                paths=["src/x/a.cpp", "src/x/b.cpp"], route="history_number", history_prs=[42]
            ),
        )
    ]
    cmp = compare_arms(a, b)
    assert cmp["n"] == 1 and cmp["recall5_a"] == 0.0 and cmp["recall5_b"] == 1.0
    assert cmp["secondary"]["history pr_hit"]["a"] == 0.0
    assert cmp["secondary"]["history pr_hit"]["b"] == 1.0


def test_miner_paraphrases_a_title_as_a_clause() -> None:
    from code_intel_mine_prs import paraphrase_title

    assert paraphrase_title("Fix detach/attach for postgres tables.") == (
        "fixed detach/attach for postgres tables"
    )
    assert paraphrase_title("Add system.stemmers") == "added system.stemmers"
    assert paraphrase_title("Parallel replicas: cache announcements") == (
        "introduced parallel replicas: cache announcements"
    )
    assert paraphrase_title("") == ""


def test_oss_fixture_loads_with_namespaces_and_dry_run_is_perfect() -> None:
    """The multi-repository lane names a namespace per row,
    carries the corpus category / language on `source`, and scores 1.0 on a
    synthetic perfect retrieval like every other conceptual fixture."""
    from code_intel_qa import build_synthetic_retrieval, load_questions

    qs = load_questions(_FIXTURES / "code_intel_oss.jsonl")
    assert len(qs) >= 300
    assert {q.qtype for q in qs} == {"conceptual"} and {q.lane for q in qs} == {"oss"}
    namespaces = {q.namespace for q in qs}
    assert len(namespaces) == 15 and all(ns.startswith("code-oss-") for ns in namespaces)
    assert {q.source["category"] for q in qs} == {"semantic", "architecture", "symbol"}
    assert {q.source["language"] for q in qs} == {"python", "cpp", "bash"}
    s = summarize([(q, build_synthetic_retrieval(q)) for q in qs])
    assert s.recall5 == 1.0
    assert set(s.by_facet) == {"lane", "category", "language"}
    assert s.by_facet["lane"]["oss"]["n"] == len(qs)
    assert sum(m["n"] for m in s.by_facet["language"].values()) == len(qs)


def test_row_namespace_overrides_the_run_identity_only_when_set() -> None:
    from code_intel_qa import _IDENTITY, _identity_for

    _IDENTITY.clear()
    _IDENTITY.update({"org": "o", "namespace": "code-run"})
    try:
        plain = Question(qid="a", qtype="conceptual", query="q", gold={"paths": ["src/a"]})
        scoped = Question(
            qid="b",
            qtype="conceptual",
            query="q",
            gold={"paths": ["src/a"]},
            namespace="code-oss-x",
        )
        assert _identity_for(plain) == {"org": "o", "namespace": "code-run"}
        assert _identity_for(scoped) == {"org": "o", "namespace": "code-oss-x"}
    finally:
        _IDENTITY.clear()


def test_compare_arms_clusters_by_namespace_on_a_multi_repo_fixture() -> None:
    """The same gold subsystem in two repositories is two bootstrap clusters
    (clustered by namespace prefix); a single-namespace fixture
    keeps the bare subsystem key."""
    from bootstrap import cluster_of_path
    from code_intel_qa import _cluster_for

    a = Question(
        qid="a",
        qtype="conceptual",
        query="q",
        gold={"paths": ["src/x/a"]},
        namespace="code-oss-one",
    )
    b = Question(
        qid="b",
        qtype="conceptual",
        query="q",
        gold={"paths": ["src/x/b"]},
        namespace="code-oss-two",
    )
    c = Question(qid="c", qtype="conceptual", query="q", gold={"paths": ["src/x/c"]})
    assert _cluster_for(a, a.gold["paths"], cluster_of_path) == "code-oss-one:src/x"
    assert _cluster_for(b, b.gold["paths"], cluster_of_path) == "code-oss-two:src/x"
    assert _cluster_for(c, c.gold["paths"], cluster_of_path) == "src/x"
    assert _cluster_for(c, [], cluster_of_path) == "c"


def test_summary_facets_appear_only_when_the_fixture_carries_them() -> None:
    plain = Question(qid="p", qtype="conceptual", query="q", gold={"paths": ["src/a"]})
    s = summarize([(plain, Retrieval(paths=["src/a.py"]))])
    assert s.by_facet == {}
    faceted = Question(
        qid="f",
        qtype="conceptual",
        query="q",
        gold={"paths": ["src/a"]},
        lane="oss",
        source={"category": "symbol", "language": "python"},
    )
    s = summarize([(faceted, Retrieval(paths=["src/b.py", "src/a.py"]))])
    assert s.by_facet["category"]["symbol"] == {
        "n": 1,
        "recall@5": 1.0,
        "recall@10": 1.0,
        "mrr": 0.5,
    }
    assert s.by_facet["lane"]["oss"]["n"] == 1 and s.by_facet["language"]["python"]["n"] == 1


def test_work_counters_skip_non_integer_values() -> None:
    """A string-valued retrieval_work key does not error the question;
    counters are coerced, the rest skipped."""
    from bench.code_intel_qa import _work_counters

    assert _work_counters({"lexical_queries": 2, "source": "table", "ms": 3.9, "n": "7"}) == {
        "lexical_queries": 2,
        "ms": 3,
        "n": 7,
    }
    assert _work_counters(None) == {}


def test_test_set_lane_scorers() -> None:
    """Hit@5 is any gold in the top 5; precision@5 is the share
    of the top 5 that are gold; set recall@5 alone caps at 5/|gold|."""
    from bench.code_intel_qa import hit_at_k, precision_at_k, recall_at_k

    gold = [f"tests/queries/0_stateless/{i:05d}_x.sql" for i in range(12)]
    got = gold[:3] + ["src/IO/a.cpp", "tests/queries/0_stateless/99999_y.sql"] + gold[3:6]
    assert hit_at_k(gold, got, 5) == 1.0
    assert precision_at_k(gold, got, 5) == 0.6
    assert recall_at_k(gold, got, 5) == 3 / 12  # the ceiling the lane does not gate on
    assert hit_at_k(gold, ["src/a.cpp"], 5) == 0.0
    assert precision_at_k(gold, [], 5) == 0.0


def test_test_set_rows_load_with_facets(tmp_path) -> None:
    from bench.code_intel_qa import load_questions

    p = tmp_path / "q.jsonl"
    p.write_text(
        '{"id":"a","type":"test_set","query":"tests that set dialect","gold":{"paths":["t/1.sql"]},'
        '"lane":"tests_for_setting","facets":[{"kind":"setting","name":"dialect"}]}\n'
        '{"id":"b","type":"test_set","query":"shell tests using KILL QUERY",'
        '"gold":{"paths":["t/2.sh"]},"lane":"shell_island"}\n'
    )
    qs = load_questions(p)
    assert [q.qtype for q in qs] == ["test_set", "test_set"]
    assert qs[0].facets == [{"kind": "setting", "name": "dialect"}]
    assert qs[1].facets == []


def test_compare_arm_track_misses_reads_that_arms_overrides(monkeypatch) -> None:
    """`make bench-code-legs` runs --compare-extra with --track-misses; the arm-B
    miss tracker gets that arm's JSON overrides, not the repeatable flag's list."""
    import code_intel_qa as M

    async def fake_run_live(questions, *_a, search_extra=None, **_k):
        return [(q, Retrieval(paths=[])) for q in questions]

    seen: list[dict | None] = []

    async def fake_track(pairs, *_a, search_extra=None, **_k):
        seen.append(search_extra)
        return []

    monkeypatch.setattr(M, "run_live", fake_run_live)
    monkeypatch.setattr(M, "track_misses", fake_track)
    M.main(
        [
            "--dataset",
            str(_FIXTURE),
            "--base-url",
            "http://bench.invalid",
            "--compare-extra",
            '{"legs": "lexical"}',
            "--track-misses",
        ]
    )
    assert seen == [None, {"legs": "lexical"}]


@pytest.mark.parametrize("path", sorted(_FIXTURES.glob("code_intel_*.jsonl")), ids=lambda p: p.name)
def test_every_code_intel_fixture_validates_and_passes_the_dry_self_check(path, capsys) -> None:
    """Each shipped fixture loads (the loader rejects unknown types, missing gold and
    duplicate ids) and a perfect retrieval scores its ceiling on every lane; a
    question with more than five gold paths caps recall@5 below 1.0."""
    import code_intel_qa as M

    assert M.main(["--dry", "--dataset", str(path)]) == 0
    assert "--dry self-check passed" in capsys.readouterr().out


def test_compare_arms_history_pr_hit_bootstraps_over_history_clusters_only() -> None:
    """On a mixed fixture the pr_hit interval resamples the history questions'
    own clusters, an errored history pair drops out of both lists, and the
    arm-B error count is reported for the paired gate."""
    from code_intel_qa import compare_arms

    def hist(qid: str, sub: str) -> Question:
        return Question(
            qid=qid,
            qtype="history",
            query=qid,
            gold={"pr_number": 1, "paths": [f"src/{sub}/a"]},
            lane="number_issue",
        )

    concept = Question(qid="c", qtype="conceptual", query="c", gold={"paths": ["src/C/x"]})
    h1, h2, h3 = hist("h1", "H1"), hist("h2", "H2"), hist("h3", "H3")
    a = [(concept, Retrieval(paths=[]))] + [(h, Retrieval(paths=[])) for h in (h1, h2, h3)]
    b = [
        (concept, Retrieval(paths=["src/C/x.cpp"])),
        (h1, Retrieval(paths=[], history_prs=[1])),
        (h2, Retrieval(error="boom")),
        (h3, Retrieval(paths=[], history_prs=[1])),
    ]
    cmp = compare_arms(a, b)
    assert cmp["n"] == 3 and cmp["errored_b"] == 1
    pr = cmp["secondary"]["history pr_hit"]
    assert pr["a"] == 0.0 and pr["b"] == 1.0 and pr["boot"].n == 2


def test_summary_records_the_served_index_revision_and_edge_tiers() -> None:
    from code_intel_qa import _as_of

    concept = Question(qid="c", qtype="conceptual", query="c", gold={"paths": ["src/a"]})
    callers = Question(
        qid="k", qtype="callers", query="k", gold={"symbol": "f", "expected_callers": ["g"]}
    )
    served = _as_of({"as_of": {"commit": "0123456789abcdef", "stale": True}})
    s = summarize(
        [
            (concept, Retrieval(paths=["src/a.cpp"], **served)),
            (callers, Retrieval(callers=["g"], edge_tiers={"scip": 2, "treesitter": 1})),
        ]
    )
    assert s.index_commits == {"0123456789ab": 1} and s.stale_answers == 1
    assert s.edge_tiers == {"scip": 2, "treesitter": 1}
    assert _as_of({}) == {"index_commit": "", "index_stale": False}
