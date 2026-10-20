"""tracker.duplicate_check: the pure half (features of a fused
candidate, the logistic confidence, the three-way verdict, the fit with its
time-split holdout and the threshold rule), the handler on a stub source
(the draft through tracker.similar's legs, calibrated and uncalibrated answers, empty
on `none`, the wall, `tracker_unavailable`), the stored fit and the weekly leg,
the source statements, the op registration, the MCP text and the SDK method."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta
from typing import Any

import pytest

import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.core.errors import ValidationError
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from server.tracker.api.models import TrackerDuplicateCheckInput
from server.tracker.app import dupcheck as hd
from server.tracker.app import dupcheck_answer as hd_answer
from server.tracker.app import dupcheck_candidates as hd_candidates
from server.tracker.app import dupcheck_fit as df
from server.tracker.app import similar as tsim
from server.tracker.app import source as tsrc
from server.tracker.domain import dupcheck as dc
from server.tracker.domain.errors import TrackerUnavailable
from server.tracker.store.source.rows import similar_row
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG, NS, REPO = "clickhouse", "tracker-clickhouse", "ClickHouse/ClickHouse"
T0 = datetime(2026, 8, 31, 14, 38, 18)


async def _no_tracker_service(credential: str) -> Any:
    """A leg the test did not fake fails the way an unreachable tracker does,
    without resolving the configured host."""
    raise qi.QaIntelUnavailable(f"no tracker service in unit tests ({credential})")


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    qi.reset_for_tests()
    src.reset_cache_for_tests()
    hd.reset_fit_cache_for_tests()
    monkeypatch.setattr(settings, "tracker_org", ORG, raising=False)
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json(f'{{"{REPO}": "{NS}"}}'),
        raising=False,
    )
    monkeypatch.setattr(
        settings, "tracker_credentials_json", tracker_credentials_json(), raising=False
    )
    monkeypatch.setattr(settings, "tracker_live_serve", True, raising=False)
    monkeypatch.setattr(settings, "tracker_similar_vector_leg", False, raising=False)
    monkeypatch.setattr(settings, "tracker_dupcheck_duplicate_threshold", 0.9, raising=False)
    monkeypatch.setattr(settings, "tracker_dupcheck_related_threshold", 0.5, raising=False)
    monkeypatch.setattr(qi, "_build_client", _no_tracker_service)
    yield
    qi.reset_for_tests()
    src.reset_cache_for_tests()
    hd.reset_fit_cache_for_tests()


# ---------------------------------------------------------------- pure half


def _row(
    number: int,
    score: float,
    why: list[str],
    *,
    bm25: float = 0.0,
    days: int = 30,
    version: str = "",
) -> dict[str, Any]:
    return {
        "kind": "issue",
        "repo": REPO,
        "number": number,
        "source_id": f"{REPO}#{number}",
        "title": f"issue {number}",
        "state": "open",
        "state_reason": "",
        "primary_component": "",
        "affected_versions": [],
        "error_code": 0,
        "version_affected": version,
        "created_at": T0 - timedelta(days=days),
        "url": f"u/{number}",
        "bm25": bm25,
        "score": score,
        "why": why,
    }


def test_versions_match_on_the_same_or_adjacent_minor() -> None:
    assert dc.minor_version("ClickHouse 25.8.3 crashes") == "25.8"
    assert dc.minor_version("no version") == ""
    assert dc.version_match("25.8", "25.8.1") == 1.0
    assert dc.version_match("25.12", "26.1") == 0.5 and dc.version_match("25.8", "25.9") == 0.5
    assert dc.version_match("25.8", "26.8") == 0.0 and dc.version_match("", "25.8") == 0.0


def test_features_read_the_fused_row_and_the_wall() -> None:
    ranked = [
        _row(1, 2 / 61, ["lexical", "mentions", "component"], bm25=10.0, days=10, version="25.8"),
        _row(2, 1 / 61, ["lexical"], bm25=5.0, days=400),
    ]
    feats = dc.feature_rows(ranked, wall=T0, query_version="25.8")
    top = feats[0]
    assert top["legs"] == pytest.approx(2.0) and top["inv_rank"] == 1.0
    assert top["gap"] == pytest.approx(0.5) and top["bm25_share"] == 1.0
    assert (top["lexical"], top["frames"], top["mentions"], top["component"]) == (1, 0, 1, 1)
    assert top["version"] == 1.0 and top["error_code"] == 0.0 and top["fix_path"] == 0.0
    assert top["log_age"] == pytest.approx(2.3979, abs=1e-3)
    assert dc.feature_rows([{**ranked[0], "fix_path": 0.5}], wall=T0)[0]["fix_path"] == 0.5
    assert feats[1]["inv_rank"] == 0.5 and feats[1]["gap"] == 1.0 and feats[1]["bm25_share"] == 0.5
    # the design row is the intercept plus every named feature, in FEATURE_NAMES order
    assert dc.design(top)[0] == 1.0 and len(dc.design(top)) == len(dc.FEATURE_NAMES)


def test_verdict_and_relation_follow_the_two_thresholds() -> None:
    th = {"duplicate": 0.9, "related": 0.5}
    assert dc.verdict_of([0.95, 0.2], **th) == "duplicate_of"
    assert dc.verdict_of([0.2, 0.95], **th) == "duplicate_of"  # any candidate: the op sorts
    assert dc.verdict_of([0.6, 0.55], **th) == "related"
    assert dc.verdict_of([0.4, 0.7], **th) == "related"  # any candidate above related
    assert dc.verdict_of([0.1, 0.2], **th) == "none"
    assert dc.verdict_of([None, None], **th) == "none"
    assert dc.relation_of(0.95, **th) == "duplicate" and dc.relation_of(0.6, **th) == "related"
    assert dc.relation_of(0.1, **th) == "none" and dc.relation_of(None, **th) == "related"


def _gold(n_pos: int = 40, n_neg: int = 120, seed: int = 3) -> list[dict[str, Any]]:
    """A synthetic gold: positives carry a target with more legs, a larger
    gap and a mention; negatives are single-leg rows with no mention."""
    import random

    rnd = random.Random(seed)
    out: list[dict[str, Any]] = []
    for i in range(n_pos + n_neg):
        pos = i < n_pos
        items = []
        for rank in range(1, 6):
            target = pos and rank == 1
            f = {
                "legs": (2.0 if target else 1.0) + rnd.random() * 0.2,
                "inv_rank": 1 / rank,
                "gap": (0.5 if target else 0.05) + rnd.random() * 0.1,
                "bm25_share": 1.0 if rank == 1 else rnd.random() * 0.8,
                "lexical": 1.0,
                "frames": 0.0,
                "mentions": 1.0 if target and rnd.random() < 0.7 else 0.0,
                "component": float(rnd.random() < 0.3),
                "version": 0.0,
                "error_code": 0.0,
                "fix_path": 0.0,
                "log_age": rnd.random() * 6,
            }
            items.append({"number": 1000 + i * 10 + rank, "features": f, "target": target})
        out.append(
            {
                "number": i,
                "label": int(pos),
                "created_at": T0 - timedelta(days=(i * 37) % 160),
                "items": items,
            }
        )
    return out


def test_fit_separates_the_synthetic_gold_and_chooses_thresholds_by_precision() -> None:
    queries = _gold()
    fit = dc.fit_model(queries, now=T0)
    assert fit["v"] == dc.FIT_VERSION and fit["queries"] == {
        "n": 160,
        "positives": 40,
        "negatives": 120,
    }
    assert fit["items"] == {"n": 800, "targets": 40}
    assert set(fit["coef"]) == set(dc.FEATURE_NAMES)
    # the target's marks — more legs, a wider gap, a mention — carry positive weight
    assert fit["coef"]["legs"] > 0 and fit["coef"]["gap"] > 0 and fit["coef"]["mentions"] > 0
    assert fit["auc"] > 0.95
    dup = fit["suggested"]["duplicate"]
    assert dup is not None and 0 < dup < 1
    at = next(p for p in fit["curve"] if p["threshold"] == dup)
    assert at["precision"] >= 0.9 and at["recall"] > 0.5
    # every point above the chosen threshold keeps the floor (the rule)
    assert all(
        p["precision"] is None or p["precision"] >= 0.9
        for p in fit["curve"]
        if p["threshold"] >= dup
    )
    assert fit["suggested"]["related"] <= dup
    hold = fit["holdout"]
    assert hold["method"] == "5-fold out-of-fold" and hold["n"] == 160 and hold["auc"] > 0.9
    assert hold["at_duplicate"]["precision"] >= 0.9 and len(hold["curve"]) == 49
    assert sum(b["n"] for b in hold["bands"]) == 160 and len(fit["bands"]) == 8
    assert len(fit["deciles"]) == 10 and dc.fit_age_days(fit, T0 + timedelta(days=8)) == 8.0
    # the items keep the whole-gold confidence after the out-of-fold pass
    assert all("confidence" in it for q in queries for it in q["items"])


def test_evidence_features_are_never_fitted_as_penalties() -> None:
    queries = _gold(n_pos=30, n_neg=60)
    # frames mark the NON-targets here: unconstrained, the weight would go negative
    for q in queries:
        for it in q["items"]:
            it["features"]["frames"] = 0.0 if it["target"] else 1.0
    raw = dc.fit_coefficients(queries, nonnegative=())
    assert raw["frames"] < 0
    pinned = dc.fit_coefficients(queries)
    assert pinned["frames"] == 0.0 and pinned["legs"] > 0
    assert all(pinned[name] >= 0 for name in dc.NONNEGATIVE)


def test_choose_threshold_takes_the_lowest_point_whose_higher_points_all_hold() -> None:
    pts = [
        {"threshold": t, "precision": p}
        for t, p in ((0.2, 0.5), (0.4, 0.92), (0.6, 0.85), (0.8, 0.95), (0.9, None))
    ]
    # 0.6 dips below the floor, so 0.4 cannot be chosen past it; 0.8 is the answer
    assert dc.choose_threshold(pts, precision_floor=0.9) == 0.8
    assert dc.choose_threshold(pts, precision_floor=0.99) is None
    assert dc.choose_threshold([], precision_floor=0.5) is None


def test_curve_counts_negatives_as_false_alarms_and_reports_none_rates() -> None:
    queries = _gold(n_pos=10, n_neg=30)
    dc.score_queries(queries, {"coef": {"intercept": -3.0, "legs": 2.5, "mentions": 2.0}})
    pts = dc.curve(queries, steps=10)
    assert len(pts) == 9 and pts[0]["threshold"] == 0.1
    for p in pts:
        assert p["fired"] >= p["hits"]
        if p["precision"] is not None:
            assert 0 <= p["precision"] <= 1
    assert pts[-1]["none_rate_negatives"] >= pts[0]["none_rate_negatives"]


# ---------------------------------------------------------------- handler on a stub


class _Src:
    def __init__(self, *, down: set[str] | None = None):
        self.down = down or set()
        self.cfg = type("C", (), {"restricted": False})()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.gold: list[dict[str, Any]] = []
        self.negatives: list[dict[str, Any]] = []
        self.items: dict[int, dict[str, Any]] = {}

    def _maybe_down(self, name: str) -> None:
        if name in self.down:
            raise TrackerUnavailable(f"{name} OPEN")

    def repos(self):
        return [REPO]

    async def similar_item(self, org, ns, number):
        self.calls.append(("item", {"number": number}))
        return self.items.get(int(number))

    async def token_stats(self, org, ns):
        self._maybe_down("stats")
        return {
            "n": 20000,
            "avg_title_len": 10.0,
            "avg_body_len": 540.0,
            "df": {
                "avgweighted": 14,
                "float": 316,
                "integer": 633,
                "weights": 149,
                "incorrect": 900,
                "loaddataparts": 40,
                "executeimpl": 5000,
                "execute": 9000,
            },
        }

    async def similar_lexical(self, org, ns, *, kind, terms, idf, before, exclude, **kw):
        self.calls.append(("lexical", {"kind": kind, "terms": terms, "before": before}))
        self._maybe_down("lexical")
        return [
            {
                "kind": "issue",
                "repo": REPO,
                "number": 31768,
                "source_id": f"{REPO}#31768",
                "title": "avgWeighted return wrong result with Decimal value",
                "state": "closed",
                "state_reason": "completed",
                "primary_component": "AggregateFunctions",
                "affected_versions": [],
                "error_code": 0,
                "version_affected": "25.8",
                "created_at": T0 - timedelta(days=100),
                "url": "u/31768",
                "bm25": 9.9,
            },
            {
                "kind": "issue",
                "repo": REPO,
                "number": 80550,
                "source_id": f"{REPO}#80550",
                "title": "avgWeighted rounds float value when weight is int",
                "state": "open",
                "state_reason": "",
                "primary_component": "Other",
                "affected_versions": [],
                "error_code": 0,
                "version_affected": "",
                "created_at": T0 - timedelta(days=900),
                "url": "u/80550",
                "bm25": 3.0,
            },
        ]

    async def similar_frames(self, org, ns, *, kind, frame_tokens, paths, before, exclude, **kw):
        self.calls.append(("frames", {"frame_tokens": frame_tokens, "before": before}))
        return [
            {
                "kind": "issue",
                "repo": REPO,
                "number": 31768,
                "source_id": f"{REPO}#31768",
                "title": "avgWeighted return wrong result with Decimal value",
                "state": "closed",
                "state_reason": "completed",
                "primary_component": "AggregateFunctions",
                "affected_versions": [],
                "error_code": 0,
                "version_affected": "25.8",
                "created_at": T0 - timedelta(days=100),
                "url": "u/31768",
                "frame_hits": [True],
                "path_hits": [],
            }
        ]

    async def similar_mentions(self, org, ns, *, numbers, kinds, before, exclude="", **kw):
        self.calls.append(("mentions", {"numbers": numbers, "before": before}))
        self._maybe_down("mentions")
        return [
            {
                "kind": "issue",
                "repo": REPO,
                "number": n,
                "source_id": f"{REPO}#{n}",
                "title": f"named #{n}",
                "state": "open",
                "state_reason": "",
                "primary_component": "",
                "affected_versions": [],
                "error_code": 0,
                "version_affected": "",
                "created_at": T0 - timedelta(days=5),
                "url": f"u/{n}",
            }
            for n in numbers
            if n in (31768, 4242) or n in getattr(self, "known", ())
        ]

    edges: list[dict[str, Any]] = []
    fixes: list[int] = []
    fix_edges: list[dict[str, Any]] = []
    files: dict[int, list[str]] = {}

    async def duplicates_of(self, org, ns, *, number, limit=20):
        self.calls.append(("dups", {"number": number}))
        self._maybe_down("record")
        return list(self.edges)

    async def fix_prs_of_issue(self, org, ns, number):
        return [{"pr_number": n} for n in self.fixes]

    async def edges_from(self, org, ns, refs, rel_types=None, *, budget_ms=None):
        self.calls.append(("edges_from", {"refs": refs, "rels": rel_types}))
        self._maybe_down("edges")
        # the real source names our end `src_ref`, not `our_ref`
        return [e for e in self.fix_edges if e["src_ref"] in refs]

    async def prs_touching_paths(self, org, ns, paths, *, before=None, days=365, limit=20):
        self.calls.append(
            (
                "prs_touching_paths",
                {"paths": list(paths), "before": before, "days": days, "limit": limit},
            )
        )
        self._maybe_down("prs")
        return [p for p in getattr(self, "prs_touching", []) if set(p["hit_paths"]) & set(paths)]

    async def pr_files(self, org, ns, numbers, *, max_paths=40):
        self.calls.append(("pr_files", {"numbers": numbers}))
        return {n: self.files[n] for n in numbers if n in self.files}

    async def duplicate_gold(self, org, ns, *, limit=500):
        return list(self.gold)

    async def duplicate_negatives(self, org, ns, *, since, until, limit=400, seed=10000):
        self.calls.append(("negatives", {"since": since, "until": until, "limit": limit}))
        return list(self.negatives)


class _State:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], str] = {}
        self.details: list[str] = []

    async def get_state(self, org, ns, key):
        return self.rows.get((org, ns, key), "")

    async def set_state(self, org, ns, key, value, detail=""):
        self.rows[(org, ns, key)] = value
        self.details.append(detail)


FIT = {
    "v": 3,
    "fit_at": "2026-09-16 00:00:00",
    "tracker_namespace": NS,
    "queries": {"n": 500, "positives": 104, "negatives": 396},
    "auc": 0.97,
    "holdout": {"auc": 0.95},
    "coef": {
        "intercept": -6.0,
        "legs": 1.5,
        "inv_rank": 1.0,
        "gap": 1.0,
        "bm25_share": 1.0,
        "lexical": 0.5,
        "frames": 1.5,
        "mentions": 3.0,
        "component": 1.0,
        "version": 0.5,
        "error_code": 0.5,
        "fix_path": 2.0,
        "log_age": -0.1,
        "vector": 0.0,
        "agreement": 0.0,
        "closeness": 0.0,
        "vector_inv_rank": 0.0,
    },
}

DRAFT = {
    "title": "avgWeighted returns incorrect results for float values and integer weights",
    "body": "avgWeighted returns 0 for float values (see #31768) on 25.8.\n"
    "0. DB::AggregateFunctionAvgWeighted::loadDataParts() @ 0x1\n"
    "1. DB::Aggregator::executeImpl() @ 0x2\n2. DB::PipelineExecutor::execute() @ 0x3",
}


def _wire(monkeypatch, s: _Src, fit: dict[str, Any] | None = FIT) -> None:
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)

    async def _fit(org, ns):
        return fit

    monkeypatch.setattr(hd, "cached_fit", _fit)


@pytest.mark.asyncio
async def test_draft_runs_the_legs_once_and_answers_duplicate_with_items_first(monkeypatch) -> None:
    s = _Src()
    _wire(monkeypatch, s)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(
            org=ORG,
            namespace=NS,
            **DRAFT,
            component="AggregateFunctions",
            version="25.8.3",
        )
    )
    assert (
        out.verdict == "duplicate_of"
        and out.epistemic == "calibrated"
        and not out.tracker_unavailable
    )
    assert out.thresholds == {"duplicate": 0.9, "related": 0.5} and out.fit_at == FIT["fit_at"]
    assert out.fit["status"] == "served" and out.fit["holdout_auc"] == 0.95
    # one lexical read (issues only), the frames parsed from the body, the mention named
    assert [c[1]["kind"] for c in s.calls if c[0] == "lexical"] == ["issue"]
    assert next(c for c in s.calls if c[0] == "frames")[1]["frame_tokens"] == ["loadDataParts"]
    assert next(c for c in s.calls if c[0] == "mentions")[1]["numbers"] == [31768]
    assert out.legs["lexical"] == "served" and out.legs["frames"] == "served"
    top = out.items[0]
    assert top["number"] == 31768 and top["relation"] == "duplicate" and top["confidence"] >= 0.9
    # three legs, the component and the version (QA's version_affected vs the draft's 25.8)
    assert {"lexical", "frames", "mentions", "component"} <= set(top["why"])
    assert top["features"]["legs"] == pytest.approx(3.0, abs=0.01)
    assert top["features"]["version"] == 1.0 and top["features"]["component"] == 1.0
    assert "distance" not in top and top["url"] == "u/31768" and top["relation_recorded"] == "none"
    assert top["evidence"][0] == "found by: lexical, frames, mentions"
    assert "the draft names #31768" in top["evidence"] and "same version: 25.8" in top["evidence"]
    assert "same component: AggregateFunctions" in top["evidence"]
    assert "1/1 frames match" in top["evidence"] and not out.undecided
    # the second candidate is below the related threshold: not served
    assert [it["number"] for it in out.items] == [31768]
    assert out.query["component"] == "AggregateFunctions" and out.query["versions"] == ["25.8"]
    assert out.query["mentions"] == [31768] and out.query["before"] == ""
    assert out.query["record"] == "skipped" and out.legs.get("fix_path") is None


@pytest.mark.asyncio
async def test_none_is_empty_and_related_carries_items(monkeypatch) -> None:
    s = _Src()
    _wire(monkeypatch, s)
    base = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    f = base.items[0]["features"]
    logit = sum(FIT["coef"][k] * f[k] for k in dc.FEATURE_NAMES[1:])
    weak = {**FIT, "coef": {**FIT["coef"], "intercept": -logit - 3.0}}  # top ≈ 0.05
    _wire(monkeypatch, s, weak)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    assert out.verdict == "none" and out.items == [] and out.epistemic == "calibrated"
    mid = {**FIT, "coef": {**FIT["coef"], "intercept": -logit + 0.5}}  # top ≈ 0.62
    _wire(monkeypatch, s, mid)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT, component="AggregateFunctions")
    )
    assert out.verdict == "related" and out.items and out.items[0]["relation"] == "related"
    assert all(0.5 <= it["confidence"] < 0.9 for it in out.items)
    # gray band AND the same component: a human should look
    assert out.undecided
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    assert out.verdict == "related" and not out.undecided


@pytest.mark.asyncio
async def test_without_a_fit_the_answer_is_uncalibrated_related_with_null_confidence(
    monkeypatch,
) -> None:
    s = _Src()
    _wire(monkeypatch, s, None)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    assert out.verdict == "related" and out.epistemic == "uncalibrated"
    assert out.fit == {"status": "no_fit"} and out.fit_at == ""
    assert [it["number"] for it in out.items] == [31768, 80550]
    assert all(it["confidence"] is None and it["relation"] == "related" for it in out.items)


@pytest.mark.asyncio
async def test_as_of_walls_the_legs_and_mentions_fold_in(monkeypatch) -> None:
    s = _Src()
    _wire(monkeypatch, s)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(
            org=ORG,
            namespace=NS,
            title="avgWeighted incorrect float",
            as_of="2026-08-31T14:38:18Z",
            mentions=[4242],
        )
    )
    assert all(c[1]["before"] == T0 for c in s.calls if c[0] in ("lexical", "mentions"))
    assert out.query["before"] == "2026-08-31 14:38:18" and out.query["mentions"] == [4242]
    with pytest.raises(ValidationError, match="ISO 8601"):
        await hd.handle_tracker_duplicate_check(
            TrackerDuplicateCheckInput(org=ORG, namespace=NS, title="x y", as_of="yesterday")
        )


@pytest.mark.asyncio
async def test_empty_draft_is_refused_after_the_gate_and_an_outage_is_named(monkeypatch) -> None:
    # empty args construct (the leakage suite calls every op as a refused principal)
    assert TrackerDuplicateCheckInput(org=ORG, namespace=NS).title == ""
    _wire(monkeypatch, _Src())
    with pytest.raises(ValidationError, match="title, body or issue_number"):
        await hd.handle_tracker_duplicate_check(
            TrackerDuplicateCheckInput(org=ORG, namespace=NS, body="  ")
        )
    _wire(monkeypatch, _Src(down={"stats"}))
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    assert out.tracker_unavailable and out.verdict == "none" and out.items == []
    assert out.epistemic == "unavailable" and set(out.legs.values()) == {"tracker_unavailable"}
    _wire(monkeypatch, _Src(down={"lexical", "mentions"}))
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, title="avgWeighted incorrect float")
    )
    assert (
        out.tracker_unavailable and out.legs["lexical"] == "tracker_unavailable" and out.items == []
    )


@pytest.mark.asyncio
async def test_a_recorded_related_to_is_never_promoted_to_duplicate(monkeypatch) -> None:
    s = _Src()
    s.items[119601] = {
        "kind": "issue",
        "source_id": f"{REPO}#119601",
        **DRAFT,
        "component": "AggregateFunctions",
        "versions": [],
        "version": "25.8",
        "error_code": 0,
        "created_at": T0,
        "has_embedding": False,
    }
    s.edges = [{"source_id": f"{REPO}#31768", "rel_type": "related_to"}]
    _wire(monkeypatch, s)
    # the filed item's own text is the query; it is excluded and walled at its creation
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, issue_number=119601)
    )
    assert out.query["number"] == 119601 and out.query["known"] and out.query["record"] == "served"
    assert all(c[1]["before"] == T0 for c in s.calls if c[0] == "lexical")
    top = out.items[0]
    assert top["number"] == 31768 and top["confidence"] >= 0.9  # the score alone says duplicate
    assert top["relation"] == "related" and top["relation_recorded"] == "related_to"
    assert "recorded relation: related_to" in top["evidence"]
    assert out.verdict == "related" and not out.tracker_unavailable
    # a recorded duplicate_of is a duplicate whatever the score says
    s.edges = [{"source_id": f"{REPO}#31768", "rel_type": "duplicate_of"}]
    weak = {**FIT, "coef": {**FIT["coef"], "intercept": -12.0}}
    _wire(monkeypatch, s, weak)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, issue_number=119601)
    )
    assert out.verdict == "duplicate_of" and out.items[0]["relation_recorded"] == "duplicate_of"
    # the record unavailable: the verdict stands on the score, the flag says nobody read the graph
    s.down = {"record"}
    _wire(monkeypatch, s)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, issue_number=119601)
    )
    assert out.tracker_unavailable and out.query["record"] == "tracker_unavailable"
    assert out.items[0]["relation_recorded"] == "none"


@pytest.mark.asyncio
async def test_fix_path_reads_the_fix_prs_files_once_and_names_them(monkeypatch) -> None:
    s = _Src()
    s.fix_edges = [
        {
            "rel_type": "fixes",
            "direction": "in",
            "src_ref": f"{REPO}#31768",
            "dst_kind": "github_pr",
            "dst_ref": f"{REPO}#71300",
        }
    ]
    s.files = {71300: ["src/AggregateFunctions/AggregateFunctionAvgWeighted.h", "tests/x.sql"]}
    _wire(monkeypatch, s)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(
            org=ORG,
            namespace=NS,
            **DRAFT,
            paths=["AggregateFunctions/AggregateFunctionAvgWeighted.h", "src/Other/Nothing.cpp"],
            symbols=["DB::AggregateFunctionAvgWeighted::add"],
        )
    )
    ef = next(c for c in s.calls if c[0] == "edges_from")[1]
    assert ef["refs"] == [f"{REPO}#31768", f"{REPO}#80550"] and ef["rels"] == ["fixes"]
    assert next(c for c in s.calls if c[0] == "pr_files")[1]["numbers"] == [71300]
    top = out.items[0]
    assert top["features"]["fix_path"] == 0.5 and "fix_path" in top["why"]
    assert "fix #71300 touches AggregateFunctions/AggregateFunctionAvgWeighted.h" in top["evidence"]
    assert out.legs["fix_path"] == "served" and out.query["paths"][0].startswith(
        "AggregateFunctions/"
    )
    # symbols ride the frames leg
    assert (
        "AggregateFunctionAvgWeighted"
        in next(c for c in s.calls if c[0] == "frames")[1]["frame_tokens"]
    )
    # no paths on the draft: the reads are skipped
    s.calls.clear()
    await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, title="x y z")
    )
    assert not [c for c in s.calls if c[0] in ("edges_from", "pr_files")]
    # an outage on the edge read leaves the feature at 0 and is named
    s.down = {"edges"}
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT, paths=["a/b.cpp"])
    )
    assert out.legs["fix_path"] == "tracker_unavailable" and not out.tracker_unavailable
    assert dc.path_overlap(["Parsers/P.cpp", "x.h"], ["src/Parsers/P.cpp"]) == ["Parsers/P.cpp"]
    assert dc.path_overlap(["src/Parsers/P.cpp"], ["Parsers/P.cpp"]) == ["src/Parsers/P.cpp"]
    assert dc.cap_relation("duplicate", "related_to") == "related"
    assert dc.cap_relation("related", "duplicate_of") == "duplicate"
    assert dc.cap_relation("none", "duplicate_of") == "none"
    assert dc.cap_relation("duplicate", "fixed_by") == "duplicate"


@pytest.mark.asyncio
async def test_cached_fit_reads_the_state_row_once_per_ttl(monkeypatch) -> None:
    state = _State()
    state.rows[(ORG, NS, df.STATE_KEY)] = json.dumps(FIT)
    monkeypatch.setattr(df, "state_repo", lambda: state)
    assert (await hd.cached_fit(ORG, NS))["auc"] == 0.97
    state.rows.clear()
    assert (await hd.cached_fit(ORG, NS))["auc"] == 0.97  # cached
    hd.reset_fit_cache_for_tests()
    assert await hd.cached_fit(ORG, NS) is None
    assert df.parse_fit("not json") is None and df.parse_fit('{"x": 1}') is None


# ---------------------------------------------------------------- the stored fit and the leg


def _item(number: int, title: str, days: int) -> dict[str, Any]:
    return {
        "kind": "issue",
        "source_id": f"{REPO}#{number}",
        "title": title,
        "body": "avgWeighted returns 0 for float values (see #31768).",
        "component": "",
        "versions": [],
        "version": "25.8",
        "error_code": 0,
        "created_at": T0 - timedelta(days=days),
        "has_embedding": False,
    }


@pytest.mark.asyncio
async def test_refit_replays_gold_at_filing_time_fits_and_stores(monkeypatch) -> None:
    monkeypatch.setattr(df, "PACE_S", 0.0)
    monkeypatch.setattr(
        df, "degenerate", lambda fit: ""
    )  # eight synthetic queries: the plumbing, not the fit
    s = _Src()
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    monkeypatch.setattr(df, "MIN_POSITIVES", 2)
    s.gold = [
        {"number": 117352, "created_at": T0, "targets": [31768], "label": 1},
        {"number": 117000, "created_at": T0 - timedelta(days=3), "targets": [31768], "label": 1},
    ]
    s.negatives = [
        {"number": 90000 + i, "created_at": T0 - timedelta(days=i), "targets": [], "label": 0}
        for i in range(6)
    ]
    for g in [*s.gold, *s.negatives]:
        s.items[g["number"]] = _item(g["number"], f"avgWeighted incorrect float {g['number']}", 0)
    state = _State()
    out = await df.refit_namespace(s, state, ORG, NS, now=T0, negatives=6)
    assert out.status == "written" and out.n == 8 and out.positives == 2 and out.replays == 8
    fit = json.loads(state.rows[(ORG, NS, df.STATE_KEY)])
    assert fit["tracker_namespace"] == NS and fit["queries"] == {
        "n": 8,
        "positives": 2,
        "negatives": 6,
    }
    assert fit["gold"]["replayed"] == 8 and set(fit["coef"]) == set(dc.FEATURE_NAMES)
    assert out.detail and out.detail in state.details[0]
    # the negatives were asked for the positives' period; every replay was walled at creation
    neg = next(c for c in s.calls if c[0] == "negatives")[1]
    assert neg["since"] == T0 - timedelta(days=3) and neg["until"] == T0 and neg["limit"] == 6
    walls = [c[1]["before"] for c in s.calls if c[0] == "lexical"]
    assert walls[0] == T0 and len(walls) == 8
    # fresh: not due for a week; forced: refits; restricted: skipped; off: off
    again = await df.refit_namespace(s, state, ORG, NS, now=T0 + timedelta(days=1))
    assert again.status == "fresh" and again.n == 8
    assert (
        await df.refit_namespace(s, state, ORG, NS, now=T0 + timedelta(days=8))
    ).status == "written"
    s.cfg = type("C", (), {"restricted": True})()
    assert (await df.refit_namespace(s, state, ORG, NS, force=True)).status == "restricted"
    monkeypatch.setattr(settings, "tracker_dupcheck_refit_enabled", False, raising=False)
    assert (await df.refit_namespace(s, state, ORG, NS, force=True)).status == "off"


@pytest.mark.asyncio
async def test_refit_without_gold_or_with_a_failing_source_never_raises(monkeypatch) -> None:
    monkeypatch.setattr(df, "PACE_S", 0.0)
    monkeypatch.setattr(df, "degenerate", lambda fit: "")
    s = _Src()
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    state = _State()
    out = await df.refit_namespace(s, state, ORG, NS, now=T0)
    assert out.status == "no_gold" and not state.rows

    class _Bad(_Src):
        async def duplicate_gold(self, org, ns, *, limit=500):
            raise RuntimeError("boom")

    out = await df.refit_namespace(_Bad(), state, ORG, NS, now=T0)
    assert out.status == "failed" and "boom" in out.detail
    dry = _Src()
    monkeypatch.setattr(df, "MIN_POSITIVES", 1)
    dry.gold = [{"number": 117352, "created_at": T0, "targets": [31768], "label": 1}]
    dry.items[117352] = _item(117352, "avgWeighted incorrect float", 0)
    out = await df.refit_namespace(dry, state, ORG, NS, now=T0, write=False, negatives=0)
    assert out.status == "dry_run" and out.fit and not state.rows
    assert out.fit["gold"] == {
        "replayed": 1,
        "with_candidates": 1,
        "legs_unavailable": 0,
        "negatives_requested": 400,
    }
    # a replay under an outage is a fit of the outage: nothing is stored
    down = _Src(down={"lexical", "mentions"})
    down.gold = list(dry.gold)
    down.items = dict(dry.items)
    out = await df.refit_namespace(down, state, ORG, NS, now=T0, negatives=0)
    assert out.status == "degraded" and "1 of 1 replays" in out.detail and not state.rows


@pytest.mark.asyncio
async def test_tracker_links_pass_carries_the_weekly_leg_and_its_scalars(monkeypatch) -> None:
    from server.tracker.workers import links as wk

    result = wk.TrackerLinksResult()
    calls: list[str] = []

    async def _refit(src, repo, org, ns, *, now=None, force=False, **kw):
        calls.append(ns)
        return df.RefitOutcome(ns, "written", n=500, positives=104, auc=0.97, replays=500)

    monkeypatch.setattr(df, "refit_namespace", _refit)
    await wk.refit_dupcheck(_Src(), ORG, NS, result=result, now=T0, state_repo=_State())
    assert calls == [NS] and result.dupcheck_fits == 1 and result.dupcheck_fit_n == 500
    assert result.dupcheck_fit_auc == 0.97 and result.statements == 500
    out = result.outcome()
    assert (
        out["dupcheck_fits"] == 1
        and out["dupcheck_fit_auc"] == 0.97
        and out["dupcheck_skipped"] == 0
    )

    async def _skip(src, repo, org, ns, *, now=None, force=False, **kw):
        return df.RefitOutcome(ns, "failed", detail="RuntimeError: x")

    monkeypatch.setattr(df, "refit_namespace", _skip)
    await wk.refit_dupcheck(_Src(), ORG, NS, result=result, now=T0, state_repo=_State())
    assert result.dupcheck_skipped == 1 and result.errors == [f"{NS}: dupcheck_fit RuntimeError: x"]
    # a source without the gold readers (a fake, another QA product) is skipped, not an error
    await wk.refit_dupcheck(object(), ORG, NS, result=result, now=T0)
    assert result.dupcheck_skipped == 2 and len(result.errors) == 1


# ---------------------------------------------------------------- statements


class _Upstream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(
        self, sql, params=None, *, credential="", max_execution_s=None, query_settings=None
    ):
        self.calls.append((sql, dict(params or {})))
        return []


@pytest.mark.asyncio
async def test_gold_and_negative_statements_carry_the_predicate_the_period_and_the_edges(
    monkeypatch,
) -> None:
    up = _Upstream()
    monkeypatch.setattr(qi, "query_rows", up)
    monkeypatch.setattr(
        "server.tracker.domain.sources.credential_configured", lambda *_a, **_k: True
    )
    s = src.TrackerSource(ORG, NS)
    assert await s.duplicate_gold(ORG, NS, limit=50) == []
    sql, params = up.calls[-1]
    assert "is_private = 0 AND state_reason = 'duplicate'" in sql
    assert "rel_type = 'related_to'" in sql and "t.created_at < d.created_at" in sql
    assert params == {"repos": [REPO], "limit": 50}
    since, until = T0 - timedelta(days=700), T0
    assert await s.duplicate_negatives(ORG, NS, since=since, until=until, limit=7, seed=5) == []
    sql, params = up.calls[-1]
    assert "state_reason IN ('completed', 'not planned')" in sql
    assert sql.count("NOT IN (SELECT") == 2 and "rel_type IN ('related_to', 'duplicate_of')" in sql
    assert "cityHash64(source_id, {seed:UInt64})" in sql and "is_private = 0" in sql
    assert params == {"repos": [REPO], "since": since, "until": until, "limit": 7, "seed": 5}
    # Every candidate reader carries QA's version_affected
    await s.similar_lexical(
        ORG,
        NS,
        kind="issue",
        terms=["avgweighted"],
        idf=[3.0],
        avg_title_len=10.0,
        avg_body_len=500.0,
        before=None,
    )
    assert "version_affected" in up.calls[-1][0]
    await s.similar_mentions(ORG, NS, numbers=[1], kinds=["issue", "pr"], before=None)
    assert up.calls[-1][0].count("version_affected") == 2
    await s.similar_item(ORG, NS, 1)
    assert all("version_affected" in c[0] for c in up.calls[-2:])
    assert (
        similar_row({"source_id": f"{REPO}#5", "version_affected": "25.8"})["version_affected"]
        == "25.8"
    )


# ---------------------------------------------------------------- registration, MCP, SDK


def test_op_is_an_experimental_viewer_read_with_defaults_and_a_description() -> None:
    op = OPERATIONS_BY_NAME["tracker.duplicate_check"]
    assert op.required_role == Role.VIEWER and op.maturity == "experimental" and op.idempotent
    assert op.budget_p99_ms == 2000 and not op.skip_namespace_authz
    # no "one of" validator on the input (the gate-first rule): empty args construct, the handler refuses
    inp = TrackerDuplicateCheckInput(org=ORG, namespace=NS)
    assert inp.title == "" and inp.body == "" and inp.top_k == 10 and inp.as_of == ""
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    text = TOOL_DESCRIPTIONS["tracker.duplicate_check"]
    assert "duplicate_of" in text and "NO items" in text and "uncalibrated" in text
    assert "relation_recorded" in text and "undecided" in text and "fix_path" in text
    assert len(text) // 4 <= 480
    from server.operations.reach_manifest import STATIC_REACH

    assert "sdk_method" in STATIC_REACH["tracker.duplicate_check"]
    for name in (
        "tracker_dupcheck_duplicate_threshold",
        "tracker_dupcheck_related_threshold",
        "tracker_dupcheck_refit_enabled",
        "tracker_dupcheck_refit_interval_s",
        "tracker_dupcheck_fit_negatives",
    ):
        assert hasattr(settings, name)


@pytest.mark.asyncio
async def test_sdk_posts_only_the_fields_it_was_given() -> None:
    from loom.tracker import TrackerClient

    class _Transport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def post(self, op, body):
            self.calls.append((op, body))
            return {"verdict": "none"}

    class _Agent:
        def __init__(self) -> None:
            self.transport = _Transport()

    ag = _Agent()
    c = TrackerClient(ag)  # type: ignore[arg-type]
    await c.duplicate_check(
        namespace=NS,
        title="t",
        body="b",
        frames=["DB::A::b()"],
        component="MergeTree",
        version="25.8",
        mentions=[1, 2],
        top_k=5,
        paths=["src/a.cpp"],
        symbols=["DB::A::b"],
        issue_number=7,
        as_of="2026-01-01T00:00:00",
    )
    op, body = ag.transport.calls[0]
    assert op == "/v1/tracker.duplicate_check"
    assert body == {
        "namespace": NS,
        "top_k": 5,
        "title": "t",
        "body": "b",
        "frames": ["DB::A::b()"],
        "component": "MergeTree",
        "version": "25.8",
        "mentions": [1, 2],
        "paths": ["src/a.cpp"],
        "symbols": ["DB::A::b"],
        "issue_number": 7,
        "as_of": "2026-01-01T00:00:00",
    }
    await c.duplicate_check(namespace=NS, title="only a title")
    assert ag.transport.calls[1][1] == {"namespace": NS, "top_k": 10, "title": "only a title"}


def test_v2_features_carry_the_vector_legs_evidence() -> None:
    """`vector`, `agreement` and `closeness` read the fused row —
    a candidate both legs found at cosine distance 0.21 scores closeness
    0.58; one the vector leg never saw scores 0 on all three; a distance past
    the span floors at 0. The three are non-negative evidence features and
    the design row grows with them."""
    both = _row(1, 2 / 61, ["lexical", "vector"], bm25=10.0, days=10)
    both["distance"] = 0.21
    only_lex = _row(2, 1 / 61, ["lexical"], bm25=5.0, days=10)
    far = _row(3, 1 / 61, ["vector"], bm25=0.0, days=10)
    far["distance"] = 0.9
    f = dc.feature_rows([both, only_lex, far], wall=T0)
    assert (f[0]["vector"], f[0]["agreement"]) == (1.0, 1.0) and f[0]["closeness"] == pytest.approx(
        0.58
    )
    assert (f[1]["vector"], f[1]["agreement"], f[1]["closeness"]) == (0.0, 0.0, 0.0)
    assert (f[2]["vector"], f[2]["agreement"], f[2]["closeness"]) == (1.0, 0.0, 0.0)
    assert dc.closeness(None) == 0.0 and dc.closeness("x") == 0.0 and dc.closeness(0.0) == 1.0
    assert {"vector", "agreement", "closeness"} <= set(dc.NONNEGATIVE)
    assert dc.FEATURE_NAMES[-4:-1] == ("vector", "agreement", "closeness") and dc.FIT_VERSION == 3
    assert len(dc.design(f[0])) == len(dc.FEATURE_NAMES)
    lines = dc.evidence_lines(both, f[0], frames_total=0, component="")
    assert "found by: lexical, vector" in lines
    assert "vector: cosine distance 0.21 (agrees with the lexical leg)" in lines
    assert not any(
        line.startswith("vector:")
        for line in dc.evidence_lines(only_lex, f[1], frames_total=0, component="")
    )


def test_v2_fit_lives_under_its_own_state_key() -> None:
    """A server on the v1 feature set never reads v2 coefficients (it would
    score the old features with the new weights and the new ones with 0)."""
    from server.tracker.app import dupcheck_fit as df

    assert (
        df.STATE_KEY == f"qa_dupcheck_fit_v{dc.FIT_VERSION}" and df.STATE_KEY != "qa_dupcheck_fit"
    )


def test_a_fit_of_another_version_is_never_scored() -> None:
    from server.tracker.app import dupcheck_fit as df

    good = json.dumps({"v": dc.FIT_VERSION, "coef": {"intercept": -1.0}})
    assert df.parse_fit(good) is not None
    assert df.parse_fit(json.dumps({"v": dc.FIT_VERSION - 1, "coef": {"intercept": -1.0}})) is None
    # the previous version parses only when asked for by version (the read fallback)
    assert (
        df.parse_fit(
            json.dumps({"v": dc.FIT_VERSION - 1, "coef": {"intercept": -1.0}}),
            version=dc.FIT_VERSION - 1,
        )
        is not None
    )
    assert df.parse_fit(json.dumps({"coef": {"intercept": -1.0}})) is None  # unversioned = not ours
    assert df.parse_fit(json.dumps({"v": dc.FIT_VERSION})) is None and df.parse_fit("") is None


def test_v3_features_read_the_candidates_rank_within_each_leg() -> None:
    """The fusion stamps `leg_ranks` (the candidate's rank within
    every leg that found it); the fit reads 1/rank for the vector and lexical
    legs, 0 when a leg did not find it — a target only the vector leg found,
    appended after every lexical candidate, keeps its vector rank 1."""
    from server.tracker.domain import similar as sim

    def row(n, **k):
        return {"source_id": f"r#{n}", "number": n, **k}

    fused = sim.fuse(
        {
            "lexical": [row(1), row(2), row(3)],
            "vector": [row(9, distance=0.21), row(2, distance=0.3)],
        }
    )
    by = {r["number"]: r for r in fused}
    assert by[1]["leg_ranks"] == {"lexical": 1}
    assert by[2]["leg_ranks"] == {"lexical": 2, "vector": 2}
    assert by[9]["leg_ranks"] == {"vector": 1} and [r["number"] for r in fused][-1] == 9
    feats = dc.feature_rows(fused, wall=T0)
    f = {r["number"]: x for r, x in zip(fused, feats, strict=True)}
    assert f[9]["vector_inv_rank"] == 1.0
    assert f[9]["inv_rank"] == 0.25  # fused rank 4: what v2 saw
    assert f[2]["vector_inv_rank"] == 0.5 and f[1]["vector_inv_rank"] == 0.0
    assert (
        dc.leg_inv_rank({}, "vector") == 0.0
        and dc.leg_inv_rank({"leg_ranks": {"vector": "x"}}, "vector") == 0.0
    )
    assert dc.leg_inv_rank(fused[1], "lexical") == 0.5  # the stamp is there for any leg
    assert dc.FEATURE_NAMES[-1] == "vector_inv_rank"
    assert "vector_inv_rank" in dc.NONNEGATIVE and dc.FIT_VERSION == 3


def test_the_verdict_scores_deeper_than_the_answer() -> None:
    """The fit scores the fused top SCORE_DEPTH whatever top_k the
    caller asked; the answer still carries top_k items."""

    assert hd_candidates.SCORE_DEPTH == 25
    src = inspect.getsource(hd_candidates.candidates)
    assert "fused[: max(int(input.top_k), SCORE_DEPTH)]" in src


def test_a_degenerate_fit_is_never_stored() -> None:
    """A refit whose out-of-fold AUC collapsed or whose
    coefficients ran away (a collinear pair can drive the intercept to −1e22)
    answers `degenerate` and leaves the serving fit alone."""
    from server.tracker.app import dupcheck_fit as df

    good = {"holdout": {"auc": 0.97}, "coef": {"intercept": -3.0, "mentions": 6.7}}
    assert df.degenerate(good) == ""
    assert "auc" in df.degenerate({"holdout": {"auc": 0.5}, "coef": {"intercept": -3.0}})
    assert "auc" in df.degenerate({"coef": {"intercept": -3.0}})
    assert "magnitude" in df.degenerate({"holdout": {"auc": 0.99}, "coef": {"intercept": -1.9e22}})

    class _Repo:
        def __init__(self) -> None:
            self.writes: list[Any] = []

        async def set_state(self, org, ns, key, value, detail=""):
            self.writes.append((key, value))

    repo = _Repo()
    bad = {**good, "holdout": {"auc": 0.5}, "queries": {"n": 505, "positives": 105}}
    out = asyncio.run(df._store(repo, ORG, NS, bad, write=True))
    assert out.status == "degenerate" and repo.writes == [] and "auc" in out.detail
    ok = asyncio.run(
        df._store(repo, ORG, NS, {**good, "queries": {"n": 505, "positives": 105}}, write=True)
    )
    assert ok.status == "written" and repo.writes and repo.writes[0][0] == df.STATE_KEY


@pytest.mark.asyncio
async def test_a_newer_server_serves_the_previous_versions_fit_until_its_own_passes() -> None:
    """Features only grow, so a v(N−1) fit applies under vN
    features (new coefficients 0) — the op stays calibrated across a deploy;
    the current version's row wins when it exists; two versions back is
    never read."""
    from server.tracker.app import dupcheck_fit as df

    class _State:
        def __init__(self, rows):
            self.rows = rows
            self.reads: list[str] = []

        async def get_state(self, org, ns, key):
            self.reads.append(key)
            return self.rows.get(key, "")

    cur, prev = dc.FIT_VERSION, dc.FIT_VERSION - 1
    only_prev = _State(
        {df.state_key_for(prev): json.dumps({"v": prev, "coef": {"intercept": -2.0}})}
    )
    fit = await df.stored_fit(only_prev, ORG, NS)
    assert fit is not None and fit["v"] == prev
    assert only_prev.reads == [df.state_key_for(cur), df.state_key_for(prev)]
    both = _State(
        {
            df.state_key_for(prev): json.dumps({"v": prev, "coef": {"intercept": -2.0}}),
            df.state_key_for(cur): json.dumps({"v": cur, "coef": {"intercept": -3.0}}),
        }
    )
    fit = await df.stored_fit(both, ORG, NS)
    assert fit is not None and fit["v"] == cur and both.reads == [df.state_key_for(cur)]
    too_old = _State(
        {df.state_key_for(prev - 1): json.dumps({"v": prev - 1, "coef": {"intercept": -2.0}})}
    )
    assert await df.stored_fit(too_old, ORG, NS) is None
    assert df.READ_VERSIONS == (cur, prev) and df.STATE_KEY == df.state_key_for(cur)


@pytest.mark.asyncio
async def test_the_judge_reads_under_the_related_line_and_serves_what_it_confirms(
    monkeypatch,
) -> None:
    """A gold target at fused rank 2 with confidence 0.046 sits under the
    related line; the judge reads the top scored candidates whatever their
    confidence, and one it labels `duplicate` is served as the duplicate."""
    # The rerank pool reads candidate text too; this test counts the judge's read
    from server.settings import settings

    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_enabled", False)

    s = _Src()
    _wire(monkeypatch, s)
    base = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    f = base.items[0]["features"]
    logit = sum(FIT["coef"][k] * f[k] for k in dc.FEATURE_NAMES[1:])
    mid = {
        **FIT,
        "coef": {**FIT["coef"], "intercept": -logit + 0.5},
    }  # top ≈ 0.62, #80550 under 0.5
    _wire(monkeypatch, s, mid)
    asked: list[list[int]] = []

    async def _text(org, ns, *, kind, numbers, body_chars, stamp=""):
        asked.append(list(numbers))
        return [{"number": n, "title": f"t{n}", "body": f"b{n}"} for n in numbers]

    monkeypatch.setattr(s, "embed_items_by_number", _text, raising=False)

    class _LLM:
        model = "fake"

        async def complete(self, system, user, max_tokens=1024, response_format=None):
            from types import SimpleNamespace

            return SimpleNamespace(
                text='{"verdicts": [{"number": 31768, "label": "related"}, '
                '{"number": 80550, "label": "duplicate", "reason": "same rounding defect"}]}',
                model="fake",
                tokens_in=10,
                tokens_out=5,
            )

    monkeypatch.setattr("server.llm.client.get_llm_client_for_model", lambda model: _LLM())
    monkeypatch.setattr(settings, "tracker_dupcheck_judge_enabled", True, raising=False)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    assert asked == [[31768, 80550]]  # the served item, then the one under the line
    assert out.verdict == "duplicate_of" and not out.undecided
    assert [it["number"] for it in out.items] == [80550, 31768]
    top = out.items[0]
    assert top["relation"] == "duplicate" and top["confidence"] < 0.5
    assert top["evidence"][-1] == "judge: same defect — same rounding defect"
    assert out.judge["promoted"] == [80550] and out.judge["inserted"] == [80550]
    assert out.items[1]["relation"] == "related" and out.items[1]["judge"]["label"] == "related"


def test_the_duplicate_line_is_the_fits_chosen_one_clamped(monkeypatch) -> None:
    """The fit's `suggested.duplicate` (the lowest line holding
    the 0.9 precision floor out of fold) is served, clamped to
    [min, max]; the static line serves when the fit has none; the related line never rises above the duplicate line."""
    from server.settings import settings

    monkeypatch.setattr(settings, "tracker_dupcheck_duplicate_threshold", 0.25, raising=False)
    monkeypatch.setattr(settings, "tracker_dupcheck_related_threshold", 0.05, raising=False)
    monkeypatch.setattr(settings, "tracker_dupcheck_duplicate_threshold_min", 0.15, raising=False)
    monkeypatch.setattr(settings, "tracker_dupcheck_duplicate_threshold_max", 0.5, raising=False)
    assert hd_answer.thresholds() == {"duplicate": 0.25, "related": 0.05}
    assert hd_answer.thresholds({"coef": {}}) == {"duplicate": 0.25, "related": 0.05}
    assert hd_answer.thresholds({"suggested": {"duplicate": 0.32}}) == {
        "duplicate": 0.32,
        "related": 0.05,
    }
    assert hd_answer.thresholds({"suggested": {"duplicate": 0.18}}) == {
        "duplicate": 0.18,
        "related": 0.05,
    }
    assert hd_answer.thresholds({"suggested": {"duplicate": 0.08}}) == {
        "duplicate": 0.15,
        "related": 0.05,
    }
    assert hd_answer.thresholds({"suggested": {"duplicate": 0.9}}) == {
        "duplicate": 0.5,
        "related": 0.05,
    }
    assert hd_answer.thresholds({"suggested": {"duplicate": None}}) == {
        "duplicate": 0.25,
        "related": 0.05,
    }
    monkeypatch.setattr(settings, "tracker_dupcheck_related_threshold", 0.2, raising=False)
    assert hd_answer.thresholds({"suggested": {"duplicate": 0.1}}) == {
        "duplicate": 0.15,
        "related": 0.15,
    }


@pytest.mark.asyncio
async def test_the_answer_serves_the_fits_line(monkeypatch) -> None:

    s = _Src()
    _wire(monkeypatch, s, {**FIT, "suggested": {"duplicate": 0.95, "related": 0.04}})
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    assert out.thresholds["duplicate"] == 0.5  # 0.95 clamped to the max
    # the top candidate scores ≥ 0.9 in this fixture: a duplicate at 0.5, still a duplicate
    assert out.verdict == "duplicate_of" and out.items[0]["relation"] == "duplicate"


@pytest.mark.asyncio
async def test_the_fix_in_flight_leg_brings_the_issue_an_open_fix_pr_names(monkeypatch) -> None:
    """A draft filed at symptom level shares no words with its
    root-cause report, but the fix in flight touches the file the finding
    names. paths → PRs touching them → the issues their `fixes` edges name
    → candidates the judge can read; evidence names the PR and its state."""
    from server.settings import settings

    s = _Src()
    s.prs_touching = [
        {
            "pr_number": 119810,
            "source_id": f"{REPO}#119810",
            "state": "open",
            "hit_paths": ["src/DataTypes/DataTypeAggregateFunction.cpp"],
            "title": "Print quantile parameters with types",
        },
        {
            "pr_number": 110997,
            "source_id": f"{REPO}#110997",
            "state": "merged",
            "hit_paths": ["src/DataTypes/DataTypeAggregateFunction.cpp"],
            "title": "Fix data race",
        },
    ]
    s.fix_edges = [
        {
            "rel_type": "fixes",
            "direction": "out",
            "src_ref": f"{REPO}#119810",
            "dst_kind": "github_issue",
            "dst_ref": f"{REPO}#120550",
        },
        {
            "rel_type": "fixes",
            "direction": "out",
            "src_ref": f"{REPO}#110997",
            "dst_kind": "github_issue",
            "dst_ref": f"{REPO}#109001",
        },
    ]
    s.known = {120550, 109001}  # the record has them; the lexical fake never finds them
    _wire(monkeypatch, s)
    # the fixture fit weighs fix_path 2.0 but the two new rows share no words with the
    # draft: a low related line serves them so the test can read them
    monkeypatch.setattr(settings, "tracker_dupcheck_related_threshold", 0.001, raising=False)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(
            org=ORG,
            namespace=NS,
            **DRAFT,
            top_k=10,
            paths=["src/DataTypes/DataTypeAggregateFunction.cpp", "src/Other/Nothing.cpp"],
        )
    )
    call = next(c for c in s.calls if c[0] == "prs_touching_paths")[1]
    assert call["paths"][0] == "src/DataTypes/DataTypeAggregateFunction.cpp" and call["days"] == 365
    first_edges = next(c for c in s.calls if c[0] == "edges_from")[1]
    assert first_edges["refs"] == [f"{REPO}#110997", f"{REPO}#119810"] and first_edges["rels"] == [
        "fixes"
    ]
    by = {it["number"]: it for it in out.items}
    assert 120550 in by and 109001 in by, [it["number"] for it in out.items]
    got = by[120550]
    assert got["why"] == ["fix_path"] and got["features"]["fix_path"] == 0.5
    assert "found by: fix_path" in got["evidence"]
    assert (
        "fix #119810 (open) touches src/DataTypes/DataTypeAggregateFunction.cpp" in got["evidence"]
    )
    assert (
        "fix #110997 touches src/DataTypes/DataTypeAggregateFunction.cpp" in by[109001]["evidence"]
    )
    assert out.legs["fix_path"] == "served"
    assert out.query["legs_top"]["fix_path"] == [120550, 109001]  # the open PR's issue first
    assert out.query["legs_top"]["lexical"][0] == 31768
    # an outage on the PR read leaves the answer standing and says so
    s.down.add("prs")
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(
            org=ORG,
            namespace=NS,
            **DRAFT,
            top_k=10,
            paths=["src/DataTypes/DataTypeAggregateFunction.cpp"],
        )
    )
    assert out.legs["fix_path"] == "tracker_unavailable" and out.verdict == "duplicate_of"


@pytest.mark.asyncio
async def test_a_vector_only_candidate_is_scored_past_a_full_lexical_leg(monkeypatch) -> None:
    """Recall-only fusion appends a vector-only candidate after every ranked
    row — fifty when the lexical leg is full — so without a head the fit would
    never score it and the judge never see it (a target at vector rank 1 and
    lexical rank 46, say). The head carries each
    recall-only leg's top rows whatever their fused rank."""
    from server.settings import settings

    s = _Src()
    fifty = [
        {
            "kind": "issue",
            "repo": REPO,
            "number": 90000 + i,
            "source_id": f"{REPO}#{90000 + i}",
            "title": f"filler {i}",
            "state": "open",
            "state_reason": "",
            "primary_component": "",
            "affected_versions": [],
            "error_code": 0,
            "version_affected": "",
            "created_at": T0 - timedelta(days=9),
            "url": f"u/{90000 + i}",
            "score": 50 - i,
        }
        for i in range(50)
    ]

    async def many_lexical(org, ns, *, kind, terms, idf, before, exclude, **kw):
        return fifty

    async def vector_local(org, ns, *, vector, dim, kinds, before, exclude, limit, repo=None):
        return [
            {
                "kind": "issue",
                "repo": REPO,
                "number": 31768,
                "source_id": f"{REPO}#31768",
                "distance": 0.21,
                "created_at": T0 - timedelta(days=9),
            }
        ]

    async def upstream_embeddings_populated(org, ns):
        return False

    monkeypatch.setattr(s, "similar_lexical", many_lexical, raising=False)
    monkeypatch.setattr(s, "similar_vector_local", vector_local, raising=False)
    monkeypatch.setattr(
        s, "upstream_embeddings_populated", upstream_embeddings_populated, raising=False
    )
    s.known = {31768} | {90000 + i for i in range(50)}
    _wire(monkeypatch, s)
    monkeypatch.setattr(settings, "tracker_similar_vector_leg", True, raising=False)
    monkeypatch.setattr(settings, "tracker_dupcheck_related_threshold", 0.001, raising=False)
    monkeypatch.setattr(settings, "tracker_dupcheck_judge_enabled", False, raising=False)
    monkeypatch.setattr(hd, "_vector_query_for_tests", None, raising=False)

    async def fake_vector_query(repo, input, q, item):
        return [0.1] * 4, 4

    monkeypatch.setattr(tsim, "_vector_query", fake_vector_query)

    async def no_catch_up(*a, **k):
        from server.tracker.app.embed_index import CatchUp

        return CatchUp(status="skipped")

    monkeypatch.setattr(tsim, "_vector_catch_up", no_catch_up)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT, top_k=10)
    )
    numbers = {it["number"] for it in out.items}
    assert 31768 in numbers, [it["number"] for it in out.items][:12]
    got = next(it for it in out.items if it["number"] == 31768)
    assert "vector" in got["why"] and got["features"]["vector_inv_rank"] == 1.0
    assert out.query["legs_top"]["vector"] == [31768]


def _flaky_lexical(s: _Src, errors: list[BaseException]) -> dict[str, int]:
    """The lexical read raises each of `errors` in turn, then answers."""
    original = s.similar_lexical
    calls = {"n": 0}

    async def flaky(*a, **kw):
        calls["n"] += 1
        if errors:
            raise errors.pop(0)
        return await original(*a, **kw)

    s.similar_lexical = flaky  # type: ignore[method-assign]
    return calls


def _leg(out: Any, name: str) -> Any:
    return next(r for r in out.leg_status if r.name == name)


def _timeout() -> BaseException:
    try:
        raise TimeoutError
    except TimeoutError as t:
        e = TrackerUnavailable("similar_lexical_issue exceeded 900 ms")
        e.__cause__ = t
        return e


@pytest.mark.asyncio
async def test_a_transient_leg_failure_is_retried_once_and_the_leg_answers(monkeypatch) -> None:
    """The first lexical read times out; the retry answers: the leg is ok, retried, and the
    verdict is the one the full legs give."""
    s = _Src()
    calls = _flaky_lexical(s, [_timeout()])
    _wire(monkeypatch, s)
    monkeypatch.setattr(settings, "tracker_dupcheck_leg_retry_delay_ms", 0, raising=False)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    assert calls["n"] == 2
    assert out.legs["lexical"] == "served" and out.epistemic == "calibrated"
    lex = _leg(out, "lexical")
    assert (lex.status, lex.retried, lex.reason) == ("ok", True, "recovered_on_retry")
    assert lex.namespace == NS and "legs_retry" in out.query["timings_ms"]
    assert out.verdict == "duplicate_of" and out.items[0]["number"] == 31768
    # the legs that answered first time are not run again
    assert [c[0] for c in s.calls].count("frames") == 1
    assert (_leg(out, "frames").status, _leg(out, "frames").retried) == ("ok", False)


@pytest.mark.asyncio
async def test_two_transient_failures_leave_the_leg_degraded_with_its_reason(monkeypatch) -> None:
    """Both reads fail with a network error: one retry, then the leg is degraded and says
    why; the message is fixed text, never the upstream's (which may carry SQL)."""
    s = _Src()

    class _Net(Exception):
        code = 210

    def net() -> BaseException:
        e = TrackerUnavailable("similar_lexical_issue: SELECT secret FROM t")
        e.__cause__ = _Net("Code: 210. NETWORK_ERROR SELECT secret")
        return e

    calls = _flaky_lexical(s, [net(), net(), net()])
    _wire(monkeypatch, s)
    monkeypatch.setattr(settings, "tracker_dupcheck_leg_retry_delay_ms", 0, raising=False)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    assert calls["n"] == 2, "one retry, not more"
    lex = _leg(out, "lexical")
    assert (lex.status, lex.retried, lex.reason, lex.cause) == (
        "degraded",
        True,
        "ch_transient",
        "ch_transient",
    )
    assert "code 210" in lex.message and "SELECT" not in lex.message
    assert out.legs["lexical"] == "tracker_unavailable" and out.epistemic == "degraded"
    assert not out.tracker_unavailable  # the record itself was readable


@pytest.mark.asyncio
async def test_a_deterministic_failure_is_not_retried(monkeypatch) -> None:
    """A refused statement (unknown table) or an open breaker would fail again: one read,
    degraded, not retried."""
    s = _Src()
    shape = TrackerUnavailable("similar_lexical_issue: DatabaseError")
    shape.__cause__ = Exception("Code: 60. UNKNOWN_TABLE")
    calls = _flaky_lexical(s, [shape])
    _wire(monkeypatch, s)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    assert calls["n"] == 1
    lex = _leg(out, "lexical")
    assert (lex.status, lex.retried, lex.reason) == ("degraded", False, "shape")
    s2 = _Src()
    calls = _flaky_lexical(s2, [TrackerUnavailable("qa_intel[public] breaker is OPEN")])
    _wire(monkeypatch, s2)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    assert calls["n"] == 1 and _leg(out, "lexical").reason == "breaker_open"


@pytest.mark.asyncio
async def test_a_retry_that_would_pass_the_budget_is_skipped_and_says_so(monkeypatch) -> None:
    """With less of the op budget left than the retry needs, the leg stays degraded and the
    reason says the retry was skipped for the budget; the call does not wait."""
    from server.tracker.app import dupcheck_legs as dl

    s = _Src()
    calls = _flaky_lexical(s, [_timeout()])
    _wire(monkeypatch, s)
    monkeypatch.setattr(dl, "op_budget_ms", lambda: 250)
    monkeypatch.setattr(settings, "tracker_dupcheck_leg_retry_reserve_ms", 300, raising=False)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
    )
    assert calls["n"] == 1
    lex = _leg(out, "lexical")
    assert (lex.status, lex.retried, lex.reason, lex.cause) == (
        "degraded",
        False,
        "retry_skipped_budget",
        "timeout",
    )
    assert "time budget" in lex.message
    assert "legs_retry" not in out.query.get("timings_ms", {})


@pytest.mark.asyncio
async def test_a_retry_is_cut_at_the_budget(monkeypatch) -> None:
    """A retry that hangs is cut at the time left, and the leg is degraded as a timeout.

    The deadline arithmetic reads a frozen clock, so the retry's window is the
    budget minus the reserve (500 ms) however long the rest of the call takes
    on a loaded host; only the cut itself waits in real time."""
    from types import SimpleNamespace

    from server.tracker.app import dupcheck_legs as dl

    s = _Src()
    calls = {"n": 0}
    never = asyncio.Event()

    async def first_fails_then_hangs(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _timeout()
        await never.wait()

    s.similar_lexical = first_fails_then_hangs  # type: ignore[method-assign]
    _wire(monkeypatch, s)
    monkeypatch.setattr(dl, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    monkeypatch.setattr(dl, "op_budget_ms", lambda: 600)
    monkeypatch.setattr(settings, "tracker_dupcheck_leg_retry_delay_ms", 0)
    monkeypatch.setattr(settings, "tracker_dupcheck_leg_retry_reserve_ms", 100)
    monkeypatch.setattr(settings, "tracker_dupcheck_leg_retry_min_ms", 50)
    assert dl.retry_window(dl.deadline(1000.0), delay_ms=0, reserve_ms=100, min_ms=50) == (
        dl.RetryWindow(0.5, left_ms=500)
    )
    out = await asyncio.wait_for(
        hd.handle_tracker_duplicate_check(
            TrackerDuplicateCheckInput(org=ORG, namespace=NS, **DRAFT)
        ),
        timeout=30,  # a retry that is never cut would hang here, not pass
    )
    lex = _leg(out, "lexical")
    assert calls["n"] == 2
    assert (lex.status, lex.retried, lex.reason) == ("degraded", True, "timeout")


def test_classify_names_transient_and_deterministic_failures() -> None:
    from server.tracker.app import dupcheck_legs as dl

    def wrapped(cause: BaseException, text: str = "read failed") -> BaseException:
        e = TrackerUnavailable(text)
        e.__cause__ = cause
        return e

    class _Http(Exception):
        status_code = 503

    class _Ch(Exception):
        def __init__(self, code: int) -> None:
            super().__init__("Received ClickHouse exception")
            self.code = code

    cases = {
        "timeout": wrapped(TimeoutError()),
        "connection": wrapped(ConnectionResetError()),
        "upstream_5xx": wrapped(_Http()),
    }
    for want, e in cases.items():
        got = dl.classify(e)
        assert (got.reason, got.transient) == (want, True), want
    for code in (159, 209, 210, 1000):
        assert dl.classify(wrapped(_Ch(code))).transient, code
    assert dl.classify(wrapped(Exception("HTTP driver received HTTP status 502"))).reason == (
        "upstream_5xx"
    )
    for text, want in (
        ("tracker-x is not a configured tracker namespace", "unconfigured"),
        ("LOOM_TRACKER_LIVE_SERVE is off", "unconfigured"),
        ("qa_intel[public] upstream quota: Code: 201.", "quota"),
        ("something else", "unavailable"),
    ):
        got = dl.classify(TrackerUnavailable(text))
        assert (got.reason, got.transient) == (want, False), text


@pytest.mark.asyncio
async def test_a_restricted_namespace_borrows_the_standard_fit(monkeypatch) -> None:
    """A private tracker namespace has no fit (restricted namespaces are never
    refit), so with the judge off its answers would be uncalibrated. It serves the org's standard fit, marked borrowed; a standard namespace with
    no fit still answers uncalibrated; the flag turns it off."""
    from server.tracker.app import dupcheck_fit as df
    from server.tracker.domain import namespaces as qn

    class _NS:
        def __init__(self, org, namespace, restricted):
            self.org, self.namespace, self.restricted = org, namespace, restricted

    table = {
        "tracker-x": _NS(ORG, "tracker-x", False),
        "tracker-x-private": _NS(ORG, "tracker-x-private", True),
        "tracker-y": _NS(ORG, "tracker-y", False),
        "tracker-other": _NS("other", "tracker-other", False),
    }
    monkeypatch.setattr(qn, "configured_namespaces", lambda: dict(table))
    monkeypatch.setattr(
        qn,
        "tracker_namespace",
        lambda org, ns: table.get(ns) if table.get(ns) and table[ns].org == org else None,
    )
    reads: list[str] = []

    async def stored(repo, org, ns):
        reads.append(ns)
        return (
            {"fit_at": "2026-09-17 18:09:37", "coef": {"intercept": -4.0}, "tracker_namespace": ns}
            if ns == "tracker-x"
            else None
        )

    monkeypatch.setattr(df, "stored_fit", stored)
    monkeypatch.setattr(df, "state_repo", lambda: None)
    hd.reset_fit_cache_for_tests()
    fit = await hd.cached_fit(ORG, "tracker-x-private")
    assert (
        fit is not None and fit["borrowed_from"] == "tracker-x" and fit["coef"]["intercept"] == -4.0
    )
    assert reads == ["tracker-x-private", "tracker-x"]
    assert (
        hd_answer._fit_block(fit)["status"] == "borrowed"
        and hd_answer._fit_block(fit)["borrowed_from"] == "tracker-x"
    )
    # cached: the second read is served from the cache
    assert await hd.cached_fit(ORG, "tracker-x-private") is fit and reads == [
        "tracker-x-private",
        "tracker-x",
    ]
    # a standard namespace without a fit borrows nothing; another org's fit is never borrowed
    hd.reset_fit_cache_for_tests()
    assert await hd.cached_fit(ORG, "tracker-y") is None
    hd.reset_fit_cache_for_tests()
    assert await hd.cached_fit("other", "tracker-other") is None
    hd.reset_fit_cache_for_tests()


def test_an_empty_vector_index_is_not_calibrated_clearance() -> None:
    """With no vectors in the namespace every candidate's vector features read 0; under a fit
    that weighs them that only lowers confidence, so the answer is degraded, not calibrated."""
    weighs = {"coef": {"intercept": -3.0, "lexical": 1.0, "closeness": 2.5}}
    ignores = {"coef": {"intercept": -3.0, "lexical": 1.0, "closeness": 0.0}}
    legs = {"lexical": "served", "vector": "not_computed", "fix_path": "skipped"}
    feats = {"lexical": 1.0, "vector": 1.0, "closeness": 0.8}
    assert dc.confidence(weighs, {**feats, "vector": 0.0, "closeness": 0.0}) < dc.confidence(
        weighs, feats
    )
    assert hd_answer._epistemic(weighs, legs) == "degraded"
    assert hd_answer._epistemic(ignores, legs) == "calibrated"
    assert hd_answer._epistemic(weighs, {**legs, "vector": "empty"}) == "calibrated"
    assert hd_answer._epistemic(None, legs) == "uncalibrated"


def test_a_failed_frames_or_mentions_leg_degrades_the_answer() -> None:
    """The fit scores the frames and mentions legs as features: a stack-trace
    draft whose duplicate only the frames leg finds, or a draft citing its
    duplicate, answers `none` when that leg is down — not clearance."""
    fit = {"coef": {"lexical": 1.0}}
    assert hd_answer._epistemic(fit, {"lexical": "served"}) == "calibrated"
    for leg in ("frames", "mentions", "lexical", "vector", "fix_path"):
        assert hd_answer._epistemic(fit, {leg: "tracker_unavailable"}) == "degraded", leg
    assert hd_answer._epistemic(fit, {"frames": "skipped", "mentions": "empty"}) == "calibrated"


@pytest.mark.asyncio
async def test_a_failed_hydrate_marks_the_legs_whose_rows_it_dropped() -> None:
    """Vector-only rows carry no title; when the read that fills them fails
    they drop out, and the vector leg must not still say `served`."""
    from types import SimpleNamespace

    class _Down:
        async def similar_mentions(self, *a, **k):
            raise TrackerUnavailable("breaker open")

    rows = [
        {"number": 1, "kind": "issue", "title": "t", "why": ["lexical", "component"]},
        {"number": 2, "kind": "issue", "why": ["vector"]},
        {"number": 3, "kind": "issue", "why": ["fix_path", "component"]},
    ]
    status = {"lexical": "served", "vector": "served", "fix_path": "served"}
    inp = SimpleNamespace(org=ORG, namespace=NS)
    out = await tsim._hydrate(_Down(), inp, rows, status)
    assert [r["number"] for r in out] == [1]
    assert status == {
        "lexical": "served",
        "vector": "tracker_unavailable",
        "fix_path": "tracker_unavailable",
    }
    assert hd_answer._epistemic({"coef": {}}, status) == "degraded"


@pytest.mark.asyncio
@pytest.mark.parametrize("held", ["switched_off", "unread"])
async def test_a_refit_whose_vector_leg_is_held_raises_and_stores_nothing(
    monkeypatch, held
) -> None:
    """The replay's vector leg embeds each gold query: a call the model-call switch
    holds is no outage of the leg, so the refit raises it (the links pass skips the
    namespace) rather than fitting, or degrading, without vectors."""
    from server.llm import customer

    error = (
        customer.ModelCallsDisabled("model calls are switched off for org 'clickhouse'")
        if held == "switched_off"
        else customer.SwitchUnread("the model-call switch could not be read")
    )
    monkeypatch.setattr(df, "PACE_S", 0.0)
    monkeypatch.setattr(df, "MIN_POSITIVES", 1)
    monkeypatch.setattr(settings, "tracker_similar_vector_leg", True, raising=False)
    s = _Src()
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: s)
    s.gold = [{"number": 117352, "created_at": T0, "targets": [31768], "label": 1}]
    s.items[117352] = _item(117352, "avgWeighted incorrect float", 0)

    class _Repo:
        async def stored_vector(self, org, ns, kind, number):
            return None  # the query text is embedded

        async def namespace_stats(self, org, ns):
            return {"live": 10, "dead": 0, "last_updated": None, "dims": []}

    class _Refusing:
        model_name, dim = "fake-ngram-8", 8

        async def embed(self, text):
            raise error

    class _NoCursor:
        async def get(self, org, ns, kind):
            return None

    monkeypatch.setattr(tsim, "_embed_repo", lambda: _Repo())
    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: _Refusing())
    monkeypatch.setattr(
        "server.tracker.app.embed_index.cursor_state", lambda code_repo=None: _NoCursor()
    )
    state = _State()
    with pytest.raises(type(error)):
        await df.refit_namespace(s, state, ORG, NS, now=T0, negatives=0)
    assert not state.rows
