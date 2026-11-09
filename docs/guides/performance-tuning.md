# Tune Loom's performance

Every lever on this page is a setting or a request field. The
[configuration reference](../reference/configuration.md) gives each setting's default and
meaning. Where a trade-off is measured, the page names the command that measures it.

## Measure first

| Question                                           | Command                                                                               | Needs                                                |
| -------------------------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| p50 / p95 / p99 and RPS of one op at concurrency N | `make bench OP=memory.search C=5 D=60` (`scripts/ops/benchmark.py`)                   | a running server; writes go to a `bench-…` namespace |
| Did a ranking or embedder change hurt recall?      | `make bench-quick` (a slice of a public long-memory benchmark with an answer judge)   | real providers, `OPENAI_API_KEY`                     |
| Recall against the recorded baseline               | `make bench-quality-gate MIN_RECALL=…`                                                | real providers                                       |
| search mode × rerank × pool width                  | `make bench-matrix` (writes `bench/MATRIX_RESULTS.md`)                                | a running server                                     |
| Extraction and graph extraction regressions        | `make bench-algorithm-gate`                                                           | a running server                                     |
| Code plane symbol, callers, recall and token cost  | `make bench-code-intel`, `make bench-code-legs`                                       | a real embedder                                      |
| What changed after a deploy                        | `loom deploy-delta` (ledger and ClickHouse statement shapes on each side of the swap) | `var/deployed.json`                                  |

> **Warning:** a recall number measured against the fake embedder says nothing about real
> retrieval. The quality gates are kept out of CI for that reason. Run them against a
> server with the providers you will deploy, and re-baseline after any embedder change.

For a single slow request, read its `Server-Timing` header (`app` vs `ch`) and its
`system.query_log` rows before touching a setting ([debugging](debugging.md)). Per-operation
cost across the deployment is one query over `log_comment`:

```sql
SELECT splitByChar(' ', log_comment)[1] AS source,
       count() AS queries, sum(read_bytes) AS bytes, sum(query_duration_ms) AS ms
FROM system.query_log
WHERE type = 'QueryFinish' AND event_time > now() - INTERVAL 1 HOUR AND log_comment != ''
GROUP BY source ORDER BY ms DESC LIMIT 20;
```

Benchmark traffic right after a swap reads cold caches; wait before comparing a deploy's
latency with the previous build's.

## Ranking and thresholds

`memory.search` ranks a first-stage candidate pool, then applies rank terms and gates
([retrieval and ranking](../concepts/retrieval-and-ranking.md)). Most terms can be set per
deployment and overridden per request, so a change can be A/B-tested on one caller first.

| Lever                              | Setting (request field)                                                                                                                                                 | Trade                                                                                                      |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Similarity gates                   | `LOOM_ADAPTIVE_THRESHOLDS` (default on)                                                                                                                                 | On: gates derive from the embedder's own cosine distribution at boot. Off: you own the scalar gates below. |
| Scalar gates (fallback)            | `LOOM_MEMORY_SEARCH_MIN_SIMILARITY`, `LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD`, `LOOM_MEMORY_SEARCH_HYDE_FALLBACK_THRESHOLD`, `LOOM_MEMORY_SEARCH_QUERY_REWRITE_THRESHOLD` | Tuned for `text-embedding-3-small`; used for fake embedders, a failed probe, or adaptivity off.            |
| Sub-floor hits                     | `LOOM_MEMORY_SEARCH_FLOOR_MODE` (`floor_mode`)                                                                                                                          | `readmit` fills `top_k` and tags `below_floor`; `ids_only` fills with empty bodies; `drop` returns fewer.  |
| Candidate pool                     | `LOOM_MEMORY_SEARCH_POOL_MULTIPLIER_STANDARD`, `_RERANK`, `_DEEP`, capped by `LOOM_MEMORY_SEARCH_MAX_POOL_TOP_K`                                                        | A wider pool raises recall and the rerank's input; it costs vector rows read and rerank tokens.            |
| Rerank skip                        | `LOOM_LLM_RERANK_SKIP_ABOVE` (fallback when the adaptive gate has no probe)                                                                                             | Skips the rerank call when the top vector hit is already strong.                                           |
| Fallback cascade                   | `LOOM_MEMORY_SEARCH_FALLBACK_MAX_ROUNDS`, `_MAX_WALL_MS`, `_MAX_LLM_CALLS`, `_MAX_VECTOR_QUERIES`                                                                       | Bounds the retries a weak first pass may spend (query rewrite, HyDE).                                      |
| Reinforcement                      | `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT` (`reinforcement_weight`), `_HALF_LIFE_DAYS`                                                                                   | How much recall and positive outcomes lift a row; 0 disables. Capped below relevance.                      |
| Provenance                         | `LOOM_MEMORY_SEARCH_TRUST_WEIGHT` (default 0)                                                                                                                           | A bounded term favouring attested sources.                                                                 |
| Recency                            | `LOOM_MEMORY_SEARCH_RECENCY_POOL_WEIGHT` (`recency_pool_weight`)                                                                                                        | Re-ranks the pool toward recent rows on a recency-intent query; 0 = off.                                   |
| Lexical leg on code-shaped queries | `rrf_adaptive_lexical` (request), `LOOM_MEMORY_RRF_CODE_SHAPE_LO` / `_HI`                                                                                               | Down-weights the lexical leg for path- or code-shaped queries under RRF.                                   |
| Default mode                       | `LOOM_MEMORY_SEARCH_DEFAULT_MODE` (`search_mode`)                                                                                                                       | Used when the caller names neither a mode nor a recipe.                                                    |
| Duplicates per key                 | `LOOM_MEMORY_SEARCH_PER_KEY_CAP`                                                                                                                                        | Rows one key may contribute to a page.                                                                     |

> **Warning:** a threshold tuned on one embedder is wrong on the next. With
> `LOOM_ADAPTIVE_THRESHOLDS=false`, or on a fake embedder, re-bench (`make bench-quick`,
> `make bench-quality-gate`) after every embedder change. `calibration.get` shows which
> tier produced the gates in force for a namespace.

## Vector reads

Vector columns are `Array(BFloat16)`; where `enable_quantized_codec` can be on the migrator
creates them coded ([ClickHouse](../architecture/clickhouse.md#gated-clickhouse-settings)).

| Lever                      | Setting                                                                                           | Trade                                                                                                                                                                                                                                                                                                          |
| -------------------------- | ------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Codes-first scan           | `LOOM_VECTOR_SEARCH_QUANTIZED_CODES` (default on), `LOOM_MEMORY_SEARCH_FACT_CODES_FIRST` (`auto`) | Reads the quantized codes, then rescores at full precision. `auto` turns it off while `memory_embeddings` has patch parts, where the rescore would be skipped.                                                                                                                                                 |
| Codes over-fetch           | `LOOM_VECTOR_SEARCH_QUANTIZED_FETCH_MULTIPLIER`, `LOOM_MEMORY_SEARCH_FACT_CODES_FETCH_MULTIPLIER` | Rows kept by code distance before the rescore. Lower is cheaper and changes the top hits more often.                                                                                                                                                                                                           |
| Threads per fact statement | `LOOM_MEMORY_SEARCH_FACT_LEG_MAX_THREADS` (0 = unpinned)                                          | Pinning bounds CPU and peak memory per statement at some latency.                                                                                                                                                                                                                                              |
| Bulk writes' peer reads    | `LOOM_MEMORY_BULK_PEER_READ` (`batched`, or `per_fact`)                                           | `per_fact` reads each bulk fact's peers codes-first, as `memory.set` does: for large namespaces, where the batched peer statement fails with `MEMORY_LIMIT_EXCEEDED` (`bulk_set: batched candidate lookup failed` in the server log) and entries are written unchecked. A peer may differ where distances tie. |

To move an existing table onto a codec or layout, use `scripts/ops/vector_codec_rebuild.py`
(twin table, consistency and recall gates, `EXCHANGE`;
[steps](../operations/deployment.md#recommended-clickhouse-settings)), never an `ALTER` in a
migration.

## ClickHouse connections and deadlines

| Lever                     | Setting                                                                                                                                                                             | Trade                                                                                                                                                                                                                                                          |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pool size                 | `LOOM_CH_POOL_MAXSIZE` (default 64 per process)                                                                                                                                     | Size to expected request concurrency plus the workers, plus the connections batched inserts hold for their wait; the driver's own default exhausts under bulk writes.                                                                                          |
| Batched inserts           | `LOOM_CH_BATCHED_INSERTS` (default on), `LOOM_CH_BATCHED_INSERT_WAIT_MIN_MS` (10), `LOOM_CH_BATCHED_INSERT_WAIT_MAX_MS` (50), `LOOM_CH_BATCHED_INSERT_BACKGROUND_WAIT_MAX_MS` (200) | Concurrent inserts share a part, at up to the window's latency each; a lone insert waits the minimum; each waiting insert holds a pooled connection, so the pool grows with the write rate ([batched inserts](../architecture/clickhouse.md#batched-inserts)). |
| Pool wait                 | `LOOM_CH_POOL_WAIT_MS` (default 500)                                                                                                                                                | Past it a request fails fast with 503 `OVERLOADED` instead of queueing; 0 = no gate.                                                                                                                                                                           |
| Keep-alive                | `LOOM_CH_KEEPALIVE_TIMEOUT` (default 5 s)                                                                                                                                           | Must stay below the load balancer's idle cutoff, so Loom closes a pooled connection first.                                                                                                                                                                     |
| Statement deadlines       | `LOOM_CH_TIMEOUT_READ_S`, `LOOM_CH_TIMEOUT_WRITE_S`, `LOOM_CH_TIMEOUT_DDL_S`                                                                                                        | Applied as `max_execution_time` and as a client bound that frees the pool slot; 0 = unbounded for that class.                                                                                                                                                  |
| Priority by traffic class | `LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS` (JSON)                                                                                                                                          | `priority` and `max_threads` per class (`production`, `interactive`, `bench`, `background`), so benches and workers yield to production reads.                                                                                                                 |
| Read-your-writes          | `LOOM_CH_READ_YOUR_WRITES`, `LOOM_CH_READ_YOUR_WRITES_WINDOW_S`                                                                                                                     | Sequentially consistent reads after a write; a cost on replicated services, a no-op on a single node.                                                                                                                                                          |
| Operation budgets         | `LOOM_LATENCY_BUDGET_MULTIPLIER`                                                                                                                                                    | Scales every op's `budget_p99_ms`; a WAN hop to ClickHouse needs about 20. Budgets only raise `budget.violated` events; they do not cut requests.                                                                                                              |

Web capacity is `LOOM_WEB_WORKERS` per container times containers; the rate limiter divides
its caps by the live replicas it counts in `loom.instances`, never fewer than
`LOOM_REPLICA_COUNT` ([replicas](../operations/replicas.md#rate-limits)).

## Providers

| Lever                        | Setting                                                                                                                           | Trade                                                                                                                                                                                                   |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Query-embedding bound        | `LOOM_MEMORY_SEARCH_EMBED_BOUND_S`, `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES`                                                      | Per-attempt bound on the query embedding; after the retries the search answers 503 `EMBED_TIMEOUT`.                                                                                                     |
| Query spelling               | `LOOM_MEMORY_SEARCH_QUERY_SPELLING_MAX_WORDS`                                                                                     | A query of at most this many words reads the namespace once, beside the embed, to embed its lowercase words in the corpus spelling: 100–300 ms for a 40k-row namespace, reused for a minute; `0` = off. |
| Query-embedding caches       | `LOOM_EMBEDDING_CACHE_SIZE`, `LOOM_EMBEDDING_CACHE_TTL_S`, `LOOM_EMBEDDING_CACHE_SHARED`, `LOOM_EMBEDDING_CACHE_SHARED_MAX_CHARS` | A per-process LRU in front of a shared `loom.embedding_cache`; repeated queries skip the provider call.                                                                                                 |
| LLM response cache per class | `LOOM_LLM_POLICY_BY_TRAFFIC_CLASS` (JSON)                                                                                         | Caches LLM responses (on by default for `bench`) and can bar a class from the strong or mid tier.                                                                                                       |
| Fallback model               | `LOOM_LLM_FALLBACK_MODEL`                                                                                                         | Answers when the primary stays unavailable after retries, instead of failing the op.                                                                                                                    |
| Spend cap                    | `LOOM_DEFAULT_DAILY_COST_CAP_USD` (per namespace `daily_cost_cap_usd`)                                                            | Over the cap, LLM-backed workers skip the namespace and `memory.ask` abstains with `budget_capped`; plain reads and writes continue.                                                                    |

### Embedder choice

| Embedder (`LOOM_EMBEDDING_MODEL`)                            | Trade                                                                                           |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------- |
| `text-embedding-3-large`, `gemini-embedding-001`             | Loom's 3072-wide default; quality and cost must still be measured on your data.                 |
| `text-embedding-3-small`                                     | Narrower 1536-vector option; changing width requires re-embedding.                              |
| `local`, `local:bge-small`, `local:bge-base`, `local:minilm` | CPU embedding in-process, no provider call or key; trades quality for latency and cost.         |
| `fake-ngram-3072`                                            | Character n-grams at the standard width for tests and wiring only; recall numbers mean nothing. |

Changing the embedder changes the vector space: set `LOOM_EMBEDDING_DIM` to match, run
`loom reembed`, and with adaptive thresholds on let the boot probe recalibrate. Run
`make bench-quick` before and after; the same holds for `LOOM_LLM_MODEL`, which the
extraction prompts are calibrated against.

## Workers and batches

Every worker has an `*_ENABLED` switch and an `*_INTERVAL_SECONDS` cadence, and many have
an `*_IDLE_INTERVAL_SECONDS` sleep after a pass that found nothing
([workers reference](../reference/workers.md)). Shorter intervals lower the lag of
background work at the cost of more scans; the idle interval keeps an empty queue cheap.

| Worker                | Levers                                                                                                                                                                                                                               |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `embedding_worker`    | `LOOM_EMBEDDING_WORKER_INTERVAL_SECONDS`, `_IDLE_INTERVAL_SECONDS`; `LOOM_EMBEDDING_WORKER_ORPHAN_FULL_SCAN_SECONDS` / `_ORPHAN_RECENT_SECONDS` bound the orphan anti-join.                                                          |
| `extraction_queue`    | `LOOM_EXTRACTION_QUEUE_INTERVAL_SECONDS`, `_MAX_BATCHES_PER_PASS`, `_NAMESPACE_CONCURRENCY`, `_LEASE_SECONDS`; `LOOM_EXTRACTION_DEFER_AGENTS` defers `set_from_messages` for listed agents so the request returns before extraction. |
| `memory_expiry_sweep` | `LOOM_MEMORY_EXPIRY_SWEEP_BATCH_SIZE`.                                                                                                                                                                                               |
| `task_lease_sweep`    | `LOOM_TASK_LEASE_SWEEP_INTERVAL_SECONDS`, `_IDLE_INTERVAL_SECONDS`.                                                                                                                                                                  |

Workers run in the `background` traffic class, so `LOOM_CH_SETTINGS_BY_TRAFFIC_CLASS` also
bounds their ClickHouse priority and threads. Run exactly one workers process: a second
copy of an LLM-backed worker doubles its scans and its spend. The code plane's ingest and
compiler-tier workers have their own levers ([code index operations](../operations/code-index-operations.md)).

## Client-side levers

Some of the largest wins are in the request:

- Ask for fewer bodies: `bodies` and `body_budget_tokens` on `memory.search`, `token_budget`
  on `context.pack` and `code.search`.
- Load context once per task (`context.pack`) instead of one search per step.
- Use `retrieval_budget` to pick a cheaper or deeper search profile per call.
- Retry idempotent ops with an `Idempotency-Key` header (the SDKs generate one): a
  replay returns the stored response instead of running the op again.

## Related

- [Failure and recovery](../architecture/failure-and-recovery.md): capacity and what to scale
- [Monitoring](../operations/monitoring.md)
- [Running benchmarks](../benchmarks/running.md)
- [Debugging](debugging.md)
