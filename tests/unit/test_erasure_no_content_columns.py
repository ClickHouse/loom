"""A table classified NO_MEMORY_CONTENT carries no column shaped like memory text.

The erasure saga skips every table in `census.NO_MEMORY_CONTENT`. If a
later migration adds a `value`, `content` or `*_text` column to one of them,
that column is erasure residue nobody counts. The classification test only
checks that each table has a reason, so this one reads the columns. A
content-shaped column on a skipped table is either named below, with why it
holds no subject data, or the table moves to the census. CH-free.
"""

from __future__ import annotations

import re

from server.erasure.store import census as erasure_census
from tests.unit.schema_ddl import column_names

_CONTENT_SHAPED = re.compile(
    r"^(value|value_json|content|text|query|query_text|excerpt|summary|prompt|answer|"
    r"question|snippet|body|message|messages|payload|transcript|evidence|reason|detail|"
    r"details|note|notes)$"
    r"|_(text|excerpt|content|body|value|summary|prompt|answer|query|payload)$"
)

# table -> {column: why it is not subject content}
_REVIEWED: dict[str, dict[str, str]] = {
    "loom.blacklist": {"reason": "the admin's reason for a refusal pattern"},
    "loom.org_model_calls": {"reason": "the org admin's note on switching model calls off"},
    "loom.runtime_overrides": {"value": "a rate limit or latency budget as JSON numbers"},
    "loom.frontend_assets": {"body": "the dashboard's own built JS, CSS and fonts"},
    "loom.categories": {"summary": "operator-authored taxonomy text"},
    "loom.code_backport_gaps": {"reason": "a backport verdict over public PRs"},
    "loom.deployment_config": {
        "value": "the root-secret fingerprint and MUST_MATCH values; no secrets"
    },
    "loom.erasure_requests": {"scope_value": "the erasure ledger naming its own scope"},
    "loom.erasure_reversals": {"scope_value": "the reversal ledger naming its own scope"},
    "loom.oauth_signin_state": {
        "value": "sign-in state: who signs in, for which client; dropped once it expires"
    },
    "loom.policy_decisions": {"detail": "rule ids and error codes, no memory text"},
    "loom.qa_link_candidates": {"bm25_body": "titles of the public tracker record"},
    "loom.revoked_tokens": {"reason": "why a token was revoked"},
    "loom.worker_status": {"detail": "a bounded worker error text"},
}


def _content_columns(table: str) -> set[str]:
    name = table.removeprefix("loom.")
    return {c for c in column_names(name, materialized=False) if _CONTENT_SHAPED.search(c)}


def test_a_skipped_table_holds_no_unreviewed_content_column() -> None:
    unreviewed = {
        table: sorted(cols - set(_REVIEWED.get(table, {})))
        for table in erasure_census.NO_MEMORY_CONTENT
        if (cols := _content_columns(table)) - set(_REVIEWED.get(table, {}))
    }
    assert not unreviewed, (
        "NO_MEMORY_CONTENT tables with content-shaped columns: either the column holds "
        f"no subject data (add it to _REVIEWED with why) or the table joins the census: "
        f"{unreviewed}"
    )


def test_the_review_names_only_skipped_tables_and_real_columns() -> None:
    for table, cols in _REVIEWED.items():
        assert table in erasure_census.NO_MEMORY_CONTENT, (
            f"{table} is no longer skipped; drop its review"
        )
        stale = set(cols) - _content_columns(table)
        assert not stale, f"{table} no longer has {sorted(stale)}"


def test_the_shape_matches_the_columns_it_exists_for() -> None:
    for col in ("value", "content", "query_text", "content_excerpt", "response_body"):
        assert _CONTENT_SHAPED.search(col), col
    for col in ("memory_key", "namespace", "version", "created_at", "hit_count"):
        assert not _CONTENT_SHAPED.search(col), col
