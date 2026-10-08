"""Go and Java lanes — symbols, and the SQL the files run
through the grammar-as-data island contract; the stamped-grammar allowlist."""

from __future__ import annotations

from server.code.domain.extract.lang_go import go_sql_units, parse_go
from server.code.domain.extract.lang_java import java_sql_units, parse_java

GO = b"""package repo

import (
	"context"
	"database/sql"
	"fmt"
	"github.com/jackc/pgx/v5"
)

const listUsers = `-- name: ListUsers :many
SELECT id, email FROM users WHERE active = $1 ORDER BY id`

type Store struct{ db *sql.DB }

type Reader interface{ Get(ctx context.Context, id int) error }

func (s *Store) Get(ctx context.Context, id int) error {
	row := s.db.QueryRowContext(ctx, "SELECT id, email FROM users WHERE id = $1", id)
	_, err := s.db.ExecContext(ctx, fmt.Sprintf("UPDATE users SET email = '%s' WHERE id = %d", "x", id))
	_, _ = s.db.Exec("INSERT INTO audit (user_id, action) VALUES ($1, $2)", id, "get")
	q := "DELETE FROM sessions WHERE user_id = " + strconv.Itoa(id) + " AND expired"
	_, _ = s.db.Exec(q)
	msg := "Select a user first"
	_ = msg
	return err
}

func helper() string { return "CREATE" }
"""

JAVA = b'''package com.acme.repo;

import java.sql.Connection;
import org.springframework.jdbc.core.JdbcTemplate;
import org.postgresql.Driver;

public class UserRepository {
    private final JdbcTemplate jdbc;

    @Query("SELECT u FROM User u WHERE u.email = :email")
    User findByEmail(String email);

    @Select("SELECT id, name FROM users WHERE id = #{id}")
    User byId(int id);

    public List<User> all() {
        return jdbc.query("SELECT id, name FROM users ORDER BY id", mapper);
    }

    public int rename(long id, String name) {
        String sql = """
            UPDATE users
            SET name = ?
            WHERE id = ?
            """;
        jdbc.update(sql, name, id);
        return jdbc.update("DELETE FROM sessions WHERE user_id = " + id);
    }

    public void batch(Connection c) throws Exception {
        try (var ps = c.prepareStatement(String.format("INSERT INTO %s (id) VALUES (?)", "users"))) {
            ps.executeUpdate();
        }
        String label = "Update available";
    }

    enum Kind { A, B }
}
'''


def test_go_symbols_and_islands() -> None:
    pf = parse_go("internal/repo/store.go", GO)
    kinds = {(s.kind, s.qualified_name) for s in pf.symbols}
    # Package-scoped names, `::` between receiver and method,
    # an interface's method signatures as body-less `method` symbols
    assert {k for k in kinds if k[0] not in ("method", "field")} == {
        ("struct", "internal.repo.repo::Store"),
        ("interface", "internal.repo.repo::Reader"),
        ("function", "internal.repo.repo::Store::Get"),
        ("function", "internal.repo.repo::helper"),
        ("const", "internal.repo.repo::listUsers"),
    }
    assert all(qn.startswith("internal.repo.repo::Reader::") for k, qn in kinds if k == "method")
    assert pf.includes == []  # module paths are not repository files
    u = go_sql_units(pf)
    rows = [(i.via, i.line, i.dialect, i.text) for i in u.islands]
    assert rows == [
        (
            "literal",
            10,
            "postgres",
            "-- name: ListUsers __var__\nSELECT id, email FROM users WHERE active = __var__ ORDER BY id",
        ),
        ("call", 18, "postgres", "SELECT id, email FROM users WHERE id = __var__"),
        ("call", 19, "postgres", "UPDATE users SET email = '__var__' WHERE id = __var__"),
        ("call", 20, "postgres", "INSERT INTO audit (user_id, action) VALUES (__var__, __var__)"),
        ("literal", 21, "postgres", "DELETE FROM sessions WHERE user_id = __var__ AND expired"),
    ]
    facets = {(f.kind, f.name) for f in u.facets}
    assert {
        ("table_read", "users"),
        ("table_write", "users"),
        ("table_write", "audit"),
        ("table_write", "sessions"),
        ("dialect", "postgres"),
    } <= facets
    assert not any("Select a user" in i.text or i.text == "CREATE" for i in u.islands)


def test_java_symbols_and_islands() -> None:
    pf = parse_java("src/main/java/com/acme/repo/UserRepository.java", JAVA)
    inner = {(s.kind, s.qualified_name.split("::", 1)[1]) for s in pf.symbols}
    assert inner == {
        ("class", "UserRepository"),
        ("function", "UserRepository.findByEmail"),
        ("function", "UserRepository.byId"),
        ("function", "UserRepository.all"),
        ("function", "UserRepository.rename"),
        ("function", "UserRepository.batch"),
        ("enum", "UserRepository.Kind"),
    }
    u = java_sql_units(pf)
    rows = [(i.via, i.line, i.text.replace("\n", " ").strip()) for i in u.islands]
    assert rows == [
        ("annotation", 10, "SELECT u FROM User u WHERE u.email = __var__"),
        ("annotation", 13, "SELECT id, name FROM users WHERE id = __var__"),
        ("call", 17, "SELECT id, name FROM users ORDER BY id"),
        (
            "literal",
            21,
            "UPDATE users             SET name = __var__             WHERE id = __var__",
        ),
        ("call", 27, "DELETE FROM sessions WHERE user_id = __var__"),
        ("call", 31, "INSERT INTO __var__ (id) VALUES (__var__)"),
    ]
    assert {i.dialect for i in u.islands} == {"postgres"}  # org.postgresql import
    assert ("table_write", "sessions") in {(f.kind, f.name) for f in u.facets}
    assert not any("__var__" in f.name for f in u.facets)


def test_ingest_dispatches_the_lanes() -> None:
    from server.code.app.index.ingest_parse import _facets_ride_symbols, parse_file

    pf = parse_file("internal/repo/store.go", GO)
    assert pf is not None and pf.lang == "go"
    assert sum(s.kind == "statement" for s in pf.symbols) == 5
    assert _facets_ride_symbols(pf, pf.path, False) is True
    plain = parse_file("a/b.go", b"package b\nfunc F() int { return 1 }\n")
    assert plain is not None and _facets_ride_symbols(plain, "a/b.go", False) is False
    pf = parse_file("src/main/java/com/acme/repo/UserRepository.java", JAVA)
    assert pf is not None and pf.lang == "java"
    assert any(s.kind == "statement" for s in pf.symbols)


def test_call_edges_walk_go_and_java_callees() -> None:
    from server.code.domain.extract.edges_treesitter import extract_edges_and_misses

    pf = parse_go(
        "a/b.go",
        b"package b\nfunc helper() int { return 1 }\nfunc F() int { return helper() + len(x) }\n",
    )
    edges, misses = extract_edges_and_misses(pf, {}, None)
    assert [(e.from_qualified_name, e.to_qualified_name) for e in edges] == [
        ("a.b::F", "a.b::helper")
    ]
    assert [m.name for m in misses] == []  # `len` is noise
    pf = parse_java(
        "A.java",
        b"class A {\n  int h() { return 1; }\n  int f() {\n    return h() + Math.max(1, 2);\n  }\n}\n",
    )
    edges, misses = extract_edges_and_misses(pf, {}, None)
    assert [(e.from_qualified_name, e.to_qualified_name) for e in edges] == [("A::A.f", "A::A.h")]


def test_only_stage_grammars_move_the_stamp() -> None:
    from server.code.domain.extract import extractor_stamp as es

    assert es.STAMPED_GRAMMARS == {
        "tree-sitter",
        "tree-sitter-cpp",
        "tree-sitter-python",
        "tree-sitter-bash",
    }
    assert set(es.grammar_versions()) <= es.STAMPED_GRAMMARS
    # the lane grammars this host carries are not part of the identity
    assert not {"tree-sitter-go", "tree-sitter-java", "tree-sitter-typescript"} & set(
        es.grammar_versions()
    )
