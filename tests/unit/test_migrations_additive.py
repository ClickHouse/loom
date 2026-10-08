"""Every migration is additive (the schema half of the wire-compatibility
rule): the build that was serving before the migration ran can still
read and write every table it knew afterwards, so a blue/green rollback is a
proxy swap and never a schema rollback.

Mechanised part: no migration drops or renames a table or a column, or
exchanges tables. Widening a column's type, adding or dropping an index or a
projection, changing a codec or a default, extending an enum, adding a column
or a table are all additive and pass. Narrowing a type is a review question
the rule text in docs/development/migrations.md carries; a regex cannot tell it from
a widening.

A rebuild (`-- loom: rebuild-if-empty <table>`) is the one exchange: the
runner swaps in a table with the same columns and a new sort or partition key,
and the last test holds the target to those columns.

Two drops are additive in the sense that matters, because no serving colour
or operator tool reads or writes what they remove:
- an insert-time materialized view nothing names. It is a trigger, not
  something a colour reads; its target table stays. `DROP VIEW IF EXISTS
  loom.x` passes when an earlier migration created `x` as a materialized
  view and no file under `server/`, `scripts/`, `bench/` or `dev/` names it.
- a superseded table nothing names. `DROP TABLE IF EXISTS loom.x` passes
  when an earlier migration created `x` as a table and no file under those
  directories names it. The tools under `scripts/`, `bench/` and `dev/` run
  against the production service, and a read of a dropped table there fails
  only when someone next runs the tool.

A dropped skip index or projection costs the old colour an optimisation, not
a column, but a setting that names it (`ignore_data_skipping_indices`, an
exclusion list) would then point at nothing: no file under those directories may name
one a migration drops."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "schemas" / "migrations"
# Where a reader of a loom object can live: the server and the operator tools.
SOURCE_DIRS = ("server", "scripts", "bench", "dev")
SOURCE_SUFFIXES = {".py", ".sh", ".sql"}

FORBIDDEN = re.compile(
    r"\b(DROP\s+TABLE(?!\s+IF\s+EXISTS\s+loom\.\w+\s*;)|DROP\s+VIEW(?!\s+IF\s+EXISTS\s+loom\.\w+\s*;)|"
    r"DROP\s+DICTIONARY|DROP\s+COLUMN|RENAME\s+TABLE|"
    r"RENAME\s+COLUMN|EXCHANGE\s+TABLES?|CLEAR\s+COLUMN)\b",
    re.IGNORECASE,
)


def _without_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


DROPPED_VIEW = re.compile(r"\bDROP\s+VIEW\s+IF\s+EXISTS\s+loom\.(\w+)\s*;", re.IGNORECASE)
CREATED_MV = re.compile(
    r"\bCREATE\s+MATERIALIZED\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?loom\.(\w+)", re.IGNORECASE
)
DROPPED_TABLE = re.compile(r"\bDROP\s+TABLE\s+IF\s+EXISTS\s+loom\.(\w+)\s*;", re.IGNORECASE)
CREATED_TABLE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?loom\.(\w+)", re.IGNORECASE
)


DROPPED_INDEX = re.compile(
    r"\bDROP\s+(?:INDEX|PROJECTION)\s+(?:IF\s+EXISTS\s+)?(\w+)", re.IGNORECASE
)


def non_additive_statements(sql: str) -> list[str]:
    return sorted({m.group(1).upper() for m in FORBIDDEN.finditer(_without_comments(sql))})


def _migrations(directory: Path = MIGRATIONS) -> list[tuple[str, str]]:
    files = sorted(directory.glob("*.sql"), key=lambda p: int(p.name.split("_", 1)[0]))
    assert files, f"no migrations in {directory}"
    return [(p.name, p.read_text("utf-8")) for p in files]


# A journal with a rebuild declaration: the sort-key rebuild the baseline has
# since folded in, kept as a fixture so the check below always runs on one.
REBUILD_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rebuild_migrations"


def _source() -> str:
    return "\n".join(
        p.read_text("utf-8")
        for d in SOURCE_DIRS
        for p in (ROOT / d).rglob("*")
        if p.suffix in SOURCE_SUFFIXES and p.is_file()
    )


def test_every_migration_is_additive() -> None:
    bad = {name: hits for name, sql in _migrations() if (hits := non_additive_statements(sql))}
    assert not bad, (
        "migrations with a non-additive statement (a table or column dropped, renamed or "
        f"exchanged): {bad}. The old colour must keep serving through the new colour's boot; "
        "retire a column in two releases — stop reading it, then drop it "
        "once no serving build names it — and say so in the PR description."
    )


def names_object(obj: str, server_src: str) -> bool:
    """`loom.<obj>` in SQL, or `<obj>` as a quoted literal (a table map's key).
    A bare word is not enough: a Python module can share the table's name."""
    return re.search(rf"\bloom\.{obj}\b|[\"']{obj}[\"']", server_src) is not None


def drop_violations(migrations: list[tuple[str, str]], server_src: str) -> list[str]:
    """Each `DROP VIEW|TABLE IF EXISTS loom.x` in apply order that is not the
    drop of an earlier-created materialized view or table no server file names."""
    views: set[str] = set()
    tables: set[str] = set()
    out: list[str] = []
    for name, raw in migrations:
        sql = _without_comments(raw)
        for kind, dropped, created in (
            ("materialized view", DROPPED_VIEW.findall(sql), views),
            ("table", DROPPED_TABLE.findall(sql), tables),
        ):
            for obj in dropped:
                if obj not in created:
                    out.append(
                        f"{name} drops loom.{obj}, which no earlier migration creates as a {kind}"
                    )
                elif names_object(obj, server_src):
                    out.append(
                        f"{name} drops loom.{obj}, but server/, scripts/, bench/ or dev/ "
                        "still names it — a serving colour or an operator tool may read it"
                    )
        views |= set(CREATED_MV.findall(sql))
        tables |= set(CREATED_TABLE.findall(sql))
    return out


def test_every_drop_in_the_tree_is_of_an_object_no_build_names() -> None:
    assert drop_violations(_migrations(), _source()) == []


def dropped_index_violations(migrations: list[tuple[str, str]], server_src: str) -> list[str]:
    """Each skip index or projection a migration drops that `server/` still names."""
    return [
        f"{name} drops index or projection {obj}, but the source still names it"
        for name, raw in migrations
        for obj in DROPPED_INDEX.findall(_without_comments(raw))
        if re.search(rf"\b{obj}\b", server_src)
    ]


def test_no_build_names_a_dropped_index_or_projection() -> None:
    assert dropped_index_violations(_migrations(), _source()) == []


def test_a_dropped_index_is_one_no_build_names() -> None:
    drop = (
        "006_x.sql",
        "ALTER TABLE loom.t DROP INDEX IF EXISTS idx_a;\n"
        "ALTER TABLE loom.t DROP PROJECTION IF EXISTS p_a;",
    )
    assert dropped_index_violations([drop], server_src="keep_a = 1") == []
    assert dropped_index_violations(
        [drop], server_src="ignore_data_skipping_indices = 'idx_a'"
    ) == ["006_x.sql drops index or projection idx_a, but the source still names it"]
    assert len(dropped_index_violations([drop], server_src="p_a, idx_a")) == 2


_BASE = (
    "001_base.sql",
    "CREATE TABLE IF NOT EXISTS loom.old (a UInt8) ENGINE = MergeTree ORDER BY a;\n"
    "CREATE MATERIALIZED VIEW IF NOT EXISTS loom.old_mv TO loom.old AS SELECT 1 AS a;\n",
)


def test_a_dropped_view_is_an_insert_time_view_no_build_names() -> None:
    drop = ("002_drop.sql", "DROP VIEW IF EXISTS loom.old_mv;")
    assert drop_violations([_BASE, drop], server_src="") == []
    assert drop_violations([_BASE, drop], server_src="FROM loom.old_mv") == [
        "002_drop.sql drops loom.old_mv, but server/, scripts/, bench/ or dev/ still names it — "
        "a serving colour or an operator tool may read it"
    ]
    assert drop_violations([drop], server_src="") == [
        "002_drop.sql drops loom.old_mv, which no earlier migration creates as a materialized view"
    ]
    # A plain table is not an insert-time view.
    table_as_view = ("002_drop.sql", "DROP VIEW IF EXISTS loom.old;")
    assert len(drop_violations([_BASE, table_as_view], server_src="")) == 1


def test_a_dropped_table_is_a_superseded_table_no_build_names() -> None:
    drop = ("002_drop.sql", "DROP TABLE IF EXISTS loom.old;")
    assert drop_violations([_BASE, drop], server_src="") == []
    assert drop_violations([_BASE, drop], server_src="INSERT INTO loom.old") == [
        "002_drop.sql drops loom.old, but server/, scripts/, bench/ or dev/ still names it — "
        "a serving colour or an operator tool may read it"
    ]
    assert drop_violations([drop], server_src="") == [
        "002_drop.sql drops loom.old, which no earlier migration creates as a table"
    ]
    # The drop comes after the create in apply order, not before it.
    assert len(drop_violations([drop, _BASE], server_src="")) == 1


def test_detector_reads_statements_not_comments() -> None:
    assert (
        non_additive_statements("-- DROP TABLE loom.x\nALTER TABLE loom.x ADD COLUMN y String;")
        == []
    )
    assert (
        non_additive_statements("/* RENAME TABLE */ ALTER TABLE loom.x DROP INDEX IF EXISTS i;")
        == []
    )
    assert non_additive_statements("ALTER TABLE loom.x DROP PROJECTION IF EXISTS p;") == []
    assert (
        non_additive_statements("ALTER TABLE loom.x MODIFY COLUMN y Array(BFloat16) CODEC(NONE);")
        == []
    )
    assert non_additive_statements("alter table loom.x drop column y;") == ["DROP COLUMN"]
    assert non_additive_statements("DROP VIEW IF EXISTS loom.x_mv;") == []
    assert non_additive_statements("DROP VIEW loom.x_mv;") == ["DROP VIEW"]
    assert non_additive_statements("DROP TABLE IF EXISTS loom.old;\nRENAME TABLE a TO b;") == [
        "RENAME TABLE"
    ]
    assert non_additive_statements("DROP TABLE loom.old;") == ["DROP TABLE"]


# A rebuild (`-- loom: rebuild-if-empty <table>`, server/core/schema/table_rebuild.py)
# exchanges a table for one with the same columns and a new sort or partition
# key: every build reads and writes it as before, so it is additive. Its sort
# key is also the ReplacingMergeTree's merge key: a reordered key merges the
# same rows, a key with other columns does not, and needs the reason here.
REVIEWED_KEY_CHANGES = {
    "code_pr_reviews": "path joins the key: a review comment's path is fixed on the forge, "
    "so one comment is still one row",
}


def _columns(stmt: str) -> list[str]:
    return [line.strip().rstrip(",") for line in stmt.splitlines() if line.strip().startswith("`")]


@pytest.mark.parametrize("directory", [MIGRATIONS, REBUILD_FIXTURE], ids=["repo", "fixture"])
def test_a_rebuild_target_restates_its_table_and_keeps_its_key_columns(directory: Path) -> None:
    from server.core.schema import table_rebuild as tr
    from server.core.schema.journal import split_statements

    created: dict[str, str] = {}
    seen = 0
    for name, sql in _migrations(directory):
        stmts = split_statements(sql)
        for stmt, table in tr.targets_in(sql, stmts).items():
            seen += 1
            before = created[table]
            assert _columns(stmt) == _columns(before), f"{name} changes the columns of loom.{table}"
            old = set(tr.layout_of(before).sorting_key.split(", "))
            new = set(tr.layout_of(stmt).sorting_key.split(", "))
            assert old == new or table in REVIEWED_KEY_CHANGES, (
                f"{name} changes which rows of loom.{table} merge ({sorted(old ^ new)}): "
                "say why in REVIEWED_KEY_CHANGES"
            )
        body = _without_comments(sql)
        for m in CREATED_TABLE.finditer(body):
            created[m.group(1)] = body[m.start() : body.find(";\n", m.start())]
    assert seen >= 3 or directory == MIGRATIONS


def test_a_module_sharing_the_tables_name_does_not_name_it() -> None:
    drop = ("002_drop.sql", "DROP TABLE IF EXISTS loom.old;")
    assert drop_violations([_BASE, drop], server_src="from server.memory.app.old import x") == []
    assert len(drop_violations([_BASE, drop], server_src='TABLES = {"old": 1}')) == 1


def test_the_operator_tools_are_searched_with_the_server() -> None:
    src = _source()
    for d in SOURCE_DIRS:
        sample = next(p for p in sorted((ROOT / d).rglob("*.py")) if p.stat().st_size > 200)
        assert sample.read_text("utf-8") in src, sample
