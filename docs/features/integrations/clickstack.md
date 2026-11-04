# ClickStack and HyperDX

Loom always emits OpenTelemetry; the HyperDX sources, dashboards and the erasure step
need configuring.

Loom emits a span per operation, plus logs and metrics. Spans carry `loom.operation`,
`loom.org`, `loom.namespace`, `loom.agent`, `loom.request_id`, and `loom.user_id` /
`enduser.id` when present. By default Loom inserts them straight into the
`default.otel_*` tables on its own ClickHouse service, in the collector's schema, so
HyperDX reads them with no collector to run. With `LOOM_CLICKSTACK_DATABASE` set, the
tables are in that database instead of `default`, here and in every query below. The column layout is
`schemas/otel/clickstack_otel_tables.sql`; if ClickStack changes its schema, that file
must follow.

## Choose the export path

| `LOOM_OTEL_EXPORTER` | Behaviour                                                                                                                                 |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `ch`                 | insert into `default.otel_traces`, `otel_logs`, `otel_metrics_sum`, `otel_metrics_histogram` and `otel_metrics_gauge` on the same service |
| `otlp`               | ship to `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT` (headers in `LOOM_OTEL_EXPORTER_OTLP_HEADERS`), for a collector or another backend             |
| empty (default)      | `otlp` when an endpoint is set, else `ch`                                                                                                 |

In `ch` mode, Loom checks that `default.otel_traces` exists first; without it the
exporter disables itself after one log line. To create the tables:

```bash
clickhouse-client --multiquery < schemas/otel/clickstack_otel_tables.sql
```

`GET /v1/health?include=otel` shows the resolved exporter. The rest of the OTel settings
(export interval, temporality, gauge heartbeat, service name) are in
[telemetry](../observability/telemetry.md#exporters).

## Point HyperDX at the tables

The rows are already in the service, so skip the collector and ingestion-key steps.
Create three HyperDX **sources**: traces over `otel_traces`, logs over `otel_logs`,
metrics over `otel_metrics_*`. `deploy/clickstack_sources.sh` creates them through the
HyperDX API and wires their correlation (hosted ClickStack authenticates with your
console session cookie, `HDX_COOKIE`).
[`deploy/hyperdx_dashboards.md`](../../../deploy/hyperdx_dashboards.md) has each source's
field mapping.

## Dashboards and alerts

[`deploy/hyperdx_dashboards.md`](../../../deploy/hyperdx_dashboards.md) is the recipe book:

- **Loom — Memory Health**: retrieval quality trend (`loom.search.top_similarity`),
  calls and errors by operation and transport, system state, spend and tokens, latency by
  retrieval plane, write outcomes, and request panels (rate, latency, errors, slowest
  requests, per-tenant traffic, logs for a trace).
- **Loom — Code Plane**: index lag, tier lag and the other `loom.code.*` gauges. The JSON
  definition is `deploy/dashboards/code_plane.json`.
- The bootstrap order, and saved searches.

Two traps when writing your own tiles:

- **Counters are cumulative.** Unless `LOOM_OTEL_METRICS_DELTA_TEMPORALITY=true`, a
  counter's row is its running total; a rate needs the difference between consecutive
  points, not a sum. Flip the setting and the dashboards together.
- **An empty result reads as 0 in HyperDX.** A minimum-sample guard on an alert must
  return a value inside the healthy range, not no rows, or the alert fires on silence.

The metric names and suggested thresholds are in the
[metrics catalog](../observability/telemetry.md#metrics).

## Erasure

Needs `LOOM_CLICKSTACK_DATABASE`. ClickStack has no admin API for deleting user data,
so the backend (`ClickStackBackend` in `server/erasure/app/backends.py`) runs lightweight
`DELETE FROM` statements against the OTel tables in that database:

| Table                                                                            | Attribute column searched |
| -------------------------------------------------------------------------------- | ------------------------- |
| `otel_logs`                                                                      | `LogAttributes`           |
| `otel_traces`                                                                    | `SpanAttributes`          |
| `otel_metrics_gauge`, `_sum`, `_histogram`, `_exponential_histogram`, `_summary` | `Attributes`              |
| `hyperdx_sessions`                                                               | `ResourceAttributes`      |

- **A user scope** matches any of the keys in `LOOM_CLICKSTACK_USER_ID_KEYS` (default
  `user_id,user.id,enduser.id`), ORed, since SDKs disagree on the key name.
- **A namespace scope** matches `loom.namespace` or `service.namespace`. Loom's exporter
  sets `service.namespace` to the constant `loom` and carries the tenant in
  `loom.namespace`; `service.namespace` covers other emitters writing into the same
  ClickStack, and its value `loom` is never matched.
- **Org guard.** A row tagged with _another_ org's `loom.org` is never erased. A row
  matched by `loom.namespace` must carry the erasing org's `loom.org`. Loom's op and
  domain metrics (counters and histograms) carry `loom.namespace` and no org, so a
  namespace erasure leaves them: they hold no memory content, but their `loom.namespace`
  label is the namespace name, which for a personal namespace is derived from the user's
  email. They stay until the collector table's own TTL, if it has one, removes them. A
  row with no `loom.org` tag matched by a user key or by `service.namespace` (another
  emitter) is erased, since it may hold the subject.

A lightweight delete writes only the row-existence mask, so rows leave every read at
once and the bytes go at the next merge; `ALTER TABLE … DELETE` would rewrite every
touched part. The predicate scans any table without a bloom filter on the attribute map
values (the metrics tables lack one); `loom doctor` names them.

The step counts rows before deleting and reports that count. A missing table or database
counts as nothing to erase; any other failure fails the step closed, and the
`erasure_recovery` worker retries it.

Check which user-id keys your data actually carries before relying on the defaults:

```sql
SELECT DISTINCT arrayJoin(mapKeys(LogAttributes))
FROM default.otel_logs WHERE TimestampTime > now() - INTERVAL 1 DAY
```

See [retention and erasure](../memory/retention-and-erasure.md) for the saga this step
belongs to.

## Settings

| Setting                            | Default                      | Effect                                       |
| ---------------------------------- | ---------------------------- | -------------------------------------------- |
| `LOOM_OTEL_EXPORTER`               | `""`                         | `ch`, `otlp`, or auto                        |
| `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT` | `""`                         | collector base URL                           |
| `LOOM_OTEL_EXPORTER_OTLP_HEADERS`  | `""`                         | comma-separated `K=V` headers                |
| `LOOM_OTEL_ENABLED`                | `true`                       | master switch                                |
| `LOOM_CLICKSTACK_DATABASE`         | `""`                         | the OTel tables' database; enables erasure   |
| `LOOM_CLICKSTACK_USER_ID_KEYS`     | `user_id,user.id,enduser.id` | attribute keys a user-scoped erasure matches |

## Related

- [Telemetry](../observability/telemetry.md): what is emitted and the full metric catalog.
- [Monitoring](../../operations/monitoring.md).
- [Langfuse](./langfuse.md): the same spans as model-call traces.
