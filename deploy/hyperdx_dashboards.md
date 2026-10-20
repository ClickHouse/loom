# ClickStack / HyperDX setup + dashboards for Loom

Loom writes OTel data **directly** into `default.otel_*` (the
`LOOM_CLICKSTACK_DATABASE` database when it is set) on the same
ClickHouse service it uses for `loom.*` — no OTel collector. So when
onboarding a hosted ClickStack (ClickHouse Cloud → service →
Observability), skip the wizard's "install the collector / here is your
ingestion API key" steps entirely. The only work is pointing HyperDX's
**sources** at the tables that already exist.

Verified against a ClickHouse Cloud service with hosted ClickStack.

---

## Provisioning: use the ClickStack MCP, not the API

**The fastest path to a working dashboard is the ClickStack MCP server**, not
`curl`. Add it once:

```bash
claude mcp add clickstack --scope user --transport http \
  https://<clickstack-mcp-host>/clickstack \
  --header "x-service-id: <SERVICE_ID>"
```

then authenticate via `/mcp`. It exposes `clickstack_list_sources`,
`clickstack_save_dashboard`, `clickstack_patch_dashboard`,
`clickstack_query_tile`, `clickstack_save_alert` and
`clickstack_save_saved_search` — everything below can be created and
**validated** without a single HTTP call. `clickstack_query_tile` is the
important one: it runs a tile's query and shows you the rows, which is how a
bucket-boundary mistake like the one further down shows itself.

The dashboard built this way is **Loom — Memory Health** (19 tiles).

### On the auth methods you'll find in the docs

Two documented forms exist, and on a hosted instance neither is the right
tool:

| Form                                                                                              | Auth                                                                         | Status here                                                         |
| ------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| Managed ClickStack: `api.clickhouse.cloud/v1/organizations/<ORG>/services/<SVC>/clickstack/<res>` | HTTP Basic, CH Cloud API key id+secret                                       | Route 401s from the hosted instance                                 |
| OSS: `/api/v2/<res>` on port 8000                                                                 | `Authorization: Bearer <Personal API Access Key>` (Team Settings → API Keys) | **404** — the v2 external API is not exposed on the hosted instance |
| Instance-internal `/api/<res>`                                                                    | session cookie                                                               | 401→works; what `clickstack_sources.sh` uses                        |

`clickstack_sources.sh` therefore still needs `HDX_COOKIE`, and that is a
limitation of the route it targets rather than a considered choice. Prefer the
MCP for anything new. There is also a third-party Terraform provider
(`teamlapse/terraform-provider-clickstack`) covering dashboards/alerts/saved
searches, but it defaults to the Cloud API and so has the same reachability
problem.

---

## Part 1 — Source configuration

Scripted: `./deploy/clickstack_sources.sh` creates all three and wires the
correlation. See its header for auth. The tables below are the same config
for doing it by hand in HyperDX → gear (**Team Settings**) → **Sources** →
**Add Source**. On CH Cloud the connection to the service is pre-created;
select it rather than making a second one.

Note there is no `PATCH` route on `/api/sources` — updates are a
whole-object `PUT`. Correlation is stored per-source and is not symmetric,
so it must be set on both sides.

**Check for auto-created sources first.** HyperDX creates a default
`Logs` / `Traces` / `Metrics` / `Sessions` set when a team is first
provisioned, already pointing at `default.otel_*`. If those exist, fix them
in place against the tables below rather than adding a second set — the
script's `HDX_PREFIX` keeps its sources distinguishable if you do want
both. The two fields most likely wrong on an auto-created source are the
logs timestamp expressions and, if it was built for a JSON-schema install,
the attribute column types.

### Traces source

| Field                                 | Value                                                                              |
| ------------------------------------- | ---------------------------------------------------------------------------------- |
| Name                                  | `loom-traces`                                                                      |
| Database / Table                      | `default` / `otel_traces`                                                          |
| Timestamp Column                      | `Timestamp`                                                                        |
| Default Select                        | `Timestamp, ServiceName, StatusCode, SpanName, round(Duration/1e6) AS duration_ms` |
| Service Name                          | `ServiceName`                                                                      |
| Trace Id / Span Id / Parent Span Id   | `TraceId` / `SpanId` / `ParentSpanId`                                              |
| Span Name / Span Kind                 | `SpanName` / `SpanKind`                                                            |
| Duration Column / Precision           | `Duration` / **nanoseconds**                                                       |
| Status Code / Message                 | `StatusCode` / `StatusMessage`                                                     |
| Resource Attributes / Span Attributes | `ResourceAttributes` / `SpanAttributes`                                            |
| Span Events                           | `Events`                                                                           |
| Correlated Log Source                 | `loom-logs` (set after creating it)                                                |

Duration precision is the most common misconfiguration: Loom writes
`Int64` **nanoseconds**. Assume milliseconds and every latency in the UI
is off by 10^6.

### Logs source

| Field                                | Value                                        |
| ------------------------------------ | -------------------------------------------- |
| Name                                 | `loom-logs`                                  |
| Database / Table                     | `default` / `otel_logs`                      |
| Timestamp Column                     | **`TimestampTime`**                          |
| Displayed Timestamp                  | **`Timestamp`**                              |
| Default Select                       | `Timestamp, ServiceName, SeverityText, Body` |
| Service Name / Severity Text         | `ServiceName` / `SeverityText`               |
| Body / Implicit Column Expression    | `Body`                                       |
| Trace Id / Span Id / Trace Flags     | `TraceId` / `SpanId` / `TraceFlags`          |
| Resource Attributes / Log Attributes | `ResourceAttributes` / `LogAttributes`       |
| Correlated Trace Source              | `loom-traces`                                |

Filter on `TimestampTime`, display `Timestamp`. The table is
`ORDER BY (ServiceName, SeverityText, TimestampTime, Timestamp)` and
`PARTITION BY toDate(TimestampTime)`, so only `TimestampTime` gets
primary-key and partition pruning — but it is `DateTime`, so using it for
display would drop sub-second precision. HyperDX has separate
`timestampValueExpression` / `displayedTimestampValueExpression` fields for
exactly this.

Set the correlation on **both** sources — it is one-directional per source,
and it is what makes span → its log lines work.

### Metrics source

One source, leave its own `tableName` empty, timestamp column `TimeUnix`.
HyperDX v2.33.0's `metricTables` takes exactly **four** keys — there is no
`exponential_histogram` slot:

| Metric type | Table                            |
| ----------- | -------------------------------- |
| Sum         | `default.otel_metrics_sum`       |
| Histogram   | `default.otel_metrics_histogram` |
| Gauge       | `default.otel_metrics_gauge`     |
| Summary     | `default.otel_metrics_summary`   |

Loom only writes Sum + Histogram; map gauge and summary anyway because the
source config expects the full set. `otel_metrics_exponential_histogram`
still needs to _exist_ on the service — the ClickStack erasure backend
iterates it — it just is not referenced by the metrics source.

### Skip the Session source

That is rrweb browser-session replay. Loom is a server; `hyperdx_sessions`
does not exist on the service.

### Schema-conflict caveat

Loom's tables come from `schemas/otel/clickstack_otel_tables.sql` and use
the `Map(LowCardinality(String), String)` attribute schema. Recent
ClickStack versions default to `JSON`-typed attribute columns. The tables
already exist with data, so ClickStack's `CREATE ... IF NOT EXISTS` leaves
them alone — but if the hosted instance auto-creates sources assuming the
JSON schema, those sources are misconfigured against Map columns and
queries error. Delete the auto-created sources and build them per above.

---

## Part 2 — Dashboard panels

Paste into HyperDX → Dashboards → new chart → SQL panel.

### The two traps these queries avoid

1. **`SpanName LIKE 'POST /v1/%'` also matches ASGI child spans.**
   FastAPI auto-instrumentation emits `POST /v1/memory.get http send` and
   `… http receive` alongside the real parent span — typically two child
   spans per real `memory.get` call, so any
   `count()`/`avg()` over that filter is ~3x inflated and "slowest
   traces" fills with 0.02ms rows. The discriminator is
   `mapContains(SpanAttributes, 'loom.operation')`: only the parent span
   carries `loom.*` attributes (set in `set_loom_attributes`).

2. **Both Loom metrics are CUMULATIVE** (`AggregationTemporality = 2`,
   OTel Python's default). `sum(Value)` over a window sums running
   totals rather than counting events, and `quantile()(Sum/Count)` is a
   quantile over cumulative averages — not a latency percentile.
   Span `Duration` gives exact per-request values, so prefer traces for
   both rate and latency.

### The cumulative-counter correction (read this first)

Every Loom metric is **cumulative** (`AggregationTemporality = 2`, OTel Python's
default) — counters _and_ histograms. Check it on your service:

```sql
SELECT MetricName, any(AggregationTemporality) FROM default.otel_metrics_sum
WHERE MetricName LIKE 'loom%' GROUP BY MetricName    -- all return 2
```

So the naive forms are wrong in ways that still produce plausible numbers:

| Naive                        | What it actually returns                                       |
| ---------------------------- | -------------------------------------------------------------- |
| `sum(Value)` on a counter    | the sum of running totals — grows without bound                |
| `Sum / Count` on a histogram | the **lifetime** mean, not the windowed one                    |
| `quantile()(Sum/Count)`      | a quantile over cumulative averages — not a latency percentile |

The fix is a per-series delta. **Every process exports once a minute**, so the
within-bucket form `max(...) - min(...)`
reads 0 in every minute bucket (one point per bucket per process) and
under-counts by one interval in larger buckets. Difference each point against
the previous point of its series instead. The series is `(ResourceAttributes,
Attributes, StartTimeUnix)`: a cumulative series' start time is the moment its
process first observed it, so it separates the four workers that share one
container `HOSTNAME` (and any restart starts a new series). Checked against
`loom.search.top_similarity` for `memory.search` per 30 min over 3 h: this form
read 39 / 26 / 27 / 16 searches, matching the `memory.search` root spans
exactly; the within-bucket form without `StartTimeUnix` read 276 / 291 / 312 /
322 — seven times the traffic, from mixing four processes' totals.

```sql
-- per-series increase, reset-aware, correct for both temporalities
if(AggregationTemporality = 1, toFloat64(Value),                            -- delta rows: the row IS the increase
   if(toFloat64(Value) < lagInFrame(toFloat64(Value), 1, toFloat64(Value)) OVER w,
      toFloat64(Value),                                                     -- reset: the new total
      toFloat64(Value) - lagInFrame(toFloat64(Value), 1, toFloat64(Value)) OVER w)) AS d
...
WINDOW w AS (PARTITION BY ResourceAttributes, Attributes, StartTimeUnix ORDER BY TimeUnix)
```

Bucket by `TimeUnix` outside that subquery and `sum(d)`.
`deploy/dashboards/code_plane.json` carries the form for a histogram
(`Sum`/`Count`) and for a counter; the builder tiles' `aggFn: increase` already
differences consecutive points. The two Memory Health SQL tiles ("Mean top
similarity", "Mean retrieval confidence") take the SQL below. The counter panels are labelled "rate/volume", not "exact",
because a reset loses the interval in which it happened.

Where an exact count matters, prefer **traces** — one parent span per request,
with `Duration` carrying the true per-request value.

### Retrieval quality trend — the flagship panel

The single most diagnostic panel Loom has. A falling line means searches are
returning worse matches; latency and error-rate panels stay flat and green
throughout, so nothing else surfaces it.

```sql
SELECT t,
       round(sum(dSum) / greatest(sum(dCount), 1), 4) AS mean_top_similarity,
       sum(dCount) AS searches
FROM (
    SELECT toStartOfInterval(TimeUnix, INTERVAL 30 MINUTE) AS t,
           if(AggregationTemporality = 1, Sum,
              if(Sum < lagInFrame(Sum, 1, Sum) OVER w, Sum,
                 Sum - lagInFrame(Sum, 1, Sum) OVER w))       AS dSum,
           if(AggregationTemporality = 1, toFloat64(Count),
              if(toFloat64(Count) < lagInFrame(toFloat64(Count), 1, toFloat64(Count)) OVER w,
                 toFloat64(Count),
                 toFloat64(Count) - lagInFrame(toFloat64(Count), 1, toFloat64(Count)) OVER w)) AS dCount
    FROM default.otel_metrics_histogram
    WHERE MetricName = 'loom.search.top_similarity'
      AND TimeUnix > now() - INTERVAL 24 HOUR
      -- Product search only. `memory.search_trace` is also registered in
      -- `_SEARCH_OPS`, deliberately. But it is the Recall
      -- Lab's debug op: it applies NO noise floor, is typically issued with a
      -- large top_k of raw ANN candidates, and can never set `abstain`. Left
      -- unfiltered it moves this panel whenever an operator opens the Lab, and
      -- deflates the abstain rate by padding the denominator with requests that
      -- cannot abstain. Filter the OP here rather than un-registering the metric.
      AND Attributes['loom.operation'] = 'memory.search'
    WINDOW w AS (PARTITION BY ResourceAttributes, Attributes, StartTimeUnix ORDER BY TimeUnix)
)
GROUP BY t HAVING searches > 0 ORDER BY t ASC
```

Time-series chart. Keep the `loom.operation` filter on every `loom.search.*`
panel for the same reason — `plane_hits`, `assist`, `abstain`, `results` and
`confidence` all carry both ops. Swap `top_similarity` for `loom.search.confidence` or
`loom.search.results` — identical shape. Re-baseline the eyeballed "normal" band
after any `LOOM_EMBEDDING_MODEL` change; a new embedder moves the whole
distribution.

**Never use a quantile on these three histograms without explicit bucket
boundaries.** A histogram created without them inherits OTel's defaults — `[0, 5, 10, 25, … 10000]`, sized for millisecond
latencies. `top_similarity` and `confidence` are valued 0..1, so _every_
observation lands in the first bucket and _every_ quantile returns that
bucket's midpoint: HyperDX renders a rock-steady `2.5` for a metric that
cannot exceed 1. `Sum/Count` — the mean the query above computes — stays
faithful across a boundary change, which is why this panel is a mean and not a p50.

`otel._quality_histogram_views()` registers decile boundaries for the two
0..1 metrics and a `[0,1,2,3,5,8,…]` ladder for `loom.search.results`, so
quantiles are meaningful. Bucket boundaries are not retroactive: series
already on disk keep the ones they were written with, so a quantile spanning a
boundary change silently mixes both; prefer the mean across such a change.

### Calls and errors by operation and transport

`loom.transport` separates agent traffic (`mcp`) from the dashboard (`http`) —
the question worth asking is whether agents see different behaviour than the UI.

```sql
SELECT Attributes['loom.operation'] AS op,
       Attributes['loom.transport'] AS transport,
       sum(d)                                        AS calls,
       sumIf(d, Attributes['success'] = 'false')     AS errors
FROM (
    -- per-series step: reads cumulative and delta rows alike
    SELECT Attributes,
           if(AggregationTemporality = 1, Value,
              if(Value < lagInFrame(Value, 1, Value) OVER w, Value,
                 Value - lagInFrame(Value, 1, Value) OVER w)) AS d
    FROM default.otel_metrics_sum
    WHERE MetricName = 'loom.requests'
      AND TimeUnix > now() - INTERVAL 6 HOUR
    WINDOW w AS (PARTITION BY ResourceAttributes, Attributes, StartTimeUnix ORDER BY TimeUnix)
)
GROUP BY op, transport HAVING calls > 0 ORDER BY calls DESC
```

Expect `grants.list` / `namespace.list_mine` / `cost.budget_status` to dominate —
that is the dashboard polling, not user traffic. Filter them out when
looking at real usage.

### System state — the single-stat row

Gauges, so no delta correction needed: the last value _is_ the level.

```sql
SELECT MetricName, anyLast(Value) AS latest
FROM default.otel_metrics_gauge
WHERE TimeUnix > now() - INTERVAL 10 MINUTE
GROUP BY MetricName ORDER BY MetricName
```

Four single-stat tiles: `loom.embed.pending` (worker backlog — non-zero means
accepted-but-unsearchable memories), `loom.vectors.total` (brute-force scan cost grows
with it), `loom.ch.mutations_pending` (a stuck `ALTER`),
`loom.langfuse.breaker` (0 closed / 1 half-open / 2 open).

### Spend and token burn

```sql
SELECT Attributes['loom.namespace'] AS ns,
       Attributes['loom.operation'] AS op,
       round(sum(d), 6) AS cost_usd
FROM (
    -- per-series step: reads cumulative and delta rows alike
    SELECT Attributes,
           if(AggregationTemporality = 1, Value,
              if(Value < lagInFrame(Value, 1, Value) OVER w, Value,
                 Value - lagInFrame(Value, 1, Value) OVER w)) AS d
    FROM default.otel_metrics_sum
    WHERE MetricName = 'loom.cost.usd'
      AND TimeUnix > now() - INTERVAL 24 HOUR
    WINDOW w AS (PARTITION BY ResourceAttributes, Attributes, StartTimeUnix ORDER BY TimeUnix)
)
GROUP BY ns, op HAVING cost_usd > 0 ORDER BY cost_usd DESC
```

Swap `loom.cost.usd` for `loom.tokens` and group by `Attributes['kind']`
(`embed` / `llm_input` / `llm_output`). `loom.agent_events.upstream_cost_usd`
remains the billing-grade record — these panels are for trend and correlation
against latency, not invoicing.

### Which retrieval planes earn their latency

```sql
SELECT Attributes['plane'] AS plane, sum(d) AS hits
FROM (
    -- per-series step: reads cumulative and delta rows alike
    SELECT Attributes,
           if(AggregationTemporality = 1, Value,
              if(Value < lagInFrame(Value, 1, Value) OVER w, Value,
                 Value - lagInFrame(Value, 1, Value) OVER w)) AS d
    FROM default.otel_metrics_sum
    WHERE MetricName = 'loom.search.plane_hits'
      AND TimeUnix > now() - INTERVAL 24 HOUR
    WINDOW w AS (PARTITION BY ResourceAttributes, Attributes, StartTimeUnix ORDER BY TimeUnix)
)
GROUP BY plane HAVING hits > 0 ORDER BY hits DESC
```

A plane that never appears is costing a round-trip and contributing nothing. On
a deployment that never writes chunks or summaries every hit comes from `fact`,
because `memory_chunks` and `session_summaries` are empty — those legs cannot
contribute
until something populates them.

`loom.search.abstain`'s `reason` label is the bounded `abstain_code`
(`no_match` / `below_threshold` / `low_calibrated_confidence`), **not** the
prose `abstain_reason` — that string interpolates the similarity and threshold
(`"top hit similarity 0.19 is below…"`) plus the calibrator's free-text
explanation, so using it as a label would mint one series per distinct float.
Truncating to 48 chars bounds the length, not the cardinality. The full prose
is on the span as `loom.search.abstain_reason`, where cardinality is free.

Same shape for `loom.search.abstain` (group by `Attributes['reason']`) and
`loom.search.assist` (group by `Attributes['kind']` — `hyde` / `rerank` /
`iterative_judge`, each adding 1-3s, which is
what separates "search is slow" from "search needed help").

Every assist is read from `retrieval_work` (built from
`SearchContext.actual_work`), not from top-level response fields:
`hyde_initial_used` / `llm_hyde_calls`, `inline_rerank_used` (code search), `external_rerank_used`,
`llm_iterative_judge_calls`.

To exercise the panel: assists mostly gate on `recipe="quality"` — a plain
`memory.search` fires none of them, no matter how large the corpus, so an empty
panel is the expected reading for a namespace whose traffic is all default-recipe.
memory.search's `rerank` assist comes only from an external rerank endpoint. `kind="hyde"`
fires under `recipe="quality"`, which uses HyDE _initially_ (`hyde_initial_used`).

`tests/unit/test_domain_metrics.py` drives the recorder off a real
`MemorySearchOutput`, and a `test_every_record_helper_has_a_production_call_site`
guard fails the build if a `record_*` helper has no caller.

### Write outcomes — is Loom still learning?

Every panel above measures reads. `loom.memory.writes` is the one that answers
whether anything new is going _in_, split by the outcome the conflict checker
reached:

```sql
-- Total per series, NOT a windowed delta. loom.memory.writes is CUMULATIVE by
-- default and re-exports the same value every minute, so a
-- windowed delta is 0 for any series that did not change inside the window.
-- Combined with `HAVING > 0` that hides exactly the namespaces this panel exists
-- to surface: one blacklisted write 20 hours ago exports Value=1 forever,
-- deltas to 0, and disappears — indistinguishable from no traffic. With delta
-- rows (AggregationTemporality = 1) the total is the sum of the increments.
SELECT Attributes['outcome']        AS outcome,
       Attributes['loom.namespace'] AS ns,
       if(any(AggregationTemporality) = 1, sum(Value), max(Value)) AS writes_total
FROM default.otel_metrics_sum
WHERE MetricName = 'loom.memory.writes'
  AND TimeUnix > now() - INTERVAL 24 HOUR
GROUP BY outcome, ns
ORDER BY writes_total DESC
```

`writes_total` counts from the exporting process's start, so it resets on
redeploy — read it as "what has this namespace done since the last restart",
and use a time-series tile if you need rate. For a windowed count prefer the
`increase` aggregation in a builder tile, which is reset-aware, over hand-rolled
`max - min`.

Outcome is a five-value enum set in `memory/app/handlers._count_write`:

| Outcome               | Means                                                                                                                                                                                        |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `created`             | first write to this `memory_key` — genuinely new knowledge                                                                                                                                   |
| `updated`             | a new version of a key that already existed                                                                                                                                                  |
| `merged`              | canonical-form match — caller restated something Loom already knew, **no row inserted**                                                                                                      |
| `duplicate`           | very-high similarity but canonical mismatch; inserted with `status='duplicate'`                                                                                                              |
| `conflicted`          | close-but-different peers; inserted and both sides flagged                                                                                                                                   |
| `rejected`            | a pinned peer blocked the write (409 PINNED_CONFLICT)                                                                                                                                        |
| `blacklisted`         | value matched a blacklist pattern (422 BLACKLISTED)                                                                                                                                          |
| `too_large`           | value exceeded `char_limit` (422 CHAR_LIMIT_EXCEEDED)                                                                                                                                        |
| `precondition_failed` | `if_match_sha256` did not match the live row (409)                                                                                                                                           |
| `error`               | any other `HTTPException` — a CH outage or a namespace-config bug, deliberately NOT labelled `rejected` so an infrastructure failure is not read as a policy refusal                         |
| `unchanged`           | bulk/extract path only: a byte-identical restatement skipped by the re-ingest gate. Distinct from `merged`, which is a canonical-form conflict merge on the point path                       |
| `unknown`             | bulk/extract path only: the existing-key lookup failed (fail-open), so created-vs-updated is not knowable. Non-zero here means the panel below is under-reporting, not that nothing happened |

The four refusal outcomes are mapped once at the handler boundary from the
`code` in the raised `HTTPException` (`_WRITE_OUTCOME_BY_ERROR_CODE`), not
counted at each raise site, so a namespace whose writes are all being
blacklisted or all failing `if_match_sha256` still emits, and does not look
identical to one receiving no traffic.

`created` vs `updated` cannot be read off the response: `memory_id` is reused
when a row already exists for that key, so both shapes return an identical
`MemorySetOutput`. The handler captures `had_existing_row` before the merge
branch rebinds `existing`, which is the only place the distinction survives.

The mix is the signal, not the total. A namespace whose writes are ~all
`merged` has stopped learning and is only re-hearing itself — indistinguishable
from healthy ingest if you only watch `loom.requests`, since every one of those
is a successful `POST /v1/memory.set`.

### Retrieval outcome on the span

Metrics tell you retrieval is degrading; they can't tell you _which_ requests
degraded. Search spans carry the outcome too, set by
`otel.set_search_attributes`:

`loom.search.results`, `loom.search.abstain`, `loom.search.abstain_reason`,
`loom.search.confidence`, `loom.search.top_similarity`,
`loom.search.assists` (comma-joined).

Which makes these answerable in Search, and clickable from the panels above:

```
SpanAttributes['loom.search.abstain'] = 'true'
mapContains(SpanAttributes, 'loom.search.confidence')
  AND toFloat64OrZero(SpanAttributes['loom.search.confidence']) < 0.3
SpanAttributes['loom.search.assists'] LIKE '%rerank%'
```

The `mapContains` guard on the confidence filter is load-bearing, not
defensive. A missing key in a `Map(String, String)` returns `''`, and
`toFloat64OrZero('')` is `0.0` — which is `< 0.3` — so without it the query
matches _every_ span in the store, including every `ch.insert.memory` and
`embed.compute`, and buries the low-confidence searches it is supposed to
surface. Any numeric comparison against one of these attributes needs the same
guard; the `= 'true'` and `LIKE` forms above are safe because a missing key
compares unequal.

Query text is deliberately **not** on the span — it is user content, and spans
are broadly readable. Everything here is a count, a float, a bool or a capped
enum, same cardinality discipline as the metric attributes.

### 1. Requests per second

```sql
SELECT
    toStartOfMinute(Timestamp) AS time,
    SpanAttributes['loom.operation'] AS op,
    count() / 60 AS rps
FROM default.otel_traces
WHERE Timestamp > now() - INTERVAL 1 HOUR
  AND mapContains(SpanAttributes, 'loom.operation')
GROUP BY time, op
ORDER BY time ASC
```

Time-series chart, group by `op`. Exact — one parent span per request.

<details>
<summary>Counter-based alternative (needs cumulative deltas)</summary>

```sql
SELECT
    time,
    op,
    greatest(cum - lagInFrame(cum, 1) OVER (PARTITION BY op ORDER BY time), 0) / 60 AS rps
FROM (
    SELECT
        toStartOfMinute(TimeUnix) AS time,
        Attributes['loom.operation'] AS op,
        max(Value) AS cum
    FROM default.otel_metrics_sum
    WHERE MetricName = 'loom.requests'
      AND TimeUnix > now() - INTERVAL 1 HOUR
    GROUP BY time, op
)
ORDER BY time ASC
```

`greatest(…, 0)` discards the negative delta produced when the counter
resets on process restart.
</details>

---

### 2. p50 / p95 / p99 latency

```sql
SELECT
    toStartOfMinute(Timestamp) AS time,
    SpanAttributes['loom.operation'] AS op,
    quantile(0.50)(Duration / 1e6) AS p50_ms,
    quantile(0.95)(Duration / 1e6) AS p95_ms,
    quantile(0.99)(Duration / 1e6) AS p99_ms
FROM default.otel_traces
WHERE Timestamp > now() - INTERVAL 1 HOUR
  AND mapContains(SpanAttributes, 'loom.operation')
GROUP BY time, op
ORDER BY time ASC
```

True percentiles over per-request durations. For a mean from the
histogram instead, delta `Sum` and `Count` across buckets — do not
`quantile()` them.

---

### 3. Error rate (by op)

```sql
SELECT
    toStartOfMinute(Timestamp) AS time,
    SpanAttributes['loom.operation'] AS op,
    countIf(StatusCode = 'Error') / count() AS error_rate
FROM default.otel_traces
WHERE Timestamp > now() - INTERVAL 1 HOUR
  AND mapContains(SpanAttributes, 'loom.operation')
GROUP BY time, op
ORDER BY time ASC
```

---

### 4. Slowest requests (last hour)

```sql
SELECT
    Timestamp,
    SpanAttributes['loom.operation'] AS op,
    SpanAttributes['loom.namespace'] AS ns,
    Duration / 1e6 AS dur_ms,
    TraceId
FROM default.otel_traces
WHERE Timestamp > now() - INTERVAL 1 HOUR
  AND mapContains(SpanAttributes, 'loom.operation')
ORDER BY Duration DESC
LIMIT 20
```

Table panel. Click TraceId → drills into the trace waterfall (the child
spans excluded above are exactly what the waterfall shows: `embed.compute`,
`ch.insert.memory`, `ch.insert.memory_embeddings`, …).

---

### 5. Per-tenant traffic

```sql
SELECT
    SpanAttributes['loom.namespace'] AS namespace,
    SpanAttributes['loom.operation'] AS op,
    count() AS calls,
    avg(Duration) / 1e6 AS avg_ms
FROM default.otel_traces
WHERE Timestamp > now() - INTERVAL 1 HOUR
  AND mapContains(SpanAttributes, 'loom.operation')
  AND SpanAttributes['loom.namespace'] != ''
GROUP BY namespace, op
ORDER BY calls DESC
LIMIT 50
```

---

### 6. Logs for a specific trace (click-through panel)

```sql
SELECT Timestamp, SeverityText, Body, LogAttributes
FROM default.otel_logs
WHERE TraceId = {{trace_id}}
ORDER BY Timestamp ASC
```

`{{trace_id}}` is a HyperDX dashboard variable — set its source to "URL
parameter `trace_id`" so clicking a span in the trace view drives this panel.

Expect this panel to be sparse. Logs only carry `TraceId` when emitted
inside an active span, typically a few percent of rows — everything logged at
startup, in workers, or in background
tasks has an empty `TraceId`. That is inherent to attaching
`LoggingHandler` at the root: it stamps trace context when there is any,
and most log volume is outside a request.

---

### 7. Saga checkpoint heatmap (operability)

How often do erasure Sagas hit each step?

```sql
SELECT
    arrayJoin(steps_completed) AS step,
    count() AS sagas
FROM loom.erasure_requests FINAL
WHERE requested_at > now() - INTERVAL 7 DAY
  AND status IN ('in_progress', 'completed')
GROUP BY step
ORDER BY sagas DESC
```

Bar chart. Reveals which step (memory.tombstone vs langfuse.delete_user
vs clickstack.delete_user) is the long-tail of the cascade.

---

### 8. Saved searches (HyperDX → Search → save query)

| Name               | Query bar                                                             |
| ------------------ | --------------------------------------------------------------------- |
| Loom errors        | `SeverityText:ERROR service.name:loom`                                |
| Request spans only | `SpanAttributes['loom.operation']:*`                                  |
| One tenant         | `SpanAttributes['loom.namespace']:eng-tooling`                        |
| One user           | `enduser.id:<your-user-id>`                                           |
| Slow knowledge.add | `SpanAttributes['loom.operation']:knowledge.add Duration:>1000000000` |

"Loom errors" is empty until something actually logs at ERROR — the
handler is attached at INFO, so a healthy service shows only
`SeverityText = INFO`. Prefer `enduser.id` over
`SpanAttributes['loom.user_id']` for the per-user search: Loom sets both
to the same value, and `enduser.id` is the OTel semconv attribute HyperDX
filters on natively.

---

## Bootstrap order

Spans flush every **3 s** (`schedule_delay_millis=3000`) and metrics every
`LOOM_OTEL_METRICS_EXPORT_INTERVAL_S` (default **60 s**), with `async_insert=0`,
so spans show within seconds and metric panels within a minute. If panels stay empty: check `docker logs loom-server | grep -i otel`.
On success you will see

```
OTel SDK initialized — writing spans+logs directly to <host>/default.otel_{traces,logs}
OTel metrics initialized — emitting every 60s (cumulative temporality, gauge heartbeat 0s) to default.otel_metrics_{sum,histogram,gauge}
OTel logs handler attached to root logger
```

If instead you see `default.otel_traces does not exist … Self-disabling
span export`, the tables are missing — apply
`schemas/otel/clickstack_otel_tables.sql`.

---

## Part 3 — Code plane ("Loom — Code Plane")

The code index has its own dashboard because its failure modes are different:
an index that silently goes stale, a compiler tier that silently degrades to
the parser tier, a worker that fails three passes in a row, a search that
keeps answering with worse matches. None of those move latency or error rate.

Six alerts. **Source of truth: `deploy/dashboards/code_plane.json`.** Every tile is listed
there with the source it queries (`metrics`, `traces`, or a plain `sql` tile
over the `loom.*` tables) and the exact SQL, and six tiles carry an `alert`
block with the threshold and the reason it is worth a page. Provision with the
ClickStack MCP: `clickstack_query_tile` each query first, `clickstack_save_dashboard`,
then `clickstack_save_alert` for the alert tiles against the placeholder
webhook (repoint it as described in Part 2). To update a board already
provisioned, find its id with `clickstack_search_dashboards` by the spec's
`name` and save over it. Do not edit tiles in the UI
without changing the JSON — the JSON is what the next person will rebuild from.

What the tiles answer, in the order they sit on the board:

| Tile                                                                | Question it answers                                                                           | Signal                                                                                                                                                                   |
| ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Fast-tier index age, compiler-tier age (C++, Python), PR-cursor age | Is the index current, per namespace?                                                          | `loom.code.index_age_seconds`, `loom.code.scip_age_seconds`, `loom.code.scip_py_age_seconds`, `loom.code.pr_cursor_age_seconds` gauges, attribute `loom.namespace`       |
| Tier lag (s), by namespace and tier                                 | Has every tier reached new merges, and since when not?                                        | `loom.code.tier_lag_seconds{tier}`: age of the oldest commit the tier has not reached; alert "code tier lag" at 1800 s                                                   |
| Files the indexer could not process                                 | Is anything being dropped at ingest?                                                          | `loom.code.files_errored` gauge; the same number `code.coverage` and the dashboard's Health page show                                                                    |
| Code ops per 5 min; p50/p95/p99 per op                              | Who is calling what, and is each op inside its `budget_p99_ms`?                               | root spans with `loom.operation LIKE 'code.%'`                                                                                                                           |
| Where code.callers spends its time                                  | Which leg is slow: resolve, dispatch, edge walk?                                              | child spans `code.<op>.<leg>`                                                                                                                                            |
| Failed code requests                                                | 4xx/5xx by op and status                                                                      | `http.status_code` on root spans (and `loom.requests{success=false}` once the HTTP transport records failures)                                                           |
| Share of answers served from a stale index                          | How often did an agent get an answer more than `LOOM_CODE_STALE_AFTER_COMMITS` behind master? | `loom.code.stale` span attribute                                                                                                                                         |
| Abstain / ambiguous rate by op                                      | Is the index refusing more than usual?                                                        | `loom.code_search_trace`, benches filtered out by `consumer`                                                                                                             |
| code.search top cosine similarity                                   | The flagship retrieval-quality line, code edition                                             | `loom.search.top_similarity{loom.operation='code.search'}` — populated from the response-level `top_similarity` because code hits carry RRF scores, not cosine distances |
| Worker passes by outcome; consecutive failures                      | Are the code workers running and succeeding?                                                  | `loom.worker.passes{worker,outcome}`, `loom.worker.consecutive_failures{worker}` (metric attributes are unprefixed; spans carry `loom.worker`)                           |
| Token shunt hits, passes, tokens saved                              | Is the Claude Code plugin's shunt actually saving tokens in production?                       | `loom.code_search_outcome` actions `shunt_hit` / `shunt_pass`, column `tokens_saved`                                                                                     |
| Feedback loop: outcomes per traced answer                           | Is anyone closing the loop?                                                                   | `code_search_trace ⋈ code_search_outcome` on `trace_run_id`, by consumer                                                                                                 |
| Code-plane LLM spend by stage                                       | What do describe, summary, answer, expand and rerank cost?                                    | `gen_ai.usage.cost` on `llm.code_*` generation spans                                                                                                                     |

Joining a Langfuse trace to its loom row: `code_search_trace.otel_trace_id`
is the 32-hex OTel trace id, which Langfuse shows as the trace id, and
`loom.code.trace_run_id` is stamped on the root span, so the join works from
either side.
