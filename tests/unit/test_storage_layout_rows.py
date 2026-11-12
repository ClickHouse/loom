"""The storage layout page has a row for every ClickHouse and Postgres table.

docs/architecture/storage-layout.md is the placement contract the waves are reviewed
against, so a table added without its row there, or a row left for a table that is
gone, leaves the contract wrong. The ClickHouse rows are checked both ways against the
dumped schema and the `_` tables the migrations create. The Postgres rows are checked
one way only: they also name the tables the waves add, which no migration creates yet.
Each ClickHouse row's Today cell must match its CREATE TABLE, `_` tables included (whose
key and TTL no later migration changes), and its Erasure cell, `_` tables aside, the
census. Its "named in" modules must name its table in a statement, a table-name literal
or a table constant, and the rows that cite none are the tables no module outside the
census names. A row whose Target is a merge or a deletion has its table in the Merges and
deletions table, and a row has a Wave exactly when its Target moves some of the table.
Every migration statement on a table names its database or schema, and a migration that
changes a `_` table's key or TTL, or renames or exchanges one, fails until its row and the
guard change.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from server.core.schema import pg_migrate
from server.core.schema.migrate_lease import LOCK_DDL, RUNNER_TABLES
from server.erasure.store import census
from tests.unit.tracked_files import tracked

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "architecture" / "storage-layout.md"
SCHEMA = ROOT / "schemas" / "schema.sql"
CH_MIGRATIONS = ROOT / "schemas" / "migrations"
PG_MIGRATIONS = ROOT / "schemas" / "pg_migrations"

_CREATE = (
    r"(?:CREATE\s+(?:OR\s+REPLACE\s+)?|REPLACE\s+)(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
)
_CH_TABLE = re.compile(_CREATE + r'[`"]?(loom(?:_telemetry)?)[`"]?\.[`"]?(\w+)[`"]?', re.IGNORECASE)
_PG_TABLE = re.compile(_CREATE + r'[`"]?loom[`"]?\.[`"]?(\w+)[`"]?', re.IGNORECASE)
# Tables no module outside the census names: claim tables nothing writes, and a lease
# table no module names.
_NAMED_NOWHERE = ("grant_claims", "oauth_claims", "task_claims", "_migration_lease")
_DROP_STATEMENT = re.compile(r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([^;]+)", re.IGNORECASE)
_DROPPED_NAME = re.compile(r'[`"]?(loom(?:_telemetry)?)[`"]?\.[`"]?(\w+)[`"]?')
_PG_RENAME = re.compile(
    r'ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?[`"]?loom[`"]?\.[`"]?(\w+)[`"]?'
    r'\s+RENAME\s+TO\s+[`"]?(\w+)',
    re.IGNORECASE,
)
_SQL = re.compile(
    r"\b(SELECT|INSERT|FROM|INTO|ALTER|TABLE|JOIN|UPDATE|DELETE|OPTIMIZE|EXCHANGE|RENAME)\b"
)
_ABBREVIATIONS = (
    ("ReplacingMergeTree", "RMT"),
    ("AggregatingMergeTree", "AMT"),
    ("SummingMergeTree", "SMT"),
    ("MergeTree", "MT"),
)
_RESHAPE = re.compile(
    r"(?:ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?[`\"]?loom(?:_telemetry)?[`\"]?\.[`\"]?_\w+.*?"
    r"(?:MODIFY\s+(?:TTL|ORDER\s+BY|PRIMARY\s+KEY)|REMOVE\s+TTL))"
    r"|(?:(?:EXCHANGE\s+TABLES|RENAME\s+TABLE)\b.*?[`\"]?loom(?:_telemetry)?[`\"]?\.[`\"]?_\w+)",
    re.IGNORECASE | re.DOTALL,
)


def _dropped(statement: str) -> list[tuple[str, str]]:
    """The (database, table) of every table a DROP TABLE statement names."""
    found = []
    for names in _DROP_STATEMENT.findall(statement):
        found.extend(_DROPPED_NAME.findall(names))
    return found


def _section(text: str, heading: str) -> str:
    assert f"\n## {heading}\n" in text, f"{PAGE.name} has no '## {heading}' section"
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def _rows(heading: str) -> list[tuple[list[str], list[str]]]:
    """Each row of a section's tables: the names in its first cell, and its cells."""
    rows = []
    for line in _section(PAGE.read_text(), heading).splitlines():
        if line.startswith("| `"):
            cells = [cell.strip() for cell in line.split("|")]
            rows.append((re.findall(r"`([\w.]+)`", cells[1]), cells))
    return rows


def _row_names(heading: str) -> list[str]:
    return [name for names, _ in _rows(heading) for name in names]


def _underscore_statements() -> dict[str, str]:
    """The CREATE TABLE of each live `_` table, in migration order (a later DROP removes it)."""
    live: dict[str, str] = {}
    for path in sorted(CH_MIGRATIONS.glob("*.sql")):
        text = path.read_text()
        for statement in re.split(r";\s*\n", text):
            created = _CH_TABLE.search(statement)
            if created and created.group(2).startswith("_"):
                database, name = created.groups()
                key = name if database == "loom" else f"{database}.{name}"
                # A `CREATE ... IF NOT EXISTS` of a live table does nothing;
                # only OR REPLACE replaces it.
                create = statement[created.start() :]
                if re.match(r"(?:CREATE\s+OR\s+)?REPLACE", create, re.IGNORECASE):
                    live[key] = create
                else:
                    live.setdefault(key, create)
            for database, name in _dropped(statement):
                live.pop(name if database == "loom" else f"{database}.{name}", None)
    return live


def _clickhouse_statements() -> dict[str, str]:
    """The CREATE TABLE of every ClickHouse table, by the name its row uses."""
    statements = {}
    for chunk in re.split(r"\n(?=CREATE TABLE)", SCHEMA.read_text()):
        match = _CH_TABLE.match(chunk)
        if match:
            database, name = match.groups()
            key = name if database == "loom" else f"{database}.{name}"
            statements[key] = chunk.split("\n\n")[0]
    # The schema dump leaves out names that start with `_`; their migrations create them,
    # and the migrator creates its own lock table before any migration runs.
    statements.update(_underscore_statements())
    for name in RUNNER_TABLES:
        statements[name] = re.sub(r" (ENGINE = |PRIMARY KEY )", r"\n\1", LOCK_DDL)
    return statements


def _postgres_tables(directory: Path = PG_MIGRATIONS) -> set[str]:
    tables: set[str] = set()
    for path in sorted(directory.glob("*.sql")):
        for statement in re.split(r";\s*\n", path.read_text()):
            tables.update(_PG_TABLE.findall(statement))
            tables.difference_update(name for _, name in _dropped(statement))
            for old, new in _PG_RENAME.findall(statement):
                tables.discard(old)
                tables.add(new)
    return tables


def _merged_tables(text: str) -> set[str]:
    """The tables in the Today cells of the first table of Merges and deletions."""
    section = _section(text, "Merges and deletions")
    first = section[section.index("\n|") :].split("\n\n")[0]
    return {
        name
        for line in first.splitlines()[3:]
        for name in re.findall(r"`(\w+)`", line.split("|")[1])
    }


def test_merges_table_names_every_merge_and_deletion() -> None:
    merged = _merged_tables(PAGE.read_text())
    targets = {name: cells[3] for names, cells in _rows("The ClickHouse tables") for name in names}
    owed = {name for name, target in targets.items() if target.startswith(("merge", "delete"))}
    assert not owed - merged, f"add to Merges and deletions: {sorted(owed - merged)}"
    assert not merged - set(targets), f"no ClickHouse row for: {sorted(merged - set(targets))}"


# Waves 1 to 4, one or a comma list, or the precondition a table retires on.
_WAVE = re.compile(r"[1-4](?:, [1-4])*|before wave 1")


def test_a_wave_moves_its_table() -> None:
    # A wave's work on a table that stays is in the row's last column, not its Wave.
    for names, cells in _rows("The ClickHouse tables"):
        target, wave = cells[3], cells[6]
        if target == "stays":
            assert wave == "—", f"{names} stays, so its Wave is —, not {wave!r}"
        else:
            assert _WAVE.fullmatch(wave), f"{names} moves ({target}); Wave {wave!r} is no wave"


def test_wave_cell_forms() -> None:
    assert all(_WAVE.fullmatch(wave) for wave in ("1", "1, 2, 3", "before wave 1"))
    assert not any(_WAVE.fullmatch(wave) for wave in ("5", "2,3", "wave 3", "—"))


def test_merged_tables_reads_the_today_cells() -> None:
    text = (
        "\n## Merges and deletions\n\nIntro.\n\n| Today | Becomes | Condition |\n| --- | --- | --- |\n"
        "| `a`, `b` | Postgres `jobs` | none |\n| KeeperMap `c` | `leases` | none |\n\n"
        "| Table | Wave |\n| --- | --- |\n| `d` | 1 |\n"
    )
    assert _merged_tables(text) == {"a", "b", "c"}


def test_no_table_has_two_rows() -> None:
    for heading in ("The ClickHouse tables", "The Postgres tables"):
        names = _row_names(heading)
        twice = sorted({name for name in names if names.count(name) > 1})
        assert not twice, f"tables with two rows under {heading}: {twice}"


def test_clickhouse_rows_match_the_tables() -> None:
    rows = set(_row_names("The ClickHouse tables"))
    tables = set(_clickhouse_statements())
    assert not tables - rows, f"add a row to {PAGE.name} for: {sorted(tables - rows)}"
    assert not rows - tables, f"remove the row from {PAGE.name} for: {sorted(rows - tables)}"


def _code_strings(path: Path) -> list[str]:
    """The string literals of a module, docstrings and logger names left out."""
    tree = ast.parse(path.read_text())
    skipped = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skipped.add(id(first.value))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute | ast.Name)
            and getattr(node.func, "attr", getattr(node.func, "id", "")) == "getLogger"
        ):
            skipped.update(id(arg) for arg in node.args)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skipped
    ]


def _module_constants(path: Path) -> set[str]:
    """The strings a module assigns to its module-level `...TABLE` names."""
    tree = ast.parse(path.read_text())
    values = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        named = any(isinstance(t, ast.Name) and t.id.endswith("TABLE") for t in targets)
        if named and isinstance(value, ast.Constant) and isinstance(value.value, str):
            values.add(value.value)
    return values


def _names_table(path: Path, table: str, in_postgres: bool) -> bool:
    """Whether a statement, a table-name literal or a table constant of the module names it."""
    # A table in both stores counts only in a module with no `pg` part in its file name,
    # whichever store the statement is for.
    if in_postgres and "pg" in re.split(r"[_.]", path.name):
        return False
    qualified = table if "." in table else f"loom.{table}"
    mention = re.compile(re.escape(qualified) + r"(?!\w)")
    for text in _code_strings(path):
        if mention.search(text) and _SQL.search(text):
            return True
        if text.strip() in (qualified, f"{qualified} FINAL"):
            return True
    return table.rsplit(".", 1)[-1] in _module_constants(path)


def test_clickhouse_rows_cite_code_that_names_the_table() -> None:
    in_postgres = _postgres_tables()
    wrong, unnamed = [], []
    for names, cells in _rows("The ClickHouse tables"):
        table = names[0]
        if "; named in " not in cells[-2]:
            unnamed.append(table)
            continue
        cited = re.findall(r"`([^`]+)`", cells[-2].rsplit("; named in ", 1)[1])
        modules = [entry for entry in cited if entry.endswith(".py")]
        wrong.extend(f"{table}: {entry} is not a module" for entry in cited if entry not in modules)
        if not 1 <= len(modules) <= 2:
            wrong.append(f"{table}: cites {len(modules)} modules")
        for module in modules:
            path = ROOT / module
            if not path.is_file() or not _names_table(path, table, table in in_postgres):
                wrong.append(f"{table}: {module}")
    assert not wrong, f"cited modules that do not name their table in code: {wrong}"
    assert sorted(unnamed) == sorted(_NAMED_NOWHERE), f"rows with no 'named in': {sorted(unnamed)}"


def _names_bare(path: Path, table: str) -> bool:
    """Whether a statement or a literal of the module names the table without `loom.`."""
    bare = re.compile(rf"(?<![\w.]){re.escape(table)}(?!\w)")
    return any(
        (bare.search(text) and _SQL.search(text)) or text.strip() == table
        for text in _code_strings(path)
    )


def test_tables_named_nowhere_are_named_nowhere() -> None:
    named = []
    for relative in tracked():
        if not relative.endswith(".py") or relative.startswith("tests/"):
            continue
        if relative == "server/erasure/store/census.py":
            continue
        path = ROOT / relative
        text = path.read_text()
        for table in _NAMED_NOWHERE:
            if table in text and (_names_table(path, table, False) or _names_bare(path, table)):
                named.append(f"{table}: {relative}")
    assert not named, f"tables the page says no module names: {named}"


def _today(statement: str) -> str:
    """A table's Today cell, as the page writes it, from its CREATE TABLE statement."""

    def clause(name: str) -> str:
        match = re.search(rf"\n{name} (.*)", statement)
        return match.group(1).strip().rstrip(";") if match else ""

    engine = clause("ENGINE =")
    for long, short in _ABBREVIATIONS:
        engine = engine.replace(long, short)
    if engine.startswith("KeeperMap"):
        engine = "KeeperMap"
    key = clause("ORDER BY") or clause("PRIMARY KEY")
    today = f"{engine} `{key}`" if key else engine
    if clause("PARTITION BY"):
        today += f", by `{clause('PARTITION BY')}`"
    if clause("TTL"):
        today += ", TTL"
    return today


def test_clickhouse_today_cells_match_the_schema() -> None:
    statements = _clickhouse_statements()
    stale = []
    for names, cells in _rows("The ClickHouse tables"):
        table = names[0]
        if table not in statements:
            stale.append(f"{table}: no CREATE TABLE parsed")
        elif cells[2] != _today(statements[table]):
            stale.append(f"{table}: {cells[2]!r} != {_today(statements[table])!r}")
    assert not stale, f"Today cells that differ from their CREATE TABLE: {stale}"


def _erasure_cell(table: str) -> set[str]:
    """A ClickHouse table's Erasure cell, as the page writes it, from the census."""
    qualified = table if "." in table else f"loom.{table}"
    parts = set()
    for substrate in census._CENSUS:
        if substrate.table == qualified:
            part = f"census, `{substrate.ns_step}`"
            if substrate.user_pred is not None:
                same = substrate.user_step == substrate.ns_step
                part += ", user scope too" if same else f", user `{substrate.user_step}`"
            parts.add(part)
    for step, tables in census.STEP_ONLY_TABLES.items():
        if qualified in tables:
            parts.add(f"step `{step}`")
    if qualified in census.NO_MEMORY_CONTENT:
        parts.add("no content")
    return parts


def test_clickhouse_erasure_cells_match_the_census() -> None:
    stale = []
    for names, cells in _rows("The ClickHouse tables"):
        table = names[0]
        if table.rsplit(".", 1)[-1].startswith("_"):
            continue  # outside the census, as their cells say
        written = {part.strip() for part in cells[5].split(";")}
        if written != _erasure_cell(table):
            stale.append(f"{table}: {sorted(written)} != {sorted(_erasure_cell(table))}")
    assert not stale, f"Erasure cells that differ from the census: {stale}"


def test_every_postgres_table_has_a_row() -> None:
    rows = set(_row_names("The Postgres tables"))
    # The runner creates its own ledger before any migration file runs.
    tables = _postgres_tables() | set(_PG_TABLE.findall(pg_migrate._ledger("loom")))
    missing = sorted(tables - rows)
    assert not missing, f"add a row to {PAGE.name} for: {missing}"


def test_a_value_or_prose_does_not_name_the_table(tmp_path: Path) -> None:
    module = tmp_path / "module.py"
    module.write_text(
        '''"""Reads loom.shared."""
import logging

log = logging.getLogger("loom.shared")
DEFAULT_SCOPE = "shared"
HELP = "Rows come from loom.shared."


def scope(row):
    return row.get("scope") == "shared"
'''
    )
    assert not _names_table(module, "shared", False)


def test_a_statement_or_a_table_constant_names_the_table(tmp_path: Path) -> None:
    statement = tmp_path / "statement.py"
    statement.write_text('SQL = "SELECT key FROM loom.shared FINAL WHERE org = {org:String}"\n')
    constant = tmp_path / "constant.py"
    constant.write_text('STATE_TABLE = "shared"\n')
    literal = tmp_path / "literal.py"
    literal.write_text('def write(ch, rows):\n    ch.insert("loom.shared", rows)\n')
    for module in (statement, constant, literal):
        assert _names_table(module, "shared", False), module.name


def test_a_postgres_module_does_not_name_a_table_in_both_stores(tmp_path: Path) -> None:
    for name, expected in (("pg_module.py", False), ("module.py", True)):
        module = tmp_path / name
        module.write_text('SQL = "DELETE FROM loom.shared WHERE org = %s"\n')
        assert _names_table(module, "shared", True) is expected, name


def test_underscore_tables_keep_their_created_shape() -> None:
    # Their Today cells are checked against the CREATE statement, so a migration that
    # changes a `_` table's key or TTL, or renames or exchanges one, must update this check
    # with the cell.
    for path in sorted(CH_MIGRATIONS.glob("*.sql")):
        for statement in re.split(r";\s*\n", path.read_text()):
            assert not _RESHAPE.search(statement), f"{path.name} reshapes a `_` table"


_STATEMENT_NAME = (
    r"\b(?:(?:CREATE(?:\s+OR)?\s+)?REPLACE\s+TABLE|CREATE\s+(?:UNLOGGED\s+)?TABLE|ALTER\s+TABLE"
    r"|DROP\s+TABLE|RENAME\s+TABLE"
    r"|EXCHANGE\s+TABLES)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(?:ONLY\s+)?(?!IF\b|ONLY\b)"
)
# The second name of a rename or an exchange.
_TARGET_NAME = r"\b(?:RENAME\s+TABLE|EXCHANGE\s+TABLES)\b[^;]*?\b(?:TO|AND)\s+"
# A comma-separated list of names, or of `x TO y` pairs, after DROP or RENAME TABLE.
_NAME_LIST = re.compile(
    r"\b(?:DROP\s+TABLE(?:\s+IF\s+EXISTS)?|RENAME\s+TABLE)\s+([^;]+)", re.IGNORECASE
)
_LIST_NAME = r"(?:^|,|\bTO\b)\s*(?!IF\b)"
# ClickHouse migrations name both databases; Postgres migrations name the loom schema.
_CH_SCHEMAS = "loom(?:_telemetry)?"
_PG_SCHEMAS = "loom"


def _unqualified(text: str, schemas: str = _CH_SCHEMAS) -> bool:
    """Whether a table statement in ``text`` names a table outside ``schemas``, or with none."""
    other = rf"(?![`\"]?{schemas}[`\"]?\.)[`\"]?\w"
    text = re.sub(r"--[^\n]*", "", text)
    if re.search(_STATEMENT_NAME + other, text, re.IGNORECASE):
        return True
    if re.search(_TARGET_NAME + other, text, re.IGNORECASE):
        return True
    return any(
        re.search(_LIST_NAME + other, names, re.IGNORECASE) for names in _NAME_LIST.findall(text)
    )


def test_migrations_qualify_table_names() -> None:
    # The guards above match `loom.`-qualified names, so every migration statement on a
    # table names its database or schema, the second name of a rename or an exchange
    # included.
    for path in sorted(CH_MIGRATIONS.glob("*.sql")):
        assert not _unqualified(path.read_text()), f"{path.name} names a table without its database"
    for path in sorted(PG_MIGRATIONS.glob("*.sql")):
        assert not _unqualified(path.read_text(), _PG_SCHEMAS), (
            f"{path.name} names a table outside the loom schema"
        )


def test_qualify_guard_forms() -> None:
    assert _unqualified("RENAME TABLE loom.tmp TO _migrations")
    assert _unqualified("EXCHANGE TABLES loom.tmp AND _migrations")
    assert _unqualified("ALTER TABLE _migrations MODIFY TTL t + 1")
    assert _unqualified("ALTER TABLE `_migrations` MODIFY TTL t + 1")
    assert _unqualified("REPLACE TABLE _migrations (a UInt8) ENGINE = Log")
    assert _unqualified("CREATE UNLOGGED TABLE carry (k int)")
    assert _unqualified("DROP TABLE IF EXISTS _migration_lease")
    assert not _unqualified("DROP TABLE IF EXISTS loom._migration_lease")
    assert _unqualified("DROP TABLE IF EXISTS loom.a, _b")
    assert _unqualified("RENAME TABLE loom.a TO loom.b, c TO loom.d")
    assert not _unqualified("RENAME TABLE loom.a TO loom.b, loom.c TO loom.d")
    assert not _unqualified("ALTER TABLE ONLY loom.memory_revisions ADD COLUMN x int")
    assert _unqualified("ALTER TABLE ONLY memory_revisions ADD COLUMN x int")
    assert not _unqualified("RENAME TABLE loom.tmp TO loom._migrations")
    assert not _unqualified("CREATE TABLE IF NOT EXISTS `loom`.`_x` (a UInt8) ENGINE = Log")


def test_postgres_qualify_guard_forms() -> None:
    assert _unqualified("CREATE TABLE IF NOT EXISTS loom_telemetry.x (k int)", _PG_SCHEMAS)
    assert _unqualified("DROP TABLE loom.a, loom_telemetry.b", _PG_SCHEMAS)
    assert _unqualified("ALTER TABLE ONLY loom_telemetry.x ADD COLUMN y int", _PG_SCHEMAS)
    assert not _unqualified("CREATE TABLE IF NOT EXISTS loom.x (k int)", _PG_SCHEMAS)
    assert not _unqualified("DROP TABLE loom.a, loom_telemetry.b")
    # Postgres refuses a schema on the target of ALTER TABLE ... RENAME TO.
    assert not _unqualified("ALTER TABLE loom.a RENAME TO b", _PG_SCHEMAS)


def test_reshape_guard_forms() -> None:
    assert _RESHAPE.search("ALTER TABLE loom._migration_lease MODIFY TTL until + 1")
    assert _RESHAPE.search("ALTER TABLE loom._migration_lease REMOVE TTL")
    assert _RESHAPE.search("ALTER TABLE loom_telemetry._x MODIFY TTL m + 1")
    assert _RESHAPE.search("ALTER TABLE loom._migrations MODIFY ORDER BY (filename, x)")
    assert _RESHAPE.search("RENAME TABLE loom.tmp TO loom._migrations")
    assert _RESHAPE.search("EXCHANGE TABLES loom.tmp AND loom._migrations")
    assert _RESHAPE.search("EXCHANGE TABLES loom._migrations AND loom.tmp")
    assert _RESHAPE.search("RENAME TABLE loom._migrations TO loom.ledger")
    assert _RESHAPE.search("ALTER TABLE `loom`.`_migrations` MODIFY TTL t + 1")
    assert _CH_TABLE.search("CREATE TABLE IF NOT EXISTS `loom`.`_x` (a UInt8) ENGINE = Log")
    assert _CH_TABLE.search("REPLACE TABLE loom._x (a UInt8) ENGINE = Log")
    assert not _RESHAPE.search("ALTER TABLE loom._migrations ADD COLUMN x UInt8")
    assert not _RESHAPE.search("ALTER TABLE loom.memory MODIFY TTL ts + 1")


def test_today_cell_forms() -> None:
    nested = "CREATE TABLE loom.t\n(\n)\nENGINE = SummingMergeTree((a, b))\nORDER BY (org, k)"
    assert _today(nested) == "SMT((a, b)) `(org, k)`"
    keeper = "CREATE TABLE loom.t\n(\n)\nENGINE = KeeperMap('/loom/t')\nPRIMARY KEY key"
    assert _today(keeper) == "KeeperMap `key`"
    bare = "CREATE TABLE loom.t\n(\n)\nENGINE = MergeTree\nPARTITION BY day\nTTL day + 1"
    assert _today(bare) == "MT, by `day`, TTL"


def test_postgres_tables_follow_renames_and_drops(tmp_path: Path) -> None:
    (tmp_path / "001_a.sql").write_text(
        "CREATE TABLE IF NOT EXISTS loom.a (k int);\nCREATE TABLE loom.gone (k int);\n"
    )
    (tmp_path / "002_b.sql").write_text(
        "ALTER TABLE loom.a RENAME TO b;\nDROP TABLE IF EXISTS loom.gone;\n"
    )
    assert _postgres_tables(tmp_path) == {"b"}


def test_a_bare_statement_names_a_table(tmp_path: Path) -> None:
    module = tmp_path / "module.py"
    module.write_text('SQL = "SELECT 1 FROM grant_claims"\n')
    assert _names_bare(module, "grant_claims")
    module.write_text('HELP = "grant_claims are gone"\n')
    assert not _names_bare(module, "grant_claims")


def test_a_drop_of_several_tables_drops_each() -> None:
    assert _dropped("DROP TABLE IF EXISTS loom.a, loom_telemetry._b") == [
        ("loom", "a"),
        ("loom_telemetry", "_b"),
    ]
