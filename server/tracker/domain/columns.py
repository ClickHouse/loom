"""Loom's logical tracker model: every column the tracker statements read.

The `source` package writes ~90 statements against these tables and columns. They are
loom's vocabulary — the model a tracker is mapped onto — not any team's
schema: a tracker that exposes them directly (views with these names) needs
no mapping; any other tracker carries a `mapping.SourceMapping`, and the
projection loom renders for a mapped table supplies EVERY column below (the
mapped ones as their expressions, the rest as typed defaults), so no
statement ever meets a missing column. The contract (`contract.REQUIRED` /
`OPTIONAL`) is the subset a views-based tracker is checked against;
`tests/unit/test_tracker_mapping.py` keeps this model a superset of it.

The inventory is taken from the `source` package's statements: a column a new statement
reads must be added here, or a mapped tracker defaults it.
"""

from __future__ import annotations

COLUMNS_READ: dict[str, dict[str, str]] = {
    "github_issues": {
        "id": "UUID",
        "source_id": "String",
        "number": "UInt",
        "url": "String",
        "title": "String",
        "body": "String",
        "author": "String",
        "labels": "Array(String)",
        "assignees": "Array(String)",
        "primary_component": "String",
        "severity_level": "String",
        "is_private": "UInt",
        "created_at": "DateTime",
        "updated_at": "DateTime",
        "closed_at": "DateTime",
        "state_reason": "String",
        "affected_versions": "Array(String)",
        "fix_versions": "Array(String)",
        "is_flaky_test": "UInt",
        "is_crash_ci": "UInt",
        "is_performance": "UInt",
        "error_code": "UInt",
        "has_test": "UInt",
        "triage_bisect_outcome": "String",
        "triage_reproduces_on": "Array(String)",
        "triage_clean_on": "Array(String)",
        "triage_introduced_commit": "String",
        "triage_component": "String",
        "triage_confidence": "String",
        "embedding": "Array(Float)",
        "version_affected": "String",
    },
    "github_prs": {
        "id": "UUID",
        "source_id": "String",
        "url": "String",
        "title": "String",
        "body": "String",
        "author": "String",
        "primary_component": "String",
        "severity_level": "String",
        "is_private": "UInt",
        "created_at": "DateTime",
        "updated_at": "DateTime",
        "closed_at": "DateTime",
        "pr_number": "UInt",
        "number": "UInt",
        "state": "String",
        "is_merged": "UInt",
        "has_test": "UInt",
        "test_types": "Array(String)",
        "reviewers": "Array(String)",
        "review_requests_test": "UInt",
        "ci_result": "String",
        "changelog_category": "String",
        "is_backport_pr": "UInt",
        "is_cherrypick_pr": "UInt",
        "is_sync_pr": "UInt",
        "upstream_pr_number": "UInt",
        "minor_version": "String",
        "base_branch": "String",
        "additions": "UInt",
        "deletions": "UInt",
        "time_to_merge_days": "UInt",
        "src_components": "Array(String)",
        "embedding": "Array(Float)",
        "diff": "String",
        "affected_file_paths": "Array(String)",
        "merged_at": "DateTime",
        "changed_files_count": "UInt",
        "is_reverted": "UInt",
        "backport_required": "UInt",
        "changelog_entry": "String",
    },
    "edges": {
        "from_id": "UUID",
        "from_type": "String",
        "to_id": "UUID",
        "to_type": "String",
        "rel_type": "String",
        "created_at": "DateTime",
    },
    "core_incidents": {
        "id": "UUID",
        "source_id": "String",
        "title": "String",
        "body": "String",
        "primary_component": "String",
        "severity_level": "String",
        "stack_functions": "Array(String)",
        "error_code": "UInt",
        "created_at": "DateTime",
        "url": "String",
        "closed_at": "DateTime",
        "error_type": "String",
        "error_name": "String",
        "signal": "String",
        "top_function": "String",
        "has_test": "UInt",
        "time_to_fix_days": "Float",
        "rca_confidence": "String",
        "rca_caused_by_prs": "Array(String)",
        "rca_fix_prs": "Array(String)",
        "rca_backport_pending": "Array(String)",
        "rca_affected_versions": "Array(String)",
        "rca_same_root_cause": "Array(String)",
        "rca_duplicates_of": "Array(String)",
        "rca_notes": "String",
    },
    "support_cases": {
        "id": "UUID",
        "source_id": "String",
        "title": "String",
        "url": "String",
        "body": "String",
        "severity": "String",
        "severity_level": "String",
        "primary_component": "String",
        "org_name": "String",
        "spoken_name": "String",
        "cloud_provider": "String",
        "cloud_region": "String",
        "cloud_tier": "String",
        "version_affected": "String",
        "error_name": "String",
        "has_test": "UInt",
        "created_at": "DateTime",
        "closed_at": "DateTime",
        "first_response_days": "Float",
        "author": "String",
    },
    "slack_threads": {
        "source_id": "String",
        "title": "String",
        "body": "String",
        "id": "UUID",
        "url": "String",
        "channel_name": "String",
        "reply_count": "UInt",
        "participants": "Array(String)",
        "severity_level": "String",
        "created_at": "DateTime",
        "is_bot": "UInt",
    },
    "component_risk_snapshot": {
        "computed_at": "DateTime",
        "component": "String",
        "total_bugs": "UInt",
        "critical_bugs": "UInt",
        "open_bugs": "UInt",
        "bugs_with_test": "UInt",
        "bugs_without_test": "UInt",
        "gap_ratio": "Float",
        "risk_score": "Float",
        "trend_delta": "Float",
        "core_incident_count": "UInt",
        "support_case_count": "UInt",
        "reviewer_no_test_count": "UInt",
        "avg_time_to_fix_days": "Float",
    },
    "release_items": {
        "release_version": "String",
        "issue_number": "UInt",
        "issue_repo": "String",
        "status": "String",
        "issue_state": "String",
        "fix_version": "String",
        "updated_at": "DateTime",
    },
    "release_health_snapshot": {
        "computed_at": "DateTime",
        "release_version": "String",
        "open_blockers": "UInt",
        "critical_untested": "UInt",
        "total_items": "UInt",
    },
    "version_history": {
        "commit_sha": "String",
        "version": "String",
        "check_start_time": "DateTime",
        "pull_request_number": "UInt",
        "parent_commits_sha": "Array(String)",
        "docker_tag": "String",
        "git_ref": "String",
        "minor_version": "String",
    },
    "version_history_private": {
        "commit_sha": "String",
        "version": "String",
        "check_start_time": "DateTime",
        "pull_request_number": "UInt",
        "parent_commits_sha": "Array(String)",
        "docker_tag": "String",
        "git_ref": "String",
        "minor_version": "String",
    },
    "checks": {
        "check_name": "String",
        "test_name": "String",
        "check_start_time": "DateTime",
        "pull_request_number": "UInt",
        "head_ref": "String",
        "test_status": "String",
        "commit_sha": "String",
    },
    "cloud_instances": {
        "instance_id": "UInt",
        "org_name": "String",
        "state": "String",
        "clickhouse_version": "String",
        "tier": "String",
        "cloud": "String",
        "snapshotted_at": "DateTime",
        "org_id": "String",
        "version_limit_reason": "String",
        "version_limit_max": "String",
        "has_version_limit": "UInt",
    },
    "rel_instance_mentions": {
        "from_id": "UUID",
        "instance_id": "UInt",
        "spoken_name": "String",
        "mention_count": "UInt",
    },
    "slack_incidents": {
        "incident_id": "String",
        "channel_name": "String",
        "title": "String",
        "status": "String",
        "lifecycle_state": "String",
        "urgency": "String",
        "created_at": "DateTime",
        "resolved_at": "DateTime",
        "mttr_seconds": "UInt",
        "affected_orgs": "Array(String)",
        "affected_instances": "Array(String)",
        "rca_status": "String",
        "url": "String",
    },
    "qa_bugs": {
        "source_id": "String",
        "url": "String",
        "title": "String",
        "body": "String",
        "source": "String",
        "severity_level": "String",
        "primary_component": "String",
        "labels": "Array(String)",
        "affected_versions": "Array(String)",
        "has_test": "UInt",
        "reactions_total": "UInt",
        "created_at": "DateTime",
        "closed_at": "DateTime",
        "error_type": "String",
        "top_function": "String",
    },
}

LOGICAL_TABLES: frozenset[str] = frozenset(COLUMNS_READ)

# What a mapping must provide for the table to serve; everything else may
# default. An item is known by its reference, its title and when it appeared.
CORE: dict[str, tuple[str, ...]] = {
    "github_issues": ("source_id", "number", "title", "created_at"),
    "github_prs": ("source_id", "pr_number", "title", "created_at"),
    "edges": ("from_ref", "to_ref", "from_type", "to_type", "rel_type"),
    "core_incidents": ("source_id", "title", "created_at"),
}

# Tables whose `id` loom derives from `source_id` (namespaces.doc_id): kind,
# and whether a private twin kind exists (`<kind>_private`).
DERIVED_IDS: dict[str, tuple[str, bool]] = {
    "github_issues": ("github_issue", True),
    "github_prs": ("github_pr", True),
    "core_incidents": ("core_incident", False),
    "support_cases": ("support_case", False),
}

# Edge columns a mapping names that the statements never read: the two ends'
# references ('Org/repo#number'), from which loom derives from_id / to_id.
EDGE_REFS: dict[str, str] = {"from_ref": "String", "to_ref": "String"}


def mappable_columns(table: str) -> dict[str, str]:
    """The columns a person maps for `table` (derived ids left out, an edge's
    end references added)."""
    cols = {c: f for c, f in COLUMNS_READ.get(table, {}).items()}
    if table in DERIVED_IDS:
        cols.pop("id", None)
    if table == "edges":
        cols.pop("from_id", None)
        cols.pop("to_id", None)
        cols = {**EDGE_REFS, **cols}
    return cols
