"""SQL statements assembled across assignments, same-folder duplicate
definitions, CWD-relative shell `source`, and test-local shell definitions."""

from __future__ import annotations

from server.code.domain.extract import lang_sql_objects as lo
from server.code.domain.extract.lang_go import go_sql_units, parse_go
from server.code.domain.extract.lang_java import java_sql_units, parse_java
from server.code.domain.extract.lang_py_islands import py_sql_units
from server.code.domain.extract.lang_python import parse_python
from server.code.domain.extract.lang_shell_islands import (
    CWD_MARK,
    cwd_alternate,
    settle_cwd_includes,
)
from server.code.domain.extract.name_scope import SQL_OBJECT_KINDS, drop_test_local_shell_defs

K = SQL_OBJECT_KINDS


# ------------------------------------------- statements assembled across assignments


def test_same_folder_duplicates_resolve_to_the_newest_definition() -> None:
    table = {
        "users": [
            ("id1", "db.migrations.0001_init::users", "table"),
            ("id2", "db.migrations.0002_recreate_users::users", "table"),
        ]
    }
    hit = lo.resolve_object("users", "", K, [], table, "db/queries/list.sql")
    assert hit == ("id2", "db.migrations.0002_recreate_users::users", 0.8)
    # two directories at the same depth still bind nothing
    table = {"users": [("a", "db.x.a::users", "table"), ("b", "db.y.b::users", "table")]}
    assert lo.resolve_object("users", "", K, [], table, "db/q.sql") is None
    # two schemas' `users` in one folder are two objects, not one re-created
    table = {"users": [("a", "db.s::old.users", "table"), ("b", "db.s::new.users", "table")]}
    assert lo.resolve_object("users", "", K, [], table, "db/q.sql") is None
    # … unless the reference is written in one of them
    assert lo.resolve_object("users", "new", K, [], table, "db/q.sql") == (
        "b",
        "db.s::new.users",
        0.95,
    )


# ------------------------------------------------- same-folder duplicate definitions


def test_python_statement_assembled_across_assignments() -> None:
    src = (
        "def test(node, db):\n"
        '    q = "SELECT id, name"\n'
        '    q += " FROM users"\n'
        "    q = q + f\" WHERE db = '{db}'\"\n"
        "    node.query(q)\n"
        '    m = "Delete"\n'
        '    m += " it"\n'
    )
    u = py_sql_units(parse_python("t/test.py", src.encode()))
    assert [(i.via, i.line, i.interpolated, i.text) for i in u.islands] == [
        ("assembled", 2, True, "SELECT id, name FROM users WHERE db = '__var__'"),
    ]
    assert ("table_read", "users") in {(f.kind, f.name) for f in u.facets}


def test_go_and_java_statements_assembled_across_assignments() -> None:
    go = (
        "package a\n"
        "func f(id int) {\n"
        '\tq := "SELECT id, name"\n'
        '\tq += " FROM users"\n'
        '\tq = q + " WHERE id = " + strconv.Itoa(id)\n'
        "\trows, _ := db.Query(q)\n"
        '\tone := "INSERT INTO t (a) VALUES (1)"\n'
        "\t_ = db.Exec(one)\n"
        "}\n"
    )
    u = go_sql_units(parse_go("a/b.go", go.encode()))
    assert [(i.via, i.line, i.text) for i in u.islands] == [
        ("assembled", 3, "SELECT id, name FROM users WHERE id = __var__"),
        ("literal", 7, "INSERT INTO t (a) VALUES (1)"),  # one assignment: the literal lane's
    ]
    java = (
        "class R {\n  void f(int id) {\n"
        '    String sql = "UPDATE users";\n'
        '    sql += " SET name = ?";\n'
        '    sql = sql + " WHERE id = " + id;\n'
        "    jdbc.update(sql, name, id);\n"
        "  }\n}\n"
    )
    u = java_sql_units(parse_java("R.java", java.encode()))
    assert [(i.via, i.line, i.text) for i in u.islands] == [
        ("assembled", 3, "UPDATE users SET name = __var__ WHERE id = __var__"),
    ]


# --------------------------------------------------------- CWD-relative shell source


def test_relative_source_carries_a_cwd_alternate_settled_by_presence() -> None:
    # the script-relative reading is `ci/lib/common.sh`; the working-directory
    # reading is `lib/common.sh` — the ingest keeps it only when it exists
    assert cwd_alternate("ci/build.sh", "lib/common.sh") == CWD_MARK + "lib/common.sh"
    assert cwd_alternate("ci/build.sh", "./lib/common.sh") == CWD_MARK + "lib/common.sh"
    assert cwd_alternate("build.sh", "lib/common.sh") == ""  # both readings coincide
    assert cwd_alternate("ci/build.sh", "../lib/common.sh") == ""  # above the root
    assert cwd_alternate("ci/build.sh", "$ROOT/lib/common.sh") == ""
    assert cwd_alternate("ci/build.sh", "/etc/profile") == ""
    includes = ["ci/lib/common.sh", CWD_MARK + "lib/common.sh", CWD_MARK + "tools/x.sh"]
    assert settle_cwd_includes(includes, {"lib/common.sh"}) == ["ci/lib/common.sh", "lib/common.sh"]
    assert settle_cwd_includes(includes, set()) == ["ci/lib/common.sh"]


# ------------------------------------------------------ test-local shell definitions


def test_test_directory_shell_functions_bind_only_within_it() -> None:
    table = {
        "git": [("m", "test.mocks.git_mock::git", "function")],
        "setup_repo": [
            ("h", "test.helpers::setup_repo", "function"),
            ("g", "scripts.lib::setup_repo", "function"),
        ],
        "log": [("l", "scripts.lib::log", "function")],
    }
    # a script outside the test tree: the mock and the test helper vanish
    assert drop_test_local_shell_defs(table, "scripts/deploy.sh") == {
        "setup_repo": [("g", "scripts.lib::setup_repo", "function")],
        "log": [("l", "scripts.lib::log", "function")],
    }
    # a test in the same tree keeps them
    kept = drop_test_local_shell_defs(table, "test/mocks/x.bats")
    assert kept["git"] == [("m", "test.mocks.git_mock::git", "function")]
    assert len(kept["setup_repo"]) == 2
    # a test in a different test tree does not see another tree's mock
    assert "git" not in drop_test_local_shell_defs(table, "spec/y.bats")
