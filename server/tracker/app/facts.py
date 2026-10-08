"""QA facts about an issue or PR number.

One live point read on the tracker source's issues and PRs and one read of the
edges around them (`server/tracker/store/source/`) give, per number:
component, severity, labels, affected versions, the triage bot's bisect verdict
with the branches that still reproduce, the PR's test presence / CI result /
reviewers / backport lineage, and the graph
around it (what fixes it, what it fixes, backports, causes). Two shapes:
the dict the wire ops carry (`qa` on a history item) and one prose line for
a synthesizer or a record answer. Absence is silence — an unknown number
adds nothing and never a claim.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("loom.tracker.facts")

_EDGE_RELS = ("fixes", "backports", "caused_by", "tested_by")
_MAX_LABELS = 6
_MAX_REFS = 5


def _num_of_ref(ref: str) -> int:
    _, _, n = (ref or "").partition("#")
    try:
        return int(n)
    except ValueError:
        return 0


def _refs(edges: list[dict[str, Any]], src_ref: str, rel: str, direction: str) -> list[int]:
    out: list[int] = []
    for e in edges:
        if (
            e.get("src_ref") == src_ref
            and e.get("rel_type") == rel
            and e.get("direction") == direction
        ):
            n = _num_of_ref(str(e.get("dst_ref") or ""))
            if n > 0 and n not in out:
                out.append(n)
    return out[:_MAX_REFS]


def fact_of(row: dict[str, Any], edges: list[dict[str, Any]]) -> dict[str, Any]:
    """The compact QA fact for one live row. Only keys with a value."""
    ref = f"{row.get('repo') or ''}#{int(row.get('number') or 0)}"
    is_pr = bool(row.get("is_pr"))
    fact: dict[str, Any] = {"kind": "pr" if is_pr else "issue"}

    def put(key: str, value: Any) -> None:
        if value in ("", None, [], 0, -1):
            return
        fact[key] = value

    put("component", str(row.get("primary_component") or row.get("triage_component") or ""))
    put("severity", str(row.get("severity_level") or ""))
    put("labels", [str(x) for x in (row.get("labels") or [])][:_MAX_LABELS])
    put("affected_versions", [str(x) for x in (row.get("affected_versions") or [])])
    if not is_pr:
        put("bisect_outcome", str(row.get("bisect_outcome") or ""))
        put("reproduces_on", [str(x) for x in (row.get("reproduces_on") or [])])
        put("clean_on", [str(x) for x in (row.get("clean_on") or [])])
        put("introduced_commit", str(row.get("introduced_commit") or "")[:12])
        put("triage_confidence", str(row.get("triage_confidence") or ""))
        if row.get("is_flaky_test"):
            fact["is_flaky_test"] = True
        if row.get("is_crash_ci"):
            fact["is_crash_ci"] = True
        # what fixes it: the reverse rows of `fixes` (PR → issue) point at us
        put("fixed_by", _refs(edges, ref, "fixes", "in"))
    else:
        has_test = row.get("has_test")
        if has_test is not None and int(has_test) >= 0:
            fact["has_test"] = bool(int(has_test))
        put("test_types", [str(x) for x in (row.get("test_types") or [])])
        put("ci_result", str(row.get("ci_result") or ""))
        put("reviewers", [str(x) for x in (row.get("reviewers") or [])][:_MAX_LABELS])
        if row.get("review_requests_test"):
            fact["review_requests_test"] = True
        put("changelog_category", str(row.get("changelog_category") or ""))
        put("minor_version", str(row.get("minor_version") or ""))
        if row.get("is_backport_pr"):
            fact["is_backport_pr"] = True
            put("upstream_pr", int(row.get("upstream_pr_number") or 0))
        put("fixes", _refs(edges, ref, "fixes", "out"))
        put("backported_by", _refs(edges, ref, "backports", "in"))
        put("backports", _refs(edges, ref, "backports", "out"))
    put("caused", _refs(edges, ref, "caused_by", "in"))
    put("caused_by", _refs(edges, ref, "caused_by", "out"))
    if row.get("url"):
        fact["url"] = str(row["url"])
    return fact


async def tracker_facts_for_numbers(
    org: str, tracker_ns: str, numbers: list[int]
) -> dict[int, dict[str, Any]]:
    """Facts keyed by number for the numbers the tracker source knows (≤ 64),
    read live through the tracker namespace's source. A failure or an
    unavailable source is an empty dict and a debug line (the surfaces that
    call this are additive)."""
    nums = sorted({int(n) for n in numbers if int(n) > 0})[:64]
    if not nums or not tracker_ns:
        return {}
    try:
        from server.tracker.app.source import tracker_source

        source = tracker_source(org, tracker_ns)
        rows = await source.items_by_number(org, tracker_ns, nums)
        if not rows:
            return {}
        refs = [f"{r.get('repo') or ''}#{n}" for n, r in rows.items()]
        edges = await source.edges_from(org, tracker_ns, refs, list(_EDGE_RELS))
    except Exception as e:  # noqa: BLE001 — additive, never a failed request
        logger.debug("qa facts skipped for %s/%s: %s", org, tracker_ns, e)
        return {}
    return {n: fact_of(r, edges) for n, r in rows.items()}


def _nums(v: Any) -> str:
    return ", ".join(f"#{int(x)}" for x in (v or []))


def tracker_fact_line(num: int, fact: dict[str, Any]) -> str:
    """One prose line: `[tracker] #N …` — what a reviewer would say first."""
    if not fact:
        return ""
    parts: list[str] = []
    if fact.get("component"):
        parts.append(f"component {fact['component']}")
    if fact.get("severity"):
        parts.append(f"severity {fact['severity']}")
    if fact.get("kind") == "issue":
        if fact.get("is_flaky_test"):
            parts.append("a flaky-test issue")
        if fact.get("bisect_outcome"):
            b = f"bisect {fact['bisect_outcome']}"
            if fact.get("introduced_commit"):
                b += f" (introduced at {fact['introduced_commit']})"
            parts.append(b)
        if fact.get("reproduces_on"):
            parts.append("still reproduces on " + ", ".join(fact["reproduces_on"]))
        if fact.get("clean_on"):
            parts.append("clean on " + ", ".join(fact["clean_on"]))
        if fact.get("affected_versions"):
            parts.append("affected " + ", ".join(fact["affected_versions"]))
        if fact.get("fixed_by"):
            parts.append("fixed by " + _nums(fact["fixed_by"]))
    else:
        if "has_test" in fact:
            parts.append("has a test" if fact["has_test"] else "adds no test")
        if fact.get("ci_result"):
            parts.append(f"CI {fact['ci_result']}")
        if fact.get("reviewers"):
            parts.append("reviewers " + ", ".join(f"@{r}" for r in fact["reviewers"]))
        if fact.get("changelog_category"):
            parts.append(f"changelog {fact['changelog_category']}")
        if fact.get("is_backport_pr"):
            up = fact.get("upstream_pr")
            parts.append(f"a backport of #{up}" if up else "a backport")
        if fact.get("fixes"):
            parts.append("fixes " + _nums(fact["fixes"]))
        if fact.get("backported_by"):
            parts.append("backported by " + _nums(fact["backported_by"]))
    if fact.get("caused"):
        parts.append("caused " + _nums(fact["caused"]))
    if not parts:
        return ""
    return f"[tracker] #{num}: " + "; ".join(parts) + "."


__all__ = ["fact_of", "tracker_fact_line", "tracker_facts_for_numbers"]
