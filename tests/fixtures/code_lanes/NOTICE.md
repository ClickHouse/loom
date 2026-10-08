# code_lanes fixture corpus

Small files that exercise every code-plane lane. The real files are excerpts
from open-source repositories: each is cut short to the constructs under test
and kept under its own licence, named here. The rest are synthetic, written
for this corpus. Some fixtures are odd or invalid on purpose (an extensionless
bats test, a Jinja template, a platform-suffixed Go file), so don't reformat or
"fix" them. Every fixture path starts with exactly one prefix below, and
`tests/unit/test_code_lanes_manifest.py` checks that it does.

| path prefix | source | licence |
|---|---|---|
| sql/clickhouse/, shell/clickhouse/ | ClickHouse/ClickHouse tests, excerpt | Apache-2.0 |
| sql/goose/ | pressly/goose examples, excerpt | MIT |
| sql/sqlc/, go/sqlc/ | sqlc-dev/sqlc examples (the Go file is sqlc's generated output), excerpt | MIT |
| sql/dbt/models/ | dbt-labs/jaffle-shop-classic, excerpt | Apache-2.0 |
| sql/dbt/macros/ | dbt-labs/dbt-utils, excerpt | Apache-2.0 |
| sql/sakila/ | jOOQ/sakila, the head of each schema | BSD-2-Clause |
| sql/timescaledb/ | timescale/timescaledb, excerpt | Apache-2.0 (sql/) |
| shell/bats-core/, hosts/Dockerfile | bats-core/bats-core, excerpt | MIT |
| shell/nvm/, hosts/.github/, hosts/Makefile | nvm-sh/nvm, excerpt | MIT |
| ts/langfuse/, rust/langfuse/, prisma/langfuse/, sql/langfuse/ | langfuse/langfuse (core, not `ee/`) at f4dccff9, excerpt | MIT |
| sql/dbmate/, sql/flyway/, sql/liquibase/, sql/prisma/, sql/sqitch/ | synthetic, in each migration tool's file layout and header syntax | — |
| shell/lib/, shell/scripts/ | synthetic: sourced libraries and islands of SQL in shell | — |
| hosts/.gitlab-ci.yml, hosts/Jenkinsfile | synthetic CI configs | — |
| python/ | synthetic: layouts (src, legacy, namespace, vendored, notebooks, stubs, plugins), imports, members, a Django-style site, a ClickHouse-style integration test | — |
| go/app/, go/constructs/, go/mono/ | synthetic: a module, build-constrained files and a two-module monorepo | — |
| java/ | synthetic: a Spring / JDBC repository, a generic interface and a generic class implementing it | — |
| js/, ts/constructs/, ts/synthetic/ | synthetic: CommonJS, IIFE, class expressions, overloads, React components, SQL in template literals | — |
| vue/ | synthetic single-file components and a composable | — |
| proto/, helm/ | synthetic | — |
| rust/synthetic/ | synthetic: a trait and its impl, a declarative macro, an async function | — |
| sql/views/ | synthetic: a ClickHouse table, a materialized view into its rollup, a view and a CTE | — |
| tla/, fbs/, cmake/, md/ | synthetic: one toy ledger described in TLA+, FlatBuffers, CMake and Markdown (with fenced code) | — |

`python -m tests.code_lanes.corpus --write` regenerates `expected.json`, and
`tests/unit/test_code_lanes_corpus.py` compares the corpus against it. Review
every moved number: it is a change to what a namespace holds. `LANES` in
`tests/code_lanes/corpus.py` names each lane's language, what its fixtures
cover and what the lane does not derive.
