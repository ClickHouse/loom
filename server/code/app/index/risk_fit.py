"""The stored risk fit of one code namespace: where it lives, when it is due, how the
weekly pass refreshes it and how the brief reads it.

The fit is JSON under `loom.code_index_state` key `jit_risk_fit` for the CODE namespace
— the same table the ingest's watermark and the reconcile stamp use — written by
`code_graph_stats` once a week for every configured namespace bound to a tracker
namespace that is NOT restricted: the fit carries author logins and per-author counts
derived from the QA record and is cached under the code namespace, so the gate is the
pack's and the brief's. The read side is `stored_fit`, one point read; a namespace
without a fit gets an `EMPTY` risk section with cause `no_fit` (or `not_bound`).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from server.settings import settings

logger = logging.getLogger(__name__)

STATE_KEY = "jit_risk_fit"


@dataclass
class RefitOutcome:
    namespace: str
    status: str  # written | fresh | not_bound | restricted | no_rows | failed
    n: int = 0
    positives: int = 0
    holdout_auc: float = 0.0
    detail: str = ""


def parse_fit(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("coef") else None


async def stored_fit(repo: Any, org: str, ns: str) -> dict[str, Any] | None:
    """The namespace's stored fit, or None (no row, unparsable, or the
    state read failed — the brief section fails open to `no_fit`)."""
    try:
        return parse_fit(str(await repo.get_state(org, ns, STATE_KEY) or ""))
    except Exception as e:  # noqa: BLE001 — a missing fit is an empty section
        logger.info("risk_fit: state read failed for %s/%s: %s", org, ns, e)
        return None


def refit_due(fit: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    if fit is None:
        return True
    from server.code.domain.index.risk_calibration import fit_age_days

    interval_days = float(settings.code_jit_risk_refit_interval_s) / 86400.0
    return fit_age_days(fit, now) >= interval_days


async def _bound_tracker_namespace(org: str, ns: str) -> tuple[str, str]:
    """(tracker_namespace, reason): the tracker namespace whose record may feed a fit
    cached under this code namespace, or "" with `not_bound` / `restricted`."""
    from server.identity.app.namespace_classification import is_restricted
    from server.tracker.app.binding import tracker_namespace_for

    tracker_ns = tracker_namespace_for(org, ns)
    if not tracker_ns:
        return "", "not_bound"
    if await is_restricted(org, tracker_ns):
        return "", "restricted"
    return tracker_ns, ""


async def refit_namespace(
    repo: Any,
    org: str,
    ns: str,
    *,
    now: datetime | None = None,
    force: bool = False,
    outcomes: Any = None,
) -> RefitOutcome:
    """Refresh one namespace's fit when it is due: read the window through
    the tracker source layer, fit, store. Never raises — the outcome says what
    happened; the pass counts them."""
    from server.code.domain.index import risk_calibration as rc

    try:
        tracker_ns, reason = await _bound_tracker_namespace(org, ns)
        if not tracker_ns:
            return RefitOutcome(ns, reason)
        current = await stored_fit(repo, org, ns)
        if not force and not refit_due(current, now=now):
            return RefitOutcome(ns, "fresh", n=int(current.get("n") or 0) if current else 0)
        months = int(settings.code_jit_risk_window_months)
        rows = await (outcomes or _tracker_outcomes)(org, tracker_ns, months)
        if len(rows) < 50:
            return RefitOutcome(ns, "no_rows", n=len(rows), detail=f"{len(rows)} rows in window")
        fit = rc.fit_model(rows, now=now or datetime.now(UTC), window_months=months)
        fit["tracker_namespace"] = tracker_ns
        holdout = dict(fit.get("holdout") or {})
        detail = (
            f"n={fit['n']} positives={fit['positives']} base_rate={fit['base_rate']} "
            f"holdout_auc={holdout.get('auc')} qa={tracker_ns}"
        )
        await repo.set_state(org, ns, STATE_KEY, json.dumps(fit, sort_keys=True), detail=detail)
        return RefitOutcome(
            ns,
            "written",
            n=int(fit["n"]),
            positives=int(fit["positives"]),
            holdout_auc=float(holdout.get("auc") or 0.0),
            detail=detail,
        )
    except Exception as e:  # noqa: BLE001 — last week's fit keeps serving
        logger.warning("risk_fit: refit failed for %s/%s: %s", org, ns, e)
        return RefitOutcome(ns, "failed", detail=f"{type(e).__name__}: {e}")


async def _tracker_outcomes(org: str, tracker_ns: str, months: int) -> list[dict[str, Any]]:
    from server.tracker.app.source import tracker_source

    return await tracker_source(org, tracker_ns).merged_pr_outcomes(org, tracker_ns, months=months)


__all__ = [
    "STATE_KEY",
    "RefitOutcome",
    "parse_fit",
    "refit_due",
    "refit_namespace",
    "stored_fit",
]
