"""tracker.* handlers: the shapes the number route does not have.

Every op is addressed at a tracker namespace (`org`, `namespace`), so the transport's gate —
including the second factor on a restricted namespace — has already decided before a handler
runs. Reads are live, bounded reads of the tracker record through that namespace's source; an
unknown number, component, test or version is an empty answer, never a claim of absence, and
a source that cannot answer marks the response `tracker_unavailable` instead of guessing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.core.errors import ValidationError
from server.tracker.api.models import (
    TrackerAnswerInput,
    TrackerAnswerOutput,
    TrackerBindingInput,
    TrackerBindingOutput,
    TrackerComponentInput,
    TrackerComponentOutput,
    TrackerFlakyInput,
    TrackerFlakyOutput,
    TrackerItemInput,
    TrackerItemOutput,
    TrackerLinksInput,
    TrackerLinksOutput,
    TrackerReleaseInput,
    TrackerReleaseOutput,
)
from server.tracker.app import common
from server.tracker.app import source as tsource
from server.tracker.app.facts import fact_of, tracker_fact_line
from server.tracker.app.test_signal import test_signal_for
from server.tracker.domain.errors import TrackerUnavailable
from server.tracker.store.links import LinkCandidate, LinkStats

logger = logging.getLogger(__name__)


async def handle_tracker_item(input: TrackerItemInput) -> TrackerItemOutput:
    input = await common._bound_input(input)

    src = tsource.tracker_source(input.org, input.namespace)
    nums = sorted({int(n) for n in input.numbers if int(n) > 0})[:10]
    try:
        rows = await src.items_by_number(input.org, input.namespace, nums) if nums else {}
        refs = [f"{r.get('repo') or ''}#{n}" for n, r in rows.items()]
        edges = await src.edges_from(input.org, input.namespace, refs) if refs else []
    except TrackerUnavailable as e:
        common._unavailable("tracker.item", input.namespace, e)
        return TrackerItemOutput(items=[], unknown=[], tracker_unavailable=True)
    items = []
    for n in nums:
        r = rows.get(n)
        if not r:
            continue
        fact = fact_of(r, edges)
        items.append(
            {
                "number": n,
                "kind": fact.get("kind"),
                "state": common._s(r.get("state")),
                "is_merged": bool(r.get("is_merged")),
                "title": common._s(r.get("title")),
                "author": common._s(r.get("author")),
                "opened_at": common._s(r.get("opened_at")),
                "closed_at": common._s(r.get("closed_at")),
                "url": common._s(r.get("url")),
                "tracker": fact,
                "line": tracker_fact_line(n, fact),
                # The files the fixing PRs touched, so a pre-filing dedup can apply its
                # code-path rule without one GitHub call per PR.
                "fixed_by_files": {},
            }
        )
    await _attach_fixed_by_files(src, input.org, input.namespace, items)
    return TrackerItemOutput(items=items, unknown=[n for n in nums if n not in rows])


async def _attach_fixed_by_files(src: Any, org: str, ns: str, items: list[dict[str, Any]]) -> None:
    """One read for every `fixed_by` PR named across the items: PR number →
    the paths its diff touched (≤ 40). Best-effort — an outage leaves the
    field empty rather than failing the op."""
    wanted: list[int] = []
    for it in items:
        for n in (it.get("tracker") or {}).get("fixed_by") or []:
            try:
                num = int(n)
            except (TypeError, ValueError):
                continue
            if num > 0 and num not in wanted:
                wanted.append(num)
    if not wanted:
        return
    try:
        files = await src.pr_files(org, ns, wanted[:32])
    except Exception as e:  # noqa: BLE001 — enrichment only
        common._unavailable("tracker.item/files", ns, e)
        return
    for it in items:
        it["fixed_by_files"] = {
            str(n): files.get(int(n), [])
            for n in (it.get("tracker") or {}).get("fixed_by") or []
            if str(n).isdigit() and int(n) in files
        }


async def handle_tracker_release(input: TrackerReleaseInput) -> TrackerReleaseOutput:
    input = await common._bound_input(input)

    try:
        rows = await tsource.tracker_source(input.org, input.namespace).release_items(
            input.org, input.namespace, input.version
        )
    except TrackerUnavailable as e:
        common._unavailable("tracker.release", input.namespace, e)
        return TrackerReleaseOutput(version=input.version, blockers=[], tracker_unavailable=True)
    blockers = []
    health: dict[str, Any] = {}
    for r in rows:
        if common._s(r.get("item_kind")) == "health":
            health = {
                "open_blockers": int(r.get("open_blockers") or 0),
                "critical_untested": int(r.get("critical_untested") or 0),
                "total_items": int(r.get("total_items") or 0),
                "snapshot_date": common._s(r.get("snapshot_date")),
            }
            continue
        status, issue_state = common._s(r.get("status")), common._s(r.get("issue_state"))
        blockers.append(
            {
                "ref": common._s(r.get("item_ref")),
                "status": status,
                "issue_state": issue_state,
                "fix_version": common._s(r.get("fix_version")),
                "active": status == "Blockers" and issue_state.upper() == "OPEN",
            }
        )
    blockers.sort(key=lambda b: (not b["active"], b["ref"]))
    return TrackerReleaseOutput(
        version=input.version,
        blockers=blockers,
        active_blockers=sum(1 for b in blockers if b["active"]),
        health=health,
    )


async def handle_tracker_component(input: TrackerComponentInput) -> TrackerComponentOutput:
    input = await common._bound_input(input)

    src = tsource.tracker_source(input.org, input.namespace)
    try:
        risk = (await src.component_risk_latest(input.org, input.namespace, [input.component])).get(
            input.component
        )
        issues = await src.open_issues_by_component(
            input.org, input.namespace, input.component, limit=input.limit
        )
    except TrackerUnavailable as e:
        common._unavailable("tracker.component", input.namespace, e)
        return TrackerComponentOutput(component=input.component, tracker_unavailable=True)
    return TrackerComponentOutput(
        component=input.component,
        risk={
            k: (v.isoformat() if hasattr(v, "isoformat") else v)
            for k, v in (risk or {}).items()
            if k != "component"
        },
        open_issues=[
            {
                "number": int(r.get("number") or 0),
                "title": common._s(r.get("title")),
                "severity": common._s(r.get("severity_level")),
                "labels": [str(x) for x in (r.get("labels") or [])][:6],
                "affected_versions": [str(x) for x in (r.get("affected_versions") or [])],
                "bisect_outcome": common._s(r.get("bisect_outcome")),
                "reproduces_on": [str(x) for x in (r.get("reproduces_on") or [])],
                "is_flaky_test": bool(r.get("is_flaky_test")),
                "is_crash_ci": bool(r.get("is_crash_ci")),
                "opened_at": common._s(r.get("opened_at")),
                "url": common._s(r.get("url")),
            }
            for r in issues
        ],
        known=bool(risk) or bool(issues),
    )


async def handle_tracker_flaky(input: TrackerFlakyInput) -> TrackerFlakyOutput:
    input = await common._bound_input(input)

    name = input.test_name.strip()
    try:
        rows = await tsource.tracker_source(input.org, input.namespace).flaky_issues_matching(
            input.org, input.namespace, name, limit=input.limit
        )
    except TrackerUnavailable as e:
        common._unavailable("tracker.flaky", input.namespace, e)
        return TrackerFlakyOutput(
            test_name=name, verdict="tracker_unavailable", tracker_unavailable=True
        )
    issues = [
        {
            "number": int(r.get("number") or 0),
            "title": common._s(r.get("title")),
            "state": common._s(r.get("state")),
            "state_reason": common._s(r.get("state_reason")),
            "labels": [str(x) for x in (r.get("labels") or [])][:6],
            "opened_at": common._s(r.get("opened_at")),
            "closed_at": common._s(r.get("closed_at")),
            "url": common._s(r.get("url")),
        }
        for r in rows
    ]
    open_n = sum(1 for i in issues if i["state"] == "open")
    out = TrackerFlakyOutput(
        test_name=name,
        known_flaky=bool(issues),
        open_issues=open_n,
        issues=issues,
        verdict="flaky_by_issue" if issues else "no_flaky_signal",
    )
    if input.include_signal:
        # The history read beside the issue verdict, on request.
        sig = await test_signal_for(
            input.org,
            input.namespace,
            name,
            window_days=int(input.window_days or 0),
        )
        out.signal = sig.model_dump(mode="json")
    return out


# --- tracker.test_signal ---------------------------------------------------------


async def handle_tracker_binding(input: TrackerBindingInput) -> TrackerBindingOutput:
    """What the addressed namespace may ask the tracker plane. No upstream read — the
    settings and the gate answer; a refused or unbound namespace is the same `none` as one
    the tracker plane never heard of."""
    from server.tracker.app.binding import resolve_binding

    input = common._with_default_org(input)  # the namespace stays as addressed
    return TrackerBindingOutput(**await resolve_binding(input.org, input.namespace))


async def handle_tracker_answer(input: TrackerAnswerInput) -> TrackerAnswerOutput:
    input = common._with_default_org(input)
    linked = await common.linked_trackers(str(input.org), str(input.namespace))
    input = await common._bound_input(input)
    if not input.question.strip() and not any(v for v in input.anchors.values()):
        # after the gate, so a refused principal never learns the shape
        raise ValidationError("question or anchors is required")
    from server.tracker.app.router import answer, render_lines, searched

    ask: dict[str, Any] = {
        "org": input.org,
        "limit": input.limit,
        "explain_only": input.explain_only,
        "anchors": dict(input.anchors or {}),
    }
    ans = await answer(input.question, tracker_namespace=input.namespace, **ask)
    d = ans.as_dict()
    lines = render_lines(ans) if not input.explain_only else []
    if len(linked) > 1 and not input.explain_only:
        more, more_lines = await _answer_elsewhere(input.question, linked[1:], ask)
        ans.sections.extend(s for _, s in more)
        d["sections"].extend({**s.as_dict(), "namespace": ns} for ns, s in more)
        d["searched"].extend(f"{ns}: {x}" for ns, x in ((n, searched(s)) for n, s in more))
        lines.extend(more_lines)
    if not ans.sections:
        verdict = "unknown"
    elif input.explain_only:
        verdict = "planned"
    elif ans.served:
        verdict = "answered"
    else:
        verdict = "empty"
    return TrackerAnswerOutput(
        intent=ans.intent,
        anchors=ans.anchors,
        sections=d["sections"],
        searched=d["searched"],
        lines=lines,
        restricted=ans.restricted,
        verdict=verdict,
    )


async def _answer_elsewhere(
    question: str, namespaces: list[str], ask: dict[str, Any]
) -> tuple[list[tuple[str, Any]], list[str]]:
    """The other linked trackers' SERVED sections of the same question — except a step
    addressed by number (`numbers`), which names the repository's items and is the
    primary's alone — each with its tracker named, and their prose lines under a
    `<namespace>:` head."""
    from server.tracker.app.router import TrackerAnswer, answer, render_lines

    outs = await asyncio.gather(
        *[answer(question, tracker_namespace=ns, **ask) for ns in namespaces],
        return_exceptions=True,
    )
    kept: list[tuple[str, Any]] = []
    lines: list[str] = []
    for ns, a in zip(namespaces, outs, strict=True):
        if isinstance(a, BaseException):
            logger.info("tracker.answer on %s failed inside a fan-out: %s", ns, a)
            continue
        served = [s for s in a.sections if s.status == "served" and not s.params.get("numbers")]
        if not served:
            continue
        kept.extend((ns, s) for s in served)
        sub = TrackerAnswer(intent=a.intent, anchors=a.anchors, namespace=ns, sections=served)
        lines.extend(f"{ns}: {ln}" for ln in render_lines(sub))
    return kept, lines


# ---------------------------------------------------------------- tracker.similar


async def links_record(src: Any, org: str, ns: str, *, issue: int, pr: int) -> dict[str, Any]:
    """The tracker graph's `fixes` edges for the subject, live: the PRs that fix
    the issue, or the issues the PR fixes. A miss is a status, not an empty
    answer."""
    from server.tracker.domain.namespaces import ref_of, split_source_id

    try:
        if issue:
            rows = await src.fix_prs_of_issue(org, ns, issue)
            nums = [int(r.get("pr_number") or 0) for r in rows]
        else:
            refs = [ref_of(r, pr) for r in src.repos()]
            edges = await src.edges_from(org, ns, refs, ["fixes"])
            nums = [
                split_source_id(str(e.get("dst_ref") or ""))[1]
                for e in edges
                if e.get("direction") == "out"
                and str(e.get("dst_kind") or "").startswith("github_issue")
            ]
    except TrackerUnavailable as e:
        common._unavailable("tracker.links", ns, e)
        return {"status": "tracker_unavailable", "numbers": []}
    nums = sorted({n for n in nums if n > 0})
    return {"status": "served" if nums else "empty", "numbers": nums}


async def handle_tracker_links(input: TrackerLinksInput) -> TrackerLinksOutput:
    input = await common._bound_input(input)
    from server.tracker.app import links as lk
    from server.tracker.store.links import TrackerLinksRepo

    issue, pr = int(input.issue_number or 0), int(input.pr_number or 0)
    if not issue and not pr:
        raise ValidationError("issue_number or pr_number is required")
    repo = TrackerLinksRepo()
    org, ns = input.org, input.namespace
    if issue:
        rows = await repo.candidates_for_issue(org, ns, issue, limit=int(input.limit))
    else:
        rows = await repo.candidates_for_pr(org, ns, pr, limit=int(input.limit))
    items = [lk.as_item(r) for r in rows]
    record: dict[str, Any] = {"status": "skipped", "numbers": []}
    if input.include_record:
        record = await links_record(tsource.tracker_source(org, ns), org, ns, issue=issue, pr=pr)
        confirmed = set(record["numbers"])
        for it in items:
            other = it["pr_number"] if issue else it["issue_number"]
            it["confirmed_by_record"] = other in confirmed
    stats = await repo.namespace_stats(org, ns)
    last = stats.get("last_computed")
    computed = items[0]["computed_at"] if items else (last.isoformat() if last else "")
    covered = _covered(rows, stats, issue)
    verdict = "candidates" if items else ("none" if covered else "not_computed")
    return TrackerLinksOutput(
        subject={"kind": "issue" if issue else "pr", "number": issue or pr},
        verdict=verdict,
        items=items,
        record=record,
        computed_at=computed,
        tracker_unavailable=record["status"] == "tracker_unavailable",
    )


def _covered(rows: list[LinkCandidate], stats: LinkStats, issue: int) -> bool:
    """Whether an empty list means "computed, nothing above the floor": the
    table has been written for the namespace and the subject is an issue
    (a PR direction cannot tell "never in any list" from "not covered")."""
    return bool(rows) or (bool(issue) and stats.get("last_computed") is not None)
