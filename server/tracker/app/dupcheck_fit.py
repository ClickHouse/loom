"""The stored duplicate fit of one tracker namespace: where it
lives, when it is due, how the weekly leg refreshes it and how the op reads
it.

The fit is JSON under `loom.code_index_state` key `qa_dupcheck_fit` (the storage
name) for the tracker namespace: the table and the keying
the `tracker_embed` cursor already uses
for the tracker plane — written by the `tracker_links` pass once a
week (`refit_if_due`) or on demand by `loom tracker-dupcheck-fit`. It carries
coefficients, the gold's sizes, the in-sample curve and the out-of-fold
holdout, so a reader can weigh the confidence it produces. The gold is read
through the source layer (`duplicate_gold`, `duplicate_negatives`) and
every query is replayed through the op's own path at its filing time.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from server.code.app.reads import CodeIndexReads
from server.llm import customer
from server.settings import settings
from server.tracker.api.models import TrackerDuplicateCheckInput
from server.tracker.domain import dupcheck as dc

logger = logging.getLogger(__name__)


# v1 fits live under the bare key; a v2+ fit under its own, so a server on
# the older feature set never reads coefficients it cannot apply. A newer
# server serves the PREVIOUS version's fit until its own refit has passed:
# features only ever grow, an older coefficient applies
# unchanged, a new feature scores 0 — calibrated, never `uncalibrated`,
# across a deploy. The refit writes the current version's key only.
def state_key_for(version: int) -> str:
    return "qa_dupcheck_fit" if version == 1 else f"qa_dupcheck_fit_v{version}"


STATE_KEY = state_key_for(dc.FIT_VERSION)


READ_VERSIONS: tuple[int, ...] = tuple(v for v in (dc.FIT_VERSION, dc.FIT_VERSION - 1) if v >= 1)
MIN_POSITIVES = 30
# a replay whose legs said `tracker_unavailable` on more than this share of the
# gold is a fit of the outage, not of the record: last week's keeps serving
MAX_UNAVAILABLE_SHARE = 0.05
# between replays: the source's text budget is per read, the upstream's
# breaker opens on three failures — 500 back-to-back queries tripped it
PACE_S = 0.1


@dataclass
class RefitOutcome:
    namespace: str
    # written | fresh | restricted | no_gold | degraded | degenerate | failed | off |
    # dry_run
    status: str
    n: int = 0
    positives: int = 0
    auc: float = 0.0
    replays: int = 0
    detail: str = ""
    fit: dict[str, Any] | None = None


def enabled() -> bool:
    return bool(settings.tracker_dupcheck_refit_enabled)


def state_repo() -> CodeIndexReads:
    from server.code.app.reads import code_index

    return code_index()


def parse_fit(text: str, *, version: int = dc.FIT_VERSION) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("coef"):
        return None
    # belt: a blob written under this key by another version is never scored
    if int(data.get("v") or 0) != int(version):
        return None
    return data


async def stored_fit(repo: Any, org: str, ns: str) -> dict[str, Any] | None:
    """The namespace's stored fit, or None (no row, unparsable, or the state
    read failed — the op then answers uncalibrated)."""
    try:
        for version in READ_VERSIONS:
            fit = parse_fit(
                str(await repo.get_state(org, ns, state_key_for(version)) or ""), version=version
            )
            if fit is not None:
                if version != dc.FIT_VERSION:
                    logger.info(
                        "dupcheck_fit: %s/%s serves the v%s fit under v%s features until its "
                        "refit passes",
                        org,
                        ns,
                        version,
                        dc.FIT_VERSION,
                    )
                return fit
        return None
    except Exception as e:  # noqa: BLE001 — a missing fit is an uncalibrated answer
        logger.info("dupcheck_fit: state read failed for %s/%s: %s", org, ns, e)
        return None


def refit_due(fit: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    if fit is None:
        return True
    interval_days = float(settings.tracker_dupcheck_refit_interval_s) / 86400.0
    return dc.fit_age_days(fit, now) >= interval_days


async def replay_query(
    src: Any,
    org: str,
    ns: str,
    gold: dict[str, Any],
    *,
    top_k: int = 10,
) -> dict[str, Any] | None:
    """One gold issue through the op's own path: its title and body as the
    draft, its component and version as the caller's, walled at its
    creation (so it is left out and nothing newer is a candidate) — and
    WITHOUT `issue_number`: the recorded relations are the gold's own
    labels and must not reach the features. The fused candidates' features
    with the target label on each."""
    from server.tracker.app import dupcheck_candidates as hd

    number = int(gold.get("number") or 0)
    created = gold.get("created_at")
    item = await src.similar_item(org, ns, number)
    if item is None or not isinstance(created, datetime):
        return None
    inp = TrackerDuplicateCheckInput(
        org=org,
        namespace=ns,
        title=str(item.get("title") or ""),
        body=str(item.get("body") or ""),
        component=str(item.get("component") or ""),
        version=str(item.get("version") or ""),
        as_of=created.isoformat(sep=" "),
        top_k=top_k,
    )
    status: dict[str, str] = {}
    run = await hd.candidates(src, inp, wall=created, status=status)
    q, ranked = run["q"], run["ranked"]
    targets = {int(t) for t in (gold.get("targets") or [])}
    version = q.versions[0] if q.versions else dc.minor_version(q.text)
    feats = dc.feature_rows(ranked, wall=created, query_version=version)
    return {
        "number": number,
        "label": int(gold.get("label") or 0),
        "created_at": created,
        "cites": any(int(n) in targets for n in q.mentions),
        "legs": dict(status),
        "items": [
            {
                "number": int(r.get("number") or 0),
                "features": f,
                "target": int(r.get("number") or 0) in targets
                and str(r.get("kind") or "") == "issue",
            }
            for r, f in zip(ranked, feats, strict=True)
        ],
    }


async def gather_gold(src: Any, org: str, ns: str, *, negatives: int) -> list[dict[str, Any]]:
    """Positives (the duplicate gold) and the hash-sampled negatives of their period."""
    pos = await src.duplicate_gold(org, ns)
    stamps = [p["created_at"] for p in pos if isinstance(p.get("created_at"), datetime)]
    if not stamps:
        return list(pos)
    neg = await src.duplicate_negatives(
        org,
        ns,
        since=min(stamps),
        until=max(stamps),
        limit=int(negatives),
    )
    return [*pos, *neg]


async def refit_namespace(
    src: Any,
    repo: Any,
    org: str,
    ns: str,
    *,
    now: datetime | None = None,
    force: bool = False,
    write: bool = True,
    negatives: int | None = None,
    progress: Any = None,
) -> RefitOutcome:
    """Refresh one namespace's fit when it is due: gold through the source,
    every query replayed at its filing time, fit, store. Raises only a call the
    model-call switch holds (`customer.HOLDS`) — otherwise the outcome says what
    happened; the pass counts them."""
    from server.tracker.domain.errors import TrackerUnavailable

    if not enabled():
        return RefitOutcome(ns, "off")
    cfg = getattr(src, "cfg", None)
    if getattr(cfg, "restricted", False):
        return RefitOutcome(ns, "restricted")
    try:
        current = await stored_fit(repo, org, ns)
        if not force and not refit_due(current, now=now):
            n = int((current or {}).get("queries", {}).get("n") or 0)
            return RefitOutcome(ns, "fresh", n=n)
        n_neg = int(negatives) if negatives else int(settings.tracker_dupcheck_fit_negatives)
        gold = await gather_gold(src, org, ns, negatives=n_neg)
        if sum(1 for g in gold if g.get("label")) < MIN_POSITIVES:
            return RefitOutcome(ns, "no_gold", n=len(gold), detail=f"{len(gold)} gold rows")
        queries, replays, unavailable = [], 0, 0
        for g in gold:
            try:
                row = await replay_query(src, org, ns, g)
            except TrackerUnavailable as e:
                logger.info("dupcheck_fit: replay of %s skipped: %s", g.get("number"), e)
                unavailable += 1
                continue
            finally:
                replays += 1
                if progress is not None:
                    progress(replays, len(gold))
                await asyncio.sleep(PACE_S)
            if row is None:
                continue
            queries.append(row)  # no candidates is a `none` the curve must count
            if any(v == "tracker_unavailable" for v in row["legs"].values()):
                unavailable += 1
        share = unavailable / replays if replays else 1.0
        if share > MAX_UNAVAILABLE_SHARE:
            return RefitOutcome(
                ns,
                "degraded",
                n=len(queries),
                replays=replays,
                detail=f"{unavailable} of {replays} replays had a leg unavailable",
            )
        fit = dc.fit_model(queries, now=now or datetime.now(UTC))
        fit["tracker_namespace"] = ns
        fit["gold"] = {
            "replayed": replays,
            "legs_unavailable": unavailable,
            "with_candidates": sum(1 for q in queries if q["items"]),
            "negatives_requested": n_neg,
        }
        return await _store(repo, org, ns, fit, write=write)
    except customer.HOLDS:
        raise  # a held call is no failed refit: the pass waits for the org
    except Exception as e:  # noqa: BLE001 — last week's fit keeps serving
        logger.warning("dupcheck_fit: refit failed for %s/%s: %s", org, ns, e)
        return RefitOutcome(ns, "failed", detail=f"{type(e).__name__}: {e}")


# A fit whose out-of-fold AUC is below this, or whose coefficients ran away,
# is not stored: last week's fit keeps serving (a collinear feature pair can
# drive the intercept to −1.9e22 and the AUC to 0.5).
MIN_HOLDOUT_AUC = 0.85
MAX_ABS_COEF = 100.0


def degenerate(fit: dict[str, Any]) -> str:
    """'' when the fit may serve, else why not."""
    hold = dict(fit.get("holdout") or {})
    auc = hold.get("auc")
    if auc is None or float(auc) < MIN_HOLDOUT_AUC:
        return f"holdout auc {auc} below {MIN_HOLDOUT_AUC}"
    coef = dict(fit.get("coef") or {})
    worst = max((abs(float(v)) for v in coef.values()), default=0.0)
    if worst > MAX_ABS_COEF or worst != worst:
        return f"coefficient magnitude {worst:.3g} above {MAX_ABS_COEF}"
    return ""


async def _store(repo: Any, org: str, ns: str, fit: dict[str, Any], *, write: bool) -> RefitOutcome:
    hold = dict(fit.get("holdout") or {})
    qn = dict(fit.get("queries") or {})
    why = degenerate(fit)
    if why:
        logger.warning("dupcheck_fit: %s/%s fit not stored — %s", org, ns, why)
        return RefitOutcome(
            ns,
            "degenerate",
            n=int(qn.get("n") or 0),
            positives=int(qn.get("positives") or 0),
            auc=float(fit.get("auc") or 0.0),
            detail=why,
            fit=fit,
        )
    detail = (
        f"queries={qn.get('n')} positives={qn.get('positives')} auc={fit.get('auc')} "
        f"holdout_auc={hold.get('auc')} suggested={fit.get('suggested')}"
    )
    out = RefitOutcome(
        ns,
        "written" if write else "dry_run",
        n=int(qn.get("n") or 0),
        positives=int(qn.get("positives") or 0),
        auc=float(fit.get("auc") or 0.0),
        replays=int((fit.get("gold") or {}).get("replayed") or 0),
        detail=detail,
        fit=fit,
    )
    if write:
        await repo.set_state(
            org,
            ns,
            STATE_KEY,
            json.dumps(fit, sort_keys=True, default=str),
            detail=detail,
        )
    return out


__all__ = [
    "MAX_UNAVAILABLE_SHARE",
    "MIN_POSITIVES",
    "PACE_S",
    "STATE_KEY",
    "RefitOutcome",
    "enabled",
    "gather_gold",
    "parse_fit",
    "refit_due",
    "refit_namespace",
    "replay_query",
    "state_repo",
    "stored_fit",
]
