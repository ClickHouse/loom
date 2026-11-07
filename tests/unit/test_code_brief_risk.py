"""Calibrated change risk on the review brief: the pure fit
recovers a planted signal and measures itself on a time split, the author
prior is Laplace-smoothed toward the base rate and leaves the row's own
outcome out, the brief's `risk` section scores from the stored fit (or
says `no_fit`), no `decisions` bit reads it, the section is absent — and
the payload byte-identical to the base brief's — with the flag off, an old payload
serves without a placeholder, the QA reader carries the privacy predicate
and the label rule, and the weekly leg writes the fit once per interval for
bound, non-restricted namespaces only."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from server.code.app.index import review_brief as rb
from server.code.app.index import review_brief_build, risk_fit
from server.code.domain.index import risk_calibration as rc
from server.settings import settings
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json
from tests.unit.test_code_review_brief_sections import (
    NS,
    ORG,
    PATHS,
    _Calls,
    _fixed_now,
    _inputs,
    _Repo,
    _tracker_not_bound,
)

NOW = datetime(2026, 9, 16, 12, 0, 0)


def _rows(n: int = 3000, *, seed: int = 7) -> list[dict[str, Any]]:
    """A planted world: risk rises with files changed and with an author's
    own rate; merges spread over twelve months."""
    rng = random.Random(seed)
    authors = [(f"dev{i}", 0.01 + 0.02 * i) for i in range(6)] + [("bot[bot]", 0.005)]
    rows = []
    for i in range(n):
        author, a_rate = rng.choice(authors)
        files = max(1, int(rng.lognormvariate(1.2, 1.0)))
        lines = max(1, int(files * rng.lognormvariate(3.0, 1.0)))
        p = rc.sigmoid(rc.logit(a_rate) + 0.6 * (rc.features(lines, files, 0.5)[2] - 1.5))
        rows.append(
            {
                "pr_number": 100000 + i,
                "author": author,
                "lines_added": lines,
                "files_changed": files,
                "merged_at": NOW - timedelta(days=365 * (n - i) / n),
                "label": int(rng.random() < p),
            }
        )
    return rows


# ---- pure ------------------------------------------------------------------


def test_fit_recovers_the_planted_signal_and_measures_itself_on_a_time_split() -> None:
    rows = _rows()
    fit = rc.fit_model(rows, now=NOW, window_months=12, holdout_months=3)
    assert (
        fit["v"] == rc.FIT_VERSION and fit["n"] == 3000 and fit["fit_at"] == "2026-09-16T12:00:00Z"
    )
    assert (
        0 < fit["positives"] < fit["n"]
        and abs(fit["base_rate"] - fit["positives"] / fit["n"]) < 1e-4
    )
    coef = fit["coef"]
    assert set(coef) == set(rc.FEATURE_NAMES)
    assert coef["log_files_changed"] > 0.2 and coef["logit_author_prior"] > 0.2
    h = fit["holdout"]
    # the holdout is the last three months, scored by the months before it
    assert h["cutoff"] == "2026-06-18T12:00:00Z" and 600 < h["n"] < 900 and h["positives"] > 0
    assert h["auc"] > 0.65 and h["auc"] >= h["auc_lines_added"] - 0.02
    assert len(h["deciles"]) == 10 and sum(d["n"] for d in h["deciles"]) == h["n"]
    assert h["deciles"][0]["predicted"] < h["deciles"][-1]["predicted"]
    # the [bot] suffix is one author key with the tracker record's login
    assert "bot" in fit["authors"] and "bot[bot]" not in fit["authors"]
    assert fit["authors"]["bot"][0] >= fit["authors"]["bot"][1]
    # the fit is JSON; scoring reads it back
    back = json.loads(json.dumps(fit, sort_keys=True))
    s = rc.score(back, lines_added=400, files_changed=30, author="dev5")
    t = rc.score(back, lines_added=400, files_changed=30, author="dev0")
    assert s["jit_risk"] > t["jit_risk"] and s["author_revert_prior"] > t["author_revert_prior"]
    assert s["author_known"] and s["author_merged"] == back["authors"]["dev5"][0]


def test_author_prior_is_smoothed_toward_the_base_rate_and_leaves_the_row_out() -> None:
    assert rc.smoothed_prior(0, 0, 0.04, m=5) == pytest.approx(0.04)
    assert rc.smoothed_prior(5, 5, 0.04, m=5) == pytest.approx((5 + 0.2) / 10)
    fit = {
        "base_rate": 0.04,
        "smoothing_m": 5.0,
        "coef": {n: 0.0 for n in rc.FEATURE_NAMES},
        "authors": {"alice": [20, 4]},
    }
    known = rc.score(fit, lines_added=1, files_changed=1, author="alice")
    unseen = rc.score(fit, lines_added=1, files_changed=1, author="nobody")
    blank = rc.score(fit, lines_added=1, files_changed=1, author="")
    assert known["author_revert_prior"] == pytest.approx(round((4 + 0.2) / 25, 4))
    assert unseen["author_revert_prior"] == pytest.approx(0.04) and not unseen["author_known"]
    assert blank["author"] == "" and not blank["author_known"]
    # leave-one-out on the fitting window: a row does not vote for itself
    rows = [
        {"author": "a", "lines_added": 1, "files_changed": 1, "label": 1, "merged_at": NOW},
        {"author": "a", "lines_added": 1, "files_changed": 1, "label": 0, "merged_at": NOW},
    ]
    counts = rc._author_counts(rows)
    _x, _y, priors = rc._design(rows, counts, 0.5, loo=True, m=2.0)
    assert priors == [pytest.approx((0 + 1.0) / 3), pytest.approx((1 + 1.0) / 3)]
    _x, _y, priors = rc._design(rows, counts, 0.5, loo=False, m=2.0)
    assert priors == [pytest.approx(2.0 / 4)] * 2


def test_auc_and_deciles_and_diff_line_count() -> None:
    assert rc.auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == pytest.approx(0.75)
    assert rc.auc([0.5, 0.5], [0, 1]) == pytest.approx(0.5)  # ties at half
    assert rc.auc([0.2, 0.3], [0, 0]) == 0.0
    dec = rc.calibration_deciles([i / 100 for i in range(100)], [int(i >= 90) for i in range(100)])
    assert [d["observed"] for d in dec] == [0.0] * 9 + [1.0] and all(d["n"] == 10 for d in dec)
    diff = "+++ b/x\n@@ -1,2 +1,3 @@\n+a\n b\n-c\n+d\n"
    assert rc.lines_added_in(diff) == 2
    assert rc.fit_age_days({"fit_at": "2026-03-16T12:00:00Z"}, NOW) == pytest.approx(184.0)
    assert rc.fit_age_days({}, NOW) == float("inf")
    assert rc.fit_logistic([], []) == [0.0] * 4


# ---- the section ------------------------------------------------------------


def _fit_json(*, fit_at: str = "2026-09-10T00:00:00Z") -> str:
    fit = rc.fit_model(_rows(), now=NOW)
    fit["fit_at"] = fit_at
    fit["authors"]["kdev"] = [40, 4]
    return json.dumps(fit, sort_keys=True)


@pytest.mark.asyncio
async def test_risk_section_scores_from_the_stored_fit_and_discloses_the_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    repo.state[risk_fit.STATE_KEY] = _fit_json()
    inp = _inputs()
    inp.author = "kdev"
    payload = await review_brief_build.build_brief(
        inp, call=_Calls(), repo=repo, tracker_rows=_tracker_not_bound, now=_fixed_now
    )
    # the optional groups/reviewers sections are off by default: the order is
    # every section but those two, `risk` included
    assert list(payload["sections"]) == list(rb.SECTION_ORDER)
    sec = payload["sections"]["risk"]
    assert sec["status"] == rb.SERVED and sec["verdict"] == "scored" and sec["epistemic"] == "prior"
    assert sec["causes"] == [] and sec["note"] == review_brief_build.RISK_NOTE
    jit, prior = sec["rows"]
    assert jit["kind"] == "jit_risk" and 0.0 < jit["value"] < 1.0
    assert jit["lines_added"] == 5 and jit["files_changed"] == len(PATHS)  # the diff's `+` lines
    assert (
        jit["fit_at"] == "2026-09-10T00:00:00Z" and jit["fit_n"] == 3000 and jit["holdout_auc"] > 0
    )
    assert set(jit["coefficients"]) == set(rc.FEATURE_NAMES)
    assert prior["kind"] == "author_revert_prior" and prior["author"] == "kdev"
    assert (
        prior["merged"] == 40 and prior["reverted_or_caused"] == 4 and prior["window_months"] == 12
    )
    assert prior["value"] == pytest.approx(jit["author_prior"])
    # a ranking signal, never a gate: no decision bit reads the section
    assert set(payload["decisions"]) == {
        "test_only",
        "symbol_with_regressions",
        "uncovered_symbol",
        "randomized_setting",
        "deleted_lines_blamed",
    }
    # the standard tier serves it; trivial does not
    served, _used, _b = rb.serve(payload, tier="standard", requested=[])
    assert "risk" in served and served["risk"]["verdict"] == "scored"
    served, _used, _b = rb.serve(payload, tier="trivial", requested=[])
    assert "risk" not in served


@pytest.mark.asyncio
async def test_risk_section_says_no_fit_unknown_author_and_stale_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_jit_risk_max_age_days", 183)
    # no stored fit: an empty section, verdict no_fit, still `prior`
    payload = await review_brief_build.build_brief(
        _inputs(), call=_Calls(), repo=_Repo(), tracker_rows=_tracker_not_bound, now=_fixed_now
    )
    sec = payload["sections"]["risk"]
    assert sec["status"] == rb.EMPTY and sec["verdict"] == "no_fit" and sec["causes"] == ["no_fit"]
    assert sec["rows"] == [] and sec["epistemic"] == "prior"
    # a fit older than six months scores, says so; a blank author is the base rate
    repo = _Repo()
    repo.state[risk_fit.STATE_KEY] = _fit_json(fit_at="2026-02-01T00:00:00Z")
    payload = await review_brief_build.build_brief(
        _inputs(), call=_Calls(), repo=repo, tracker_rows=_tracker_not_bound, now=_fixed_now
    )
    sec = payload["sections"]["risk"]
    assert sec["verdict"] == "scored_stale" and sec["causes"] == ["author_unknown", "fit_stale"]
    fit = json.loads(repo.state[risk_fit.STATE_KEY])
    assert sec["rows"][1]["value"] == pytest.approx(round(fit["base_rate"], 4))
    assert sec["rows"][1]["author"] == "" and sec["rows"][1]["merged"] == 0
    # an author the window never saw
    inp = _inputs()
    inp.author = "newcomer"
    payload = await review_brief_build.build_brief(
        inp, call=_Calls(), repo=repo, tracker_rows=_tracker_not_bound, now=_fixed_now
    )
    assert payload["sections"]["risk"]["causes"] == ["author_unseen", "fit_stale"]


@pytest.mark.asyncio
async def test_a_payload_stored_without_the_risk_section_serves_unchanged() -> None:
    repo = _Repo()
    repo.state[risk_fit.STATE_KEY] = _fit_json()
    off = await review_brief_build.build_brief(
        _inputs(), call=_Calls(), repo=repo, tracker_rows=_tracker_not_bound, now=_fixed_now
    )
    off["sections"].pop("risk")
    row_off = review_brief_build.brief_row(ORG, NS, off)
    assert "risk" not in row_off["sections"]
    # a payload stored without the risk section: standard and full serve it without
    # a placeholder; asking for `risk` by name gets the honest not_built
    served, _u, _b = rb.serve(off, tier="standard", requested=[])
    assert "risk" not in served and list(served) == [
        "paths",
        "symbols",
        "tests",
        "settings",
        "history",
        "do_not_flag",
    ]
    served, _u, _b = rb.serve(off, tier="full", requested=[])
    assert "risk" not in served
    served, _u, _b = rb.serve(off, tier="full", requested=["risk"])
    assert served["risk"]["status"] == rb.NOT_BUILT


@pytest.mark.asyncio
async def test_build_for_merge_reads_the_author_from_the_ledger_when_not_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    class _Ledger(_Repo):
        def __init__(self) -> None:
            super().__init__()
            self.asked: list[int] = []
            self.state[risk_fit.STATE_KEY] = _fit_json()

        async def pr_merge_facts(
            self, org: str, ns: str, *, pr_number: int = 0, merge_sha: str = ""
        ) -> dict[str, Any]:
            self.asked.append(pr_number)
            return {"pr_number": pr_number, "author": "kdev", "title": "t", "files": []}

    class _Git:
        @staticmethod
        async def run(git_dir: str, *args: str) -> str:
            return "b" * 40 if args[0] == "rev-parse" else ""

    from server.code.app.index import gitrepo

    monkeypatch.setattr(gitrepo, "run", _Git.run)
    repo = _Ledger()
    payload = await review_brief_build.build_for_merge(
        repo,
        ORG,
        NS,
        git_dir="/nowhere",
        pr_number=117354,
        title="t",
        merge_sha="a" * 40,
        changed_paths=PATHS,
        call=_Calls(),
        store=False,
    )
    assert repo.asked == [117354]
    assert payload["sections"]["risk"]["rows"][1]["author"] == "kdev"
    # given an author, the ledger is not asked
    repo = _Ledger()
    await review_brief_build.build_for_merge(
        repo,
        ORG,
        NS,
        git_dir="/nowhere",
        pr_number=117354,
        title="t",
        merge_sha="a" * 40,
        changed_paths=PATHS,
        call=_Calls(),
        store=False,
        author="amir",
    )
    assert repo.asked == []


# ---- the QA reader ------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracker_reader_carries_the_privacy_predicate_and_the_label_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.tracker.store import source as src
    from server.tracker.store.source import qa_intel as qi

    monkeypatch.setattr(settings, "tracker_org", ORG, raising=False)
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json(
            json.dumps({"ClickHouse/ClickHouse": "tracker-clickhouse"}), restricted=""
        ),
        raising=False,
    )
    monkeypatch.setattr(
        settings, "tracker_credentials_json", tracker_credentials_json(), raising=False
    )
    monkeypatch.setattr(settings, "tracker_live_serve", True, raising=False)
    src.reset_cache_for_tests()
    seen: list[tuple[str, dict[str, Any], Any]] = []

    async def up(sql: str, params: dict[str, Any], **kw: Any) -> list[dict[str, Any]]:
        seen.append((sql, params, kw))
        return [
            {
                "pr_number": 1,
                "author": "a",
                "additions": 10,
                "changed_files_count": 2,
                "closed_at": datetime(2026, 1, 1),
                "is_reverted": 1,
                "revert_edge": 0,
                "caused_edge": 0,
            },
            {
                "pr_number": 2,
                "author": "b",
                "additions": 5,
                "changed_files_count": 1,
                "closed_at": datetime(2026, 2, 1),
                "is_reverted": 0,
                "revert_edge": 0,
                "caused_edge": 1,
            },
            {
                "pr_number": 3,
                "author": "c",
                "additions": 7,
                "changed_files_count": 3,
                "closed_at": datetime(2026, 3, 1),
                "is_reverted": 0,
                "revert_edge": 0,
                "caused_edge": 0,
            },
        ]

    monkeypatch.setattr(qi, "query_rows", up)
    rows = await src.TrackerSource(ORG, "tracker-clickhouse").merged_pr_outcomes(
        ORG, "tracker-clickhouse", months=12
    )
    sql, params, kw = seen[0]
    assert "is_private = 0 AND is_merged = 1" in sql and "is_backport_pr = 0" in sql
    assert "rel_type = 'reverts'" in sql and "rel_type = 'caused_by'" in sql
    assert "FINAL" in sql and "PREWHERE" not in sql and "ORDER BY closed_at" in sql
    assert params == {"months": 12, "repos": ["ClickHouse/ClickHouse"], "limit": 20000}
    assert kw.get("max_execution_s") == pytest.approx(6.0)  # the scan budget, not the point read's
    assert [(r["pr_number"], r["label"], r["reverted"], r["caused"]) for r in rows] == [
        (1, 1, 1, 0),
        (2, 1, 0, 1),
        (3, 0, 0, 0),
    ]
    assert rows[0]["lines_added"] == 10 and rows[0]["files_changed"] == 2
    assert rows[0]["merged_at"] == datetime(2026, 1, 1)
    src.reset_cache_for_tests()


# ---- the weekly leg -----------------------------------------------------------


class _StateRepo:
    def __init__(self, existing: str = "") -> None:
        self.state: dict[str, str] = {risk_fit.STATE_KEY: existing} if existing else {}
        self.writes: list[tuple[str, str, str, str]] = []

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return self.state.get(key, "")

    async def set_state(self, org: str, ns: str, key: str, value: str, detail: str = "") -> None:
        self.state[key] = value
        self.writes.append((ns, key, value, detail))


def _bind(monkeypatch: pytest.MonkeyPatch, *, restricted: bool = False) -> None:
    from server.identity.app import namespace_classification as nc
    from server.tracker.app import binding

    monkeypatch.setattr(
        binding,
        "tracker_namespace_for",
        lambda org, ns: "tracker-clickhouse" if ns.startswith("code-") else "",
    )

    async def _restricted(org: str, ns: str) -> bool:
        return restricted

    monkeypatch.setattr(nc, "is_restricted", _restricted)


@pytest.mark.asyncio
async def test_refit_writes_once_per_interval_for_bound_namespaces_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_jit_risk_refit_interval_s", 7 * 86400)
    monkeypatch.setattr(settings, "code_jit_risk_window_months", 12)
    _bind(monkeypatch)
    asked: list[tuple[str, str, int]] = []

    async def outcomes(org: str, tracker_ns: str, months: int) -> list[dict[str, Any]]:
        asked.append((org, tracker_ns, months))
        return _rows(800)

    repo = _StateRepo()
    out = await risk_fit.refit_namespace(
        repo, ORG, NS, now=NOW.replace(tzinfo=UTC), outcomes=outcomes
    )
    assert out.status == "written" and out.n == 800 and out.positives > 0 and out.holdout_auc > 0
    assert asked == [(ORG, "tracker-clickhouse", 12)]
    ns, key, value, detail = repo.writes[0]
    assert (ns, key) == (NS, risk_fit.STATE_KEY)
    fit = json.loads(value)
    assert (
        fit["tracker_namespace"] == "tracker-clickhouse" and fit["fit_at"] == "2026-09-16T12:00:00Z"
    )
    assert "holdout_auc=" in detail and "n=800" in detail
    # a fresh fit is not rewritten; a week later it is
    out = await risk_fit.refit_namespace(
        repo, ORG, NS, now=NOW.replace(tzinfo=UTC), outcomes=outcomes
    )
    assert out.status == "fresh" and len(repo.writes) == 1
    later = (NOW + timedelta(days=8)).replace(tzinfo=UTC)
    out = await risk_fit.refit_namespace(repo, ORG, NS, now=later, outcomes=outcomes)
    assert out.status == "written" and len(repo.writes) == 2
    # unbound and restricted namespaces get no fit cached under them
    assert (
        await risk_fit.refit_namespace(repo, ORG, "memory-x", outcomes=outcomes)
    ).status == "not_bound"
    _bind(monkeypatch, restricted=True)
    fresh = _StateRepo()
    assert (
        await risk_fit.refit_namespace(fresh, ORG, NS, outcomes=outcomes)
    ).status == "restricted"
    assert fresh.writes == []
    # too few rows: nothing written; a raising reader: failed, nothing written
    _bind(monkeypatch)

    async def few(org: str, tracker_ns: str, months: int) -> list[dict[str, Any]]:
        return _rows(10)

    async def boom(org: str, tracker_ns: str, months: int) -> list[dict[str, Any]]:
        raise RuntimeError("qa down")

    assert (await risk_fit.refit_namespace(fresh, ORG, NS, outcomes=few)).status == "no_rows"
    assert (await risk_fit.refit_namespace(fresh, ORG, NS, outcomes=boom)).status == "failed"
    assert fresh.writes == []


@pytest.mark.asyncio
async def test_graph_stats_pass_counts_the_leg_on_its_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.workers import graph_stats as cgs

    class _Cfg:
        def __init__(self, ns: str) -> None:
            self.org, self.namespace, self.label = ORG, ns, f"{ORG}/{ns}"

    calls: list[str] = []

    async def fake_refit(repo: Any, org: str, ns: str, **kw: Any) -> risk_fit.RefitOutcome:
        calls.append(ns)
        if ns == "code-a":
            return risk_fit.RefitOutcome(
                ns, "written", n=1200, positives=50, holdout_auc=0.71, detail="d"
            )
        if ns == "code-b":
            return risk_fit.RefitOutcome(ns, "failed", detail="RuntimeError: x")
        return risk_fit.RefitOutcome(ns, "not_bound")

    import server.code.store.index as cr

    monkeypatch.setattr(cr, "CodeRepo", lambda: object())
    monkeypatch.setattr(risk_fit, "refit_namespace", fake_refit)
    result = cgs.CodeGraphStatsResult()
    await cgs._refit_jit_risk([_Cfg("code-a"), _Cfg("code-b"), _Cfg("memory-c")], result)
    assert calls == ["code-a", "code-b", "memory-c"]
    assert result.jit_risk_fits == 1 and result.jit_risk_fit_n == 1200
    assert result.jit_risk_holdout_auc == pytest.approx(0.71) and result.jit_risk_skipped == 1
    assert result.errors == [f"{ORG}/code-b: jit_risk: RuntimeError: x"]


def test_the_input_accepts_the_section_name() -> None:
    from server.code.api.models_review import CodeReviewBriefInput

    assert CodeReviewBriefInput(pr_number=1, sections=["risk"]).sections == ["risk"]
    with pytest.raises(ValidationError):
        CodeReviewBriefInput(pr_number=1, sections=["danger"])
