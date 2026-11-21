"""Inventory cell control SQL writers and the shared boundaries they must retain.

This recognizes Python string literals (including adjacent literals) and f-strings. It is a
source change gate, not a proof for arbitrary generated SQL; backend/session behavior
is exercised against Postgres in test_cell_control_fence.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Include immutable placement writes: their exemption must remain explicit.
TABLES = r"(?:loom\.(?:cell_self|cell_first|tenancy_mode)|loom_router\.[A-Za-z_]+)"
TARGET = rf"(?:{TABLES}|loom(?:_router)?\.\{{[^}}]+\}}|\{{[^}}]+\}})"
WRITES = re.compile(
    rf"(?:^|;|[()])\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|"
    rf"ALTER\s+TABLE(?:\s+IF\s+EXISTS)?|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|"
    rf"DROP\s+TABLE(?:\s+IF\s+EXISTS)?)\s+({TARGET})"
    rf"|(?:^|;|[()])\s*((?:CREATE|DROP)\s+TRIGGER)\b[^;]*?\bON\s+({TARGET})",
    re.IGNORECASE,
)


def mutations(sql: str) -> list[tuple[str, str]]:
    sql = re.sub(r"--[^\n]*|/\*.*?\*/", "", sql, flags=re.DOTALL).replace('"', "")
    return [(" ".join((a or c).upper().split()), b or d) for a, b, c, d in WRITES.findall(sql)]


class Inventory(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.writes: Counter[tuple[str, str, str]] = Counter()
        self.functions: dict[str, ast.AST] = {}

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.functions[".".join(self.scope)] = node
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def record(self, sql: str) -> None:
        for verb, target in mutations(sql):
            self.writes[(".".join(self.scope), verb, target)] += 1

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.record(node.value)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        self.record(
            "".join(
                str(part.value)
                if isinstance(part, ast.Constant)
                else "{" + ast.unparse(part.value) + "}"
                for part in node.values
            )
        )
        # Do not count each f-string fragment as another SQL statement.


def inventory(source: str) -> Inventory:
    found = Inventory()
    found.visit(ast.parse(source))
    return found


def test_repository_cell_control_writers_are_classified():
    actual = {}
    for path in (ROOT / "server").rglob("*.py"):
        writes = inventory(path.read_text()).writes
        if writes:
            actual[str(path.relative_to(ROOT))] = dict(writes)
    expected = {}
    for group in (GUARDED_WRITERS, EXEMPT_WRITERS, GENERIC_SQL_EXCLUSIONS):
        for path, entries in group.items():
            expected.setdefault(path, {}).update(entries)
    assert actual == expected


def test_cell_control_migrations_are_classified():
    actual = {}
    for path in (ROOT / "schemas").rglob("*.sql"):
        writes = Counter(mutations(path.read_text()))
        if writes:
            actual[str(path.relative_to(ROOT))] = dict(writes)
    assert actual == MIGRATION_WRITERS


# Entries name every sink, including nested callbacks and dynamic table expressions.
# A new sink, including another statement in an existing owner, requires classification.
GUARDED_WRITERS = {
    "server/cli/cell_update_pg.py": {("write.change", "UPDATE", "loom.cell_self"): 1},
    "server/cli/cell_attach_pg.py": {("_mode_transaction", "UPDATE", "loom.tenancy_mode"): 1},
    "server/cli/cell_control.py": {
        ("backout.clear_state", "ALTER TABLE", "loom.cell_first"): 3,
        ("backout.clear_state", "DELETE FROM", "loom.{table}"): 1,
        ("backout.clear_triggers", "DROP TRIGGER", "loom.{table}"): 1,
    },
    "server/cli/cell_prepare.py": {
        ("_write_rows.write", "INSERT INTO", "loom.cell_first"): 1,
        ("_write_rows.write", "INSERT INTO", "loom.tenancy_mode"): 1,
        ("_write_rows.write", "INSERT INTO", "loom.cell_self"): 1,
    },
    "server/router/directory.py": {
        ("update_addresses.change", "UPDATE", "loom_router.cells"): 1,
        ("set_limit.change", "UPDATE", "loom_router.cells"): 1,
        ("rotate_credential.change", "UPDATE", "loom_router.cells"): 1,
        ("claim.change_locked", "UPDATE", "loom_router.cells"): 1,
        ("_configuration_commit", "INSERT INTO", "loom_router.placements"): 1,
        ("_configuration_commit", "INSERT INTO", "loom_router.audit"): 2,
        ("_configuration_commit", "UPDATE", "loom_router.cells"): 1,
        ("attach_begin.change_locked", "UPDATE", "loom_router.cells"): 1,
        ("attach_place.change_locked", "INSERT INTO", "loom_router.placements"): 1,
        ("attach_place.change_locked", "UPDATE", "loom_router.cells"): 1,
        ("attach_place.change_locked", "INSERT INTO", "loom_router.audit"): 1,
        ("attach_enforced.change_locked", "UPDATE", "loom_router.cells"): 1,
        ("attach_serving.change_locked", "UPDATE", "loom_router.cells"): 1,
        ("remove.change_locked", "DELETE FROM", "loom_router.hosts"): 1,
        ("remove.change_locked", "DELETE FROM", "loom_router.cells"): 1,
    },
}

# Registration only bootstraps or validates its immutable rerun. Ordinary placement
# writes an immutable org placement/count under its org and cell locks. Reads write
# audit rows only. Trigger setup only enables/installs guards under the migration lock.
EXEMPT_WRITERS = {
    "server/core/cell_triggers.py": {
        ("_create", "CREATE TRIGGER", "{qualified}"): 2,
        ("ensure", "ALTER TABLE", "loom.cell_first"): 1,
        ("_table_statements", "ALTER TABLE", "loom.{_ident(table)}"): 1,
    },
    "server/router/directory.py": {
        ("register", "INSERT INTO", "loom_router.cells"): 1,
        ("register", "INSERT INTO", "loom_router.hosts"): 1,
        ("register", "INSERT INTO", "loom_router.audit"): 1,
        ("_place_one", "INSERT INTO", "loom_router.placements"): 1,
        ("_place_one", "UPDATE", "loom_router.cells"): 1,
        ("place", "INSERT INTO", "loom_router.audit"): 1,
        ("_audit", "INSERT INTO", "loom_router.audit"): 1,
    },
}

# Unresolved table SQL outside cell controls is classified by exact function and
# target expression, never by a whole-file or execute/f-string exemption. These are
# ClickHouse content/retention/rebuild stores, Keeper locks/marks and provider tables;
# pg_migrate writes only its named migration ledger, and prepare's nonce writes only
# the probe lock table. None is a cell acceptance or directory control write.
GENERIC_SQL_EXCLUSIONS = {
    # The allowlisted feed tables belong to the separate verified record database.
    "server/tracker/store/github_feed.py": {
        ("create_table", "CREATE TABLE IF NOT EXISTS", "{name}"): 1
    },
    "server/cli/cell_prepare.py": {("_write_nonces", "INSERT INTO", "{leases.LOCK_TABLE}"): 1},
    "server/code/store/index/prefix_twin.py": {("fill_sql", "INSERT INTO", "{table}"): 1},
    "server/core/ch.py": {("_fenced_statement", "INSERT INTO", "{qualify(table)}"): 1},
    "server/core/leases.py": {
        ("", "INSERT INTO", "{LOCK_TABLE}"): 1,
        ("", "ALTER TABLE", "{LOCK_TABLE}"): 4,
    },
    "server/core/namespace_write_marks.py": {("", "INSERT INTO", "{KEYS_TABLE}"): 1},
    "server/core/schema/migrate_lease.py": {("", "CREATE TABLE IF NOT EXISTS", "{LOCK_TABLE}"): 1},
    "server/core/schema/pg_migrate.py": {
        ("_ledger", "CREATE TABLE IF NOT EXISTS", "{schema}"): 1,
        ("_record_online", "INSERT INTO", "{schema}"): 1,
        ("_apply", "INSERT INTO", "{schema}"): 1,
    },
    "server/core/schema/table_rebuild.py": {
        ("copy_sql", "INSERT INTO", "loom.{dst}"): 1,
        ("apply", "DROP TABLE", "loom.{twin}"): 2,
    },
    # The token selects a source metadata column, not a cell control table.
    "server/erasure/store/crawl_controls.py": {("controls", "UPDATE", "{field}"): 1},
    "server/erasure/store/mutations.py": {
        ("run_mutation", "ALTER TABLE", "{table}"): 1,
        ("run_lightweight_delete", "DELETE FROM", "{table}"): 1,
        ("run_keeper_delete", "ALTER TABLE", "{table}"): 1,
    },
    "server/erasure/store/reclaim.py": {("apply_deleted_mask", "ALTER TABLE", "{table}"): 1},
    "server/erasure/store/steps_memory.py": {
        ("_scrub_link_cursors", "DELETE FROM", "{_CURSORS}"): 1
    },
    "server/erasure/store/steps_principals.py": {("_rewrite", "INSERT INTO", "{table}"): 1},
    "server/insights/domain/analysis.py": {("entity_sql", "INSERT INTO", "{db}"): 1},
    "server/insights/domain/provider_prepared_ddl.py": {
        ("prepared_ddl", "CREATE TABLE", "loom.{prepared}"): 1
    },
    "server/insights/store/provider_cache.py": {
        ("invalidate_staged_cache", "TRUNCATE TABLE", "loom.{names.target}"): 1
    },
    "server/insights/store/provider_cleanup.py": {
        ("cleanup_rebuild_copies", "DROP TABLE IF EXISTS", "{db}"): 1
    },
    "server/insights/store/provider_participants.py": {("", "ALTER TABLE", "{TABLE}"): 1},
    "server/insights/store/provider_prefix.py": {
        ("derive_staged_prefixes", "TRUNCATE TABLE", "loom.{names.target}"): 1,
        ("derive_staged_prefixes", "INSERT INTO", "loom.{names.target}"): 1,
    },
    "server/insights/store/provider_prepared.py": {
        ("prepare_live_table", "DROP TABLE", "loom.{names.prepared}"): 1,
        ("prepare_live_table", "INSERT INTO", "loom.{names.prepared}"): 1,
        ("prepare_live_table", "ALTER TABLE", "loom.{names.prepared}"): 1,
    },
    "server/insights/store/provider_staging.py": {
        ("create_snapshot", "CREATE TABLE", "loom.{names.snapshot}"): 1,
        ("complete_snapshot", "ALTER TABLE", "loom.{names.snapshot}"): 1,
        ("discard_snapshot", "DROP TABLE", "loom.{names.snapshot}"): 1,
        ("snapshot_query_running", "CREATE TABLE", "loom.{names.snapshot}"): 1,
    },
    "server/insights/store/provider_target.py": {
        ("ensure_target", "CREATE TABLE", "loom.{names.target}"): 1,
        ("_insert_patches", "INSERT INTO", "loom.{names.target}"): 1,
    },
    "server/knowledge/store/blobs.py": {
        ("copy_held", "INSERT INTO", "{TABLE}"): 1,
        ("unwrite", "DELETE FROM", "{TABLE}"): 1,
        ("delete", "DELETE FROM", "{TABLE}"): 1,
        ("delete", "DELETE FROM", "{UPLOADS}"): 1,
        ("forget_uploads", "DELETE FROM", "{UPLOADS}"): 1,
    },
    "server/knowledge/store/chunks.py": {
        ("_record_supersessions", "INSERT INTO", "{SUPERSESSIONS_TABLE}"): 1
    },
    # Account erasure deletes only the connected registry, not cell control rows.
    "server/knowledge/store/connected_accounts.py": {("erase", "DELETE FROM", "{TABLE}"): 1},
    "server/knowledge/store/conversations.py": {
        ("mark_message_saved", "ALTER TABLE", "{table}"): 1,
        ("unsave_message", "DELETE FROM", "{table}"): 1,
        ("unsave_message", "ALTER TABLE", "{table}"): 1,
    },
    "server/knowledge/store/datasets.py": {
        ("forget", "DELETE FROM", "{ROWS}"): 1,
        ("forget", "DELETE FROM", "{DATASETS}"): 1,
        ("forget_published", "DELETE FROM", "{ROWS}"): 1,
        ("forget_published", "DELETE FROM", "{DATASETS}"): 1,
        ("copy_snapshot", "INSERT INTO", "{ROWS}"): 1,
        ("forget_snapshot", "DELETE FROM", "{ROWS}"): 1,
    },
    "server/knowledge/store/gaps.py": {
        ("delete_miss", "DELETE FROM", "{MISSES}"): 1,
        ("delete_build", "DELETE FROM", "{GAPS}"): 1,
        ("delete_misses", "DELETE FROM", "{MISSES}"): 1,
        ("delete_builds", "DELETE FROM", "{GAPS}"): 1,
    },
    "server/knowledge/store/restore.py": {("unretire", "DELETE FROM", "{SUPERSESSIONS_TABLE}"): 1},
    # The stopped owner and namespace lease scope deletion to knowledge sources.
    "server/knowledge/store/sources.py": {
        ("erase_connected_owner", "DELETE FROM", "{TABLE}"): 1,
        ("reseal_crawl_approval", "ALTER TABLE", "{TABLE}"): 1,
    },
    "server/memory/app/reembed.py": {
        ("in_database_sql", "INSERT INTO", "loom.{table}"): 1,
        ("delete_off_model", "DELETE FROM", "loom.{table}"): 1,
    },
    "server/memory/store/code_links.py": {
        ("_purge_tombstones", "DELETE FROM", "{TABLE}"): 1,
        ("_drop_gone", "DELETE FROM", "{TABLE}"): 1,
    },
    "server/memory/store/graph_projections.py": {("KeeperLayout", "INSERT INTO", "{TABLE}"): 1},
    "server/memory/store/lifecycle.py": {("_tombstone_where", "ALTER TABLE", "{table}"): 1},
    "server/memory/store/redaction.py": {("delete_by_key", "ALTER TABLE", "{table}"): 1},
    "server/memory/store/term_freq.py": {("rebuild", "ALTER TABLE", "{table}"): 1},
    "server/memory/store/webhooks.py": {("mark_dead", "INSERT INTO", "{ATTEMPTS}"): 1},
    "server/memory/workers/usage_fold.py": {
        ("_apply_update", "UPDATE", "{table}"): 1,
        ("_apply_update", "ALTER TABLE", "{table}"): 1,
    },
    "server/storage/retention_overrides.py": {
        ("compute_alters", "ALTER TABLE", "{target.table}"): 1,
        ("current_ttl_matches", "ALTER TABLE", "{table}"): 1,
        ("apply", "ALTER TABLE", "{target.table}"): 1,
    },
}

# Schema creation/extension and immutable-trigger installation run as migrations,
# never as live acceptance writers. Offline restore generation rotation stays stopped
# and fenced as described in the recovery guide, not a live SQL exemption.
MIGRATION_WRITERS = {
    "schemas/pg_migrations/008_cells.sql": {
        ("CREATE TABLE IF NOT EXISTS", "loom.cell_first"): 1,
        ("DROP TRIGGER", "loom.cell_first"): 2,
        ("CREATE TRIGGER", "loom.cell_first"): 2,
        ("ALTER TABLE", "loom.cell_first"): 2,
        ("CREATE TABLE IF NOT EXISTS", "loom.tenancy_mode"): 1,
        ("CREATE TABLE IF NOT EXISTS", "loom.cell_self"): 1,
    },
    "schemas/router_migrations/001_directory.sql": {
        ("CREATE TABLE IF NOT EXISTS", "loom_router.cells"): 1,
        ("CREATE TABLE IF NOT EXISTS", "loom_router.placements"): 1,
        ("DROP TRIGGER", "loom_router.placements"): 2,
        ("CREATE TRIGGER", "loom_router.placements"): 2,
        ("CREATE TABLE IF NOT EXISTS", "loom_router.hosts"): 1,
        ("CREATE TABLE IF NOT EXISTS", "loom_router.audit"): 1,
    },
    "schemas/router_migrations/002_cell_upstream.sql": {("ALTER TABLE", "loom_router.cells"): 1},
    "schemas/router_migrations/003_cell_next_credential.sql": {
        ("ALTER TABLE", "loom_router.cells"): 1
    },
    "schemas/router_migrations/005_control_generation.sql": {
        ("ALTER TABLE", "loom_router.cells"): 1
    },
}


def calls(node: ast.AST) -> set[str]:
    return {ast.unparse(child) for child in ast.walk(node) if isinstance(child, ast.Call)}


def function(path: str, name: str) -> ast.AST:
    return inventory((ROOT / path).read_text()).functions[name]


@pytest.mark.parametrize(
    ("path", "name", "required"),
    [
        (
            "server/cli/cell_update_run.py",
            "update",
            ['cell_control.run("update", run.start)'],
        ),
        (
            "server/cli/cell_update_run.py",
            "Run.start",
            [
                "cell_control.claim(owner.conn, self.call, self.row)",
                "local.paired(self.state, self.row)",
            ],
        ),
        (
            "server/cli/cell_update_pg.py",
            "write",
            ["owner.transaction(change)"],
        ),
        (
            "server/cli/cell_update_pg.py",
            "write.change",
            ["cell_control.paired(conn, registered)", "read(conn, expected.cell)"],
        ),
        (
            "server/router/directory.py",
            "update_addresses",
            ["_attach_change(cell_name, expected, change)"],
        ),
        (
            "server/router/directory.py",
            "configuration_placements",
            ["_attach_change(cell_name, expected, read)"],
        ),
        (
            "server/cli/cell_prepare.py",
            "prepare",
            [
                'cell_control.run("prepare", lambda owner: _prepare_locked(ch, args, router, transport, owner))'
            ],
        ),
        (
            "server/cli/cell_prepare.py",
            "_prepare_locked",
            [
                "cell_control.claim(conn, call, snapshot)",
                "owner.call(lambda: _write_rows(conn, rows, answer, current, nxt), total_s=cell_control.TRANSACTION_S)",
            ],
        ),
        (
            "server/cli/cell_prepare.py",
            "_write_rows",
            ["cell_control.transaction(conn, write)", "_read_rows(conn, count_tenancy=False)"],
        ),
        ("server/cli/cell_attach.py", "Run.run", ['cell_control.run("attach", self._owned)']),
        (
            "server/cli/cell_attach_pg.py",
            "_mode_try",
            [
                "owner.call(lambda: _mode_transaction(owner.conn, lock_ms, mark, expected_cell, expected_fp), total_s=bound_s)"
            ],
        ),
        (
            "server/cli/cell_control.py",
            "Owner.transaction",
            ["self.call(lambda: transaction(self.conn, work), total_s=TRANSACTION_S)"],
        ),
        (
            "server/cli/cell_attach.py",
            "Run._owned",
            ["cell_control.claim(self.lock_conn, self.router.call, self.row)"],
        ),
        (
            "server/cli/cell_attach.py",
            "Run.commit_mode",
            [
                "apg.commit_enforced(self.owner, self.confirm_lock, self.ready_for_commit, self.wait, self.log, mark, expected_cell=self.name, expected_fp=self.own_fp)"
            ],
        ),
        (
            "server/cli/cell_attach_pg.py",
            "_mode_transaction",
            ["cell_control.check_lock(conn)", "conn.transaction()", "tx.commit()"],
        ),
        (
            "server/cli/cell_control.py",
            "transaction",
            ["check_lock(conn)", "tx.commit()"],
        ),
        (
            "server/cli/cell_control.py",
            "claim",
            ["check_lock(conn)", "paired(conn, row)"],
        ),
        (
            "server/cli/cell_control.py",
            "backout",
            [
                "claim(conn, call, row)",
                'mutate(conn, call, row, "/limit", {"closed": True})',
                "owner.transaction(clear_state)",
                "owner.transaction(clear_triggers)",
                "paired(conn, row)",
            ],
        ),
        (
            "server/cli/cell.py",
            "_remove_locked",
            [
                'cell_control.run("remove", remove)',
                "cell_control.claim(conn, call, row)",
                "cell_control.check_lock(conn)",
            ],
        ),
        (
            "server/router/directory.py",
            "_control_transaction",
            [
                "owner.start(lambda: owner.work(change))",
                "owner.wait(task, owner.work_end)",
                "owner.finish(task)",
            ],
        ),
        (
            "server/router/directory.py",
            "_ControlOwner.work",
            [
                "self.context.__aenter__()",
                "self.conn.transaction()",
                "tx.start()",
                "change(self.conn)",
                "tx.commit()",
            ],
        ),
        (
            "server/router/directory.py",
            "_attach_change",
            [
                "_control_transaction(locked, seconds=CONTROL_MAX_S)",
                "control_refusal(attaching, expected, claim=claim)",
                "change(conn, attaching)",
            ],
        ),
        (
            "server/router/directory.py",
            "_operator_change",
            ["_attach_change(cell_name, expected, change_locked)", "change(conn, found.row)"],
        ),
        (
            "server/router/directory.py",
            "set_limit",
            ['_operator_change(cell_name, "limit", change, expected)'],
        ),
        (
            "server/router/directory.py",
            "rotate_credential",
            ['_operator_change(cell_name, "credential", change, expected)'],
        ),
        (
            "server/router/directory.py",
            "claim",
            ["_attach_change(cell_name, expected, change_locked, claim=True)"],
        ),
        (
            "server/router/directory.py",
            "update_configuration",
            [
                "_control_transaction(locked, seconds=CONTROL_MAX_S)",
                "control_refusal(found, expected)",
                "_configuration_commit(conn, found, current, pending, new, action)",
            ],
        ),
        *[
            (
                "server/router/directory.py",
                name,
                ["_attach_change(cell_name, expected, change_locked)"],
            )
            for name in (
                "attach_begin",
                "attach_place",
                "attach_enforced",
                "attach_serving",
                "remove",
            )
        ],
    ],
)
def test_guarded_entrypoints_keep_shared_boundaries(path, name, required):
    actual = calls(function(path, name))
    for expression in required:
        assert ast.unparse(ast.parse(expression, mode="eval").body) in actual, (
            path,
            name,
            expression,
        )


def test_dynamic_cleanup_scope_cannot_expand_unclassified():
    backout = function("server/cli/cell_control.py", "backout")
    cleanup_tables = [
        ast.literal_eval(node.iter)
        for node in ast.walk(backout)
        if isinstance(node, ast.For)
        and ast.unparse(node.target) == "table"
        and isinstance(node.iter, ast.Tuple)
    ]
    assert cleanup_tables == [("cell_first", "tenancy_mode", "cell_self", "tenancy")]
    assert "t.tgname IN ('tenancy_insert', 'tenancy_update', 'tenancy_record')" in ast.unparse(
        backout
    )


def test_reads_registration_and_immutable_placement_are_explicit_exceptions():
    path = "server/router/directory.py"
    found = inventory((ROOT / path).read_text())
    for name in (
        "routing",
        "credentials",
        "first_cell",
        "first_cell_named",
        "lookup",
        "attach_read",
    ):
        assert name in found.functions
        assert not any(owner == name for owner, _verb, _target in found.writes)
    # Operator reads may audit, but cannot bypass control guards for a state write.
    assert any(call.startswith("_audit(") for call in calls(found.functions["attach_read"]))
    assert "_rerun_refusal(held, reg)" in calls(found.functions["register"])
    assert "_place_one(conn, org, cell_name, candidate, refusal)" in calls(found.functions["place"])
    assert "_cell(locked)" in calls(found.functions["place"])
    assert "verify(candidate)" in calls(found.functions["place"])
    assert "refusal(candidate)" in calls(found.functions["_place_one"])
    for name in ("set_limit", "rotate_credential"):
        assert not any(call.startswith("claim(") for call in calls(found.functions[name]))


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ('"UPDATE loom.cell_self SET current_fp = $1"', ("UPDATE", "loom.cell_self")),
        (
            '"WITH expected AS (SELECT 1) UPDATE loom.cell_self SET current_fp = $1"',
            ("UPDATE", "loom.cell_self"),
        ),
        (
            '"WITH expected AS (SELECT 1) DELETE FROM loom_router.cells WHERE cell = $1"',
            ("DELETE FROM", "loom_router.cells"),
        ),
        (
            '"WITH changed AS (UPDATE loom.cell_self SET current_fp = $1 RETURNING 1) SELECT * FROM changed"',
            ("UPDATE", "loom.cell_self"),
        ),
        (
            '"WITH outer_cte AS (WITH inner_cte AS (SELECT 1) UPDATE loom_router.cells SET closed = true RETURNING cell) SELECT * FROM outer_cte"',
            ("UPDATE", "loom_router.cells"),
        ),
        (
            '"INSERT INTO loom_router.cells (cell) VALUES ($1)"',
            ("INSERT INTO", "loom_router.cells"),
        ),
        ('f"DELETE FROM loom.{table}"', ("DELETE FROM", "loom.{table}")),
        ('f"UPDATE {target} SET current_fp = $1"', ("UPDATE", "{target}")),
        ('"DROP TABLE IF EXISTS loom.cell_self"', ("DROP TABLE IF EXISTS", "loom.cell_self")),
        ('f\'DROP TRIGGER "{name}" ON loom."{table}"\'', ("DROP TRIGGER", "loom.{table}")),
    ],
)
def test_inventory_detects_new_literal_and_dynamic_writers(statement, expected):
    source = (
        f"async def unclassified(conn, table, target, name):\n    await conn.execute({statement})\n"
    )
    found = inventory(source).writes
    assert found == Counter({("unclassified", *expected): 1})
    assert not any(
        "unclassified" == owner
        for group in (GUARDED_WRITERS, EXEMPT_WRITERS, GENERIC_SQL_EXCLUSIONS)
        for entries in group.values()
        for owner, _verb, _target in entries
    )


def test_acceptance_writer_callsites_are_classified():
    # Another caller of a checked writer still needs its own claim and ownership proof.
    names = {"_write_rows", "_mode_try", "_mode_transaction", "commit_enforced", "backout"}
    actual = Counter()
    for path in (ROOT / "server").rglob("*.py"):
        found = inventory(path.read_text())
        for owner, node in found.functions.items():
            # These owners have no nested functions; walk only the direct callsites.
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    target = ast.unparse(child.func)
                    if target.rsplit(".", 1)[-1] in names:
                        actual[(str(path.relative_to(ROOT)), owner, target)] += 1
    assert actual == Counter(
        {
            ("server/cli/cell_prepare.py", "_prepare_locked", "_write_rows"): 1,
            ("server/cli/cell_attach_pg.py", "commit_enforced", "_mode_try"): 1,
            ("server/cli/cell_attach_pg.py", "_mode_try", "_mode_transaction"): 1,
            ("server/cli/cell_attach.py", "Run.commit_mode", "apg.commit_enforced"): 1,
        }
    )


def test_cte_data_modification_classifies_every_literal_sink():
    source = """
async def unclassified(conn):
    await conn.execute("WITH changed AS (DELETE FROM loom.cell_self RETURNING 1) "
                       "/* body done */ UPDATE loom_router.cells SET closed = true")
"""
    assert inventory(source).writes == Counter(
        {
            ("unclassified", "DELETE FROM", "loom.cell_self"): 1,
            ("unclassified", "UPDATE", "loom_router.cells"): 1,
        }
    )
