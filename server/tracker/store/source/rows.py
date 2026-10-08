"""Row shapes: upstream values coerced (DateTime64, arrays, `repo#number`
ids) and the issue / PR / document / similar-candidate rows the tracker
facts and handlers consume, with the column lists the item reads select.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.core.datetimes import to_naive_utc

_EPOCH = datetime(1970, 1, 1)


def ch_datetime(value: datetime) -> str:
    """A naive UTC `YYYY-MM-DD HH:MM:SS` for a DateTime parameter."""
    return to_naive_utc(value).replace(microsecond=0).isoformat(sep=" ")


def upstream_dt(v: Any) -> datetime | None:
    """Upstream DateTime64 → naive UTC datetime; epoch-0 (upstream's unset) → None."""
    if not isinstance(v, datetime):
        return None
    v = to_naive_utc(v)
    return None if v <= _EPOCH else v


def str_list(v: Any) -> list[str]:
    return [str(x) for x in (v or [])]


def split_source(source_id: str) -> tuple[str, int]:
    from server.tracker.domain.namespaces import split_source_id

    return split_source_id(source_id)


def ids_of(refs: list[str], private: bool) -> dict[str, tuple[str, str]]:
    """Derived document id → (ref, kind) for the issue and the PR a ref may
    name (a number is one or the other; both ids are cheap and one matches)."""
    from server.tracker.domain.namespaces import doc_id, item_kinds

    out: dict[str, tuple[str, str]] = {}
    for ref in refs:
        for kind in item_kinds(private):
            out[doc_id(kind, ref)] = (ref, kind)
    return out


_ITEM_COMMON = """
    source_id, url, title, author, labels, assignees, primary_component, severity_level,
    is_private, created_at, updated_at, closed_at
"""

ISSUE_COLS = (
    _ITEM_COMMON
    + """,
    state_reason, affected_versions, fix_versions, is_flaky_test, is_crash_ci, is_performance,
    triage_bisect_outcome, triage_reproduces_on, triage_clean_on,
    triage_introduced_commit, triage_component, triage_confidence
"""
)

# `github_prs` has no `labels` / `assignees` (verified live: the
# UNION with the issue branch failed on them); the PR branch supplies empty arrays
_PR_COMMON = _ITEM_COMMON.replace(
    "labels, assignees,",
    "CAST([] AS Array(String)) AS labels, CAST([] AS Array(String)) AS assignees,",
)
PR_COLS = (
    _PR_COMMON
    + """,
    pr_number, state, is_merged, has_test, test_types, reviewers, review_requests_test,
    ci_result, changelog_category, is_backport_pr, upstream_pr_number, minor_version,
    base_branch, additions, deletions, time_to_merge_days, src_components
"""
)


def issue_row(r: dict[str, Any]) -> dict[str, Any]:
    repo, number = split_source(str(r.get("source_id") or ""))
    closed = upstream_dt(r.get("closed_at"))
    return {
        "repo": repo,
        "number": number,
        "is_pr": 0,
        "is_private": int(r.get("is_private") or 0),
        "state": "closed" if closed else "open",
        "state_reason": str(r.get("state_reason") or ""),
        "is_merged": 0,
        "author": str(r.get("author") or ""),
        "title": str(r.get("title") or ""),
        "labels": str_list(r.get("labels")),
        "assignees": str_list(r.get("assignees")),
        "primary_component": str(r.get("primary_component") or ""),
        "severity_level": str(r.get("severity_level") or ""),
        "affected_versions": str_list(r.get("affected_versions")),
        # the releases the upstream records the issue as fixed in
        "fix_versions": str_list(r.get("fix_versions")),
        "is_flaky_test": int(r.get("is_flaky_test") or 0),
        "is_crash_ci": int(r.get("is_crash_ci") or 0),
        "is_performance": int(r.get("is_performance") or 0),
        "bisect_outcome": str(r.get("triage_bisect_outcome") or ""),
        "reproduces_on": str_list(r.get("triage_reproduces_on")),
        "clean_on": str_list(r.get("triage_clean_on")),
        "introduced_commit": str(r.get("triage_introduced_commit") or ""),
        "triage_component": str(r.get("triage_component") or ""),
        "triage_confidence": str(r.get("triage_confidence") or ""),
        "has_test": -1,
        "test_types": [],
        "reviewers": [],
        "review_requests_test": 0,
        "ci_result": "",
        "changelog_category": "",
        "is_backport_pr": 0,
        "upstream_pr_number": 0,
        "minor_version": "",
        "base_branch": "",
        "opened_at": upstream_dt(r.get("created_at")),
        "closed_at": closed,
        "updated_at": upstream_dt(r.get("updated_at")),
        "url": str(r.get("url") or ""),
    }


def pr_row(r: dict[str, Any]) -> dict[str, Any]:
    repo, number = split_source(str(r.get("source_id") or ""))
    number = int(r.get("pr_number") or number)
    has_test = r.get("has_test")
    closed = upstream_dt(r.get("closed_at"))
    return {
        "repo": repo,
        "number": number,
        "is_pr": 1,
        "is_private": int(r.get("is_private") or 0),
        "state": str(r.get("state") or ("closed" if closed else "open")),
        "state_reason": "",
        "is_merged": int(r.get("is_merged") or 0),
        "author": str(r.get("author") or ""),
        "title": str(r.get("title") or ""),
        "labels": str_list(r.get("labels")),
        "assignees": str_list(r.get("assignees")),
        "primary_component": str(r.get("primary_component") or ""),
        "severity_level": str(r.get("severity_level") or ""),
        "affected_versions": [],
        "is_flaky_test": 0,
        "is_crash_ci": 0,
        "is_performance": 0,
        "bisect_outcome": "",
        "reproduces_on": [],
        "clean_on": [],
        "introduced_commit": "",
        "triage_component": "",
        "triage_confidence": "",
        "has_test": -1 if has_test is None else int(has_test),
        "test_types": str_list(r.get("test_types")),
        "reviewers": str_list(r.get("reviewers")),
        "review_requests_test": int(r.get("review_requests_test") or 0),
        "ci_result": str(r.get("ci_result") or ""),
        "changelog_category": str(r.get("changelog_category") or ""),
        "is_backport_pr": int(r.get("is_backport_pr") or 0),
        "upstream_pr_number": int(r.get("upstream_pr_number") or 0),
        "minor_version": str(r.get("minor_version") or ""),
        "base_branch": str(r.get("base_branch") or ""),
        "additions": int(r.get("additions") or 0),
        "deletions": int(r.get("deletions") or 0),
        "time_to_merge_days": int(r.get("time_to_merge_days") or 0),
        "src_components": str_list(r.get("src_components")),
        "opened_at": upstream_dt(r.get("created_at")),
        "closed_at": closed,
        "updated_at": upstream_dt(r.get("updated_at")),
        "url": str(r.get("url") or ""),
    }


def doc_row(r: dict[str, Any], source: str) -> dict[str, Any]:
    """A bug-like document from any tracker source in one shape."""
    has_test = r.get("has_test")
    out: dict[str, Any] = {
        "source": source,
        "source_id": str(r.get("source_id") or ""),
        "url": str(r.get("url") or ""),
        "title": str(r.get("title") or "")[:160],
        "severity": str(r.get("severity_level") or ""),
        "component": str(r.get("primary_component") or ""),
        "created_at": upstream_dt(r.get("created_at")),
        "closed_at": upstream_dt(r.get("closed_at")),
        "open": upstream_dt(r.get("closed_at")) is None,
        "has_test": None if has_test is None else bool(int(has_test)),
    }
    for k in (
        "error_type",
        "error_name",
        "signal",
        "top_function",
        "org_name",
        "spoken_name",
        "cloud_provider",
        "cloud_region",
        "cloud_tier",
        "version_affected",
        "labels",
        "affected_versions",
        "reactions_total",
        "time_to_fix_days",
        "first_response_days",
    ):
        v = r.get(k)
        if v not in (None, "", [], 0):
            out[k] = str_list(v) if isinstance(v, list) else v
    return out


def similar_row(r: dict[str, Any]) -> dict[str, Any]:
    """One tracker.similar candidate in the wire shape."""
    repo, number = split_source(str(r.get("source_id") or ""))
    closed = upstream_dt(r.get("closed_at"))
    kind = str(r.get("kind") or "issue")
    if kind == "pr":
        state = "merged" if int(r.get("is_merged") or 0) else ("closed" if closed else "open")
    else:
        state = "closed" if closed else "open"
    return {
        "kind": kind,
        "repo": repo,
        "number": number,
        "source_id": str(r.get("source_id") or ""),
        "title": str(r.get("title") or "")[:200],
        "state": state,
        "state_reason": str(r.get("state_reason") or ""),
        "primary_component": str(r.get("primary_component") or ""),
        "affected_versions": str_list(r.get("affected_versions")),
        "error_code": int(r.get("error_code") or 0),
        # The first minor version the body names (QA's column)
        "version_affected": str(r.get("version_affected") or ""),
        "created_at": upstream_dt(r.get("created_at")),
        "url": str(r.get("url") or ""),
        "bm25": float(r.get("bm25") or 0.0),
    }
