"""The duplicate check's answer: the calibrated verdict over the fused
candidates, then the enrichments that read beside it (the runner-up, the
canonical issue a duplicate points to, the closest reported item, the judge
of an uncertain pair) and the blocks the output carries.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from server.core.datetimes import to_naive_utc
from server.settings import settings
from server.tracker.api.models import (
    TrackerDuplicateCheckOutput,
)
from server.tracker.app import similar as tsim
from server.tracker.domain import dupcheck as dc

logger = logging.getLogger(__name__)


def thresholds(fit: dict[str, Any] | None = None) -> dict[str, float]:
    """The served lines. The duplicate line is the
    one the fit chose for the 0.9 precision floor (`suggested.duplicate`,
    clamped to [`tracker_dupcheck_duplicate_threshold_min`, `_max`]) when the
    fit carries one; the static `tracker_dupcheck_duplicate_threshold` otherwise.
    The related line is static — the judge reads that band."""
    dup = float(settings.tracker_dupcheck_duplicate_threshold)
    rel = float(settings.tracker_dupcheck_related_threshold)
    if fit:
        chosen = (fit.get("suggested") or {}).get("duplicate")
        if isinstance(chosen, int | float) and chosen > 0:
            lo = float(settings.tracker_dupcheck_duplicate_threshold_min)
            hi = float(settings.tracker_dupcheck_duplicate_threshold_max)
            dup = min(hi, max(lo, float(chosen)))
    return {"duplicate": dup, "related": min(rel, dup)}


def parse_as_of(text: str) -> datetime | None:
    """The replay wall: ISO 8601, naive UTC; '' is the open present."""
    from server.core.errors import ValidationError

    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValidationError(f"as_of is not an ISO 8601 timestamp: {raw!r}") from e
    return to_naive_utc(at)


def _with_runner_up(out: Any, run: dict[str, Any], input: Any) -> Any:
    """Up to RUNNER_UP_MAX nearest rows beside a `duplicate_of` answer: the fit's best
    scored-out issues, on topic or a leg's first row; the verdict and items are untouched."""
    from server.tracker.domain import dupcheck_closest as dcl

    title = str(input.title or "").strip() or str((run.get("item") or {}).get("title") or "")
    served = {int(row_number(it)) for it in out.items or []}
    nearest = dcl.nearest_rows(
        run.get("unserved") or [],
        title,
        exclude=served,
        limit=RUNNER_UP_MAX,
        legs_top=(run.get("meta") or {}).get("legs_top"),
    )
    return out.model_copy(update={"nearest": nearest[:RUNNER_UP_MAX]}) if nearest else out


# Nearest rows beside a `duplicate_of` answer
RUNNER_UP_MAX = 2


# At most this many canonical issues added per answer
CANONICAL_MAX = 2


async def with_canonical(src: Any, input: Any, out: Any) -> Any:
    """A shown issue the tracker closed as a duplicate points at the issue it duplicates, the
    one the reporter should comment on. A draft's nearest row can itself be a closed
    duplicate (a chain of two or more), and without this the answer would stop one hop
    short. The duplicate→target map is the fit's own gold (`duplicate_gold`,
    kept an hour); a target already shown, or one filed after `as_of`, is not
    added; any failure keeps the answer as it was."""
    if out.tracker_unavailable:
        return out
    rows = [*(out.items or []), *(out.closest or []), *(out.nearest or [])]
    shown = {int(row_number(r)) for r in rows}
    try:
        gold = await src.duplicate_gold(input.org, input.namespace, limit=2000)
    except Exception as e:  # noqa: BLE001 — a pointer beside the answer, never the answer
        logger.info("duplicate_check: duplicate map unavailable (%s)", e)
        return out
    targets_of = {int(g["number"]): [int(t) for t in g.get("targets") or []] for g in gold}
    wanted: list[tuple[int, int]] = []
    for r in rows:
        dup = int(row_number(r))
        for t in targets_of.get(dup, []):
            if t not in shown and t not in {w for w, _ in wanted}:
                wanted.append((t, dup))
    wanted = wanted[:CANONICAL_MAX]
    if not wanted:
        return out
    try:
        found = await src.items_by_number(input.org, input.namespace, [t for t, _ in wanted])
    except Exception as e:  # noqa: BLE001 — canonicals are extra rows
        logger.info("duplicate_check: canonical rows unavailable (%s)", e)
        found = {}
    wall = parse_as_of(input.as_of)
    extra: list[dict[str, Any]] = []
    for t, dup in wanted:
        it = found.get(t) or {}
        created = it.get("created_at")
        late = isinstance(created, datetime) and wall is not None
        if late and created.replace(tzinfo=None) >= wall.replace(tzinfo=None):  # type: ignore[union-attr]
            continue
        row: dict[str, Any] = {"number": t, "canonical_of": dup}
        for k in ("namespace", "title", "state", "url"):
            if it.get(k) not in (None, ""):
                row[k] = it[k]
        extra.append(row)
    if not extra:
        return out
    return out.model_copy(update={"nearest": [*(out.nearest or []), *extra]})


def row_number(row: Any) -> int:
    get = row.get if isinstance(row, dict) else lambda k, d=None: getattr(row, k, d)
    try:
        return int(get("number") or 0)
    except (TypeError, ValueError):
        return 0


def with_closest(out: Any, run: dict[str, Any], input: Any, fit: Any) -> Any:
    """On `none` / `related`, the scored-out rows whose title is near-identical to the
    draft's, and the inputs the fit rewards that the draft left empty. The verdict and items
    are the fit's, untouched."""
    from server.tracker.domain import dupcheck_closest as dcl

    if out.tracker_unavailable:
        return out
    if out.verdict not in ("none", "related"):
        # A duplicate answer still shows the best scored-out issue under the line: on the
        # benchmark the filed target can sit just below a `duplicate_of` pick.
        return _with_runner_up(out, run, input)
    title = str(input.title or "").strip() or str((run.get("item") or {}).get("title") or "")
    served = {int(it.get("number") or 0) for it in out.items or []}
    closest = dcl.closest_rows(run.get("unserved") or [], title, exclude=served)
    # the nearest on-topic rows under the line, up to NEAREST_MAX rows shown in all
    shown = served | {int(r.get("number") or 0) for r in closest}
    room = dcl.NEAREST_MAX - len(out.items or []) - len(closest)
    nearest = dcl.nearest_rows(
        run.get("unserved") or [],
        title,
        exclude=shown,
        limit=room,
        legs_top=(run.get("meta") or {}).get("legs_top"),
    )
    if nearest:
        out = out.model_copy(update={"nearest": nearest})
    if not closest:
        return out
    paths = list(run.get("draft_paths") or [])
    has = dcl.draft_inputs(run["q"], body=str(input.body or ""), paths=paths)
    need = dcl.needs(fit, has)
    related = float((out.thresholds or {}).get("related") or 0.0)
    return out.model_copy(
        update={"closest": closest, "needs": need, "closest_note": dcl.note(closest, need, related)}
    )


def _strong_unserved(it: dict[str, Any]) -> bool:
    """A scored row under the related line the fit had a strong reason to
    miss: a fix in flight names it, or the vector leg put it in its top rows."""
    why = it.get("why") or []
    if "fix_path" in why:
        return True
    # the vector leg's FIRST row only: its top five under the line fired the
    # judge on 137 of 503 gold calls (from 58) for no extra hit
    vir = float((it.get("features") or {}).get("vector_inv_rank") or 0.0)
    return vir >= 1.0


async def judge_uncertain(src: Any, input: Any, run: dict[str, Any], out: Any, fit: Any) -> Any:
    """When the fit stopped short of `duplicate_of`
    but scored candidates in the related band, one model call reads the
    draft against the top `tracker_dupcheck_judge_top_k` scored candidates —
    the served ones, then those under the related line — and may promote a
    same-defect candidate, serving it if it was not. Fails open; never runs
    on a `duplicate_of` or an empty answer."""
    from server.tracker.app import dup_judge

    if not bool(settings.tracker_dupcheck_judge_enabled):
        return out
    if fit is None or not out.items or out.verdict not in ("related", "duplicate_of"):
        return out
    k = max(1, int(settings.tracker_dupcheck_judge_top_k))
    below = [it for it in run.get("unserved") or [] if str(it.get("kind") or "issue") == "issue"]
    served = [it for it in out.items if str(it.get("kind") or "issue") == "issue"][:k]
    if out.verdict == "related":
        # the served related items first, then the scored rows under the
        # related line in fused order, up to k; one call reads them all
        unserved = below[: max(0, k - len(served))]
    else:
        # The fit already found a duplicate, but a row under the line that a fix in flight
        # names, or that the vector leg put in its top rows, is a duplicate the fit could not
        # see, so the judge reads it too.
        unserved = [it for it in below if _strong_unserved(it)][:2]
        if not unserved:
            return out
        served = served[: max(1, k - len(unserved))]
    head = [*served, *unserved]
    if not head:
        return out
    q = run["q"]
    title = str(input.title or "").strip()
    body = str(input.body or "").strip()
    if not title and not body:  # an issue_number replay: the item's own text is the draft
        item = run.get("item") or {}
        title, body = str(item.get("title") or ""), str(item.get("body") or q.text or "")
    chars = int(settings.tracker_dupcheck_judge_body_chars)
    try:
        rows = await src.embed_items_by_number(
            input.org,
            input.namespace,
            kind="issue",
            numbers=[int(it["number"]) for it in head],
            body_chars=chars,
        )
    except Exception as e:  # noqa: BLE001 — no text, no judge
        logger.info("dupcheck judge: candidate text unavailable (%s)", e)
        return out
    text_by = {int(r["number"]): r for r in rows}
    cands = []
    for it in head:
        n = int(it["number"])
        row = text_by.get(n, {})
        cands.append(
            {
                "number": n,
                "title": row.get("title") or it.get("title") or "",
                "body": row.get("body") or "",
            }
        )
    from server.llm.client import get_llm_client_for_model

    model = str(settings.tracker_dupcheck_judge_model) or None
    judgement = await dup_judge.judge(get_llm_client_for_model(model), title, body, cands)
    return dup_judge.apply(out, judgement, unserved=unserved, top_k=int(input.top_k))


def answer(
    run: dict[str, Any],
    fit: dict[str, Any] | None,
    *,
    status: dict[str, str],
    wall: datetime | None,
    top_k: int,
) -> Any:
    """Features → confidence → relation (capped by the recorded one) →
    verdict; the items above the related threshold, duplicates first, each
    with its evidence; nothing on `none`."""

    q, ranked = run["q"], run["ranked"]
    th = thresholds(fit)
    moment = wall or (run["item"] or {}).get("created_at") or datetime.now(UTC).replace(tzinfo=None)
    version = q.versions[0] if q.versions else dc.minor_version(q.text)
    feats = dc.feature_rows(ranked, wall=moment, query_version=version)
    confs: list[float | None] = (
        [dc.confidence(fit, f) for f in feats] if fit else [None] * len(feats)
    )
    recorded = dict((run.get("record") or {}).get("relations") or {})
    items: list[dict[str, Any]] = []
    # Scored rows under the related line, in fused order: the judge reads them too (a gold
    # target can sit at fused rank 2 with a low confidence).
    unserved: list[dict[str, Any]] = []
    for r, f, c in zip(ranked, feats, confs, strict=True):
        rec = recorded.get(str(r.get("source_id") or ""), "none")
        rel = dc.cap_relation(dc.relation_of(c, **th), rec)
        base = tsim._similar_item(r)
        base.pop("relation", None)
        base.pop("distance", None)
        item = {
            **base,
            "confidence": c,
            "relation": rel,
            "relation_recorded": rec,
            "evidence": dc.evidence_lines(
                r,
                f,
                frames_total=len(q.frames),
                component=q.component,
                relation_recorded=rec,
            ),
            "features": {k: round(v, 4) for k, v in f.items()},
        }
        (items if rel != "none" else unserved).append(item)
    run["unserved"] = unserved
    items.sort(key=lambda it: (it["relation"] != "duplicate", -(it["confidence"] or 0.0)))
    if not fit:
        verdict = "related" if items else "none"
    else:
        verdict = (
            "duplicate_of"
            if items and items[0]["relation"] == "duplicate"
            else ("related" if items else "none")
        )
    return TrackerDuplicateCheckOutput(
        verdict=verdict,
        items=items[:top_k] if verdict != "none" else [],
        undecided=dc.undecided(items, **th) if fit else False,
        epistemic=_epistemic(fit, status),
        thresholds=th,
        fit_at=str((fit or {}).get("fit_at") or ""),
        fit=_fit_block(fit),
        query=query_block(run, wall),
        legs=status,
        leg_status=run.get("leg_status") or [],
        tracker_unavailable=(run.get("record") or {}).get("status") == "tracker_unavailable",
    )


# Legs whose failure leaves the fit's features at 0 for what they would have found.
_EPISTEMIC_LEGS = ("lexical", "frames", "mentions", "vector", "fix_path")
# The features only the vector leg sets; the fit holds each at or above 0
_VECTOR_FEATURES = ("vector", "agreement", "closeness", "vector_inv_rank")


def _epistemic(fit: dict[str, Any] | None, status: dict[str, str]) -> str:
    """`calibrated`; `uncalibrated` without a fit; `degraded` when
    a leg that ran — lexical, frames, mentions, vector, or the fix-in-flight
    leg the caller's `paths` asked for — stayed `tracker_unavailable` (the fit
    scores every one of them as a feature): the verdict was built from
    fewer legs than the fit was trained on, so a `none` is not clearance and
    a caller should retry. A vector leg that is `not_computed` (the namespace
    has no vectors) degrades it too when the fit weighs a vector feature: every
    candidate's vector features read 0, which can only lower its confidence."""
    if any(status.get(leg) == "tracker_unavailable" for leg in _EPISTEMIC_LEGS):
        return "degraded"
    if not fit:
        return "uncalibrated"
    coef = fit.get("coef") or {}
    if status.get("vector") == "not_computed" and any(
        float(coef.get(name) or 0.0) > 0 for name in _VECTOR_FEATURES
    ):
        return "degraded"
    return "calibrated"


def _fit_block(fit: dict[str, Any] | None) -> dict[str, Any]:
    if not fit:
        return {"status": "no_fit"}
    hold = dict(fit.get("holdout") or {})
    block = {
        "status": "borrowed" if fit.get("borrowed_from") else "served",
        "fit_at": str(fit.get("fit_at") or ""),
        "queries": dict(fit.get("queries") or {}),
        "auc": fit.get("auc"),
        "holdout_auc": hold.get("auc"),
        "tracker_namespace": str(fit.get("tracker_namespace") or ""),
    }
    if fit.get("borrowed_from"):
        block["borrowed_from"] = str(fit["borrowed_from"])
    return block


def query_block(run: dict[str, Any], wall: datetime | None) -> dict[str, Any]:
    q, item = run["q"], run.get("item")
    before = wall or (item or {}).get("created_at")
    return {
        "number": int(q.number or 0),
        "known": item is not None,
        "terms": list(q.terms),
        "tokens": len(q.tokens),
        "frames": [f.raw for f in q.frames],
        "frames_source": q.frames_source,
        "mentions": list(q.mentions),
        "component": q.component,
        "versions": list(q.versions),
        "error_code": q.error_code,
        "paths": list(run.get("draft_paths") or []),
        "before": before.isoformat(sep=" ") if isinstance(before, datetime) else "",
        "record": str((run.get("record") or {}).get("status") or "skipped"),
        **(run.get("meta") or {}),
    }
