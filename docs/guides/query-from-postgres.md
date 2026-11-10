# Query Loom from Postgres

This guide reads a tenant's memories, conversations and usage from the org's own
Postgres, where they can be joined with the application's tables. It uses the
open-source [`pg_clickhouse`](https://github.com/ClickHouse/pg_clickhouse) extension,
which imports ClickHouse tables and views as Postgres foreign tables and pushes queries
down to ClickHouse. What the views hold and what they leave out is in the
[SQL surface reference](../reference/sql-surface.md).

The data stays in ClickHouse: a query from Postgres reads the views at query time,
under the same scope, withholding and freshness rules as any other SQL client. This
Postgres is the application's, not Loom's [memory authority](../operations/postgres.md):
the extension reads, Loom never writes through it, and Loom's own database needs no
extension. Use a separate database, or a separate service, from the one Loom's
`LOOM_PG_DSN` names.

`pg_clickhouse` 0.11.0 on Postgres 16 and 18 is the tested combination
(`tests/integration/test_pg_clickhouse_live_ch.py`).

## 1. Make a SQL user for the tenant

On the Loom side, as an operator ([access](../reference/sql-surface.md#access)):

```bash
loom sql-access grant --user acme_bi --org acme --namespaces support,research
```

It prints the user's password once.

## 2. Install the extension

The extension is in the `ghcr.io/clickhouse/pg_clickhouse:<postgres major>-<version>`
images, such as `ghcr.io/clickhouse/pg_clickhouse:16-0.11.0`, and on
[PGXN](https://pgxn.org/dist/pg_clickhouse/) to build into an existing Postgres
(`pgxn install pg_clickhouse`). Then, as a Postgres user that may create extensions:

```sql
CREATE EXTENSION pg_clickhouse;
```

## 3. Point it at the ClickHouse service

```sql
CREATE SERVER loom_ch FOREIGN DATA WRAPPER clickhouse_fdw
  OPTIONS (driver 'http', host 'abc123.us-east-1.aws.clickhouse.cloud', dbname 'loom');

CREATE USER MAPPING FOR app_reader SERVER loom_ch
  OPTIONS (user 'acme_bi', password '<password from step 1>');
```

- `dbname` is Loom's database (`LOOM_CH_DATABASE`, `loom` by default).
- On a ClickHouse Cloud host the extension uses TLS and port 8443 by itself, as its
  documentation says; the test here runs plain HTTP against a local server. On a
  self-managed server, set `port` and `secure 'on'` to match the server's TLS port.
- Map only the Postgres roles that may read the tenant's data: the user mapping holds the
  ClickHouse password, and every query through the server runs as that SQL user.

Set the extension's session settings on the role, so every session sends the four a SQL
user of the surface may change and nothing else:

```sql
ALTER ROLE app_reader SET pg_clickhouse.session_settings =
  'join_use_nulls 1, group_by_use_nulls 1, final 1, transform_null_in 0';
```

These are the extension's defaults, written down so a later release's defaults cannot add
one. A session that sets another setting fails in ClickHouse with `READONLY`: the SQL user's
profile refuses it ([access](../reference/sql-surface.md#access)), whatever the role says.

## 4. Import the views

```sql
CREATE SCHEMA loom;
IMPORT FOREIGN SCHEMA loom
  LIMIT TO (sql_v1_memories, sql_v1_memory_history, sql_v1_conversations,
            sql_v1_agent_events, sql_v1_usage_daily)
  FROM SERVER loom_ch INTO loom;
```

The first `loom` is the ClickHouse database and the second the Postgres schema. Name the
views with `LIMIT TO`: the SQL user can describe only the views, and an import without it
asks for every table of the database. Import again after a Loom upgrade to pick up any
columns a `v1` view gained.

## 5. Query and join

```sql
-- The facts Loom holds about each account on the team plan
SELECT a.email, m.memory_key, m.value, m.updated_at
FROM accounts AS a
JOIN loom.sql_v1_memories AS m ON m.user_id = a.loom_user_id
WHERE a.plan = 'team' AND m.status = 'active'
ORDER BY m.updated_at DESC;

-- Model spend per day for the last month
SELECT day, sum(upstream_cost_usd) AS spend
FROM loom.sql_v1_usage_daily
WHERE day >= current_date - 30
GROUP BY day ORDER BY day;
```

`value` is NULL where Loom withholds it; `value_withheld` says why.

## Cost and scale

- `EXPLAIN (VERBOSE)` shows which part of a query runs in ClickHouse. A join between a
  foreign table and a local one runs in Postgres, which fetches every view row the
  pushed-down filters leave, so filter each view as narrowly as the query allows: by
  namespace, by time, by a literal list of ids.
- A pushed-down filter narrows what ClickHouse sends to Postgres, not what it reads: each
  view reads the grant's whole scope, and the ledger views their 31-day window
  ([what a read costs](../reference/sql-surface.md#what-a-read-costs)).
- A statement past the SQL user's ceilings (`LOOM_CH_ANALYSIS_MAX_EXECUTION_S`,
  `_MAX_ROWS_TO_READ`, `_MAX_MEMORY_BYTES`) fails in ClickHouse rather than slowing down, and
  Postgres returns its error.

## Copies and erasure

The foreign tables hold nothing: every query reads ClickHouse, so an erasure in Loom reaches
them at once. A copy taken in this Postgres (`CREATE TABLE … AS SELECT` from a foreign table,
a materialized view, a dump) is the org's own data and outside Loom's erasure.

## Revoking

`loom sql-access revoke --user acme_bi` stops every query through the user mapping at
once: the next one fails to log in. Drop the user mapping and the foreign server
afterwards.
