"""The runner lets aggregating tables keep plain columns outside their sort
key on servers that check it, and changes nothing anywhere else."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from server.core.schema import migrate as m
from server.core.schema.journal import split_statements
from server.core.schema.migrate import DIMENSIONS_SETTING, dimensions_rewrite

AGG = """CREATE TABLE IF NOT EXISTS loom.chunk_term_freq
(
    `org` LowCardinality(String),
    `df_state` AggregateFunction(uniqExact, String),
    `df_added_at` DateTime DEFAULT now()
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(df_added_at)
ORDER BY (org)
SETTINGS index_granularity = 8192"""


def test_an_aggregating_create_gains_the_setting_first_among_its_settings() -> None:
    out = dimensions_rewrite(AGG)
    assert out == AGG.replace(
        "SETTINGS index_granularity", f"SETTINGS {DIMENSIONS_SETTING} = 1, index_granularity"
    )
    assert dimensions_rewrite(out) == out


def test_summing_and_replicated_engines_and_a_create_without_settings() -> None:
    summing = AGG.replace("AggregatingMergeTree", "SummingMergeTree").replace(
        "\nSETTINGS index_granularity = 8192", ""
    )
    assert dimensions_rewrite(summing).endswith(f"\nSETTINGS {DIMENSIONS_SETTING} = 1")
    shared = AGG.replace("AggregatingMergeTree", "SharedAggregatingMergeTree('/p', '{replica}')")
    assert DIMENSIONS_SETTING in dimensions_rewrite(shared)


def test_every_other_statement_is_untouched() -> None:
    for stmt in (
        AGG.replace("AggregatingMergeTree", "ReplacingMergeTree(version)"),
        "ALTER TABLE loom.chunk_term_freq ADD COLUMN IF NOT EXISTS `x` UInt8 DEFAULT 0",
        "ALTER TABLE loom.search_trace_log ADD COLUMN IF NOT EXISTS `actor` String DEFAULT ''",
        "CREATE MATERIALIZED VIEW IF NOT EXISTS loom.chunk_term_freq_mv TO loom.chunk_term_freq "
        "AS SELECT org FROM loom.chunks",
        "ALTER TABLE loom.chunk_term_freq MODIFY SETTING index_granularity = 1024",
    ):
        assert dimensions_rewrite(stmt) == stmt


def test_the_baseline_rewrites_exactly_its_aggregating_tables() -> None:
    root = Path(__file__).resolve().parents[2] / "schemas" / "migrations"
    stmts = [s for f in sorted(root.glob("*.sql")) for s in split_statements(f.read_text())]
    changed = [s for s in stmts if dimensions_rewrite(s) != s]
    engines = [s for s in stmts if "AggregatingMergeTree" in s or "SummingMergeTree" in s]
    assert changed and len(changed) == len(
        [s for s in engines if s.lstrip().startswith("CREATE TABLE")]
    )


class _CH:
    def __init__(self, count: int | None) -> None:
        self.count = count

    def query(self, sql: str) -> Any:
        if self.count is None:
            raise RuntimeError("no such table")
        assert "system.merge_tree_settings" in sql and DIMENSIONS_SETTING in sql
        return type("R", (), {"result_rows": [(self.count,)]})()


def test_the_probe_is_false_unless_the_server_knows_the_setting() -> None:
    assert m._dimensions_setting_known(_CH(1)) is True  # type: ignore[arg-type]
    assert m._dimensions_setting_known(_CH(0)) is False  # type: ignore[arg-type]
    assert m._dimensions_setting_known(_CH(None)) is False  # type: ignore[arg-type]
