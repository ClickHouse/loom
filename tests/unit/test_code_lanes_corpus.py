"""The SQL / shell lanes corpus gate — every fixture under
tests/fixtures/code_lanes derives exactly what expected.json records.
Regenerate with `python -m tests.code_lanes.corpus --write` and review the
diff; a moved number is a changed namespace."""

from __future__ import annotations

import json

import pytest

from tests.code_lanes.corpus import EXPECTED, summarize


@pytest.fixture(scope="module")
def summary() -> dict:
    return summarize()


def test_corpus_matches_expected_json(summary: dict) -> None:
    expected = json.load(open(EXPECTED))
    changed = {
        p
        for p in set(expected["files"]) | set(summary["files"])
        if expected["files"].get(p) != summary["files"].get(p)
    }
    assert not changed, (
        f"lanes drifted for {sorted(changed)} — review, then "
        "`python -m tests.code_lanes.corpus --write`"
    )
    assert summary["edges"] == expected["edges"], "edge derivation drifted (see corpus.py)"
    assert summary["misses"] == expected["misses"]


def test_the_lanes_each_carry_their_rows(summary: dict) -> None:
    f = summary["files"]
    # a ClickHouse test has statement records, its table, reads and writes
    ch = f["sql/clickhouse/00061_merge_tree_alter.sql"]
    assert ch["symbols"]["statement"] > 10 and ch["symbols"]["table"] >= 1
    assert ch["facets"]["table_read"] > 0 and ch["facets"]["table_write"] > 0
    # a .sql.j2 test yields only SQL kinds
    j2 = f["sql/clickhouse/00700_to_decimal_or_something_1.sql.j2"]
    assert set(j2["symbols"]) == {"statement"}
    # every migration tool names itself, its direction and (where the name says) its version
    for path in (
        "sql/goose/migrations/00002_rename_root.sql",
        "sql/dbmate/db/migrate/20240101120000_users.sql",
        "sql/liquibase/changelog/001.sql",
        "sql/flyway/V1_2__users.sql",
        "sql/prisma/migrations/20240101120000_init/migration.sql",
        "sql/sqitch/deploy/users.sql",
    ):
        assert f[path]["facets"].get("tool") == 1 and f[path]["facets"].get("direction"), path
    assert f["sql/liquibase/changelog/001.sql"]["symbols"]["changeset"] == 2
    assert f["sql/sqlc/authors/query.sql"]["symbols"]["query"] == 4
    # terminators: GO / DELIMITER / slash schemas split into their objects
    assert f["sql/sakila/mysql-sakila-schema.sql"]["symbols"]["trigger"] == 3
    assert f["sql/sakila/sql-server-sakila-schema.sql"]["symbols"]["table"] == 4
    assert f["sql/sakila/oracle-sakila-schema.sql"]["symbols"]["sequence"] == 2
    assert f["sql/timescaledb/test/sql/updates/post.v11.sql"]["includes"]  # psql \ir
    # dbt models and macros
    assert f["sql/dbt/models/orders.sql"]["symbols"]["model"] == 1
    assert f["sql/dbt/macros/star.sql"]["symbols"]["macro"] == 2
    # shell islands with dialects, programs, resolved includes
    sh = f["shell/clickhouse/00092_obfuscator.sh"]
    assert sh["facets"]["dialect"] > 0 and sh["facets"]["statement_kind"] > 0
    load = f["shell/scripts/load.sh"]
    assert load["facets"]["dialect"] == 2 and load["facets"]["program"] == 2
    assert load["includes"] == ["shell/lib/common.sh", "shell/scripts/notify.sh"]
    # bats tests and extensionless tests are shell with functions
    assert f["shell/bats-core/test/cat-formatter.bats"]["symbols"]["function"] >= 3
    assert any(p.startswith("shell/nvm/") and f[p]["symbols"].get("function") for p in f)
    # host files: Dockerfile, Makefile, CI configs
    assert f["hosts/Dockerfile"]["symbols"]["stage"] == 3
    assert f["hosts/Makefile"]["symbols"]["target"] >= 3
    assert f["hosts/.gitlab-ci.yml"]["symbols"]["job"] == 1
    assert f["hosts/Jenkinsfile"]["includes"] == ["scripts/package.sh"]
    # the SQL a Python file runs — harness client and DB-API / SQLAlchemy clients
    py = f["python/tests/integration/test_kafka_islands/test.py"]
    assert (
        py["symbols"]["statement"] == 10
        and py["symbols"]["table"] == 2
        and py["symbols"]["view"] == 1
    )
    assert (
        py["facets"]["dialect"] == 10
        and py["facets"]["engine"] == 2
        and py["facets"]["setting"] == 5
    )
    app = f["python/app/repository.py"]
    assert (
        app["symbols"]["statement"] == 4 and "dialect" not in app["facets"]
    )  # no client named one
    # Go (sqlc's generated query constants) and Java (JDBC / Spring / annotations)
    go = f["go/sqlc/authors/postgresql/query.sql.go"]
    # sqlc's `const listUsers = `-- name: …`` query texts are const symbols too
    assert go["symbols"] == {"const": 4, "field": 2, "function": 4, "statement": 4, "struct": 1}
    assert go["facets"]["dialect"] == 4 and go["facets"]["table_read"] == 2  # pgx import → postgres
    jv = f["java/src/main/java/com/acme/repo/UserRepository.java"]
    assert jv["symbols"]["statement"] == 7 and jv["symbols"]["function"] == 6
    assert jv["symbols"]["table"] == 1 and jv["symbols"]["column"] == 3  # its own CREATE TABLE


def test_edges_cross_files_where_the_corpus_defines_the_target(summary: dict) -> None:
    e = summary["edges"]
    # queries → schema.sql's authors; four, counting `INSERT INTO authors (name, bio)`
    assert e["sql/sqlc/authors/query.sql | calls | table"] == 4
    assert e["sql/dbt/models/orders.sql | calls | model"] == 1  # ref('stg_orders')
    assert e["sql/clickhouse/00061_merge_tree_alter.sql | calls | table"] > 20
    assert e["shell/scripts/load.sh | includes | file"] == 2
    assert e["sql/timescaledb/test/sql/updates/post.v11.sql | includes | file"] == 1
    # island statements bind to the tables / views the Python file itself creates
    assert e["python/tests/integration/test_kafka_islands/test.py | calls | table"] == 5
    assert e["python/tests/integration/test_kafka_islands/test.py | calls | view"] == 1
    # sqlc's Go binds to schema.sql's table and columns across files
    assert e["go/sqlc/authors/postgresql/query.sql.go | calls | table"] == 4
    assert e["go/sqlc/authors/postgresql/query.sql.go | calls | column"] == 9
    # a Go import inside the fixture module and a Java import under the
    # file's source root resolve to repository files (and the call binds through)
    assert e["go/app/cmd/main.go | includes | file"] == 1
    assert e["go/app/cmd/main.go | calls | function"] == 1  # s.Count() → repo.Store.Count
    # a multi-module fixture — one import from the file's own module
    # (through the directory-listing rung: `serve.go` carries no conventional name)
    # and one from a sibling module a replace directive names
    assert e["go/mono/svc/main.go | includes | file"] == 2
    assert e["go/mono/svc/main.go | calls | function"] == 2
    assert e["java/src/main/java/com/acme/repo/UserRepository.java | includes | file"] == 1
    # `SELECT *` over a known table and CTE columns widen the column edges
    assert e["sql/clickhouse/00061_merge_tree_alter.sql | calls | column"] == 72
    assert not any(k.endswith("| ?") for k in e), "an edge to a symbol the corpus never defined"


def test_the_typescript_rust_and_prisma_lanes_carry_their_rows(summary: dict) -> None:
    f = summary["files"]
    ts = "ts/langfuse/"
    # a tRPC router is a const with its procedures as functions; a
    # Next.js API route's handler is a function under `default`
    router = f[ts + "web/src/server/api/routers/observations.ts"]
    assert router["symbols"] == {"const": 1, "function": 1}
    route = f[ts + "web/src/pages/api/public/events.ts"]
    assert route["symbols"]["const"] == 1 and route["symbols"]["function"] == 1
    # a vitest file is suites and test functions, a component a function
    spec = f[ts + "web/src/__tests__/server/sessionExpiration.servertest.ts"]
    assert spec["symbols"]["suite"] == 1 and spec["symbols"]["function"] == 2
    assert f[ts + "web/src/components/EnvLabelBadge.tsx"]["symbols"]["function"] >= 1
    # a relative import binds to the corpus file it names, a package does not
    repo = f[ts + "packages/shared/src/server/repositories/daily-metrics.ts"]
    assert repo["includes"] == [ts + "packages/shared/src/server/repositories/constants.ts"]
    # a subpath import resolves through the package's `exports` table
    uses = f["ts/synthetic/web/src/usesQuery.ts"]
    assert uses["includes"] == [ts + "packages/shared/src/features/query/index.ts"]
    # the barrel it lands on forwards to `widgetVersion.ts`, one level, at 0.9
    assert summary["edges"].get("ts/synthetic/web/src/usesQuery.ts | includes | file") == 2
    # the ClickHouse queries inside it are statement records with the
    # client's dialect, and they read `observations` — which the canonical
    # migration in the corpus creates, so the edge binds
    assert repo["symbols"]["statement"] == 2
    assert repo["facets"]["dialect"] == 2 and repo["facets"]["table_read"] >= 1
    assert (
        summary["edges"].get(
            f"{ts}packages/shared/src/server/repositories/daily-metrics.ts | calls | table"
        )
        == 2
    )
    # a Rust module's items, its impl skeleton, its `mod x;` includes
    sse = f["rust/langfuse/ai-gateway/src/capture/sse.rs"]
    assert sse["symbols"]["struct"] == 1 and sse["symbols"]["impl"] == 1
    assert sse["symbols"]["function"] >= 3
    mod = f["rust/langfuse/ai-gateway/src/capture/mod.rs"]
    assert "rust/langfuse/ai-gateway/src/capture/sse.rs" in mod["includes"]
    # the schema's models and enums, its mapped tables, its relations
    schema = f["prisma/langfuse/packages/shared/prisma/schema.prisma"]
    assert schema["symbols"] == {"enum": 1, "model": 5}
    assert schema["facets"]["creates"] == 5
    assert summary["edges"].get(
        "prisma/langfuse/packages/shared/prisma/schema.prisma | uses_type | model"
    )
