"""`-- loom: rebuild-if-empty <table>`: a migration declares a sort or
partition key, the runner rebuilds an empty table in place and refuses one
with rows, and the operator tool reads the same target statement."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from server.core.schema import migrate as _migrate
from server.core.schema import table_rebuild as tr
from server.core.schema.journal import split_statements

TARGET = """CREATE TABLE loom.t
(
    `k` String,
    `path` String,
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (k, path)
SETTINGS index_granularity = 1024"""

FILE = f"-- why\n-- loom: rebuild-if-empty t\n\n{TARGET};\n"


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class _CH:
    """Answers the few system-table reads the runner makes; records commands."""

    def __init__(self, tables: dict[str, tuple[str, str, int]]) -> None:
        self.tables = dict(tables)  # name -> (sorting key, partition key, rows)
        self.commands: list[str] = []
        self.race_rows = 0

    def query(
        self, sql: str, parameters: dict[str, Any] | None = None, settings: Any = None
    ) -> _Result:
        name = (parameters or {}).get("t")
        if "system.tables" in sql:
            t = self.tables.get(str(name))
            return _Result([(t[0], t[1])] if t else [])
        if "system.columns" in sql:
            return _Result([("k", "String", "", "", ""), ("version", "UInt64", "", "", "")])
        table = sql.split("FROM loom.", 1)[1].split()[0]
        return _Result([(self.tables[table][2],)])

    def command(self, stmt: str, settings: Any = None) -> None:
        self.commands.append(" ".join(stmt.split()))
        if stmt.startswith("CREATE TABLE loom.t_rebuild"):
            self.tables["t_rebuild"] = ("k, path", "", 0)
        elif stmt.startswith("EXCHANGE TABLES"):
            self.tables["t"], self.tables["t_rebuild"] = self.tables["t_rebuild"], self.tables["t"]
            if self.race_rows:
                old = self.tables["t_rebuild"]
                self.tables["t_rebuild"] = (old[0], old[1], self.race_rows)
        elif stmt.startswith("DROP TABLE loom.t_rebuild"):
            self.tables.pop("t_rebuild")


def test_the_directive_and_its_target_statement_are_parsed() -> None:
    assert tr.declared_tables(FILE) == ["t"]
    assert tr.declared_tables(
        "--loom:rebuild-if-empty a -- why\n-- loom: rebuild-if-empty b\n"
    ) == [
        "a",
        "b",
    ]
    assert tr.declared_tables("-- rebuild-if-empty a without the prefix\n") == []
    assert tr.target_table(TARGET) == "t"
    assert tr.target_table("CREATE TABLE IF NOT EXISTS loom.t (k String)") is None
    assert tr.targets_in(FILE, split_statements(FILE)) == {TARGET: "t"}


@pytest.mark.parametrize(
    ("clauses", "sorting", "partition"),
    [
        ("ORDER BY (org, namespace, path)", "org, namespace, path", ""),
        (
            "PARTITION BY toYYYYMM(merged_at)\nORDER BY (org, `namespace`)",
            "org, namespace",
            "toYYYYMM(merged_at)",
        ),
        ("PARTITION BY tuple()\nORDER BY k", "k", ""),
        (
            "PARTITION BY (org, namespace)\nORDER BY (org, cityHash64(a, b))",
            "org, cityHash64(a, b)",
            "org, namespace",
        ),
        ("ORDER BY (a) + (b)", "(a) + (b)", ""),
    ],
)
def test_layouts_read_the_way_system_tables_prints_them(
    clauses: str, sorting: str, partition: str
) -> None:
    stmt = (
        f"CREATE TABLE loom.t\n(\n    `k` String\n)\nENGINE = MergeTree\n{clauses}\nSETTINGS x = 1"
    )
    assert tr.layout_of(stmt) == tr.Layout(sorting, partition)


def test_the_twin_statement_renames_only_the_table() -> None:
    twin = tr.twin_statement(TARGET, "t", "t_rebuild")
    assert twin.startswith("CREATE TABLE loom.t_rebuild\n") and twin.count("loom.") == 1
    with pytest.raises(ValueError):
        tr.twin_statement(TARGET, "other", "other_rebuild")


REBUILD_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rebuild_migrations"


def test_a_declaring_file_names_one_target_per_rebuilt_table() -> None:
    for table in ("code_test_facets", "code_pr_reviews", "code_pr_history"):
        name, stmt = tr.find_target(table, REBUILD_FIXTURE)
        assert name == "008_sort_keys.sql" and tr.target_table(stmt) == table
    assert tr.layout_of(tr.find_target("code_test_facets", REBUILD_FIXTURE)[1]) == tr.Layout(
        "org, namespace, path, facet_kind, name, ordinal", ""
    )
    assert tr.layout_of(tr.find_target("code_pr_history", REBUILD_FIXTURE)[1]).partition_key == ""
    with pytest.raises(LookupError):
        tr.find_target("memory", REBUILD_FIXTURE)


def test_the_repo_declares_no_rebuild_the_baseline_has_not_folded_in() -> None:
    # A database whose ledger holds the baseline has every target layout.
    with pytest.raises(LookupError):
        tr.find_target("code_test_facets")


def test_a_table_on_the_target_layout_is_left_alone() -> None:
    ch = _CH({"t": ("k, path", "", 5)})
    assert tr.preflight(ch, FILE) == {"t": ("matches", 0)}
    tr.apply(ch, "t", TARGET)
    assert ch.commands == []


def test_an_empty_table_is_exchanged_for_the_target_and_dropped() -> None:
    ch = _CH({"t": ("path, k", "toYYYYMM(x)", 0)})
    tr.apply(ch, "t", TARGET)
    heads = [c.split(" (")[0] for c in ch.commands]
    assert heads == [
        "CREATE TABLE loom.t_rebuild",
        "EXCHANGE TABLES loom.t AND loom.t_rebuild",
        "DROP TABLE loom.t_rebuild SYNC",
    ]
    assert ch.tables == {"t": ("k, path", "", 0)}


def test_a_write_that_races_the_exchange_is_moved_over() -> None:
    ch = _CH({"t": ("path, k", "", 0)})
    ch.race_rows = 2
    tr.apply(ch, "t", TARGET)
    assert (
        "INSERT INTO loom.t (`k`, `version`) SELECT `k`, `version` FROM loom.t_rebuild"
        in ch.commands
    )


def test_a_table_with_rows_fails_before_any_change_and_names_the_tool() -> None:
    ch = _CH({"t": ("path, k", "", 7)})
    with pytest.raises(
        tr.RebuildBlocked,
        match=r"loom.t \(7 rows\).*scripts/ops/table_rebuild.py.*--dry-run`, then .*`--yes`",
    ):
        tr.preflight(ch, FILE)
    with pytest.raises(tr.RebuildBlocked, match=r"--dry-run, then --yes"):
        tr.apply(ch, "t", TARGET)
    assert ch.commands == []


def test_a_twin_with_rows_is_an_operator_run_in_progress() -> None:
    ch = _CH({"t": ("path, k", "", 0), "t_rebuild": ("k, path", "", 3)})
    with pytest.raises(tr.RebuildBlocked, match="in progress"):
        tr.apply(ch, "t", TARGET)
    assert ch.commands == []


def test_a_directive_without_its_target_statement_is_an_error() -> None:
    with pytest.raises(ValueError, match="without a"):
        tr.preflight(_CH({}), "-- loom: rebuild-if-empty t\nSELECT 1;\n")


def test_the_runner_routes_the_target_through_the_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "999_keys.sql"
    path.write_text(FILE)
    ch = _CH({"t": ("path, k", "", 0)})
    _migrate._apply_file(ch, path, dry=False)
    assert "CREATE TABLE loom.t (" not in " ".join(ch.commands)
    assert ch.tables["t"][0] == "k, path"


def test_a_dry_run_reports_a_blocked_rebuild_as_a_failure(tmp_path: Path) -> None:
    path = tmp_path / "999_keys.sql"
    path.write_text(FILE)
    ch = _CH({"t": ("path, k", "", 9)})
    with pytest.raises(tr.RebuildBlocked):
        _migrate._apply_file(ch, path, dry=True)
    assert ch.commands == []
