---
paths:
  - "server/**/store/**"
  - "server/core/ch.py"
  - "server/core/schema/**"
  - "schemas/**"
  - "scripts/ops/**"
---

# ClickHouse statements

- Bind values as server-side parameters (`{org:String}`, `params={...}`); never format a caller's value into SQL.
- Every statement goes through `server/core/ch.py`. It marks naive datetimes UTC on insert; a raw `clickhouse_connect` client reads them as the host's local time and shifts every row. A loop outside a transport, the worker supervisor or the CLI calls `set_query_tag(...)` so `system.query_log.log_comment` names it.
- Hot paths filter on a prefix of the table's `ORDER BY` key, carry a `LIMIT`, and use `FINAL` only on `ReplacingMergeTree` reads that need the latest version.
- `SELECT * REPLACE (expr AS col) … WHERE col = …` filters on `expr`, not on the stored `col`: the alias shadows the column. Filter in an inner query, and check `written_rows` of an `INSERT … SELECT` before trusting it.
- A `DELETE` or mutation on the target of a refreshable materialized view that is not `APPEND` never finishes (each refresh exchanges the table); a `DELETE` on a `TO` view goes to its target table.
- `DateTime` ends in 2106. A "never" expiry cast through `toDateTime` wraps into the past and a `TTL` on it drops the row at once; use `DateTime64` or a sentinel the `TTL` expression excludes.
- A new `ALTER … UPDATE|DELETE` is a mutation: add it to `_EXPECTED_MUTATIONS` with its reason (`tests/unit/test_clickhouse_mutation_inventory.py`). Prefer a new version row, a tombstone column or a lightweight `DELETE FROM`; never make a request wait on a mutation.
- Measure a query change on real data with `system.query_log` (`read_rows`, `read_bytes`, `memory_usage`, `query_duration_ms`), not wall clock alone.
