# Telemetry

Loom reports on itself in three layers:

| Layer                        | Lands in                                                                    | Answers                                               |
| ---------------------------- | --------------------------------------------------------------------------- | ----------------------------------------------------- |
| The ledger                   | `loom.agent_events`, one row per operation or worker pass                   | who called what, how long it took, what it cost       |
| OpenTelemetry                | `default.otel_traces`, `otel_logs`, `otel_metrics_*` (or an OTLP collector) | where time went inside a request; trends and alerts   |
| ClickHouse query attribution | `system.query_log.log_comment`                                              | which operation or worker made ClickHouse do the work |

All three share the request id. The [audit log](./audit.md) is a separate record of who
changed what, with tamper evidence.

## The ledger

`loom.agent_events` has one row per operation: `operation`, the identity columns (`org`,
`namespace`, `agent`, `user_id`, `session_id`, `run_id`), `request_id`, `trace_id`,
`latency_ms`, `success`, `error_code`, the token counts, `upstream_cost_usd`, `model`,
`llm_calls`, `consumer`, `traffic_class`, `subject`, `build_commit`, `instance`,
`instance_label`, and an `args` map of bounded, redacted request and output fields. Worker
passes write rows with `kind='worker'`. [Cost and usage](./cost-and-usage.md) is built on
this table.

Each request row also carries its service-level columns:

- `transport`: `http` or `mcp`.
- `slo_class`: `code`, `search`, `admin`, then `write` for any other op that is not
  read-only, else `read`.
- `status_class`: `ok`; `client` for a 4xx refusal or a retried idempotency key still
  running; `server` otherwise.
- `budget_ms`: the latency budget the request was held to. A budget of 0 (an override that
  forces violations) makes every successful request slow.

A call that names no org or namespace is recorded under the ones the gate admitted it
against (a code op's default is the deployment's code namespace), so the row is that
tenant's: its SLIs, the cost rollup, `subject_last_seen` and the history policy's counts
all see it there. A call that names no org and is refused before the gate is recorded
under the token's org (`-` for an org-wide token or with auth off), so `cost.summary`,
the connected-agents view and `subject_last_seen` count it in that org too.

What counts against the server:

- A write refused because its history policy could not be evaluated.
- A refusal made because a read the check needed failed (a grant, a subject's aliases, a
  service account's or a delegated token's creator, a namespace classification). A
  request whose own answer was refused or hidden that way (an id lookup's `found=False`,
  a 403 or 404 from a handler's own check, an org-wide rollup emptied whole) is `server`
  even when it succeeded on the wire, with an empty `error_code` then.
- Not a namespace left out of an answer that is whole without it: one Loom adds on its
  own (code links, grounding, repository documents, a trunk, a tracker enriching a code
  answer whose item the caller did not name), an entry a listing the caller did not
  scope or a default pick leaves out, or an owner check on a task whose poster is
  admitted anyway. Those keep the request `ok`.

Refusals before a call is admitted:

- MCP writes a row for a call it refuses before dispatch (an org or namespace the token
  may not use, an op kept off MCP, a write on a read-only connection), as HTTP does for
  its refusals. The caller is not bound yet, so that row carries the token's org (or the
  org the call named, when the token may act in it), subject and consumer, `-` for the
  rest and no arguments.
- On either transport, a refusal naming an org the token may not act in is recorded in
  the token's org, never the named one.
- A refusal recorded before the call is classified carries the token's `traffic_class`
  claim, or '' without one.
- Within one MCP batch, a caller's refusal before a handler runs is recorded once per
  operation, code, org and status class, and a server fault at most ten times; the rows not written
  count in `loom.ledger.batch_repeats` (a caller's) or
  `loom.ledger.dropped{reason="batch_repeat"}` (a server fault's, lost server errors).
  An idempotency refusal (after the rate limiter) and a handler's failure always have
  their own row.

A refreshable materialized view folds the ledger into `loom_telemetry.sli_minute` once a
minute (requests, server and client errors, rate-limited requests, which are among the
client errors, over-budget successes, latency quantiles, kept 400 days; latency is the
handler's time, the one the budgets are set against, not the gates before it), for SLO
dashboards and alert rules to read without scanning the ledger. It resumes from when
ClickHouse took each row (`ingested_at`), not from the row's `ts`, so a row whose flush
was delayed is still counted once, in the minute of its `ts`; a row is folded at least 70
seconds (up to about 130) after ClickHouse took it, which leaves its insert that long to
commit. A refresh writes one part, so a failed refresh leaves nothing for its retry to
fold twice or skip. A ledger part that commits more than 70 seconds after ClickHouse
stamped its rows is never folded; `loom.sli.rollup_missed_rows` counts such rows over the
last day, and refolding the rollup ([ClickHouse](../../architecture/clickhouse.md))
recovers them. Rows with no SLO class (the `/v1/me/*` routes, and MCP calls that name no
registered op, such as `initialize`) are left out. A minute can arrive as several rows
until they merge, so a reader groups by its keys with `sum()` for the counts and
`quantilesTDigestMerge(0.5, 0.95, 0.99)(latency)` for the latencies. Each refresh also
writes a row with every key empty and every count 0, where the next refresh resumes: a
reader filters `slo_class != ''`. To leave out test traffic, filter `traffic_class NOT IN
('bench', 'background')`: a refusal recorded before the call is classified has an empty
class.

The emitter is batched and never fails a request; a crash can lose the unflushed queue.

Every process that writes rows names its launcher. The compose files set
`LOOM_DEPLOYMENT=1` and label services `deploy:web`, `deploy:workers` or `deploy:dev`. Any
other process (`make server`, a test server) must set
`LOOM_INSTANCE_LABEL=<who>:<what>:<port>` or it refuses to boot; `loom serve` defaults to
`cli:serve:<port>`. This keeps the deployment's rows apart from anything else on the same
ClickHouse.

## Traces and logs

- **Traces.** Spans go to `default.otel_traces` (or the OTLP endpoint), tagged
  `loom.org`, `loom.namespace`, `loom.agent`, `loom.operation`, `loom.request_id` and
  `enduser.id`. Health polls open no span (`LOOM_OTEL_EXCLUDED_URLS`, default
  `/v1/health`). Every HTTP response carries `X-Request-Id` (the client's, when it sends
  one of 1 to 64 letters, digits, `.`, `_` or `-`) and `X-Trace-Id`. Spans never carry query text.
- **Model calls.** Each LLM and embedding call is its own span, `llm.<stage>` or
  `embed.provider`, with the OpenTelemetry GenAI attributes: `gen_ai.provider.name`,
  `gen_ai.operation.name` (`chat` or `embeddings`), `gen_ai.request.model`,
  `gen_ai.usage.input_tokens` and `output_tokens`, the provider prompt-cache share in
  `gen_ai.usage.cache_read.input_tokens` and `cache_creation.input_tokens` (both part
  of `input_tokens`), and `gen_ai.usage.cost` from Loom's price table. The leg that made
  the call is `loom.llm.stage`. Tokens served by Loom's own embedding cache are
  `loom.embed.cached_tokens`, apart from the billed input. HyperDX's LLM view and
  Langfuse read these keys as they are.
- **Logs.** `docker logs loom-server` and `docker logs loom-workers`, and the OTel log
  export to `default.otel_logs`. `LOOM_LOG_FORMAT=json` for a log pipeline,
  `LOOM_LOG_LEVEL` for verbosity. The boot log names the resolved providers and each
  applied migration; the supervisor logs every failed pass and the `DEGRADED` line.
- **Langfuse.** With `LOOM_LANGFUSE_URL` and `LOOM_LANGFUSE_ADMIN_TOKEN` set, the span
  tree is also forwarded to Langfuse as a second span processor, sampled and filtered. See
  [Langfuse](../integrations/langfuse.md).

`init_otel` does not start under pytest, so tests never write into a deployment's
observability tables. `LOOM_OTEL_ENABLED=false` turns emission off.

### Exporters

| Setting                                           | Default       | Effect                                                                                                                                                                 |
| ------------------------------------------------- | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_OTEL_EXPORTER`                              | `""`          | `ch` writes straight into the `default.otel_*` tables on Loom's own ClickHouse service; `otlp` ships to a collector; empty = `otlp` when an endpoint is set, else `ch` |
| `LOOM_OTEL_EXPORTER_OTLP_ENDPOINT`                | `""`          | OTLP/HTTP collector base URL (`/v1/traces` and `/v1/metrics` are appended)                                                                                             |
| `LOOM_OTEL_EXPORTER_OTLP_HEADERS`                 | `""`          | comma-separated `K=V` headers, e.g. `Authorization=Bearer <token>`                                                                                                     |
| `LOOM_OTEL_SERVICE_NAME`, `LOOM_OTEL_ENVIRONMENT` | `loom`, `dev` | `service.name` and `deployment.environment.name` on every span, log and metric                                                                                         |
| `LOOM_OTEL_METRICS_EXPORT_INTERVAL_S`             | `60`          | metric export cadence; spans keep their own 3-second batch delay                                                                                                       |
| `LOOM_OTEL_METRICS_DELTA_TEMPORALITY`             | `false`       | delta instead of cumulative counters and histograms; the stock dashboards read cumulative rows, so flip them together                                                  |
| `LOOM_OTEL_GAUGE_HEARTBEAT_S`                     | `0`           | `ch` exporter only: write a gauge point only on change or after this many seconds; 0 = every export (gauge tiles fill gaps with 0). OTLP exports every point           |
| `LOOM_OTEL_CENSUS_SCOPE`                          | `leader`      | who takes the whole-table censuses: only the supervisor-lease holder, or every process (costly)                                                                        |

The `ch` exporter writes the tables ClickStack/HyperDX reads, so the stock stack has
dashboards with no collector. See [ClickStack](../integrations/clickstack.md) and
`deploy/hyperdx_dashboards.md`.

## Metrics

Metrics are exported every `LOOM_OTEL_METRICS_EXPORT_INTERVAL_S`. With the `ch` exporter
they are written into `default.otel_metrics_sum`, `default.otel_metrics_histogram` and
`default.otel_metrics_gauge`; with `otlp` they go to the collector's `/v1/metrics`.
Dashboard definitions are in `deploy/dashboards/` and
`deploy/hyperdx_dashboards.md`.

Labels come from a closed set (operation, namespace, bounded enums). Keys, user ids,
session ids and query text are never labels; `tests/unit/test_domain_metrics.py` enforces
that. A model label (`loom.llm.fallback`, `loom.llm.response_cache`) names a model the
pricing table lists or the deployment's `LOOM_LLM_MODEL` / `LOOM_LLM_FALLBACK_MODEL`, and
reads `unlisted` for any other, since a request can name a model.

Gauges that need a table scan are computed by the workers-lease holder and cached: the
integrity invariants and embed backlog every `LOOM_OTEL_INVARIANTS_PROBE_S` (3600 s), the
retention census every `LOOM_OTEL_RETENTION_CENSUS_S` (3600 s), and code-plane counts every
`LOOM_OTEL_CODE_COUNTS_CACHE_S` (3600 s).

### Requests and cost

| Metric                          | Type      | Meaning                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------------- | --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.requests`                 | counter   | operations processed, per operation, namespace and success                                                                                                                                                                                                                                                                                                                                                      |
| `loom.request.duration`         | histogram | operation wall time, ms                                                                                                                                                                                                                                                                                                                                                                                         |
| `loom.cost.usd`                 | counter   | upstream provider spend attributed to an operation                                                                                                                                                                                                                                                                                                                                                              |
| `loom.tokens`                   | counter   | tokens by `kind` (`embed`, `llm_input`, `llm_output`)                                                                                                                                                                                                                                                                                                                                                           |
| `loom.ops.paid_errors`          | counter   | failed requests that had already spent upstream                                                                                                                                                                                                                                                                                                                                                                 |
| `loom.llm.fallback`             | counter   | LLM calls answered by `LOOM_LLM_FALLBACK_MODEL`                                                                                                                                                                                                                                                                                                                                                                 |
| `loom.llm.response_cache`       | counter   | exact-prompt LLM cache lookups by outcome                                                                                                                                                                                                                                                                                                                                                                       |
| `loom.memory.build.tokens`      | counter   | LLM tokens spent building memory, by stage and direction                                                                                                                                                                                                                                                                                                                                                        |
| `loom.memory.extraction_yield`  | histogram | prompt tokens per memory written, by `messages` range                                                                                                                                                                                                                                                                                                                                                           |
| `loom.extract.cost_per_row_usd` | histogram | USD per memory row written by one extraction                                                                                                                                                                                                                                                                                                                                                                    |
| `loom.auth.refusals`            | counter   | requests refused for their bearer token (none, one that failed verification, or one that could not be checked yet; also a mint by a token ClickHouse says is revoked), by `reason` (`missing`, `malformed`, `expired`, `revoked`, `invalid`, `unavailable`) and `loom.transport` (`http`, `mcp`, or `scim` for a SCIM request whose provider token is missing, malformed or wrong); none reaches the SLI rollup |

### Agent trusts

A workload trust registered with `--agent-claim` makes a namespace for each agent on its
first exchange, up to `--max-agents` ([bots and CI](../../operations/sso.md#bots-and-ci)).
`loom.trust` is the trust's name, which only an operator registers.

| Metric                              | Type    | Meaning                                                                                                                                                                  |
| ----------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `loom.auth.agents_provisioned`      | counter | first exchanges of a trust's agents, by `loom.org`, `loom.trust` and `outcome`: `made` (its namespace was made) or `at_cap` (refused: the trust has made `--max-agents`) |
| `loom.auth.trust_agents`            | gauge   | live agents of an active trust (a released one does not count), as the cap counts them; computed by the workers-lease holder every five minutes                          |
| `loom.auth.trust_agents_used_ratio` | gauge   | `loom.auth.trust_agents` over the trust's `--max-agents`; at 1 every new agent is refused                                                                                |

### Retrieval quality

These catch a search that is fast and wrong, which HTTP metrics cannot see.

| Metric                                  | Type      | Meaning                                                                                               |
| --------------------------------------- | --------- | ----------------------------------------------------------------------------------------------------- |
| `loom.search.results`                   | histogram | results per search; a drift toward 0 is silent breakage                                               |
| `loom.search.top_similarity`            | histogram | cosine of the best hit; the most diagnostic retrieval signal (re-baseline after an embedder change)   |
| `loom.search.confidence`                | histogram | the response's `retrieval_confidence`                                                                 |
| `loom.search.abstain`                   | counter   | searches that abstained, by `reason`                                                                  |
| `loom.search.plane_hits`                | counter   | hits per retrieval plane                                                                              |
| `loom.search.assist`                    | counter   | optional assists engaged (HyDE, rerank, iterative judge), by `kind`                                   |
| `loom.search.judged_relevance`          | histogram | an independent LLM judge's rating of sampled searches (`LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE`, default 0) |
| `loom.search.judge_verdict`             | counter   | the judge's verdicts by category                                                                      |
| `loom.search.bm25_stats_failures`       | counter   | BM25 corpus-statistics reads that failed open (the lexical leg ran unweighted), by plane and error    |
| `loom.memory.search.enumeration_shaped` | counter   | searches phrased to enumerate stored records as a class; a signal, not a refusal                      |

### Memory writes

| Metric                                         | Type    | Meaning                                                                                                                  |
| ---------------------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------------ |
| `loom.memory.writes`                           | counter | write outcomes: created, updated, merged, duplicate, conflicted, rejected, blacklisted, too_large, …                     |
| `loom.memory.drops`                            | counter | items the ingest pipeline dropped or truncated, by `stage` and `reason` (rows in `loom.memory_drops`)                    |
| `loom.memory.attribute_tagged`                 | counter | rows labelled `attribute:<class>` at write time                                                                          |
| `loom.policy.decisions`                        | counter | history-policy gate decisions                                                                                            |
| `loom.procedure.runs`, `loom.procedure.evolve` | counter | procedure runs by outcome; `procedure.evolve` results                                                                    |
| `loom.memory.agent_labels_24h`                 | gauge   | usage labels agents wrote in the last 24 h, per namespace (top `LOOM_OTEL_AGENT_LABELS_MAX_NAMESPACES`, rest as `other`) |
| `loom.memory.agent_labels_namespaces_at_pace`  | gauge   | namespaces per org whose agent labels reach the daily pace of 1,000 a month                                              |

`loom drops --namespace NS [--hours 24]` prints the drop counts by stage and reason. An
extraction that fails outright is not a drop: `set_from_messages` answers 502
`EXTRACTION_FAILED`, and partial failures come back in the response's `pipeline_errors`.

### Backlogs and process state

A level is expected to be non-zero and to drain.

| Metric                                                                                                                       | Type      | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ---------------------------------------------------------------------------------------------------------------------------- | --------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.embed.pending`                                                                                                         | gauge     | rows waiting for a vector (`embedding_dim = 0`); memories not yet searchable                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `loom.vectors.total`                                                                                                         | gauge     | embedded vectors present                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `loom.vector_index.searches_total`, `adds_total`, `reads_indexed_ratio`                                                      | gauge     | vector-index use on the probed replica: searches served against build cost paid, and the share of recent vector reads that opened the index                                                                                                                                                                                                                                                                                                                                                               |
| `loom.process.open_fds`, `loom.process.fd_limit`                                                                             | gauge     | open descriptors and the soft limit per process (`loom.pid`); a rising line is a leak                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `loom.host.disk.free_bytes`, `loom.host.disk.total_bytes`                                                                    | gauge     | bytes free and total on the disk that holds `var/`                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| `loom.ch.mutations_pending`                                                                                                  | gauge     | unfinished ClickHouse mutations                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `loom.ch.table.active_parts`, `wide_parts`, `patch_parts`, `bytes_on_disk`, `rows_per_mark`                                  | gauge     | per table (`loom.table`) for the housekept tables                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `loom.memory_embeddings.patch_parts`                                                                                         | gauge     | patch parts on `loom.memory_embeddings`; non-zero switches off the quantized-codes read                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `loom.ch.view_refresh.scheduled`, `loom.ch.view_refresh.age_s`                                                               | gauge     | per refreshable view (`loom.view`, bare in `loom`, else with its database: `loom_telemetry.sli_minute_refresh`): 1 while scheduled without error; seconds since its last success                                                                                                                                                                                                                                                                                                                          |
| `loom.sli.rollup_lag_s`                                                                                                      | gauge     | seconds `loom_telemetry.sli_minute`'s mark trailed now at the last storage probe (every five minutes, on the workers-lease holder): about 70 to 130 while current; more is a rollup catching up, whose newest minutes read empty                                                                                                                                                                                                                                                                          |
| `loom.sli.rollup_missed_rows`                                                                                                | gauge     | ledger request rows of the last day whose insert committed after the rollup's mark had passed them, so `sli_minute` never counts them (storage probe, every five minutes, on the workers-lease holder); alert above 0 and refold the rollup                                                                                                                                                                                                                                                               |
| `loom.audit.checkpoint_lag_windows`                                                                                          | gauge     | hourly audit windows not yet sealed into the checkpoint chain ([audit log](./audit.md#checkpoint-lag))                                                                                                                                                                                                                                                                                                                                                                                                    |
| `loom.langfuse.breaker`                                                                                                      | gauge     | Langfuse score breaker: 0 closed, 1 half-open, 2 open (scores dropped; span export is not affected)                                                                                                                                                                                                                                                                                                                                                                                                       |
| `loom.health.problems`                                                                                                       | gauge     | 1 per open `/v1/health` problem on this process, by `code` and `severity` (`provider` for `provider_failing`), except those `/v1/health` finds with a ClickHouse read (`otel_erasure_index`); a resolved problem stops reporting, so an alert reads a window of at least `LOOM_OTEL_GAUGE_HEARTBEAT_S` plus one `LOOM_OTEL_METRICS_EXPORT_INTERVAL_S` (two intervals without a heartbeat): with the `ch` exporter and a heartbeat, an unchanged value is written at the first export after each heartbeat |
| `loom.ledger.dropped`                                                                                                        | counter   | ledger rows this process lost since it started, by `reason` (`queue_full`, `flush_failed`, and `batch_repeat`: server faults an MCP batch did not write past its ten rows); the SLIs undercount by as much                                                                                                                                                                                                                                                                                                |
| `loom.ledger.batch_repeats`                                                                                                  | counter   | a caller's refusal rows an MCP batch did not write again (each is ledgered once per batch); the caller's doing, not a loss                                                                                                                                                                                                                                                                                                                                                                                |
| `loom.worker.passes`                                                                                                         | counter   | passes by `worker` and `outcome`                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `loom.worker.pass_duration_ms`                                                                                               | histogram | pass wall time                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `loom.worker.consecutive_failures`                                                                                           | gauge     | current failure streak per worker; alert at 3                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `loom.worker.partial_failures`                                                                                               | gauge     | failures the last pass survived                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `loom.retention.active_rows`, `never_recalled_ratio`, `low_information_rows`, `sensitive_without_expiry`, `stale_candidates` | gauge     | per-org memory hygiene counts; Loom deletes nothing by age                                                                                                                                                                                                                                                                                                                                                                                                                                                |

### Integrity invariants

Each should read 0 forever. A non-zero value is a defect that has already happened and
will not drain.

| Metric                                                          | A non-zero value means                                                          | Repair                                          |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------- | ----------------------------------------------- |
| `loom.integrity.divergent_entities`                             | entities differ between `loom.memory` and `loom.memory_embeddings` for a memory | investigate the write path                      |
| `loom.integrity.orphan_vectors`                                 | embedded vectors whose memory row is gone                                       | `orphan_vector_reconcile` worker                |
| `loom.integrity.orphan_entity_vectors`                          | entity vectors whose memory row is gone                                         | `orphan_vector_reconcile` worker                |
| `loom.integrity.orphan_graph_namespaces`                        | namespaces with entity edges but no live memory or knowledge                    | `loom erasure-re-verify --orphan-graph --erase` |
| `loom.integrity.live_ns_unregistered`, `live_rows_unregistered` | live memory in namespaces with no `loom.namespaces` row                         | register the namespace                          |
| `loom.integrity.dim0_with_vector`                               | rows with a vector but `embedding_dim = 0`, invisible to search                 | `loom integrity-repair --apply`                 |
| `loom.integrity.tombstone_status_drift`                         | rows with `tombstoned_at` set and another status                                | listed by `loom integrity-repair`               |
| `loom.integrity.active_past_valid_to`                           | active rows past their own `valid_to`                                           | listed by `loom integrity-repair`               |
| `loom.integrity.high_with_embedding`                            | high-sensitivity rows that still have a live embedding row                      | `loom sensitivity-sweep --apply`                |

### Code plane

Per namespace.

| Metric                                                                  | Type               | Meaning                                                                                                                                          |
| ----------------------------------------------------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `loom.code.index_lag_commits`                                           | gauge              | commits between the index watermark and the branch tip; the staleness signal                                                                     |
| `loom.code.index_age_seconds`                                           | gauge              | seconds since the watermark last advanced                                                                                                        |
| `loom.code.stale_files`                                                 | gauge              | indexed paths whose chunks predate the file's content at head                                                                                    |
| `loom.code.scip_lag_commits`, `scip_age_seconds`, `scip_py_age_seconds` | gauge              | how far each compiler tier (`tier` attribute) trails the fast tier                                                                               |
| `loom.code.tier_lag_seconds`                                            | gauge              | age of the oldest commit a tier (`fast`, `clang`, `python`, …) has not reached                                                                   |
| `loom.code.pr_cursor_age_seconds`                                       | gauge              | how far the PR-history sync trails GitHub                                                                                                        |
| `loom.code.files_errored`, `loom.code.embed_backlog`                    | gauge              | files with an index error; chunks waiting for a vector                                                                                           |
| `loom.code.symbols`                                                     | gauge              | live symbols by `tier` (`treesitter`, `scip`); a drop is a bad apply                                                                             |
| `loom.code.edges_unqualified`, `loom.code.edges_low_confidence`         | gauge              | parser-tier call edges bound by name only; compare with `loom.code.symbols`                                                                      |
| `loom.code.grep.wrong_abstain_24h`                                      | gauge              | `code.grep` abstains on a literal the corpus contains; top 8 consumers + `other` (recounted every `LOOM_OTEL_CODE_GREP_REPLAY_CACHE_S`; 0 = off) |
| `loom.code.repos_config_errors`                                         | gauge              | unusable entries in `LOOM_CODE_REPOS_JSON` (0 = parsed clean)                                                                                    |
| `loom.code.outcome`, `loom.code.shunt`, `loom.code.shunt_tokens_saved`  | counter            | client-reported outcomes; coding-agent shunt decisions and tokens kept out of context                                                            |
| `loom.code.ask_cache`, `ask_cache_saved_ms`, `ask_cache_saved_usd`      | counter            | `code.ask` answer-cache outcomes and what hits saved                                                                                             |
| `loom.tracker.sections`, `loom.tracker.section_ms`                      | counter, histogram | `tracker.answer` sections by template and status; read time                                                                                      |

See [code index freshness](../code-index/freshness.md) for what the lag gauges mean to a
caller.

### Suggested alerts

| Condition                                                                                                                | Why                                                                                                                                                  |
| ------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom.search.top_similarity` mean below your baseline for 30 minutes                                                     | retrieval quality dropped (embedder or index change)                                                                                                 |
| `loom.embed.pending` above 100 for 15 minutes                                                                            | new memories are not becoming searchable                                                                                                             |
| any `loom.integrity.*` above 0                                                                                           | a data defect has happened                                                                                                                           |
| `loom.worker.consecutive_failures` ≥ 3                                                                                   | a worker is failing in a loop                                                                                                                        |
| `loom.process.open_fds / loom.process.fd_limit` above 0.5                                                                | a descriptor leak                                                                                                                                    |
| `loom.host.disk.free_bytes / loom.host.disk.total_bytes` below 0.10                                                      | the data disk filling                                                                                                                                |
| `loom.langfuse.breaker` = 2                                                                                              | scores to Langfuse are being dropped                                                                                                                 |
| `loom.audit.checkpoint_lag_windows` rising for several hours                                                             | the audit sealer is stalled                                                                                                                          |
| `loom.code.index_lag_commits` above the per-pass commit budget for 15 minutes                                            | code ingest is falling behind                                                                                                                        |
| `loom.code.tier_lag_seconds` above 1800 for 10 minutes                                                                   | a compiler tier is stuck (the "code tier lag" alert in `deploy/dashboards/code_plane.json`; set the threshold above the slowest tier's normal cycle) |
| `loom.auth.agents_provisioned{outcome=made}` per trust above its control plane's normal rate (start at 50 in 10 minutes) | a trust is naming new agents faster than its fleet grows: a leaked issuer key or a runaway control plane                                             |
| `loom.auth.trust_agents_used_ratio` above 0.8                                                                            | the trust nears `--max-agents`                                                                                                                       |
| `loom.auth.agents_provisioned{outcome=at_cap}` increase above 0 over 10 minutes                                          | new agents of the trust are refused                                                                                                                  |

With HyperDX, an empty result is read as 0, so a minimum-sample guard must return a value
inside the healthy range rather than no rows.

Starting queries:

```sql
-- mean top similarity per namespace, last hour
SELECT Attributes['loom.namespace'] AS ns,
       round(anyLast(Sum) / greatest(anyLast(Count), 1), 3) AS mean_top_similarity
FROM default.otel_metrics_histogram
WHERE MetricName = 'loom.search.top_similarity' AND TimeUnix > now() - INTERVAL 1 HOUR
GROUP BY ns;

-- every gauge's latest value
SELECT MetricName, anyLast(Value) AS latest
FROM default.otel_metrics_gauge
WHERE TimeUnix > now() - INTERVAL 5 MINUTE
GROUP BY MetricName ORDER BY MetricName;
```

## Query attribution in ClickHouse

Every ClickHouse query carries a `log_comment` setting naming its issuer: `<operation>
<request_id>` for a request, `worker.<name>` for a worker pass, `cli.<command>` for a CLI
command, else `untagged:<module>.<function>`. The tag table, the `query_id` join and the
statement digest are in [ClickHouse: query attribution](../../architecture/clickhouse.md#query-attribution).

```sql
-- one request end to end
SELECT * FROM loom.agent_events WHERE request_id = '<id>';
SELECT query_duration_ms, read_bytes, log_comment
FROM system.query_log
WHERE type = 'QueryFinish' AND log_comment LIKE '% <id>%';
```

## Not telemetry: the `observe.*` operations

`observe.record` and `observe.crystallize` belong to the memory plane. They capture an
agent's tool calls:

- `observe.record` stores one tool call (tool name, input, output, file paths, error) as an
  episodic, non-searchable memory row keyed `obs:<session_id>:<sha8>`, with no LLM call and
  no synchronous embedding. A tool call that is Loom itself or a harness meta tool is
  stored without content (`skipped_reason: "self_referential"`), and so is one whose
  content matches a blacklist pattern (`skipped_reason: "blacklisted"`).
- `observe.crystallize` is the session-end flush: it marks the session's pending
  observations ready for extraction. It queues; it runs no LLM.

Distilling observations needs the `observe_extract` worker (off by default,
`LOOM_OBSERVE_EXTRACT_ENABLED`). With it off, observations are stored and never extracted.

See [conversations and sessions](../conversations-and-sessions.md) for how the coding
agent plugins use them.

`ui.vitals` serves the dashboard: it records one page's web vitals on its own ledger row.

## Related

- [Monitoring](../../operations/monitoring.md): health checks, dashboards and the
  operator's daily view.
- [Troubleshooting](../../operations/troubleshooting.md).
- [Cost and usage](./cost-and-usage.md) and [audit log](./audit.md).
- [ClickStack](../integrations/clickstack.md) and [Langfuse](../integrations/langfuse.md).
- [Workers reference](../../reference/workers.md) and
  [configuration reference](../../reference/configuration.md#observability).
