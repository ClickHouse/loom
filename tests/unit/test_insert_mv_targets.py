"""An insert-time materialized view writes each inserted block into its `TO`
table by column name. A column the SELECT produces that the target lacks fails
every insert into the source table; a target column the SELECT leaves out and
that has no DEFAULT gets the type's zero value, silently. The dump records
each view's output columns, so both halves are checked here, CH-free."""

from __future__ import annotations

import re

import pytest

from tests.unit.schema_ddl import columns, ddl, views

# Every insert-time view in the dump. A new one is added here, and so reviewed.
INSERT_VIEWS = {
    "chunk_context_term_freq_mv": "chunk_term_freq",
    "chunk_term_freq_mv": "chunk_term_freq",
    "code_doc_stats_mv": "code_doc_stats",
    "code_symbol_ids_mv": "code_symbol_ids",
    "code_symbol_names_mv": "code_symbol_names",
    "code_term_freq_mv": "code_term_freq",
    "entity_degree_mv": "entity_degree",
    "memory_code_link_queue_mv": "memory_code_link_queue",
    "memory_record_versions_mv": "memory_record_versions",
    "memory_usage_totals_mv": "memory_usage_totals",
    "memory_versions_mv": "memory_versions",
    "session_brief_revisions_mv": "session_brief_revisions",
}

_TO = re.compile(r"^CREATE MATERIALIZED VIEW IF NOT EXISTS loom\.\w+ TO loom\.(\w+)$", re.M)
_FILLED = re.compile(r"\b(?:DEFAULT|MATERIALIZED|ALIAS|EPHEMERAL)\b")


def test_the_insert_views_are_the_ones_listed() -> None:
    found = {}
    for view in views():
        m = _TO.search(ddl(view))
        if m:
            found[view] = m.group(1)
    assert found == INSERT_VIEWS


@pytest.mark.parametrize(("view", "target"), sorted(INSERT_VIEWS.items()))
def test_a_view_writes_only_columns_its_target_has(view: str, target: str) -> None:
    extra = sorted(set(columns(view)) - set(columns(target)))
    assert not extra, f"loom.{view} produces {extra}, which loom.{target} lacks"


@pytest.mark.parametrize(("view", "target"), sorted(INSERT_VIEWS.items()))
def test_a_view_fills_every_target_column_without_a_default(view: str, target: str) -> None:
    produced = set(columns(view))
    unfilled = sorted(
        c for c, spec in columns(target).items() if c not in produced and not _FILLED.search(spec)
    )
    assert not unfilled, f"loom.{view} leaves {unfilled} of loom.{target} at the type's zero"
