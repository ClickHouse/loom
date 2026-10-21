"""Every repository's full-row column set is the table's column set in schema.sql.

The builders and inserts of these repositories refuse a row that misses one of
their columns, and a version row that omits a persisted column lands its
DEFAULT under FINAL. That check only compares a row with the repository's own
list; this one keeps the list equal to the table, so a migration that adds a
column fails here until the repository carries it.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from tests.unit.schema_ddl import column_names

# (repository module, constant, table, columns the DDL fills and no writer sends)
CASES: list[tuple[str, str, str, frozenset[str]]] = [
    (
        "server.tracker.store.daily",
        "TRACKER_TEST_DAILY_COLS",
        "loom.qa_test_daily",
        frozenset(),
    ),
    (
        "server.tracker.store.daily",
        "TRACKER_FAIL_HOURS_COLS",
        "loom.qa_fail_hours",
        frozenset(),
    ),
    (
        "server.tracker.store.daily",
        "TRACKER_ROLLUP_DAYS_COLS",
        "loom.qa_rollup_days",
        frozenset(),
    ),
    ("server.memory.store.code_links", "LINK_COLS", "loom.memory_code_links", frozenset()),
    (
        "server.memory.app.extraction_queue",
        "COLUMNS",
        "loom.memory_extraction_queue",
        frozenset(),
    ),
    ("server.tasks.store.repo", "INBOX_COLS", "loom.task_inboxes", frozenset()),
    ("server.tasks.store.repo", "POLICY_COLS", "loom.task_poster_policies", frozenset()),
    ("server.tasks.store.repo", "TASK_COLS", "loom.tasks", frozenset()),
    ("server.tasks.store.repo", "EVENT_COLS", "loom.task_events", frozenset()),
    ("server.tasks.store.repo", "WEBHOOK_CURSOR_COLS", "loom.task_webhook_cursor", frozenset()),
    (
        "server.tracker.store.embed",
        "TRACKER_EMBED_COLS",
        "loom.qa_item_embeddings",
        frozenset(),
    ),
    (
        "server.tracker.store.links",
        "TRACKER_LINK_COLS",
        "loom.qa_link_candidates",
        frozenset(),
    ),
    (
        "server.code.store.backport_gaps",
        "GAP_COLS",
        "loom.code_backport_gaps",
        frozenset(),
    ),
    (
        "server.code.store.index.rows",
        "CODE_COVERAGE_FILE_COLS",
        "loom.code_coverage_files",
        frozenset(),
    ),
    (
        "server.code.store.coverage_watches",
        "WATCH_COLS",
        "loom.code_coverage_watches",
        frozenset(),
    ),
    # `version` is stamped by the DDL (now64 nanoseconds), never by a writer.
    (
        "server.identity.store.policy",
        "_RULE_COLUMNS",
        "loom.namespace_policies",
        frozenset({"version"}),
    ),
]


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [c.strip() for c in value.split(",")]
    return list(value)


@pytest.mark.parametrize(("module", "const", "table", "ddl_only"), CASES)
def test_the_column_set_is_the_tables(
    module: str, const: str, table: str, ddl_only: frozenset[str]
) -> None:
    cols = _names(getattr(importlib.import_module(module), const))
    assert len(cols) == len(set(cols)), f"{const} repeats a column"
    schema = set(column_names(table.removeprefix("loom."), materialized=False))
    assert set(cols) == schema - ddl_only, set(cols) ^ (schema - ddl_only)
