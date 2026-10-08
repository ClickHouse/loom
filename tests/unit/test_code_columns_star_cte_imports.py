"""SQL columns (`SELECT *`, CTE / derived-table columns, writes inside rule
and function bodies) and Go module-path / Java source-root import resolution."""

from __future__ import annotations

from server.code.domain.extract import lang_sql_columns as lc
from server.code.domain.extract.lang_go import (
    go_import_candidates,
    go_module_path,
    go_resolve_imports,
    parse_go,
)
from server.code.domain.extract.lang_java import (
    java_import_candidates,
    java_package,
    java_resolve_imports,
    java_source_root,
    parse_java,
)
from server.code.domain.extract.lang_sql_split import split_statements

DDL = """
CREATE TABLE users (id UInt64, email String, created DateTime) ENGINE = MergeTree ORDER BY id;
CREATE TABLE orders (id UInt64, user_id UInt64, total Decimal(10,2)) ENGINE = MergeTree ORDER BY id;
CREATE TABLE payment_p2007_01 (payment_id Int, customer_id Int, amount Int);
"""


def _refs(sql: str) -> list[lc.ColumnRefs]:
    sts = split_statements(DDL + sql)
    known = lc.file_columns(sts)
    return [lc.column_refs(st, known) for st in sts[3:]]


# ------------------------------------------------------------------ SQL columns


def test_select_star_expands_to_the_known_columns() -> None:
    (plain, aliased, unknown) = _refs("""
SELECT * FROM users WHERE id = 1;
SELECT u.* FROM users u JOIN orders o ON o.user_id = u.id;
SELECT * FROM nobody_defines_me;
""")
    assert plain.reads == [
        ("users", "id", 0.9),
        ("users", "created", lc.CONF_STAR),
        ("users", "email", lc.CONF_STAR),
    ]
    assert {(t, c, k) for t, c, k in aliased.reads} == {
        ("orders", "user_id", 0.95),
        ("users", "id", 0.95),
        ("users", "created", lc.CONF_STAR),
        ("users", "email", lc.CONF_STAR),
    }
    assert unknown.reads == []  # nothing known to expand to


def test_cte_and_derived_table_columns_carry_their_source() -> None:
    (cte_qualified, cte_bare, derived) = _refs("""
WITH recent AS (SELECT id, email AS mail FROM users WHERE created > now()) SELECT recent.mail FROM recent;
WITH c AS (SELECT id, email FROM users) SELECT email FROM c WHERE id > 1;
SELECT s.total FROM (SELECT o.total, o.user_id FROM orders o) AS s WHERE s.user_id = 3;
""")
    assert ("users", "email", 0.95) in cte_qualified.reads  # recent.mail → users.email
    assert {(t, c) for t, c, _ in cte_bare.reads} == {("users", "id"), ("users", "email")}
    assert {(t, c, k) for t, c, k in derived.reads} == {
        ("orders", "total", 0.95),
        ("orders", "user_id", 0.95),
    }


def test_writes_inside_rule_and_function_bodies_are_explicit() -> None:
    (rule,) = _refs("""
CREATE RULE payment_insert_p2007_01 AS ON INSERT TO payment WHERE (new.payment_date >= '2007-01-01')
DO INSTEAD INSERT INTO payment_p2007_01 (payment_id, customer_id, amount)
VALUES (DEFAULT, new.customer_id, new.amount);
""")
    assert rule.writes == [
        ("payment_p2007_01", "payment_id", 1.0),
        ("payment_p2007_01", "customer_id", 1.0),
        ("payment_p2007_01", "amount", 1.0),
    ]


# -------------------------------------------------------- Go and Java imports


def test_go_imports_resolve_inside_the_module_only() -> None:
    assert go_module_path("module github.com/acme/app\n\ngo 1.22\n") == "github.com/acme/app"
    assert go_module_path("go 1.22\n") == ""
    cands = go_import_candidates("github.com/acme/app/internal/repo", "github.com/acme/app")
    assert cands[:2] == ["internal/repo/repo.go", "internal/repo/doc.go"]
    assert go_import_candidates("fmt", "github.com/acme/app") == []
    assert go_import_candidates("github.com/jackc/pgx/v5", "github.com/acme/app") == []
    present = {"internal/repo/repo.go", "cmd/main.go"}
    assert go_resolve_imports(
        ["fmt", "github.com/acme/app/internal/repo", "github.com/acme/app/missing"],
        "github.com/acme/app",
        present,
    ) == ["internal/repo/repo.go"]
    # a module rooted below the repository root keeps its directory
    assert go_import_candidates("example.com/app/internal/repo", "example.com/app", "go/app")[
        0
    ] == ("go/app/internal/repo/repo.go")
    pf = parse_go("cmd/main.go", b'package main\nimport (\n\t"fmt"\n\t"example.com/app/x"\n)\n')
    assert pf.includes == []  # raw module paths are not include edges at parse


def test_java_imports_resolve_under_the_files_source_root() -> None:
    pf = parse_java(
        "svc/src/main/java/com/acme/repo/UserRepository.java",
        b"package com.acme.repo;\nimport com.acme.model.User;\nclass UserRepository {}\n",
    )
    assert java_package(pf) == "com.acme.repo"
    root = java_source_root(pf.path, "com.acme.repo")
    assert root == "svc/src/main/java"
    assert java_source_root("Foo.java", "") == ""
    assert (
        java_source_root("x/y/Foo.java", "com.acme") == ""
    )  # the path does not end in the package
    cands = java_import_candidates("com.acme.model.User", [root])
    assert cands[0] == "svc/src/main/java/com/acme/model/User.java"
    assert java_import_candidates("java.util.List", [root]) == []
    assert java_import_candidates("static com.acme.util.Ids.next", [root])[1] == (
        "svc/src/main/java/com/acme/util/Ids.java"
    )
    present = {"svc/src/main/java/com/acme/model/User.java"}
    assert java_resolve_imports(["com.acme.model.User", "java.util.List"], [root], present) == [
        "svc/src/main/java/com/acme/model/User.java"
    ]
    assert pf.includes == []
