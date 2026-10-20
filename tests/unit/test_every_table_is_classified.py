"""Every `loom.*` and `loom_telemetry.*` table is a census row, a documented
step-only table or a NO_MEMORY_CONTENT entry with a reason. A CREATE TABLE
in schema.sql that is in none of the three lists fails
here, so a new table is classified the day it lands. CH-free."""

from __future__ import annotations

import re

from server.erasure.store import census as erasure_census
from tests.unit.schema_ddl import SCHEMA, engine, tables

_TELEMETRY_TABLE = re.compile(r"^CREATE TABLE IF NOT EXISTS (loom_telemetry\.\w+)", re.M)


def _schema_tables() -> set[str]:
    telemetry = set(_TELEMETRY_TABLE.findall(SCHEMA.read_text(encoding="utf-8")))
    return {f"loom.{name}" for name in tables()} | telemetry


def test_schema_has_the_expected_shape() -> None:
    tables = _schema_tables()
    assert len(tables) >= 115, len(tables)
    assert "loom.memory" in tables and "loom.search_trace_log" in tables


def test_every_schema_table_is_classified() -> None:
    classified = erasure_census.classified_tables()
    missing = sorted(_schema_tables() - set(classified))
    assert not missing, (
        "tables in schemas/schema.sql that are in neither _CENSUS, STEP_ONLY_TABLES nor "
        f"NO_MEMORY_CONTENT (server/erasure/store/census.py): {missing}"
    )


def test_the_registry_names_only_tables_that_exist() -> None:
    """No rot: a renamed or dropped table leaves the lists the same day."""
    tables = _schema_tables()
    assert not {s.table for s in erasure_census._CENSUS} - tables
    assert not set(sum(erasure_census.STEP_ONLY_TABLES.values(), ())) - tables
    assert not set(erasure_census.NO_MEMORY_CONTENT) - tables


def test_the_three_lists_are_disjoint_and_every_reason_is_written() -> None:
    census = {s.table for s in erasure_census._CENSUS}
    step_only = set(sum(erasure_census.STEP_ONLY_TABLES.values(), ()))
    no_content = set(erasure_census.NO_MEMORY_CONTENT)
    assert not census & no_content, census & no_content
    assert not step_only & no_content, step_only & no_content
    for table, reason in erasure_census.NO_MEMORY_CONTENT.items():
        assert table.startswith(("loom.", "loom_telemetry.")), table
        assert len(reason.strip()) >= 20, (table, reason)


def test_the_review_moved_the_named_tables_into_the_census() -> None:
    """These tables are census rows with an owner step in the namespace
    scope; the user scope reaches the ones with a user axis."""
    ns = set(erasure_census.census_tables("namespace"))
    user = set(erasure_census.census_tables("user_id"))
    for t in (
        "loom.conversations",
        "loom.context_blobs",
        "loom.memory_exposures",
        "loom.search_trace_log",
        "loom.memory_drops",
        "loom.learnings_queue",
        "loom.events",
        "loom.code_search_trace",
    ):
        assert t in ns, t
    for t in (
        "loom.conversations",
        "loom.context_blobs",
        "loom.memory_exposures",
        "loom.events",
        "loom.code_search_trace",
        # Through the candidates' supporter_users.
        "loom.learnings_queue",
    ):
        assert t in user, t
    assert "loom.search_trace_log" not in user  # no user axis
    # namespace_snapshots stays out: it holds counts and a namespace name, never the payload
    assert "loom.namespace_snapshots" in erasure_census.NO_MEMORY_CONTENT
    # embedding_cache is deleted at both scopes but not counted: traffic refills it
    assert "loom.embedding_cache" not in ns and "loom.embedding_cache" not in user
    assert erasure_census.STEP_ONLY_TABLES["idempotency.delete"] == ("loom.embedding_cache",)


def test_a_plain_mergetree_with_a_live_predicate_is_marked_final_false() -> None:
    for sub in erasure_census._CENSUS:
        if sub.live == "1":
            continue
        kind = engine(sub.table.split(".", 1)[1])
        assert (kind == "MergeTree") == (not sub.final), (sub.table, kind, sub.final)
