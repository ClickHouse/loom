# Debug a request

Loom tags everything one request does with the same id, so tracing a request is a join.
This page follows one request from its response headers to the component that decided
the answer. When the deployment itself is unhealthy, start from
[troubleshooting](../operations/troubleshooting.md); when a worker has not run, see
[worker status](../operations/monitoring.md#worker-status).

```mermaid
flowchart TD
    A["Response headers<br/>X-Request-Id, X-Trace-Id, Server-Timing"] --> B["loom.agent_events<br/>request_id"]
    B --> C["system.query_log<br/>log_comment = 'op request_id'"]
    B --> D["default.otel_traces<br/>loom.request_id"]
    B --> E["Langfuse trace metadata<br/>request_id"]
    B --> F["loom.audit_log<br/>writes only"]
    G["memory.search explain=true<br/>per-component scores"] -.-> B
```

## 1. Capture the request's ids

Every HTTP response carries:

| Header                            | Meaning                                                                                                                                                                                  |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `X-Request-Id`                    | The request's id. Send your own (1 to 64 letters, digits, `.`, `_` or `-`) and Loom echoes it, so your log and Loom's rows share one key; any other value is replaced by a generated id. |
| `X-Trace-Id`                      | The OpenTelemetry trace id of the request's span tree.                                                                                                                                   |
| `Server-Timing`                   | `app` (handler time) and `ch` (ClickHouse time, statement count, bytes read) on success.                                                                                                 |
| `X-Loom-Hint`, `X-Loom-Hint-Code` | The mentor's hint for this call, when it has one; the SDKs log it.                                                                                                                       |

```bash
curl -si "$LOOM_BASE_URL/v1/memory.search" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' -H 'X-Request-Id: dbg-0001' \
  -d '{"org":"acme","namespace":"support","agent":"bot","query":"refund window"}' \
  | grep -iE '^(x-request-id|x-trace-id|server-timing)'
```

`Server-Timing` often answers "why was it slow". A large `ch` duration or byte count
points at the query. A large `app` with a small `ch` points at a provider call
(embedding, rerank, LLM).

## 2. Join the request across systems

The same id is in `loom.agent_events.request_id`, the Langfuse trace metadata, and the
span attribute `loom.request_id` in `default.otel_traces`.

```sql
-- the ledger row: operation, identity, latency, success, error_code, tokens, cost, model
SELECT * FROM loom.agent_events WHERE request_id = 'dbg-0001';

-- every ClickHouse statement the request issued
SELECT query_duration_ms, read_rows, read_bytes, substring(query, 1, 200) AS q
FROM system.query_log
WHERE type = 'QueryFinish' AND log_comment LIKE '% dbg-0001%'
ORDER BY event_time_microseconds;

-- its spans
SELECT SpanName, Duration, SpanAttributes['loom.operation'] AS op
FROM default.otel_traces
WHERE SpanAttributes['loom.request_id'] = 'dbg-0001'
ORDER BY Timestamp;
```

The `log_comment` tags are listed in
[query attribution](../operations/monitoring.md#logs-traces-and-query-attribution).
Spans go to `default.otel_traces` (in `LOOM_CLICKSTACK_DATABASE` when it is set) when
`LOOM_OTEL_EXPORTER` resolves to `ch`, or to the OTLP endpoint. With Langfuse configured they also go to
[Langfuse](../features/integrations/langfuse.md).

> **Note:** the ledger never stores query text. `loom.agent_events` replaces every
> free-text argument with a `sha256:<16 hex>/<length>` shape. To see what was asked, use
> the search trace log below or your own client log.

## 3. Explain a search answer

**Why did this rank here?** `memory.search` with `explain=true` attaches `score_components`
to each served hit: the ranking signals and, after a reranker, its move (`rerank_from`,
`rerank_pos`, `rerank_delta`) and, when the reranker changed the order, its own fused score
(`rank_score_pre_rerank`). A row moved down because its linked code changed carries the
places it moved (`code_changed_places`).

```python
out = await agent.call("memory.search", {"query": "refund window", "explain": True})
for h in out["results"]:
    c = h["score_components"]
    print(
        h["memory_key"],
        h["similarity"],
        h["rank_score"],
        c.get("rerank_from"),
        c.get("rank_score_pre_rerank"),
    )
```

`memory.search_trace` takes the arguments of `memory.search` and scores the vector leg
alone (no fusion, assembly or reranker), returning, per hit, `similarity`, `distance`,
`entity_boost`, `rank_score`, `rank` and `source`.

**Why did it abstain?** Read the `memory.search` answer itself:

| Field                                     | Tells you                                                                                                      |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `abstain`, `abstain_code`                 | Which gate fired: `no_match`, `below_threshold`, `entity_absent`, `low_calibrated_confidence`, `page_expired`. |
| `retrieval_risk`, `retrieval_confidence`  | How far the best hit is from the gates.                                                                        |
| `calibration_explanation`                 | Which similarity gates were in force (calibrated from the embedder, or the scalar fallbacks).                  |
| `degraded`                                | Providers that fell back on this read, and what the response did instead.                                      |
| `pending_extraction`                      | Deferred `set_from_messages` batches not yet extracted; above 0, an empty answer is not proof of absence.      |
| `namespaces_searched`, `candidates_total` | Whether the search reached the namespaces and rows you expected.                                               |

`calibration.get` shows the thresholds in effect for a namespace and which tier
(namespace, cluster, scalar) produced them.
[Retrieval and ranking](../concepts/retrieval-and-ranking.md) explains each term.

**What did an agent search, over time?** Needs `LOOM_SEARCH_TRACE_LOG_ENABLED=true`
(default off). `memory.search_trace_log(since_hours=, agent=, run_id=)` returns one row
per search: `returned_keys`, `returned_scores`, `abstain`, `retrieval_risk`,
`latency_ms`, and the `query` when `LOOM_LANGFUSE_CAPTURE_IO_LEVEL` is not `off` (its
default is `off`, so by default the rows carry keys and scores without the question).

## Diagnose coding-agent overhead

Separate three clocks: the agent's model requests, its tool and local work, and the
Loom request. A Loom trace measures the last one. The caller must supply its own
model duration, input/output and cache usage, and tool-call groups. Several tools
issued in one model response are several tool calls but one model turn; replacing
them with a batch does not necessarily save a model round trip. Concurrent spans
can overlap, so summing their duration is not elapsed agent time.

For a completed code call, inspect the handler result's content counts and work:

```sql
SELECT request_id, trace_id, operation, latency_ms, response_tokens,
       args['out_grep_match_lines'] AS grep_lines,
       args['out_grep_related_lines'] AS grep_related_lines,
       args['out_source_bodies'] AS bodies,
       args['out_source_windows'] AS source_windows,
       args['out_source_chars'] AS source_chars,
       args['out_elided_bodies'] AS elided_bodies,
       args['out_lesson_rows'] AS lessons,
       args['out_lesson_chars'] AS lesson_chars,
       args['out_lesson_rows_omitted'] AS omitted_lessons,
       args['out_ch_elapsed_ms'] AS ch_ms,
       args['out_stage_ms'] AS stages
FROM loom.agent_events
WHERE org = 'acme' AND namespace = 'repository'
  AND operation LIKE 'code.%' AND kind = 'request'
ORDER BY ts DESC
LIMIT 20;
```

An absent map entry is unknown, including rows written by an older build. A zero
count says what that result contained; it does not explain why a lesson was absent.
The [telemetry field definitions](../features/observability/telemetry.md#the-ledger)
name the measured containers and character units.

For grep in matches mode, compare line and body counts to see what the response
returned. Check coverage and partial-result flags before interpreting zero lines.
The ledger stage timings include inline lesson work with the exporter disabled.
A trace row written before enrichment can contain fewer stages than the request
row.

Use `trace_id` to find the same request in the configured trace backend. The
[Langfuse integration](../features/integrations/langfuse.md) forwards the existing
span tree and these numeric response attributes when its credentials and OTel are
configured. An installed integration alone does not establish export: check
`GET /v1/health?include=langfuse` for local processor state, then verify a known
request in the remote project. Local exporter state is not proof of remote delivery.

Retain the caller's trajectory and the scoped ledger before removing a disposable
evaluation environment. Include failed calls, distinguish interrupted attempts from
failed solutions, and keep setup and judging costs separate from coding costs.
Record whether source and lessons were present before attributing a bad patch to
retrieval. Successful delivery does not establish relevance or correct use.

## 4. Explain a write

- **The write's own answer.** `memory.set` returns `status`, `conflict_verdict`, `peers`,
  `duplicate_of`, `merged_into_key`, `noop` and `advisories`. A write that "disappeared"
  was usually merged into a canonical match or marked a duplicate. `advisories` names a
  double-encoded or oversized value.
- **Its history.** `memory.history` lists a key's versions from `loom.memory_versions`.
  `memory.provenance` says who wrote it, from which source, session and run.
- **What extraction dropped.** `memory.extraction_status` lists deferred
  `set_from_messages` batches (`pending`, `failed`). `loom drops --namespace <ns>` prints
  what the pipeline discarded, truncated or gave up on, by stage and reason
  ([memory ingest drops](../operations/maintenance.md#memory-ingest-drops)). A
  `set_from_messages` response carries `pipeline_errors` and per-step `stage_ms`
  ([extraction pipeline](extraction-pipeline.md)).
- **The audit trail.** `audit.list(event_type=, since=)` reads `loom.audit_log`: who
  changed what, with a per-row content hash. Every write that creates or changes memory
  leaves a hash-only row ([audit](../features/observability/audit.md)).
- **A run's view.** `run.get(run_id)` returns what a run recalled (`recalled_keys`), what
  it wrote, its turns and its cost ([trace and resume](trace-and-resume.md)).

## Related

- [Troubleshooting](../operations/troubleshooting.md)
- [Monitoring](../operations/monitoring.md)
- [Telemetry](../features/observability/telemetry.md)
- [System overview](../architecture/system-overview.md)
- [Performance tuning](performance-tuning.md)
