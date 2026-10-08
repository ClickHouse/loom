"""A first mapping from a team's tables onto loom's logical model.

Given the tables a read-only credential can see (`onboarding._inspect_tables`)
this proposes, without any knowledge of the team: which table holds the items
(issues, tickets, bugs, incidents…), which the pull requests, where the
links between them come from, and for each logical column the team's column
that most likely holds it — by name (a synonym table, generic, not any one
deployment's spelling) and by type family. The person confirms or changes
every field on the page; nothing here is saved without them.

A database that already exposes loom's logical tables (views named
`github_issues`, `github_prs`, `edges` with the contract's columns) needs no
mapping: the proposal says `mode = views`.
"""

from __future__ import annotations

import re
from typing import Any

from server.tracker.domain.contract import REQUIRED

# Logical column → the names a team's column is likely to have, best first.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "number": (
        "number",
        "issue_number",
        "issue_id",
        "ticket_id",
        "ticket_number",
        "num",
        "iid",
        "key_number",
        "id",
    ),
    "pr_number": (
        "pr_number",
        "pr_id",
        "pull_request_number",
        "pull_request_id",
        "merge_request_id",
        "mr_id",
        "number",
        "iid",
        "id",
    ),
    "title": ("title", "summary", "subject", "name", "headline", "short_description"),
    "body": ("body", "description", "details", "content", "text", "message", "long_description"),
    "created_at": (
        "created_at",
        "created",
        "opened_at",
        "creation_date",
        "created_on",
        "reported_at",
        "submitted_at",
        "inserted_at",
        "timestamp",
        "date",
    ),
    "updated_at": ("updated_at", "updated", "modified_at", "last_modified", "updated_on"),
    "closed_at": (
        "closed_at",
        "resolved_at",
        "closed",
        "resolved",
        "resolution_date",
        "done_at",
        "completed_at",
        "closed_on",
        "resolved_on",
    ),
    "merged_at": ("merged_at", "merge_date", "merged_on"),
    "author": (
        "author",
        "reporter",
        "created_by",
        "opened_by",
        "submitter",
        "user",
        "login",
        "owner",
        "creator",
    ),
    "labels": ("labels", "tags", "label", "tag"),
    "assignees": ("assignees", "assignee", "owners"),
    "url": ("url", "html_url", "web_url", "link", "permalink", "href"),
    "primary_component": (
        "primary_component",
        "component",
        "area",
        "module",
        "team",
        "category",
        "project_component",
    ),
    "severity_level": ("severity_level", "severity", "priority", "sev", "urgency"),
    "state": ("state", "status"),
    "state_reason": ("state_reason", "resolution", "close_reason", "resolution_type"),
    "is_merged": ("is_merged", "merged"),
    "base_branch": ("base_branch", "base", "target_branch", "base_ref"),
    "additions": ("additions", "lines_added", "insertions"),
    "deletions": ("deletions", "lines_deleted", "lines_removed"),
    "diff": ("diff", "patch"),
    "affected_file_paths": ("affected_file_paths", "files", "changed_files", "file_paths", "paths"),
    "affected_versions": ("affected_versions", "versions", "affects_versions", "version"),
    "fix_versions": ("fix_versions", "fixed_in", "fix_version", "fixed_versions"),
    "stack_functions": ("stack_functions", "stack", "frames", "stack_trace_functions"),
    "error_code": ("error_code", "code", "errno"),
}
_REPO_COLUMNS = ("repo", "repository", "repo_name", "full_name", "project", "repo_full_name")
_REF_COLUMNS = ("source_id", "ref", "reference", "issue_ref", "key")
_KEY_COLUMNS = (
    "key",
    "issue_key",
    "ticket_key",
    "ticket",
    "ticket_id",
    "issue_id",
    "id",
    "reference",
    "ref",
)
_FIXES_COLUMNS = (
    "fixes_issue",
    "fixes",
    "closes",
    "closes_issue",
    "issue_number",
    "issue_id",
    "linked_issue",
    "resolves",
)

_ROLE_NAMES: dict[str, tuple[str, ...]] = {
    "github_issues": (
        "issue",
        "ticket",
        "bug",
        "defect",
        "item",
        "task",
        "story",
        "incident",
        "problem",
        "case",
    ),
    "github_prs": (
        "pull_request",
        "pullrequest",
        "pr",
        "merge_request",
        "mr",
        "change",
        "patch",
        "review",
    ),
    "edges": ("link", "edge", "relation", "relationship", "issue_link"),
}
_LOGICAL = ("github_issues", "github_prs", "edges")


def _family(ch_type: str) -> str:
    from server.tracker.domain.mapping import family_of_type

    return family_of_type(ch_type)


def _cols(table: dict[str, Any]) -> dict[str, str]:
    return {str(c["name"]): str(c["type"]) for c in table.get("columns") or []}


def _compatible(want: str, have: str) -> bool:
    """Whether a column of family `have` can hold a logical column of family
    `want` — the renderer converts between scalars, never array ↔ scalar."""
    if not have:
        return False
    if want.startswith("Array(") or have.startswith("Array("):
        return want.startswith("Array(") == have.startswith("Array(")
    if want == "DateTime":
        return have in ("DateTime", "String")
    if want in ("UInt", "Int", "Float"):
        return have in ("UInt", "Int", "Float")
    return True


def _pick(logical: str, want: str, cols: dict[str, str], taken: set[str]) -> tuple[str, str] | None:
    """(column, confidence) for a logical column, or None."""
    for i, name in enumerate(SYNONYMS.get(logical, (logical,))):
        for col, typ in cols.items():
            if col in taken or col.lower() != name:
                continue
            if _compatible(want, _family(typ)):
                return col, "high" if i == 0 else "medium"
    return None


def _score_table(role: str, table: dict[str, Any]) -> int:
    name = str(table.get("table") or "").lower()
    cols = {c.lower() for c in _cols(table)}
    score = 0
    for i, hint in enumerate(_ROLE_NAMES[role]):
        if re.search(rf"(^|_){re.escape(hint)}s?($|_)", name):
            score += 10 - min(i, 5)
            break
    if role == "github_issues":
        score += 3 * bool(cols & set(SYNONYMS["title"]))
        score += 2 * bool(cols & set(SYNONYMS["created_at"]))
    if role == "github_prs":
        score += 3 * bool(cols & {"merged", "is_merged", "merged_at"})
        score += bool(cols & set(SYNONYMS["title"]))
    if role == "edges":
        score += 4 * bool({"from", "from_id", "source"} & cols and {"to", "to_id", "target"} & cols)
    return score


def looks_like_views(tables: list[dict[str, Any]]) -> bool:
    """The database already exposes loom's required logical tables."""
    names = {str(t.get("table")) for t in tables}
    return all(t in names for t in REQUIRED)


def _source_id(
    cols: dict[str, str], number: str | None, repos: list[str]
) -> tuple[Any, str] | None:
    for name in _REF_COLUMNS:
        for col, typ in cols.items():
            if col.lower() == name and _family(typ) == "String":
                return col, "medium"
    if number is None:
        return None
    repo_col = next((c for c in cols if c.lower() in _REPO_COLUMNS), None)
    if repo_col is not None:
        return {"concat": [repo_col, {"lit": "#"}, number]}, "high"
    if len(repos) == 1:
        return {"concat": [{"lit": f"{repos[0]}#"}, number]}, "medium"
    return None


def _map_items(role: str, table: dict[str, Any], repos: list[str]) -> dict[str, Any]:
    from server.tracker.domain.columns import mappable_columns

    cols = _cols(table)
    fields: dict[str, dict[str, Any]] = {}
    taken: set[str] = set()
    num_key = "number" if role == "github_issues" else "pr_number"
    for logical, want in mappable_columns(role).items():
        if logical in ("source_id", "is_private"):
            continue
        hit = _pick(logical, want, cols, taken if logical != num_key else set())
        if hit is None:
            continue
        col, conf = hit
        taken.add(col)
        fields[logical] = {"from": col, "confidence": conf}
    if role == "github_prs" and "number" not in fields and num_key in fields:
        fields["number"] = dict(fields[num_key])
    if role == "github_prs" and "is_merged" not in fields:
        state = next((c for c in cols if c.lower() in ("state", "status")), None)
        if state is not None:
            fields["is_merged"] = {"from": {"eq": [state, {"lit": "merged"}]}, "confidence": "low"}
    if num_key not in fields:
        # a key like 'HD-7' / 'PROJ-1234' and no numeric id: its trailing number
        key = next(
            (c for c in cols if c.lower() in _KEY_COLUMNS and _family(cols[c]) == "String"), None
        )
        if key is not None:
            fields[num_key] = {"from": {"digits": key}, "confidence": "medium"}
            if role == "github_prs":
                fields["number"] = dict(fields[num_key])
    num = fields.get(num_key, {}).get("from")
    sid = _source_id(cols, num if isinstance(num, str) else None, repos)
    if sid is None and isinstance(num, dict) and len(repos) == 1:
        sid = ({"concat": [{"lit": f"{repos[0]}#"}, num]}, "medium")
    if sid is not None:
        fields["source_id"] = {"from": sid[0], "confidence": sid[1]}
    return fields


def _links_from_prs(
    prs: dict[str, Any], pr_fields: dict[str, Any], repos: list[str]
) -> dict[str, Any] | None:
    """`edges` derived from a PR table's "fixes issue N" column, when it has one."""
    cols = _cols(prs)
    fixes = next(
        (
            c
            for c in cols
            if c.lower() in _FIXES_COLUMNS and _family(cols[c]) in ("UInt", "Int", "String")
        ),
        None,
    )
    pr_ref = pr_fields.get("source_id", {}).get("from")
    if fixes is None or pr_ref is None:
        return None
    repo_col = next((c for c in cols if c.lower() in _REPO_COLUMNS), None)
    if repo_col is not None:
        to_ref: Any = {"concat": [repo_col, {"lit": "#"}, fixes]}
    elif len(repos) == 1:
        to_ref = {"concat": [{"lit": f"{repos[0]}#"}, fixes]}
    else:
        return None
    created = pr_fields.get("created_at", {}).get("from")
    fields: dict[str, Any] = {
        "from_ref": {"from": pr_ref, "confidence": "medium"},
        "to_ref": {"from": to_ref, "confidence": "medium"},
        "from_type": {"from": {"lit": "github_pr"}, "confidence": "high"},
        "to_type": {"from": {"lit": "github_issue"}, "confidence": "high"},
        "rel_type": {"from": {"lit": "fixes"}, "confidence": "medium"},
    }
    if created is not None:
        fields["created_at"] = {"from": created, "confidence": "medium"}
    return {"table": prs["table"], "fields": fields, "derived_from": f"{prs['table']}.{fixes}"}


def propose(tables: list[dict[str, Any]], *, database: str, repos: list[str]) -> dict[str, Any]:
    """The first mapping for the page: roles, fields with confidence, and the
    mapping itself (`mapping.SourceMapping` JSON) ready to check."""
    from server.tracker.domain.mapping import final_for_engine

    if looks_like_views(tables):
        return {"mode": "views", "roles": {}, "fields": {}, "mapping": None}
    roles: dict[str, Any] = {}
    fields: dict[str, Any] = {}
    used: set[str] = set()
    for role in _LOGICAL:
        ranked = sorted(
            (t for t in tables if t["table"] not in used),
            key=lambda t: (-_score_table(role, t), t["table"]),
        )
        best = ranked[0] if ranked and _score_table(role, ranked[0]) >= 5 else None
        if role == "edges" and best is None:
            prs = next((t for t in tables if t["table"] == roles.get("github_prs")), None)
            derived = _links_from_prs(prs, fields.get("github_prs", {}), repos) if prs else None
            if derived is not None:
                roles["edges"] = derived["table"]
                fields["edges"] = derived["fields"]
                roles["edges_derived_from"] = derived["derived_from"]
            continue
        if best is None:
            continue
        roles[role] = best["table"]
        used.add(best["table"])
        if role in ("github_issues", "github_prs"):
            fields[role] = _map_items(role, best, repos)
    engines = {t["table"]: str(t.get("engine") or "") for t in tables}
    mapping = {
        "version": 1,
        "tables": {
            role: {
                "source": f"{database}.{roles[role]}",
                "final": final_for_engine(engines.get(roles[role], "")),
                "columns": {col: spec["from"] for col, spec in fields.get(role, {}).items()},
            }
            for role in _LOGICAL
            if role in roles
        },
    }
    return {"mode": "mapping", "roles": roles, "fields": fields, "mapping": mapping}
