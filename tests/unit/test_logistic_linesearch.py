"""The shared ridge-logistic solver halves its Newton
step until the penalised log-likelihood does not fall. On a well-conditioned
design the full step is always taken and the answer is the old one; on a
design where undamped Newton overshoots, the damped fit reaches a higher
objective and every coefficient stays finite."""

from __future__ import annotations

import math
import random

from server.code.domain.index import risk_calibration as rc


def _undamped(x: list[list[float]], y: list[int], ridge: float, iters: int = 30) -> list[float]:
    """An undamped Newton solver without the line search — for the comparison only."""
    k = len(x[0])
    w = [0.0] * k
    w[0] = rc.logit(sum(y) / len(y))
    for _ in range(iters):
        grad = [0.0] * k
        hess = [[0.0] * k for _ in range(k)]
        for xi, yi in zip(x, y, strict=True):
            p = rc.sigmoid(sum(a * b for a, b in zip(w, xi, strict=True)))
            r, s = yi - p, p * (1 - p)
            for a in range(k):
                grad[a] += r * xi[a]
                for b in range(k):
                    hess[a][b] += s * xi[a] * xi[b]
        for a in range(1, k):
            grad[a] -= ridge * w[a]
            hess[a][a] += ridge
        step = rc._solve(hess, grad)
        w = [a + b for a, b in zip(w, step, strict=True)]
        if max(abs(s) for s in step) < 1e-6:
            break
    return w


def _benign(n: int = 2000, seed: int = 7) -> tuple[list[list[float]], list[int]]:
    rng = random.Random(seed)
    x, y = [], []
    for _ in range(n):
        yi = 1 if rng.random() < 0.05 else 0
        x.append(
            [
                1.0,
                (2.0 if yi else -2.0) + rng.gauss(0, 0.5),
                rng.random(),
                float(rng.random() < 0.5),
            ]
        )
        y.append(yi)
    return x, y


def _spiky(n: int = 3000, seed: int = 11) -> tuple[list[list[float]], list[int]]:
    """One feature is 0 on every row but a handful of positives, where it is
    60: the quadratic model is poor there and the full Newton step overshoots."""
    rng = random.Random(seed)
    x, y = [], []
    for _ in range(n):
        yi = 1 if rng.random() < 0.01 else 0
        x.append(
            [
                1.0,
                60.0 if yi and rng.random() < 0.5 else 0.0,
                rng.random(),
                float(rng.random() < 0.5),
            ]
        )
        y.append(yi)
    return x, y


def test_well_conditioned_design_gives_the_undamped_answer() -> None:
    x, y = _benign()
    old, new = _undamped(x, y, 0.5), rc.fit_logistic(x, y, ridge=0.5)
    assert all(abs(a - b) < 1e-6 for a, b in zip(old, new, strict=True))


def test_damped_steps_reach_a_higher_objective_where_newton_overshoots() -> None:
    x, y = _spiky()
    old, new = _undamped(x, y, 0.5), rc.fit_logistic(x, y, ridge=0.5)
    ll_old, ll_new = (rc.penalised_loglik(w, x, y, ridge=0.5) for w in (old, new))
    assert ll_new > ll_old + 10, (
        ll_old,
        ll_new,
    )  # the undamped fit sits ~100 nats below the maximum
    assert all(math.isfinite(c) and abs(c) < 50 for c in new)
    # and it is a maximum: nudging any coefficient lowers the objective
    for i in range(len(new)):
        for d in (-1e-3, 1e-3):
            w = list(new)
            w[i] += d
            assert rc.penalised_loglik(w, x, y, ridge=0.5) <= ll_new + 1e-9


def test_saturated_start_cannot_run_the_intercept_away() -> None:
    """Every probability at 0 makes p(1-p) vanish and the Hessian collapse to
    the ridge — the shape that can send an undamped refit's intercept to -1e10.
    The line search refuses the runaway step."""
    x, y = _spiky()
    scaled = [
        [v * 1e3 if i else v for i, v in enumerate(row)] for row in x
    ]  # z far from 0 after one step
    w = rc.fit_logistic(scaled, y, ridge=1e-3)
    assert all(math.isfinite(c) and abs(c) < 1e3 for c in w), w
    ll0 = rc.penalised_loglik([rc.logit(sum(y) / len(y)), 0.0, 0.0, 0.0], scaled, y, ridge=1e-3)
    assert rc.penalised_loglik(w, scaled, y, ridge=1e-3) >= ll0 - 1e-9


def test_log1pexp_is_stable_at_both_ends() -> None:
    assert rc._log1pexp(1000.0) == 1000.0
    assert rc._log1pexp(-1000.0) == 0.0
    assert abs(rc._log1pexp(0.0) - math.log(2)) < 1e-12
