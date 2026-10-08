"""Calibrated change risk for the review brief.

Two numbers per merged PR, both priors, both ranking signals and never a gate:
`jit_risk`, a per-repository logistic calibration of lines added, files changed and the
author's revert prior (one size feature through logistic regression is a strong
just-in-time baseline; the developer dimension is the strongest of the three); and
`author_revert_prior`, the author's reverted-or-caused share of merged PRs over the
window, smoothed toward the repository's base rate.

Everything here is pure: rows in, a fit out; the fit is a few coefficients and the
per-author counts, serialised as JSON into `loom.code_index_state` by the weekly
graph-stats pass (`fit_model`), and read back at brief build time (`score`). The fit's
own quality is measured on a time split — the last `holdout_months` of the window scored
by a model fitted on the months before, with author priors from the fitting months only
— and stored with it, so the brief can disclose what the number is worth. Recalibrate
within six months: just-in-time models' AUC and Brier score degrade considerably a year
after training.

Labels: a merged original PR is positive when the tracker record says it was reverted
(`is_reverted`, or the target of a `reverts` edge) or an issue was traced to it
(`caused_by`). Base rate on ClickHouse/ClickHouse over twelve months: 553 of 12,821
(4.3%).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

FIT_VERSION = 1
FEATURE_NAMES: tuple[str, ...] = (
    "intercept",
    "log_lines_added",
    "log_files_changed",
    "logit_author_prior",
)
# pseudo-count the author prior is smoothed with, toward the base rate: an
# author with no history scores the repository's rate; five merged PRs move
# it halfway to their own record (swept 2, 5, 10, 20, 50 on the live window:
# holdout AUC .703/.702/.701/.700/.696 at three months — noise apart, the
# lighter smoothing ranks a little better, and the heavier ones hand the
# fit's weight to the prior coefficient instead)
SMOOTHING_M = 5.0
_EPS = 1e-6
_RIDGE = 1e-3
_MAX_ITER = 60
# step halvings per Newton step before the fit is declared at its maximum
_MAX_HALVINGS = 40


# ---------------------------------------------------------------------------
# scalars
# ---------------------------------------------------------------------------


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def logit(p: float) -> float:
    p = min(1.0 - _EPS, max(_EPS, p))
    return math.log(p / (1.0 - p))


def normalize_author(login: str) -> str:
    """The tracker record keeps `clickhouse-gh`; loom's ledger keeps
    `clickhouse-gh[bot]`. One key for both."""
    s = str(login or "").strip()
    return s[:-5] if s.endswith("[bot]") else s


def smoothed_prior(
    positives: int,
    merged: int,
    base_rate: float,
    *,
    m: float = SMOOTHING_M,
) -> float:
    return (float(positives) + m * base_rate) / (float(merged) + m)


def features(lines_added: int, files_changed: int, author_prior: float) -> list[float]:
    return [
        1.0,
        math.log1p(max(0, int(lines_added))),
        math.log1p(max(0, int(files_changed))),
        logit(author_prior),
    ]


# ---------------------------------------------------------------------------
# logistic regression (IRLS, tiny ridge) — 4 parameters, no dependencies
# ---------------------------------------------------------------------------


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting on a small dense system."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        m[col], m[pivot] = m[pivot], m[col]
        p = m[col][col] or _EPS
        for r in range(n):
            if r == col:
                continue
            f = m[r][col] / p
            if f:
                for c in range(col, n + 1):
                    m[r][c] -= f * m[col][c]
    return [m[i][n] / (m[i][i] or _EPS) for i in range(n)]


def _log1pexp(z: float) -> float:
    """log(1 + e^z) without overflow."""
    return z + math.log1p(math.exp(-z)) if z > 0 else math.log1p(math.exp(z))


def penalised_loglik(w: list[float], x: list[list[float]], y: list[int], *, ridge: float) -> float:
    """The objective Newton climbs: log-likelihood minus the ridge on every
    weight but the intercept."""
    ll = 0.0
    for xi, yi in zip(x, y, strict=True):
        z = sum(wj * xj for wj, xj in zip(w, xi, strict=True))
        ll += yi * z - _log1pexp(z)
    return ll - 0.5 * ridge * sum(wj * wj for wj in w[1:])


def fit_logistic(x: list[list[float]], y: list[int], *, ridge: float = _RIDGE) -> list[float]:
    """Newton–Raphson on the ridge-penalised log-likelihood (the intercept
    unpenalised), each step halved until the objective does not fall.

    Undamped Newton can saturate every probability in one step (a 12.6k-row
    design with 1% positives does): with p(1-p) = 0 everywhere the Hessian
    collapses to the ridge, the intercept pivot to `_EPS`, and the next step
    puts the intercept at -1e10 (AUC 0.5). Backtracking keeps every step an
    ascent, so the fit is the penalised maximum on any design; on a
    well-conditioned one the full step is always accepted."""
    if not x:
        return [0.0] * len(FEATURE_NAMES)
    k = len(x[0])
    w = [0.0] * k
    pos = sum(y)
    w[0] = logit(pos / len(y)) if 0 < pos < len(y) else 0.0
    obj = penalised_loglik(w, x, y, ridge=ridge)
    for _ in range(_MAX_ITER):
        grad = [0.0] * k
        hess = [[0.0] * k for _ in range(k)]
        for xi, yi in zip(x, y, strict=True):
            p = sigmoid(sum(wj * xj for wj, xj in zip(w, xi, strict=True)))
            r = yi - p
            s = p * (1.0 - p)
            for a in range(k):
                grad[a] += r * xi[a]
                for b in range(k):
                    hess[a][b] += s * xi[a] * xi[b]
        for a in range(1, k):
            grad[a] -= ridge * w[a]
            hess[a][a] += ridge
        step = _solve(hess, grad)
        if not all(math.isfinite(sj) for sj in step):
            break
        scale = 1.0
        for _ in range(_MAX_HALVINGS):
            cand = [wj + scale * sj for wj, sj in zip(w, step, strict=True)]
            new = penalised_loglik(cand, x, y, ridge=ridge)
            if new >= obj - 1e-12:
                break
            scale *= 0.5
        else:
            break  # no ascent along the Newton direction: w is the maximum we can reach
        moved = max(abs(scale * sj) for sj in step)
        w, obj = cand, new
        if moved < 1e-6:
            break
    return w


def predict(coef: list[float], x: list[float]) -> float:
    return sigmoid(sum(c * v for c, v in zip(coef, x, strict=True)))


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------


def auc(scores: list[float], labels: list[int]) -> float:
    """Area under the ROC curve by the rank statistic, ties at half."""
    pos = sum(labels)
    neg = len(labels) - pos
    if pos == 0 or neg == 0:
        return 0.0
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    rank_sum = 0.0
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        rank_sum += avg_rank * sum(labels[order[t]] for t in range(i, j + 1))
        i = j + 1
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def calibration_deciles(scores: list[float], labels: list[int]) -> list[dict[str, Any]]:
    """Ten equal-count bins by predicted probability, lowest first: the mean
    prediction against the observed positive rate."""
    if not scores:
        return []
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    n = len(order)
    out: list[dict[str, Any]] = []
    for d in range(10):
        lo, hi = (d * n) // 10, ((d + 1) * n) // 10
        idx = order[lo:hi]
        if not idx:
            continue
        out.append(
            {
                "decile": d + 1,
                "n": len(idx),
                "predicted": round(sum(scores[i] for i in idx) / len(idx), 4),
                "observed": round(sum(labels[i] for i in idx) / len(idx), 4),
            }
        )
    return out


# ---------------------------------------------------------------------------
# the fit
# ---------------------------------------------------------------------------


def _author_counts(rows: list[dict[str, Any]]) -> dict[str, list[int]]:
    counts: dict[str, list[int]] = {}
    for r in rows:
        a = normalize_author(str(r.get("author") or ""))
        if not a:
            continue
        c = counts.setdefault(a, [0, 0])
        c[0] += 1
        c[1] += int(bool(r.get("label")))
    return counts


def _design(
    rows: list[dict[str, Any]],
    counts: dict[str, list[int]],
    base_rate: float,
    *,
    loo: bool,
    m: float = SMOOTHING_M,
) -> tuple[list[list[float]], list[int], list[float]]:
    """Feature rows and labels; the author prior leaves the row's own
    outcome out when `loo` (the fitting window scores itself) and reads the
    counts as given otherwise (a later window scored by an earlier one)."""
    x: list[list[float]] = []
    y: list[int] = []
    priors: list[float] = []
    for r in rows:
        a = normalize_author(str(r.get("author") or ""))
        merged, pos = counts.get(a, [0, 0])
        label = int(bool(r.get("label")))
        if loo and a in counts:
            merged, pos = merged - 1, pos - label
        prior = smoothed_prior(pos, merged, base_rate, m=m)
        priors.append(prior)
        x.append(features(int(r.get("lines_added") or 0), int(r.get("files_changed") or 0), prior))
        y.append(label)
    return x, y, priors


def _holdout(
    rows: list[dict[str, Any]],
    cutoff: datetime,
    *,
    m: float = SMOOTHING_M,
) -> dict[str, Any]:
    """The time split: fit on rows merged before `cutoff`, score the rest
    with author priors from the fitting rows only."""
    train = [r for r in rows if _merged(r) < cutoff]
    test = [r for r in rows if _merged(r) >= cutoff]
    if len(train) < 50 or not test or sum(int(bool(r.get("label"))) for r in test) == 0:
        return {
            "n": len(test),
            "positives": 0,
            "auc": 0.0,
            "auc_lines_added": 0.0,
            "auc_author_prior": 0.0,
            "deciles": [],
            "cutoff": _stamp(cutoff),
        }
    base = sum(int(bool(r.get("label"))) for r in train) / len(train)
    counts = _author_counts(train)
    x_tr, y_tr, _ = _design(train, counts, base, loo=True, m=m)
    coef = fit_logistic(x_tr, y_tr)
    x_te, y_te, priors = _design(test, counts, base, loo=False, m=m)
    scores = [predict(coef, xi) for xi in x_te]
    la_scores = [xi[1] for xi in x_te]
    return {
        "n": len(test),
        "positives": sum(y_te),
        "cutoff": _stamp(cutoff),
        "auc": round(auc(scores, y_te), 4),
        "auc_lines_added": round(auc(la_scores, y_te), 4),
        "auc_author_prior": round(auc(priors, y_te), 4),
        "deciles": calibration_deciles(scores, y_te),
        "coef": {n: round(c, 4) for n, c in zip(FEATURE_NAMES, coef, strict=True)},
    }


def fit_model(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    window_months: int = 12,
    holdout_months: int = 3,
    smoothing_m: float = SMOOTHING_M,
) -> dict[str, Any]:
    """The stored fit for one repository: coefficients over the whole window
    (author priors leave-one-out), the per-author counts the brief's prior
    reads, and the time-split holdout measured first. Rows carry
    `pr_number, author, lines_added, files_changed, merged_at, label`."""
    moment = now or datetime.now(UTC)
    rows = [r for r in rows if _merged(r) is not None]
    n = len(rows)
    positives = sum(int(bool(r.get("label"))) for r in rows)
    base = positives / n if n else 0.0
    counts = _author_counts(rows)
    x, y, _ = _design(rows, counts, base, loo=True, m=smoothing_m)
    fittable = n >= 50 and 0 < positives < n
    coef = fit_logistic(x, y) if fittable else [logit(base or _EPS), 0.0, 0.0, 0.0]
    cutoff = (moment - timedelta(days=30 * holdout_months)).replace(tzinfo=None)
    return {
        "v": FIT_VERSION,
        "fit_at": _stamp(moment.replace(tzinfo=None)),
        "window_months": int(window_months),
        "n": n,
        "positives": positives,
        "base_rate": round(base, 5),
        "smoothing_m": float(smoothing_m),
        "coef": {name: round(c, 6) for name, c in zip(FEATURE_NAMES, coef, strict=True)},
        "holdout": _holdout(rows, cutoff, m=smoothing_m),
        "authors": {a: c for a, c in sorted(counts.items())},
    }


def score(
    model: dict[str, Any],
    *,
    lines_added: int,
    files_changed: int,
    author: str,
) -> dict[str, Any]:
    """The brief's two numbers from a stored fit: `jit_risk` and
    `author_revert_prior`, with the counts behind the prior."""
    base = float(model.get("base_rate") or 0.0)
    m = float(model.get("smoothing_m") or SMOOTHING_M)
    key = normalize_author(author)
    merged, pos = (model.get("authors") or {}).get(key, [0, 0]) if key else [0, 0]
    prior = smoothed_prior(int(pos), int(merged), base, m=m)
    coef_map = dict(model.get("coef") or {})
    coef = [float(coef_map.get(name) or 0.0) for name in FEATURE_NAMES]
    return {
        "jit_risk": round(predict(coef, features(lines_added, files_changed, prior)), 4),
        "author_revert_prior": round(prior, 4),
        "author": key,
        "author_merged": int(merged),
        "author_reverted": int(pos),
        "author_known": bool(key) and key in (model.get("authors") or {}),
    }


def fit_age_days(model: dict[str, Any], now: datetime | None = None) -> float:
    at = _parse(str(model.get("fit_at") or ""))
    if at is None:
        return float("inf")
    moment = (now or datetime.now(UTC)).replace(tzinfo=None)
    return (moment - at).total_seconds() / 86400.0


def lines_added_in(diff: str) -> int:
    """Added lines of a unified diff (`+` lines that are not the `+++` header)."""
    n = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++ "):
            n += 1
    return n


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------


def _merged(r: dict[str, Any]) -> Any:
    v = r.get("merged_at")
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo is not None else v
    if isinstance(v, str):
        return _parse(v)
    return None


def _parse(text: str) -> datetime | None:
    t = text.strip().replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(t[:26], fmt)
        except ValueError:
            continue
    return None


def _stamp(d: datetime) -> str:
    return d.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "FEATURE_NAMES",
    "FIT_VERSION",
    "SMOOTHING_M",
    "auc",
    "calibration_deciles",
    "features",
    "fit_age_days",
    "fit_logistic",
    "fit_model",
    "lines_added_in",
    "logit",
    "normalize_author",
    "penalised_loglik",
    "predict",
    "score",
    "sigmoid",
    "smoothed_prior",
]
