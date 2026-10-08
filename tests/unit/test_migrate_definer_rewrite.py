"""A view's DEFINER in a migration names whoever dumped the schema; the runner
creates the view under the user that runs the migration instead, so a server
without that user (ClickHouse Cloud's `default`) accepts it."""

from __future__ import annotations

from pathlib import Path

from server.core.schema.migrate import definer_rewrite

MIGRATIONS = Path(__file__).resolve().parents[2] / "schemas" / "migrations"


def test_a_named_definer_becomes_the_current_user() -> None:
    stmt = "CREATE MATERIALIZED VIEW v REFRESH EVERY 1 HOUR TO loom.t (a UInt8) DEFINER = loom SQL SECURITY DEFINER AS SELECT 1"
    assert definer_rewrite(stmt) == stmt.replace("DEFINER = loom", "DEFINER = CURRENT_USER")
    assert "CURRENT_USER" in definer_rewrite("... DEFINER = `svc-user` SQL SECURITY DEFINER ...")


def test_the_words_outside_the_definer_clause_are_left_alone() -> None:
    stmt = "CREATE TABLE t (a UInt8 COMMENT 'DEFINER = x') ENGINE = MergeTree ORDER BY a"
    assert definer_rewrite(stmt) == stmt
    quoted = '... DEFINER = "svc" SQL SECURITY DEFINER ...'
    assert definer_rewrite(quoted) == quoted  # not ours to guess: fails as UNKNOWN_USER


def test_statements_without_a_definer_are_untouched() -> None:
    for stmt in (
        "CREATE TABLE t (definer_id UInt8) ENGINE = MergeTree ORDER BY definer_id",
        "... DEFINER = CURRENT_USER SQL SECURITY DEFINER ...",
    ):
        assert definer_rewrite(stmt) == stmt


def test_every_view_definer_in_the_journal_is_rewritten() -> None:
    for f in sorted(MIGRATIONS.glob("*.sql")):
        out = definer_rewrite(f.read_text())
        assert "DEFINER = loom" not in out, f.name
