# Langfuse

Needs `LOOM_LANGFUSE_URL` and `LOOM_LANGFUSE_ADMIN_TOKEN`; nothing is sent until both
are set. Once they are, the OpenTelemetry span tree is forwarded to Langfuse over OTLP.
Langfuse is optional and is not part of Loom's local Compose stack.

Loom is tested against self-hosted **Langfuse 3.227.0** and **Langfuse 4.76.0**. Langfuse
Cloud runs Langfuse 4. A local Langfuse deployment also runs PostgreSQL, ClickHouse,
Redis/Valkey and S3-compatible object storage, so it is substantially larger than the
two-service Loom profile.

Loom keeps its own telemetry in ClickHouse ([telemetry](../observability/telemetry.md)).
Langfuse adds a trace per request with nested model calls, per-generation cost,
filterable scores and prompt versions. Loom plays four roles against it:

| Role            | What Loom does                                                                                    | Needs                         |
| --------------- | ------------------------------------------------------------------------------------------------- | ----------------------------- |
| Trace writer    | the OTel span tree of each request, with its generations, forwarded over OTLP                     | a `pk:sk` token, OTel on      |
| Score writer    | retrieval-quality scores on the trace of each search and ask                                      | a `pk:sk` token               |
| Prompt linker   | tags each generation with the Langfuse prompt name and version it used, from a committed lockfile | the lockfile; no runtime call |
| Erasure backend | deletes the traces of an erased user or namespace                                                 | URL and token (any form)      |

Loom is **not** in the prompt-fetch path. An agent that manages its own prompts in
Langfuse fetches them with the Langfuse SDK's `get_prompt`.

## Setup

```bash
LOOM_LANGFUSE_URL=https://langfuse.example.com
LOOM_LANGFUSE_ADMIN_TOKEN=pk-lf-...:sk-lf-...
```

The token is a `public_key:secret_key` pair, sent as HTTP Basic. A single bearer token
(no `:`) still works for erasure, but disables trace and score emission; the server logs
one warning saying so.

Check the result with `GET /v1/health?include=langfuse`: it reports local exporter state
and, under `scores` and `retrieval_judge`, how many scores were posted, failed or dropped
(queue full, breaker open). This is not a remote Langfuse readiness probe. The boot log's
`mode` token includes `langfuse` when both settings are present.

## Traces

Loom attaches a second span processor beside the ClickHouse exporter and forwards the
OpenTelemetry span tree to Langfuse's OTLP endpoint. The trace carries the real nesting
(the HTTP span, the handler, each generation), the session id and per-generation cost.
With OTel disabled (`LOOM_OTEL_ENABLED=false`) no traces reach Langfuse.

| Setting                                                                       | Default                                             | Effect                                                                                                   |
| ----------------------------------------------------------------------------- | --------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `LOOM_LANGFUSE_SAMPLE_RATE`                                                   | `1.0`                                               | fraction of traces forwarded, sampled by trace id so a trace arrives whole                               |
| `LOOM_LANGFUSE_SPAN_DENYLIST`                                                 | `ch.query,ch.insert,GET /v1/health,POST /v1/health` | span-name prefixes never forwarded (infrastructure spans belong in ClickStack)                           |
| `LOOM_LANGFUSE_CIRCUIT_FAILURE_THRESHOLD`, `LOOM_LANGFUSE_CIRCUIT_COOLDOWN_S` | `5`, `30`                                           | the breaker: after N consecutive failed score POSTs, scores are dropped for the cooldown, then one probe |

Langfuse never sits on Loom's request path. The breaker keeps failing score POSTs from
becoming retry pressure; the OTLP batch processor owns its own delivery queue, so a span
export failure does **not** open it. The `loom.langfuse.breaker` gauge (0 closed,
1 half-open, 2 open) therefore reads score posting, not remote availability. Worker spend
traces are named `worker.<name>` with the worker's org and namespace. Embedding spans that
are trace roots (the embedding backfill) and worker pass roots are never forwarded; a
request-path embed nests under its handler span and is kept.

> **Tip:** span volume is dominated by worker embeddings. Turn
> `LOOM_LANGFUSE_SAMPLE_RATE` down in production before configuring Langfuse.

## What text leaves Loom

How much user content reaches Langfuse:

- **Request and response payloads.** Never sent: spans carry no request or response
  bodies.
- **Prompt and completion text of Loom's own model calls**, by what the prompt holds:

  | `LOOM_LANGFUSE_CAPTURE_IO_LEVEL` | Records                                                                                                               |
  | -------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
  | `off` (default)                  | none                                                                                                                  |
  | `query`                          | only legs whose prompt is the user's own search text (HyDE, query rewrite, enrichment, …)                             |
  | `all`                            | also legs carrying retrieved memories or conversation turns (rerank, iterative judge, extraction, session summary, …) |

  A namespace's `capture_generation_io` (set with `namespace.register`) overrides it:
  `false` means off whatever the server says, `true` means `all`, unset inherits. Each
  field is capped at `LOOM_LANGFUSE_GENERATION_IO_MAX_CHARS` (2000) with an explicit
  truncation marker. The same level gates `loom.search_trace_log.query`.

Token counts, cost, latency, quality scores and prompt-version links carry no user
content and are never gated.

## Scores

`server/observability/langfuse_scores.py` posts retrieval-quality scores onto the trace of
each search: `retrieval_confidence`, `top_similarity`, `abstained` and `abstain_reason`.
`code.ask` adds `code_ask_cache_hit` and `code_ask_coverage`. With
`LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE` above 0 (default 0; it costs a model call per sampled
search), an independent LLM judge writes a `retrieval_relevance` score that shares no
state with retrieval.

Scores stay off the request path: a bounded queue, drained in the background, drops
oldest-first when Langfuse is slow and counts the drops. A score is posted only when its
trace was forwarded (same sampling gate), and sits behind the same breaker.

## Prompt versions

Loom's system prompts are Python constants. `scripts/codegen/push_langfuse_prompts.py`
registers them in Langfuse and writes `server/observability/langfuse_prompts.lock.json`,
keyed by `sha256(prompt)[:12]`. At runtime `server/observability/genai.py` looks up the
prompt it sent in that lockfile and tags the generation with the Langfuse prompt name and
integer version, with no call to Langfuse. An edited prompt that was not pushed is left
unlinked rather than linked to the wrong version.

```bash
python scripts/codegen/push_langfuse_prompts.py          # plan against the lockfile, offline
python scripts/codegen/push_langfuse_prompts.py --write  # push, then write the lockfile
```

`--write` needs both Langfuse settings. A prompt whose text is already the latest version
is not pushed again.

`POST /v1/admin/prompts.evaluate` (platform admin) reads Langfuse traces tagged
`prompt:<name>:<version>` and returns a should-promote verdict from their rubric scores
against a minimum count and threshold. It never writes to Langfuse, and answers 501
`INTEGRATION_NOT_CONFIGURED` when Langfuse is not set up.

## Erasure

`erasure.request` runs the Langfuse backend (`LangfuseBackend` in
`server/erasure/app/backends.py`) as one step of its saga:

- **A user scope** finds the user's traces filtered by `userId` and the `org:<org>` tag,
  then deletes them in batches with `DELETE /api/public/traces`. Deleting a trace removes
  its observations and scores. The org filter matters because every org writes into the
  one Langfuse project the key pair implies, and a `user_id` is only unique within an org.
- **A namespace scope** deletes the traces carrying both `namespace:<ns>` and `org:<org>`.

The lookup depends on the server's major version, read from `GET /api/public/health`:
Langfuse 3 lists traces (`GET /api/public/traces`), and Langfuse 4, which no longer serves
that list, lists observations (`GET /api/public/v2/observations`) and collects their trace
ids. Langfuse 4 deletes asynchronously, so a deleted trace can stay visible for a few
minutes.

The step fails closed. A health answer with no version, a 5xx, a timeout, or a 404 from
the delete endpoint (an older self-hosted build, or a proxy that forwards only GET and
POST) raises, the saga records the request `failed`, and the `erasure_recovery` worker
retries it. A re-run is idempotent: the lookup finds nothing and issues no delete. With
Langfuse unconfigured, a stub backend logs the intent and reports 0.
`LOOM_EXTERNAL_ERASURE_TIMEOUT_S` (30) bounds each call. See
[retention and erasure](../memory/retention-and-erasure.md).

## Joining a request

The request id Loom returns in `X-Request-Id` is in the Langfuse trace metadata, in
`loom.agent_events.request_id` and on the `loom.request_id` span attribute, so a request
can be followed from Langfuse back into Loom's ledger. See
[joining a request across systems](./index.md#joining-a-request-across-systems).

## Related

- [Telemetry](../observability/telemetry.md): the ClickHouse side of the same spans.
- [ClickStack](./clickstack.md).
- [Configuration reference](../../reference/configuration.md#observability) for every `LOOM_LANGFUSE_*`
  setting.
