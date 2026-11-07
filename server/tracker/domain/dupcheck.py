"""tracker.duplicate_check — the pure half: the features of one
fused `tracker.similar` candidate, a logistic confidence over them, the
three-way verdict, and the fit (stratified k-fold out-of-fold first, then the
whole gold) with the precision / recall curve a threshold is chosen on.

The legs are `tracker.similar`'s, run once by the handler; nothing here reads. A
candidate's features are what the fused row already says — how many legs
found it and where, how far ahead of the next candidate it is, which legs
and which field equalities named it, how old it is relative to the draft —
so a coefficient is a statement about a leg, and the record reports them.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from server.code.domain.index.risk_calibration import (
    auc,
    calibration_deciles,
    fit_logistic,
    sigmoid,
)
from server.tracker.domain.similar import RRF_K

# The fit reads the vector leg's evidence: `vector` (the leg found the
# candidate), `agreement` (lexical AND vector), `closeness` (1 − cosine
# distance / 0.5, floored at 0). Without a vector feature the only road to
# `duplicate_of` is `mentions`, so a fit calls the duplicates a draft cites and
# none of the uncited ones. `vector_inv_rank` is 1 / the candidate's rank WITHIN
# the vector leg (0 when the leg did not find it; a lexical twin is not used: on
# lexical-only lists it equals `inv_rank` exactly and the fit diverges). The
# fused `inv_rank` punishes a candidate only the vector leg found: recall-only
# fusion appends it after every lexical candidate, so a target at vector rank 1
# (cosine 0.21) can sit at fused rank 12 and score 0.02. Each FIT_VERSION's fit
# is stored under its own state key, so the other build of a rollout keeps
# reading its own.
FIT_VERSION = 3
# cosine distance at which `closeness` reaches 0 (text-embedding-3-large;
# the duplicate gold's true targets sit at 0.21–0.35)
CLOSENESS_SPAN = 0.5
# the ridge is heavier than the JIT fit's (1e-3): version and error_code are
# sparse on this gold (35 rows carry affected_versions) and a separable
# sparse flag would otherwise run away
RIDGE = 0.5
FEATURE_NAMES: tuple[str, ...] = (
    "intercept",
    "legs",
    "inv_rank",
    "gap",
    "bm25_share",
    "lexical",
    "frames",
    "mentions",
    "component",
    "version",
    "error_code",
    "fix_path",
    "log_age",
    "vector",
    "agreement",
    "closeness",
    "vector_inv_rank",
)
VERDICTS = ("duplicate_of", "related", "none")
# evidence that counts FOR a duplicate (a shared frame, the
# same component / version / error code, a fix that touched the draft's
# paths): the gold barely exercises some of them (three crash bodies, no
# paths), so a fit may hand one a negative weight by accident — such a
# coefficient is pinned at 0 and the rest refitted, never a penalty
NONNEGATIVE: tuple[str, ...] = (
    "frames",
    "component",
    "version",
    "error_code",
    "fix_path",
    "vector",
    "agreement",
    "closeness",
    "vector_inv_rank",
)
FOLDS = 5
_VERSION_RE = re.compile(r"\b(\d{2})\.(\d{1,2})\b")
_LEGS = ("lexical", "frames", "mentions", "vector", "fix_path")


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------


def minor_version(text: str) -> str:
    """`X.Y` of the first ClickHouse-shaped version in `text` ('' when none)."""
    m = _VERSION_RE.search(str(text or ""))
    return f"{int(m.group(1))}.{int(m.group(2))}" if m else ""


def version_match(a: str, b: str) -> float:
    """1.0 for the same minor, 0.5 for the adjacent one (25.12 ↔ 26.1
    included), 0 otherwise or when either side is unknown."""
    va, vb = minor_version(a), minor_version(b)
    if not va or not vb:
        return 0.0
    if va == vb:
        return 1.0
    ma, mb = (int(v.split(".")[0]) * 12 + int(v.split(".")[1]) for v in (va, vb))
    return 0.5 if abs(ma - mb) == 1 else 0.0


def item_features(
    row: dict[str, Any],
    *,
    rank: int,
    next_score: float,
    top_bm25: float,
    wall: datetime,
    query_version: str = "",
) -> dict[str, float]:
    """The feature row of one fused candidate: leg credits (the RRF score in
    units of one leg at rank 1), 1/rank, the score gap to the next
    candidate, the BM25F share of the query's best lexical row, one flag per
    leg and per field equality in `why`, the version match on QA's
    `version_affected`, the share of the draft's paths a fix PR of the
    candidate touched (`fix_path`, set on the row by the handler), and
    log1p of the candidate's age in days at `wall`."""
    score = float(row.get("score") or 0.0)
    why = set(row.get("why") or [])
    created = row.get("created_at")
    age_days = 0.0
    if isinstance(created, datetime):
        moment = wall.replace(tzinfo=None) if wall.tzinfo else wall
        age_days = max(0.0, (moment - created.replace(tzinfo=None)).total_seconds() / 86400.0)
    version = max(
        1.0 if "version" in why else 0.0,
        version_match(query_version, str(row.get("version_affected") or "")),
    )
    return {
        "legs": score * (RRF_K + 1),
        "inv_rank": 1.0 / max(1, int(rank)),
        "gap": (score - float(next_score)) / score if score > 0 else 0.0,
        "bm25_share": float(row.get("bm25") or 0.0) / top_bm25 if top_bm25 > 0 else 0.0,
        "lexical": 1.0 if "lexical" in why else 0.0,
        "frames": 1.0 if "frames" in why else 0.0,
        "mentions": 1.0 if "mentions" in why else 0.0,
        "component": 1.0 if "component" in why else 0.0,
        "version": version,
        "error_code": 1.0 if "error_code" in why else 0.0,
        "fix_path": max(0.0, min(1.0, float(row.get("fix_path") or 0.0))),
        "log_age": math.log1p(age_days),
        # v2: the vector leg's evidence
        "vector": 1.0 if "vector" in why else 0.0,
        "agreement": 1.0 if "vector" in why and "lexical" in why else 0.0,
        "closeness": closeness(row.get("distance")),
        "vector_inv_rank": leg_inv_rank(row, "vector"),
    }


def leg_inv_rank(row: dict[str, Any], leg: str) -> float:
    """1 / the candidate's rank within `leg` (`leg_ranks`, stamped by the
    fusion), 0 when that leg did not find it."""
    ranks = row.get("leg_ranks") or {}
    try:
        rank = int(ranks.get(leg) or 0)
    except (TypeError, ValueError):
        return 0.0
    return 1.0 / rank if rank > 0 else 0.0


def closeness(distance: Any) -> float:
    """1 at cosine distance 0, 0 at `CLOSENESS_SPAN` and beyond, 0 when the
    vector leg did not see the candidate."""
    if distance is None:
        return 0.0
    try:
        d = float(distance)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, 1.0 - d / CLOSENESS_SPAN))


def feature_rows(
    ranked: list[dict[str, Any]],
    *,
    wall: datetime,
    query_version: str = "",
) -> list[dict[str, float]]:
    """One feature row per fused candidate, in rank order."""
    top_bm25 = max((float(r.get("bm25") or 0.0) for r in ranked), default=0.0)
    out: list[dict[str, float]] = []
    for i, r in enumerate(ranked):
        nxt = float(ranked[i + 1].get("score") or 0.0) if i + 1 < len(ranked) else 0.0
        out.append(
            item_features(
                r,
                rank=i + 1,
                next_score=nxt,
                top_bm25=top_bm25,
                wall=wall,
                query_version=query_version,
            )
        )
    return out


def design(features: dict[str, float]) -> list[float]:
    return [1.0, *(float(features.get(name) or 0.0) for name in FEATURE_NAMES[1:])]


def confidence(fit: dict[str, Any], features: dict[str, float]) -> float:
    coef = dict(fit.get("coef") or {})
    w = [float(coef.get(name) or 0.0) for name in FEATURE_NAMES]
    return round(sigmoid(sum(a * b for a, b in zip(w, design(features), strict=True))), 4)


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def path_overlap(draft_paths: list[str], fix_files: list[str]) -> list[str]:
    """The draft's paths a fix touched: equal, or one a suffix of the other
    (a report names `Parsers/ParserSelectQuery.cpp`, the diff
    `src/Parsers/ParserSelectQuery.cpp`)."""
    out: list[str] = []
    files = [str(f).strip("/") for f in fix_files if str(f).strip()]
    tails = ["/" + f for f in files]
    for raw in draft_paths:
        p = str(raw).strip().strip("/")
        if not p:
            continue
        tail = "/" + p
        if any(
            f == p or f.endswith(tail) or p.endswith(t) for f, t in zip(files, tails, strict=True)
        ):
            out.append(p)
    return out


def evidence_lines(
    row: dict[str, Any],
    feats: dict[str, float],
    *,
    frames_total: int,
    component: str,
    relation_recorded: str = "",
) -> list[str]:
    """What a reader checks by eye: the legs that found the candidate, the
    field equalities, the frames it carries, the fix that touched the
    draft's paths, the relation the tracker graph already records."""
    why = set(row.get("why") or [])
    lines: list[str] = []
    legs = [leg for leg in _LEGS if leg in why]
    if legs:
        lines.append("found by: " + ", ".join(legs))
    if "mentions" in why:
        lines.append(f"the draft names #{int(row.get('number') or 0)}")
    if feats.get("component"):
        name = component or str(row.get("primary_component") or "")
        lines.append(f"same component: {name}".rstrip(": "))
    if feats.get("version"):
        same = "same" if feats["version"] >= 1.0 else "adjacent"
        lines.append(f"{same} version: {row.get('version_affected') or ''}".rstrip(": "))
    if feats.get("error_code"):
        lines.append(f"same error code: {int(row.get('error_code') or 0)}")
    if feats.get("vector") and row.get("distance") is not None:
        lines.append(
            f"vector: cosine distance {float(row['distance']):.2f}"
            + (" (agrees with the lexical leg)" if feats.get("agreement") else "")
        )
    hits = [bool(x) for x in (row.get("frame_hits") or [])]
    if hits and frames_total:
        lines.append(f"{sum(hits)}/{frames_total} frames match")
    states = row.get("fix_pr_state") or {}
    for pr, paths in sorted((row.get("fix_paths") or {}).items()):
        state = f" ({states[pr]})" if states.get(pr) and states[pr] != "merged" else ""
        lines.append(f"fix #{pr}{state} touches {', '.join(paths[:3])}")
    if relation_recorded and relation_recorded != "none":
        lines.append(f"recorded relation: {relation_recorded}")
    return lines


def cap_relation(relation: str, relation_recorded: str) -> str:
    """The tracker graph's word beats the score: a candidate the record links as
    `related_to` is never promoted to `duplicate` (same pattern, another
    issue); one it records as `duplicate_of` is one."""
    if relation_recorded == "related_to" and relation == "duplicate":
        return "related"
    if relation_recorded == "duplicate_of" and relation != "none":
        return "duplicate"
    return relation


def undecided(items: list[dict[str, Any]], *, duplicate: float, related: float) -> bool:
    """The top candidate sits in the gray band [related, duplicate) AND
    shares the component: a human should look at the list."""
    if not items:
        return False
    top = items[0]
    c = top.get("confidence")
    if c is None or not related <= float(c) < duplicate:
        return False
    return bool((top.get("features") or {}).get("component"))


# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------


def verdict_of(confidences: list[float | None], *, duplicate: float, related: float) -> str:
    """`duplicate_of` when any candidate's confidence reaches the duplicate
    threshold (the op sorts by confidence, so that candidate is served
    first — the fused rank is a feature, not the order of the answer),
    `related` when any reaches the related threshold, else `none`. A None
    confidence (no fit) never reaches either."""
    known = [c for c in confidences if c is not None]
    if not known:
        return "none"
    if max(known) >= duplicate:
        return "duplicate_of"
    if any(c >= related for c in known):
        return "related"
    return "none"


def relation_of(conf: float | None, *, duplicate: float, related: float) -> str:
    if conf is None:
        return "related"
    return "duplicate" if conf >= duplicate else ("related" if conf >= related else "none")


# ---------------------------------------------------------------------------
# fit
# ---------------------------------------------------------------------------


def _flat(queries: list[dict[str, Any]]) -> tuple[list[list[float]], list[int]]:
    x: list[list[float]] = []
    y: list[int] = []
    for q in queries:
        for it in q.get("items") or []:
            x.append(design(it["features"]))
            y.append(int(bool(it.get("target"))))
    return x, y


def _coef_map(w: list[float]) -> dict[str, float]:
    return {name: round(c, 6) for name, c in zip(FEATURE_NAMES, w, strict=True)}


def fit_coefficients(
    queries: list[dict[str, Any]],
    *,
    ridge: float = RIDGE,
    nonnegative: tuple[str, ...] = NONNEGATIVE,
) -> dict[str, float]:
    """Ridge logistic over every item; an evidence feature in `nonnegative`
    that comes out below zero is pinned at 0 and the others refitted."""
    x, y = _flat(queries)
    if not x or not 0 < sum(y) < len(y):
        return _coef_map([0.0] * len(FEATURE_NAMES))
    pinned: set[int] = set()
    for _ in range(len(nonnegative) + 1):
        cols = [i for i in range(len(FEATURE_NAMES)) if i not in pinned]
        w = fit_logistic([[row[i] for i in cols] for row in x], y, ridge=ridge)
        full = [0.0] * len(FEATURE_NAMES)
        for i, c in zip(cols, w, strict=True):
            full[i] = c
        bad = [i for i in cols if FEATURE_NAMES[i] in nonnegative and full[i] < 0]
        if not bad:
            return _coef_map(full)
        pinned.update(bad)
    return _coef_map(full)


def score_queries(queries: list[dict[str, Any]], fit: dict[str, Any]) -> None:
    """Write `confidence` on every item of every query from `fit`."""
    for q in queries:
        for it in q.get("items") or []:
            it["confidence"] = confidence(fit, it["features"])


def query_outcome(q: dict[str, Any], *, duplicate: float, related: float) -> dict[str, Any]:
    """One query under two thresholds: its verdict and whether a
    `duplicate_of` verdict named a true target (a negative query never does)."""
    items = q.get("items") or []
    confs = [it.get("confidence") for it in items]
    v = verdict_of(confs, duplicate=duplicate, related=related)
    hit = any(
        it.get("target")
        for it in items
        if it.get("confidence") is not None and float(it["confidence"]) >= duplicate
    )
    return {
        "verdict": v,
        "hit": bool(hit and v == "duplicate_of"),
        "label": int(q.get("label") or 0),
    }


def curve(
    queries: list[dict[str, Any]],
    *,
    related: float = 0.0,
    steps: int = 50,
) -> list[dict[str, Any]]:
    """Precision / recall of the `duplicate_of` verdict at each threshold of
    a grid (query level: a hit is a duplicate verdict naming a true target;
    a false alarm is any other duplicate verdict, negatives included), with
    the `none` rate on the negatives and on the positives beside them."""
    pos = [q for q in queries if int(q.get("label") or 0)]
    neg = [q for q in queries if not int(q.get("label") or 0)]
    out: list[dict[str, Any]] = []
    for i in range(1, steps):
        t = round(i / steps, 4)
        rows = [
            query_outcome(q, duplicate=t, related=min(related, t) if related else t)
            for q in queries
        ]
        fired = [r for r in rows if r["verdict"] == "duplicate_of"]
        hits = sum(1 for r in fired if r["hit"])
        none_neg = sum(
            1
            for q, r in zip(queries, rows, strict=True)
            if not r["label"] and r["verdict"] == "none"
        )
        none_pos = sum(
            1 for q, r in zip(queries, rows, strict=True) if r["label"] and r["verdict"] == "none"
        )
        out.append(
            {
                "threshold": t,
                "fired": len(fired),
                "hits": hits,
                "precision": round(hits / len(fired), 4) if fired else None,
                "recall": round(hits / len(pos), 4) if pos else None,
                "none_rate_negatives": round(none_neg / len(neg), 4) if neg else None,
                "none_rate_positives": round(none_pos / len(pos), 4) if pos else None,
            }
        )
    return out


def choose_threshold(points: list[dict[str, Any]], *, precision_floor: float) -> float | None:
    """The lowest threshold whose precision — and every higher threshold's —
    reaches the floor; None when no point does. Lowest, because recall
    falls as the threshold rises; "every higher one too", so a lucky dip
    below the floor further up cannot be chosen past."""
    ok = [p for p in points if p["precision"] is not None and p["precision"] >= precision_floor]
    if not ok:
        return None
    chosen: float | None = None
    for p in sorted(points, key=lambda r: -float(r["threshold"])):
        if p["precision"] is None:
            continue  # nothing fired up here: the floor holds vacuously
        if p["precision"] < precision_floor:
            break
        chosen = float(p["threshold"])
    return chosen


def item_deciles(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scores: list[float] = []
    labels: list[int] = []
    for q in queries:
        for it in q.get("items") or []:
            if it.get("confidence") is not None:
                scores.append(float(it["confidence"]))
                labels.append(int(bool(it.get("target"))))
    return calibration_deciles(scores, labels)


def item_auc(queries: list[dict[str, Any]]) -> float:
    scores: list[float] = []
    labels: list[int] = []
    for q in queries:
        for it in q.get("items") or []:
            if it.get("confidence") is not None:
                scores.append(float(it["confidence"]))
                labels.append(int(bool(it.get("target"))))
    return round(auc(scores, labels), 4)


def confidence_bands(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Query level: the served top candidate's confidence (the highest, as
    the op sorts) in fixed bands against how often that candidate is a
    true target — the calibration a caller reads before choosing a
    threshold."""
    edges = (0.0, 0.02, 0.05, 0.1, 0.2, 0.4, 0.6, 0.8, 1.01)
    rows: list[dict[str, Any]] = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        scored = [
            q["items"]
            for q in queries
            if q.get("items") and q["items"][0].get("confidence") is not None
        ]
        heads = [max(items, key=lambda it: float(it.get("confidence") or 0.0)) for items in scored]
        tops = [
            (float(it["confidence"]), bool(it.get("target")))
            for it in heads
            if lo <= float(it["confidence"]) < hi
        ]
        rows.append(
            {
                "band": f"[{lo:.2f}, {min(hi, 1.0):.2f})",
                "n": len(tops),
                "predicted": round(sum(c for c, _ in tops) / len(tops), 4) if tops else None,
                "observed": round(sum(1 for _, t in tops if t) / len(tops), 4) if tops else None,
            }
        )
    return rows


def _folds(queries: list[dict[str, Any]], folds: int) -> list[int]:
    """Fold per query, stratified by label, deterministic in the query order."""
    out = [0] * len(queries)
    for label in (0, 1):
        idx = [i for i, q in enumerate(queries) if int(q.get("label") or 0) == label]
        idx.sort(key=lambda i: int(queries[i].get("number") or 0))
        for k, i in enumerate(idx):
            out[i] = k % folds
    return out


def _crossval(
    queries: list[dict[str, Any]],
    *,
    folds: int,
    precision_floor: float,
    related_floor: float,
) -> dict[str, Any]:
    """Every query scored by a fit that never saw it (stratified k-fold), so
    the AUC, curve, thresholds and bands here are out-of-sample. A time
    split was rejected: the positives are recent (QA closes duplicates by
    hand mostly in the last year) and the negatives span the period, so the
    older training half held almost no positives."""
    if len(queries) < 4 * folds:
        return {"method": f"{folds}-fold out-of-fold", "n": len(queries), "auc": None}
    fold_of = _folds(queries, folds)
    for k in range(folds):
        train = [q for q, f in zip(queries, fold_of, strict=True) if f != k]
        test = [q for q, f in zip(queries, fold_of, strict=True) if f == k]
        score_queries(test, {"coef": fit_coefficients(train)})
    pts = curve(queries)
    dup = choose_threshold(pts, precision_floor=precision_floor)
    rel = choose_threshold(pts, precision_floor=related_floor)
    at: dict[str, Any] = {}
    if dup is not None:
        at = next(p for p in pts if p["threshold"] == dup)
    return {
        "method": f"{folds}-fold out-of-fold",
        "n": len(queries),
        "auc": item_auc(queries),
        "duplicate_threshold": dup,
        "related_threshold": rel,
        "at_duplicate": at,
        "curve": pts,
        "deciles": item_deciles(queries),
        "bands": confidence_bands(queries),
    }


def fit_model(
    queries: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    precision_floor: float = 0.9,
    related_floor: float = 0.5,
    folds: int = FOLDS,
) -> dict[str, Any]:
    """The stored fit: the out-of-fold measurement first (`holdout`), then
    the coefficients over every query, the in-sample curve, bands and the
    thresholds each suggests (informational — the served thresholds are
    settings on the probability scale, which is what calibration makes
    stable across refits)."""
    moment = (now or datetime.now(UTC)).replace(tzinfo=None, microsecond=0)
    holdout = _crossval(
        queries,
        folds=folds,
        precision_floor=precision_floor,
        related_floor=related_floor,
    )
    coef = fit_coefficients(queries)
    fit: dict[str, Any] = {"coef": coef}
    score_queries(queries, fit)
    pts = curve(queries)
    items = sum(len(q.get("items") or []) for q in queries)
    targets = sum(1 for q in queries for it in (q.get("items") or []) if it.get("target"))
    return {
        "v": FIT_VERSION,
        "fit_at": moment.isoformat(sep=" "),
        "queries": {
            "n": len(queries),
            "positives": sum(1 for q in queries if int(q.get("label") or 0)),
            "negatives": sum(1 for q in queries if not int(q.get("label") or 0)),
        },
        "items": {"n": items, "targets": targets},
        "ridge": RIDGE,
        "coef": coef,
        "auc": item_auc(queries),
        "deciles": item_deciles(queries),
        "bands": confidence_bands(queries),
        "curve": pts,
        "suggested": {
            "duplicate": choose_threshold(pts, precision_floor=precision_floor),
            "related": choose_threshold(pts, precision_floor=related_floor),
            "precision_floor": precision_floor,
            "related_floor": related_floor,
        },
        "holdout": holdout,
    }


def fit_age_days(fit: dict[str, Any], now: datetime | None = None) -> float:
    try:
        at = datetime.fromisoformat(str(fit.get("fit_at") or ""))
    except ValueError:
        return float("inf")
    moment = (now or datetime.now(UTC)).replace(tzinfo=None)
    return (moment - at.replace(tzinfo=None)).total_seconds() / 86400.0


__all__ = [
    "FEATURE_NAMES",
    "FIT_VERSION",
    "FOLDS",
    "NONNEGATIVE",
    "RIDGE",
    "VERDICTS",
    "cap_relation",
    "choose_threshold",
    "confidence",
    "confidence_bands",
    "curve",
    "design",
    "evidence_lines",
    "feature_rows",
    "fit_age_days",
    "fit_coefficients",
    "fit_model",
    "item_auc",
    "item_deciles",
    "item_features",
    "minor_version",
    "path_overlap",
    "query_outcome",
    "relation_of",
    "score_queries",
    "undecided",
    "verdict_of",
    "version_match",
]
