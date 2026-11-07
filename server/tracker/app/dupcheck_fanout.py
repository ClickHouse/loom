"""A duplicate check reads every tracker record the caller may read.

A bug filed against a private repository may already exist in the public tracker and the
other way round. So `tracker.duplicate_check` addressed to a code namespace (or to the
tracker namespace bound to it) answers over every tracker namespace the principal may read,
the bound one first, at most `tracker_dupcheck_fanout_namespaces`. An explicit
`namespaces: [...]` names the set; a tracker namespace addressed by name alone stays a
single read (an explicit namespace is never widened). Pure merge here; the handler runs the
per-namespace checks.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any

from server.tracker.api.models import TrackerDuplicateCheckOutput

logger = logging.getLogger(__name__)

_RANK = {"duplicate_of": 2, "related": 1, "none": 0}


async def _may_read_tracker(
    org: str,
    ns: str,
    code_kinds: dict[str, str],
    *,
    hides_answer: bool,
    operator: Collection[tuple[str, str]] | None = None,
) -> bool:
    """Read gate for a tracker namespace the transport did not bind: a restricted one is
    read only with a grant on itself; a standard one by whoever may read it or any code
    namespace bound to it. `hides_answer`: the caller named it (see `row_in_scope`).
    `operator`: the operator's repositories (`binding.operator_repositories`) when the
    caller read them once for many trackers; None: `code_namespaces_linking` reads them."""
    from server.auth.authz import row_in_scope
    from server.operations.base import Role
    from server.tracker.app.binding import code_namespaces_linking
    from server.tracker.domain.namespaces import tracker_namespace

    target = tracker_namespace(org, ns)
    if target is None:
        return False
    if await row_in_scope(org, ns, Role.VIEWER, hides_answer=hides_answer):
        return True
    if target.restricted:
        return False
    code = [c for c, kind in code_kinds.items() if kind == "code"]
    # The org's links: a maintainer's, else the operator's map for its own repositories.
    for code_ns in code_namespaces_linking(org, ns, code, operator=operator):
        if await row_in_scope(org, code_ns, Role.VIEWER, hides_answer=hides_answer):
            return True
    return False


async def readable_tracker_namespaces(
    org: str, bound: str, requested: list[str], cap: int, *, linked: list[str] | None = None
) -> list[str]:
    """The tracker namespaces one check answers, `bound` first, at most `cap`.
    `requested` (each a tracker namespace or a code namespace bound to one) names
    the set; otherwise the trackers linked to the addressed repository
    (`linked`, already gated) come next, never cut by `cap`,
    then every configured tracker namespace the principal may read joins up to
    `cap`. A namespace that fails the gate is left out in silence."""
    from server.identity.app.namespace_kinds import kind_map
    from server.tracker.app.binding import authorized_tracker_namespace, operator_repositories
    from server.tracker.domain.namespaces import configured_namespaces, tracker_namespace

    out: list[str] = [bound] if bound else []
    for ns in linked or []:
        if ns and ns not in out:
            out.append(ns)
    cap = max(cap, len(out))
    if cap <= 1 and not requested:
        return out
    if requested:
        operator = operator_repositories()  # once for every requested name
        for raw in requested:
            ns = str(raw or "").strip()
            if not ns or ns in out:
                continue
            if tracker_namespace(org, ns) is None:
                ns = await authorized_tracker_namespace(org, ns, operator=operator)
                if not ns or ns in out:
                    continue
                out.append(ns)
                continue
            if await _may_read_tracker(org, ns, {}, hides_answer=True, operator=operator):
                out.append(ns)
        return out[: max(cap, 1)]
    kinds = await kind_map(org)
    operator = operator_repositories()  # once for every tracker below
    for ns in sorted(configured_namespaces()):
        if ns in out:
            continue
        # hides_answer=False: Loom adds this tracker, the caller did not name it
        if await _may_read_tracker(org, ns, kinds, hides_answer=False, operator=operator):
            out.append(ns)
        if len(out) >= cap:
            break
    return out[:cap]


def _item_key(it: dict[str, Any]) -> tuple[int, float]:
    return (0 if it.get("relation") == "duplicate" else 1, -float(it.get("confidence") or 0.0))


def _merged_nearest(served: list[tuple[str, Any]], verdict: str) -> list[dict[str, Any]]:
    """Every namespace's `nearest` rows (each naming its namespace), highest confidence
    first, up to NEAREST_TOTAL_MAX rows shown in all, counting the merged served items (on
    `related`)."""
    from server.tracker.domain import dupcheck_closest as dcl

    # Interleave: each namespace's own order is kept (its leg leaders ride first even at
    # a low confidence that a merge by confidence would bury), namespaces taking turns,
    # bound one first.
    lists = [
        [{**r, "namespace": ns} for r in (getattr(o, "nearest", None) or [])] for ns, o in served
    ]
    rows = [lst[i] for i in range(max(map(len, lists), default=0)) for lst in lists if i < len(lst)]
    # A rerank pick leads, whichever namespace it came from.
    order = {"same": 0, "related": 1}
    picks = [r for r in rows if isinstance(r.get("rerank"), dict)]
    picks.sort(
        key=lambda r: (
            order.get(str(r["rerank"].get("label")), 2),
            -float(r["rerank"].get("confidence") or 0),
        )
    )
    rows = [*picks, *[r for r in rows if not isinstance(r.get("rerank"), dict)]]
    if verdict not in ("none", "related"):
        from server.tracker.app.dupcheck_answer import RUNNER_UP_MAX

        return rows[:RUNNER_UP_MAX]
    shown = 0 if verdict == "none" else sum(len(o.items or []) for _, o in served)
    cap = max(0, dcl.NEAREST_TOTAL_MAX - shown)
    return dcl.keep_leaders(rows, cap)


def _merged_closest(
    served: list[tuple[str, Any]], verdict: str, th: dict[str, float]
) -> dict[str, Any]:
    """Every namespace's `closest` rows (each naming its namespace), most similar first,
    capped; none when the merged verdict is a duplicate. `needs` is the first namespace's
    that has one."""
    from server.tracker.domain import dupcheck_closest as dcl

    nearest = _merged_nearest(served, verdict)
    if verdict not in ("none", "related"):
        return {"nearest": nearest} if nearest else {}
    rows = [{**r, "namespace": ns} for ns, o in served for r in (getattr(o, "closest", None) or [])]
    if not rows:
        return {"nearest": nearest} if nearest else {}
    rows.sort(
        key=lambda r: (-float(r.get("title_similarity") or 0), -float(r.get("confidence") or 0))
    )
    rows = rows[: dcl.CLOSEST_MAX]
    need = next((list(o.needs) for _, o in served if getattr(o, "needs", None)), [])
    taken = {(r.get("namespace"), r.get("number")) for r in rows}
    return {
        "nearest": dcl.keep_leaders(
            [n for n in nearest if (n.get("namespace"), n.get("number")) not in taken],
            max(0, dcl.NEAREST_TOTAL_MAX - len(rows)),
        ),
        "closest": rows,
        "needs": need,
        "closest_note": dcl.note(rows, need, float(th.get("related") or 0.0)),
    }


def picks_cap(out: Any) -> int:
    """How many `nearest` rows an answer shows (the same caps as `_merged_nearest`): two
    beside a duplicate, else six less the served items and `closest` rows."""
    from server.tracker.app.dupcheck_answer import RUNNER_UP_MAX
    from server.tracker.domain import dupcheck_closest as dcl

    if out.verdict not in ("none", "related"):
        return RUNNER_UP_MAX
    served = 0 if out.verdict == "none" else len(out.items or [])
    return dcl.NEAREST_TOTAL_MAX - served - len(out.closest or [])


def _merged_rerank(served: list[tuple[str, Any]]) -> dict[str, Any]:
    """The bound namespace's rerank block, with every namespace's tokens summed and each
    namespace's own block kept."""
    blocks = {ns: dict(getattr(o, "rerank", None) or {}) for ns, o in served}
    blocks = {ns: b for ns, b in blocks.items() if b}
    if not blocks:
        return {}
    first = next(iter(blocks.values()))
    return {
        **first,
        "tokens_in": sum(int(b.get("tokens_in") or 0) for b in blocks.values()),
        "tokens_out": sum(int(b.get("tokens_out") or 0) for b in blocks.values()),
        "ms": max(int(b.get("ms") or 0) for b in blocks.values()),
        "by_namespace": blocks,
    }


def merge_outputs(namespaces: list[str], outs: list[Any], top_k: int) -> Any:
    """One answer over several namespaces' `TrackerDuplicateCheckOutput`s: every
    item carries its `namespace`; duplicates first, then confidence; the
    verdict is the strongest; `undecided` when any is; `epistemic` is
    `unavailable` only when nobody answered, `degraded` when some tracker did
    not answer (down, or its check failed) or any answer was degraded — a
    `none` over the trackers that did answer is not clearance —
    `uncalibrated` when any served namespace was, `calibrated` otherwise;
    `thresholds`, `legs`, `judge`,
    `fit` and `query` are the bound (first) namespace's; `leg_status` is every
    served namespace's rows (each names its namespace); `by_namespace`
    keeps each namespace's verdict, item count, epistemic and legs."""
    served = [(ns, o) for ns, o in zip(namespaces, outs, strict=True) if o is not None]
    if not served:
        return TrackerDuplicateCheckOutput(
            verdict="none",
            epistemic="unavailable",
            tracker_unavailable=True,
            namespaces=list(namespaces),
        )
    items: list[dict[str, Any]] = []
    by_ns: dict[str, dict[str, Any]] = {}
    for ns, o in served:
        for it in o.items or []:
            items.append({**it, "namespace": ns})
        by_ns[ns] = {
            "verdict": o.verdict,
            "items": len(o.items or []),
            "epistemic": o.epistemic,
            "undecided": bool(o.undecided),
            "legs": dict(o.legs or {}),
            "tracker_unavailable": bool(o.tracker_unavailable),
        }
    items.sort(key=_item_key)
    verdict = max((o.verdict for _, o in served), key=lambda v: _RANK.get(v, 0), default="none")
    answered = [o for _, o in served if not o.tracker_unavailable]
    if not answered:
        epistemic = "unavailable"
    elif len(answered) < len(namespaces) or any(o.epistemic == "degraded" for o in answered):
        epistemic = "degraded"
    elif any(o.epistemic == "uncalibrated" for o in answered):
        epistemic = "uncalibrated"
    else:
        epistemic = "calibrated"
    first = served[0][1]
    return TrackerDuplicateCheckOutput(
        **_merged_closest(served, verdict, dict(first.thresholds or {})),
        verdict=verdict,
        items=items[:top_k] if verdict != "none" else [],
        undecided=any(bool(o.undecided) for _, o in served),
        epistemic=epistemic,
        thresholds=dict(first.thresholds or {}),
        judge=dict(first.judge or {}),
        rerank=_merged_rerank(served),
        fit_at=first.fit_at,
        fit=dict(first.fit or {}),
        query=dict(first.query or {}),
        legs=dict(first.legs or {}),
        leg_status=[row for _, o in served for row in o.leg_status or []],
        tracker_unavailable=all(bool(o.tracker_unavailable) for _, o in served),
        namespaces=list(namespaces),
        by_namespace=by_ns,
    )
