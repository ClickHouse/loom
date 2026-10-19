"""The QA-intent route of `memory.ask`: a question that is
about a test, a release or a component is answered from the tracker record
before synthesis — the same shape as the PR-record route — through the tracker
namespace bound to the
bound code namespace, as the current principal.

Three deterministic shapes, no LLM:

  flaky   — the question names a test (`02345_keeper_session`,
            `test_storage_s3_queue/test.py::test_x`) and asks about it:
            the flaky-test issues that name it, open first.
  release — the question names a release minor (`26.3`) next to a release
            word (blocker, ready, ship, release, health): the active
            blockers and the latest health row.
  component — "how risky is Keeper", "open bugs in MergeTree": the newest
            risk snapshot and the open issues of that component.

Each answers `source="record"` with the keys `tracker_flaky:<ns>:<test>`,
`tracker_release:<ns>:<version>`, `tracker_component:<ns>:<name>`; None when the
route is off, nothing QA-shaped is in the question, no tracker namespace is
bound or authorized, the source is unavailable, or the record is empty —
absence is unknown, never "not flaky".
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("loom.tracker.ask_route")

_RELEASE_WORDS = re.compile(
    r"(?i)\b(release|blocker|blockers|blocking|ready to ship|ship|shippable|health|readiness)\b"
)
_VERSION_RE = re.compile(r"(?<![\d.])(\d{2}\.\d{1,2})(?![\d.]\d)")
_COMPONENT_RE = re.compile(
    r"(?i)\b(?:risk(?:y)?|open bugs?|open issues?|gap ratio|how (?:stable|risky|healthy))\b"
    r"[^A-Za-z0-9]{0,24}(?:of|in|for|is|are)?\s*"
    # the component itself is case-sensitive CamelCase (Keeper, MergeTree, S3Queue)
    r"((?-i:[A-Z][A-Za-z0-9]{2,}(?:[ _-]?[A-Z][A-Za-z0-9]+)?))"
)
_FLAKY_WORDS = re.compile(
    r"(?i)\b(flaky|flakiness|fail(?:s|ed|ing)?|broken|unstable|timeout|known)\b"
)


def classify(question: str) -> tuple[str, str]:
    """(shape, subject) or ("", "")."""
    from server.memory.domain.retrieval_calibration import salient_test_identifiers

    q = question or ""
    tests = salient_test_identifiers(q)
    if tests and (_FLAKY_WORDS.search(q) or len(q.split()) <= 8):
        return "flaky", tests[0]
    versions = _VERSION_RE.findall(q)
    if versions and _RELEASE_WORDS.search(q):
        return "release", versions[0]
    m = _COMPONENT_RE.search(q)
    if m:
        return "component", m.group(1).strip()
    # Every other QA-shaped question the catalogue can plan
    from server.tracker.app.router import make_plan

    p = make_plan(q)
    if p.intent and p.intent not in ("item",):
        return "catalogue", p.intent
    return "", ""


def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _date(v: Any) -> str:
    return v.date().isoformat() if hasattr(v, "date") else _s(v)[:10]


async def qa_route_answer(
    question: str, search_input: Any, settings: Any, results: list[Any], embedding_model: str
) -> Any:
    """A `MemoryAskResult(source="record")` or None. Never raises."""
    shape, subject = classify(question)
    if not shape:
        return None
    try:
        from server.memory.app.code_grounding import _resolve_code_target
        from server.tracker.app.binding import authorized_tracker_namespace
        from server.tracker.app.source import tracker_source

        code_org, code_ns = _resolve_code_target(search_input, settings)
        if not code_ns:
            return None
        tracker_ns = await authorized_tracker_namespace(code_org, code_ns, hides_answer=False)
        if not tracker_ns:
            return None
        source = tracker_source(code_org, tracker_ns)
        if shape == "flaky":
            lines, key = await _flaky(source, code_org, tracker_ns, subject)
        elif shape == "release":
            lines, key = await _release(source, code_org, tracker_ns, subject)
        else:
            lines, key = await _searched_everywhere(
                shape, question, code_org, code_ns, tracker_ns, subject
            )
    except Exception as e:  # noqa: BLE001 — the route never blocks an ask
        logger.debug("memory.ask qa route skipped (%s): %s", shape, e)
        return None
    if not lines:
        return None
    from server.memory.app.ask import MemoryAskResult

    return MemoryAskResult(
        answer="\n".join(lines),
        source="record",
        abstain=False,
        abstain_reason="",
        memory_keys=[key],
        results=results,
        embedding_model=embedding_model,
    )


async def _searched_everywhere(
    shape: str, question: str, org: str, code_ns: str, primary: str, subject: str
) -> tuple[list[str], str]:
    """A component or catalogue question is a search, so it
    reads every tracker linked to the repository — the primary first — and a
    tracker other than the primary heads its lines with its name. Flaky-test
    and release questions stay the primary's (the repository's own CI and
    releases). The key is the primary's when it answered, else the first
    that did. A tracker that could not be read is named on a last line, so
    an answer from the others never reads as everything the repository's
    trackers hold."""
    from server.tracker.app.binding import authorized_tracker_namespaces
    from server.tracker.app.source import tracker_source

    others = await authorized_tracker_namespaces(org, code_ns, hides_answer=False)
    linked = [primary] + [t for t in others if t != primary]
    lines: list[str] = []
    unread: list[str] = []
    key = ""
    for t in linked:
        try:
            if shape == "component":
                got, k = await _component(tracker_source(org, t), org, t, subject)
            else:
                got, k = await _catalogue(question, org, t, subject)
        except Exception as e:  # noqa: BLE001 — one tracker never blocks the others; named below
            logger.debug("memory.ask qa route on %s skipped: %s", t, e)
            unread.append(t)
            continue
        if not got:
            continue
        lines.extend(got if t == primary else [f"{t}:"] + got)
        key = key or k
    if lines and unread:
        lines.append("Not read (tracker unavailable): " + ", ".join(unread))
    return lines, key


async def _flaky(source: Any, org: str, ns: str, test: str) -> tuple[list[str], str]:
    rows = await source.flaky_issues_matching(org, ns, test, limit=5)
    if not rows:
        return [], ""
    open_rows = [r for r in rows if _s(r.get("state")) == "open"]
    head = (
        f"{test}: known flaky — {len(open_rows)} open flaky-test issue"
        f"{'s' if len(open_rows) != 1 else ''} of {len(rows)} that name it."
    )
    lines = [head]
    for r in rows[:5]:
        state = _s(r.get("state"))
        when = _date(r.get("opened_at"))
        tail = f", closed {_date(r.get('closed_at'))}" if state == "closed" else ""
        lines.append(
            f"- #{int(r.get('number') or 0)} ({state}, opened {when}{tail}): "
            f"{_s(r.get('title'))[:120]}"
        )
    return lines, f"tracker_flaky:{ns}:{test}"


async def _release(source: Any, org: str, ns: str, version: str) -> tuple[list[str], str]:
    rows = await source.release_items(org, ns, version)
    if not rows:
        return [], ""
    health = next((r for r in rows if _s(r.get("item_kind")) == "health"), None)
    blockers = [r for r in rows if _s(r.get("item_kind")) == "blocker"]
    active = [
        r
        for r in blockers
        if _s(r.get("status")) == "Blockers" and _s(r.get("issue_state")).upper() == "OPEN"
    ]
    lines = [
        f"Release {version}: {len(active)} active blocker{'s' if len(active) != 1 else ''} "
        f"of {len(blockers)} tracked item{'s' if len(blockers) != 1 else ''}."
    ]
    for r in active[:8]:
        fix = f" (fix version {_s(r.get('fix_version'))})" if r.get("fix_version") else ""
        lines.append(f"- {_s(r.get('item_ref'))}: blocker, open{fix}")
    if health:
        lines.append(
            f"Health snapshot {_date(health.get('snapshot_date'))}: "
            f"{int(health.get('open_blockers') or 0)} open blockers, "
            f"{int(health.get('critical_untested') or 0)} critical items shipped without a test, "
            f"{int(health.get('total_items') or 0)} items in the project."
        )
    return lines, f"tracker_release:{ns}:{version}"


async def _component(source: Any, org: str, ns: str, component: str) -> tuple[list[str], str]:
    risk = (await source.component_risk_latest(org, ns, [component])).get(component)
    issues = await source.open_issues_by_component(org, ns, component, limit=5)
    if not risk and not issues:
        return [], ""
    lines: list[str] = []
    if risk:
        lines.append(
            f"{component} (risk snapshot {_date(risk.get('snapshot_date'))}): "
            f"{int(risk.get('total_bugs') or 0)} bugs in two years, "
            f"{int(risk.get('open_bugs') or 0)} open, "
            f"gap ratio {float(risk.get('gap_ratio') or 0.0):.2f} "
            f"(bugs fixed without a test), risk score {float(risk.get('risk_score') or 0.0):.1f}, "
            f"trend {float(risk.get('trend_delta') or 0.0):+.2f}."
        )
    if issues:
        lines.append(f"Open issues, most severe first ({len(issues)} shown):")
        for r in issues:
            sev = _s(r.get("severity_level")) or "unrated"
            bis = f", bisect {_s(r.get('bisect_outcome'))}" if r.get("bisect_outcome") else ""
            lines.append(f"- #{int(r.get('number') or 0)} [{sev}{bis}] {_s(r.get('title'))[:120]}")
    return lines, f"tracker_component:{ns}:{component}"


__all__ = ["classify", "qa_route_answer"]


async def _catalogue(question: str, org: str, ns: str, intent: str) -> tuple[list[str], str]:
    """The catalogue's templates, rendered as record lines;
    the key names the intent so the answer's citation says what was run."""
    from server.tracker.app.router import answer, render_lines

    ans = await answer(question, org=org, tracker_namespace=ns, limit=5)
    if not ans.served and not any(s.status == "restricted_required" for s in ans.sections):
        return [], ""
    lines = render_lines(ans, max_rows=5)
    if lines:
        lines.append("Searched: " + "; ".join(ans.as_dict()["searched"]))
    return lines, f"qa_{intent}:{ns}"
