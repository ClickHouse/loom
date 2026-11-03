# LLM and embedding architecture

Loom has two pluggable providers: an embedder (`server/embedding/`) that turns text into
vectors, and an LLM client (`server/llm/`) that completes prompts. Each is chosen once
per process from settings. Each falls back to a deterministic fake when its key is
missing, so a keyless install boots and answers. Operator setup is in
[deployment](../operations/deployment.md); every setting is in the
[configuration reference](../reference/configuration.md).

## Goals and non-goals

| Goals                                                                                   | Non-goals                                                                           |
| --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Every correctness gate on the write path works without an LLM.                          | A general LLM gateway. Loom calls models for its own features only.                 |
| A keyless install boots and says clearly that it runs on fakes.                         | Provider-agnostic prompts. Prompts are tuned to a model and re-benched on a change. |
| Swapping the embedder does not silently miscalibrate recall.                            | Mixing vector spaces. Vectors from different models are never compared.             |
| Every provider call is attributed to the op or worker pass that made it, with its cost. | Runtime prompt management. Prompts are code.                                        |

## Embedders

### Selection

`server/embedding/embedder.py` (`provider_for`, `get_embedder`) picks the served embedder
once per process:

1. A label resolves first. `prefix:<dim>:<model>` and `ctx[<variant>]:<model>` are views
   over the served embedder and cannot be the served model.
2. `LOOM_EMBEDDING_PROVIDER` (`fake`, `local`, `openai`, `gemini`) when set; any other
   value raises.
3. Otherwise inferred from `LOOM_EMBEDDING_MODEL`: `fake-ngram*` is fake, `local` or
   `local:*` is local, `text-embedding-*` is OpenAI, `gemini*` is Gemini. Any other name
   raises `unknown embedding model`.

| Provider | Module                       | Model names                                                                | Needs                                    | Notes                                                                     |
| -------- | ---------------------------- | -------------------------------------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------- |
| `fake`   | `server/embedding/fake.py`   | `fake-ngram-<dim>`                                                         | nothing                                  | Hashed character trigrams, L2-normalised. Deterministic; the dev default. |
| `local`  | `server/embedding/local.py`  | `local` (`bge-small`), `local:bge-small`, `local:bge-base`, `local:minilm` | the `local-embedder` extra (`fastembed`) | CPU ONNX; 384, 768 and 384 dimensions. No network.                        |
| `openai` | `server/embedding/openai.py` | `text-embedding-3-small`, `text-embedding-3-large`                         | `OPENAI_API_KEY`                         | Requests `dimensions=LOOM_EMBEDDING_DIM`.                                 |
| `gemini` | `server/embedding/gemini.py` | `gemini-embedding-001`                                                     | `GEMINI_API_KEY` or `GOOGLE_API_KEY`     | Requests `outputDimensionality=LOOM_EMBEDDING_DIM` (up to 3072).          |

**Width.** The served width is `LOOM_EMBEDDING_DIM` (default 3072). OpenAI and Gemini are
asked for that width; choose a model that supports it. Local models emit their native width. Boot probes the real
width (`probe_embedding_dim`); a mismatch shows as `embedding_dim_mismatch` on
`/v1/health`.

**Image build.** The Docker image installs the local embedder only when built with a
`local*` `LOOM_EMBEDDING_MODEL` build argument.

### Protocol and paths

The `Embedder` protocol is `model_name`, `dim`, `async embed(text)` and
`async embed_many(texts)`.

| Path                 | Sends                      | Used by                                                                  |
| -------------------- | -------------------------- | ------------------------------------------------------------------------ |
| `embed` (query)      | Whitespace-normalised text | Search. Cached.                                                          |
| `embed_many` (write) | Raw text                   | Every stored vector: `memory.set`, extraction, ingest, recovery workers. |

- **Cache** (`server/embedding/_cache.py`). A query is served from the per-process LRU
  (`LOOM_EMBEDDING_CACHE_SIZE`, `LOOM_EMBEDDING_CACHE_TTL_S`), then an in-flight call for
  the same text, then `loom.embedding_cache` (texts up to
  `LOOM_EMBEDDING_CACHE_SHARED_MAX_CHARS`, matching `dim` only), then the provider. The
  key hashes the request's org, the model and the normalised text, so one org is never
  served another's vector, nor shares an in-flight call with it: a fast answer would tell
  it that another org embedded the same text. A call outside a request (a worker, a CLI,
  the startup probe) has no org to scope by and skips the cache: a worker's identity
  carries the org of the row it last worked on, which is no scope for the next text.
  Batch entries use a separate key over the raw text, so the paths never serve each
  other. A cache failure falls through to the provider.
- **Batches.** OpenAI and Gemini take up to 100 inputs per request; `memory.set` embeds
  value and key in one request.
- **Second vector legs.** `prefix:<dim>:<model>` (a Matryoshka prefix,
  `server/embedding/prefix.py`) and `ctx[<variant>]:<model>` (the served model over
  rewritten text, `server/embedding/alias.py`) are built only when listed in
  `LOOM_CODE_SEARCH_VECTOR_MODELS`. A secondary embedder never falls back to the fake; a
  missing key raises.

### Provider calls

OpenAI and Gemini share `server/embedding/_provider.py`:

| Concern      | Behaviour                                                                                                                                                                                                                                                                   |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Retry        | Transport errors and 408, 409, 425, 429, 500, 502, 503, 504; `LOOM_EMBEDDING_RETRY_ATTEMPTS` (3) attempts, exponential backoff (0.5 s base, `LOOM_EMBEDDING_RETRY_MAX_WAIT_S` cap, 10 s).                                                                                   |
| Input guard  | Inputs are token-counted first (8191 tokens for OpenAI, 2048 for Gemini); an oversize input raises instead of being truncated.                                                                                                                                              |
| Output guard | An empty vector, a width mismatch or a non-finite value raises `MalformedEmbedding`.                                                                                                                                                                                        |
| Timeouts     | The shared HTTP client's `LOOM_HTTP_CLIENT_TIMEOUT_S` (20 s; `LOOM_HTTP_CLIENT_CONNECT_TIMEOUT_S`, 5 s, to connect). The query path uses `LOOM_MEMORY_SEARCH_EMBED_BOUND_S` (2.5 s) per attempt and `LOOM_MEMORY_SEARCH_EMBED_BOUND_RETRIES` (1), then 503 `EMBED_TIMEOUT`. |
| Health       | Calls report to `/v1/health`. A cancellation is not a provider failure.                                                                                                                                                                                                     |

### Changing the embedder

Changing `LOOM_EMBEDDING_MODEL` or `LOOM_EMBEDDING_DIM` changes the vector space. Stored
vectors carry their `embedding_model`, and search never compares across models, so old
rows stop matching until they are re-embedded.

`loom reembed` (`server/memory/app/reembed.py`, `reembed_all`) rewrites every row whose
`embedding_model` differs from the served one in `memory_embeddings`, `entity_vectors`
and `conversation_embeddings`. It regenerates vectors from stored content and keeps every
other column. In `memory_embeddings` and `conversation_embeddings` the model is in the
sorting key, so old and new rows coexist until `--delete-old`. `entity_vectors`
(`ReplacingMergeTree(created_at)`) is replaced in place. Flags: `--org`, `--namespace`,
`--batch-size`, `--dry-run`, `--key-only`, `--resume`, and `--in-database <named
collection>`, which runs one `INSERT … SELECT` per table through ClickHouse's `aiEmbed`
(it needs a ClickHouse release that ships it, and is refused while a customer or identity
header is set or the gateway is required, since those calls name no one and skip the
gateway's check). See the [CLI reference](../reference/cli.md).

`embedding_worker` covers the rest. It embeds `embedding_dim = 0` stubs newest first; a
stub labelled with an older model is embedded under the served one and the old stub
tombstoned. A memory with no embedding row first gets a stub under the served model,
copying its `status`, `invalid_at` and attestation. The tracker pass re-embeds rows
another model wrote.

## Adaptive thresholds

A cosine of 0.4 means different things to different embedders. The search gates
(similarity floor, abstain, HyDE fallback, query rewrite, rerank skip) and the write gates
(conflict, duplicate) are cosine thresholds, so a model swap would move all of them.

With `LOOM_ADAPTIVE_THRESHOLDS` on (the default), `calibrate_embedder` in
`server/memory/app/embedder_calibration.py` derives them at boot. It embeds three built-in
corpora (paraphrase pairs, question-to-fact pairs, distractor pairs) and reads their
cosine distributions (`derive_thresholds`):

| Threshold     | Derived as                                 |
| ------------- | ------------------------------------------ |
| noise floor   | distractor p90 + 0.02                      |
| abstain       | noise floor + 0.05                         |
| query rewrite | max(question-fact p10, noise floor + 0.01) |
| HyDE fallback | max(question-fact p25, query rewrite)      |
| rerank skip   | max(question-fact p75, HyDE fallback)      |
| conflict      | paraphrase p50                             |
| duplicate     | paraphrase p75                             |

Each search resolves thresholds in three tiers:

1. a per-namespace calibration (`loom.namespace_calibration_result`, written by
   `code.calibrate`);
2. the cluster calibration (`loom.embedder_calibration`, one row per embedder model);
3. the scalar settings (`LOOM_MEMORY_SEARCH_MIN_SIMILARITY`,
   `LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD`, `LOOM_MEMORY_SEARCH_HYDE_FALLBACK_THRESHOLD`,
   `LOOM_MEMORY_SEARCH_QUERY_REWRITE_THRESHOLD`, `LOOM_LLM_RERANK_SKIP_ABOVE`,
   `LOOM_CONFLICT_THRESHOLD`).

`calibration.get` and `/v1/health` (`adaptive_thresholds`) show what is in force;
`adaptive_thresholds.calibration_hash` is equal on every replica serving the same gates.

- Every replica serves one calibration. At boot a replica adopts the
  `loom.embedder_calibration` row of its embedder and probes only when there is none
  (the first replica to boot publishes its probe). Each replica re-reads the row every
  `LOOM_CALIBRATION_REFRESH_SECONDS` (60) and adopts a changed one; a replica whose boot
  probe failed probes again on that tick. Per-namespace rows are cached for 60 s, so a
  `code.calibrate` run on one replica serves on all of them within a minute.
- Fake embedders skip calibration and use the scalar gates. So does a failed probe; it
  never fails boot.
- With adaptivity off and a real embedder other than `text-embedding-3-small` (the model
  the scalar gates were tuned for), boot logs a warning. You own re-benching.
- The `recalibration` worker (weekly, `LOOM_RECALIBRATION_ENABLED`, on the leader)
  re-runs the probe, publishes the result as the cluster calibration,
  warns on a drift of 0.05 or more, and re-probes namespace corpora with at least 5
  similar and 3 distractor pairs.

**Why.** Deriving thresholds from the model's own distribution makes an embedder swap a
configuration change. The cost: thresholds are statistical. A namespace corpus whose
"similar" pairs are revisions, not paraphrases, sets the conflict and duplicate gates too
low.

## LLM clients

### Selection

`resolve_llm_provider` in `server/core/llm_provider.py` picks the provider:

1. An explicit `LOOM_LLM_PROVIDER` is never overridden.
2. Else from `LOOM_LLM_MODEL`: `claude*` is `anthropic`; `gpt*`, `o1*`, `o3*`, `o4*`,
   `chatgpt*` are `openai`; `fake*` is `fake`.
3. Else from the key present: `OPENAI_API_KEY` gives `openai` with `gpt-6-luna`, then
   `ANTHROPIC_API_KEY` gives `anthropic` with `claude-haiku-4-5`.
4. Else `fake` (`fake-echo-1`).

`Settings` resolves it once at load; `settings.llm_provider` is always concrete, and
`llm_provider_inferred()` / `llm_provider_source()` say how. Boot logs one INFO line for
an inferred provider. `loom doctor`'s `llm_provider` line warns when the key is missing.
The stock `.env` pins `LOOM_LLM_PROVIDER=fake`.

| Provider    | Module                    | Key                 |
| ----------- | ------------------------- | ------------------- |
| `openai`    | `server/llm/openai.py`    | `OPENAI_API_KEY`    |
| `anthropic` | `server/llm/anthropic.py` | `ANTHROPIC_API_KEY` |
| `fake`      | `server/llm/fake.py`      | none                |

Per-stage models (`LOOM_MEMORY_ASK_SYNTH_MODEL`, `LOOM_CODE_SUMMARY_MODEL` and the other
`*_model` settings) build their own client with `get_llm_client_for_model`, inferring
the provider from the name.

### Protocol

`LLMClient` has `provider`, `model` and one method:

```python
async def complete(system, user, max_tokens=1024, response_format=None) -> LLMResult
```

`LLMResult` carries the text, input and output tokens, cached-input and cache-write
tokens, the answering provider and model, and `from_cache`. There is no tool-use path.

**Structured output.** Callers pass `response_format={"type": "json_object"}`. The OpenAI
client passes it through. The Anthropic client has no JSON mode: it appends a JSON
directive to the system prompt, strips code fences and marks the system block cacheable.
The caller parses (`server/llm/json_text.py`).

**Request shape per model, learned.** No model name decides what a request carries.
Each client builds the request it would like (temperature, the lowest reasoning effort,
an output cap), and `server/llm/traits.py` learns from the provider's answers, once per
model per process:

- a 400 naming a field the model does not take (`temperature`, `reasoning_effort`, an
  `output_config`, the cap field under its other name) drops that field: the call is
  re-sent without it, and later calls leave it out;
- a 400 refusing a reasoning effort settles on the lowest one it lists
  (`LOOM_LLM_OPENAI_REASONING_EFFORT` pins one instead);
- a 400 naming the model's output limit clamps caps to it;
- reasoning and thinking tokens count against the output cap, so the tokens a reply
  reports become the model's reserve (twice the most seen, at most
  `LOOM_LLM_HIDDEN_REASONING_RESERVE_MAX`), added to every caller's cap. A reply that hit
  the cap with no visible text is re-sent once with the ceiling as its reserve.

A new model therefore needs no code change; its first call or two teach the process
what it takes. `LOOM_LLM_ANTHROPIC_EFFORT` sends a thinking effort to Claude models.

### Retries, timeouts, fallback

- **Retry** (`server/llm/_retry.py`): transport errors and 429, 500, 502, 503, 504, 529;
  `LOOM_LLM_RETRY_ATTEMPTS` (4) attempts, exponential backoff (0.5 s base,
  `LOOM_LLM_RETRY_MAX_WAIT_S` cap, 20 s). The request timeout,
  `LOOM_LLM_REQUEST_TIMEOUT_S`, is 300 s: it only catches a hung connection, so a slow
  model or a long output never hits it. Callers that need less wrap the call (HyDE on
  `memory.search` waits `LOOM_MEMORY_SEARCH_HYDE_TIMEOUT_S`, 6 s).
- **Fallback model.** `LOOM_LLM_FALLBACK_MODEL` wraps the client in `FallbackLLMClient`
  (`server/llm/fallback.py`). It hands the call to the second model on an outage
  (transport error, retryable status, timeout) or a rejected request (400, 413, 422);
  401, 403 and 404 propagate. Each hand-over is logged and counted (`loom.llm.fallback`),
  and the result names the model that answered. Fallbacks do not chain.
- **Missing key.** The client is the fake (`_build_llm_client`), logged at WARNING.
- **One identity refused.** With identity headers set, an answer from a gateway host that
  `LOOM_INFERENCE_IDENTITY_REFUSALS_JSON` lists is the gateway refusing the call's identity or
  asking it to wait ([identity headers](#identity-headers)): never retried, never handed to the
  fallback model.

### Response cache

`CachedLLMClient` (`server/llm/cache.py`) caches completions in `loom.llm_response_cache`
(7-day TTL). The key is per model, over the org the client serves, the system prompt, user
prompt, `max_tokens`, `response_format` and extra arguments, so one org is never served
another's answer; a client built with no org calls the provider. Only a non-empty answer
from the requested model is stored, so a fallback answer is never cached. Each row records
the memory keys whose text the prompt carried (`window_keys`), so erasure and redaction can
delete it by key.

| User         | Switch                                                                                                    |
| ------------ | --------------------------------------------------------------------------------------------------------- |
| `memory.ask` | `LOOM_MEMORY_ASK_LLM_CACHE_DEFAULT` (on), `LOOM_MEMORY_ASK_LLM_CACHE_NAMESPACES`, per-request `llm_cache` |
| `code.ask`   | `LOOM_CODE_ASK_LLM_CACHE` (on); a non-zero temperature bypasses it                                        |
| Extraction   | Off, except where `LOOM_LLM_POLICY_BY_TRAFFIC_CLASS` enables it (the `bench` class by default)            |

## The customer of a model call

With `LOOM_INFERENCE_CUSTOMER_HEADER` set, every request to a model (chat, embedding, rerank,
classify) carries the org whose data it holds, and with `LOOM_INFERENCE_FEATURE_HEADER` the
feature that made it (`server/llm/customer.py`). The senders add the headers themselves:
the shared HTTP client also carries non-model calls, which name no customer.

- **Where the org comes from.** A request: the identity the transport records after the
  token is verified (`observability.genai`), which is the org the op addresses. A worker or
  a CLI: `customer.for_org(org)` around the call, from the rows it works on; a CLI command
  with `--org` is scoped to it. A worker's own identity never counts, since a pass stamps the
  org of the row it last worked on and the next row's call must not inherit it. The
  deployment's own calls (the startup embedder probe, the recalibration worker's fixed
  pairs, the provider checks) run under `customer.system()` and name
  `LOOM_INFERENCE_SYSTEM_CUSTOMER`. The sampled retrieval judge sends the judged org's query
  and memory text, so it names that org. `code.refresh` runs the poller's ingest pass, and
  its calls stay the caller's (its token's subject) where the poller's are the namespace's.
- **One customer per call.** A job that batches several orgs' rows (the embedding worker,
  `loom reembed`, a model rebuild) splits its batches by org before calling.
- **The gateway's names.** `LOOM_INFERENCE_CUSTOMER_FORMAT` turns an org id into the gateway's
  customer id, and `LOOM_INFERENCE_FEATURE_MAP_JSON` maps Loom's feature names to the gateway's.
  A cell that serves several customers sets `LOOM_INFERENCE_CUSTOMER_REQUIRED`, so it does not
  start without the header.
- **No customer, no call.** A call that names none raises `Unattributed`
  (`MODEL_CALL_UNATTRIBUTED`) before anything is sent. A missed call path fails loudly
  instead of being billed to no one. The embedding workers (memory and code) check first and
  give up on a row that names no org at once, recording that error with no model call, since a
  retry cannot help and the refusal is not a provider failure.

- **An org switched off makes no call.** `org.set_model_calls` writes `loom.org_model_calls`;
  `customer.require()`, first in every sender (before the gateway token, the embedding caches
  and provider health), checks the call's org against each process's copy of the switched-off
  orgs (`customer.switched_off_orgs`). A cached vector or answer is refused too: the switch is
  a kill switch, so nothing model-made reaches the org. The copy is read at most every
  `LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS` and trusted no longer than
  `LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS`, past which every model call for an org is
  refused (`customer.SwitchUnread`). A job treats a SwitchUnread refusal as it treats a
  switch-off (`customer.HOLDS`): its rows stay as they are with no attempt spent, a rebuild
  step runs again, and a fail-open reranker, classifier or judge lets it through. A call made
  during a read waits for it when the copy is too old or a change made in this process has
  not been read yet; otherwise it is served the copy in hand. Every job leaves a
  switched-off org out when it selects work (in the query, so its rows neither spend attempts
  nor fill the batch ahead of other orgs); code ingest skips its repositories, freezing its
  whole code index. A conflict mark older than the sweep's hour when the org comes back is past
  the sweep, as a judge lost in an outage is: the write stands as made. The check runs in
  `require()`, before provider-health recording, so a refusal is never a provider failure. With
  no customer or identity header a job's batch of several orgs is one call, unless an org in it
  is switched off: `customer.groups` then splits it by org, so the switch refuses that org's
  call and the others go on.

**Why headers, not a token per customer.** The gateway already knows Loom as one service
(its token); a credential per org would mean thousands to issue, rotate and revoke for no
gain in what the gateway can check. The org in the header is the org the request addresses,
as authorized for its token.

### Identity headers

`LOOM_INFERENCE_IDENTITY_HEADERS_JSON` names more of whom a call is for, for a gateway that
bills or limits each agent or person (`server/llm/identity.py`). It maps header names to
templates; `customer.require()` renders them from the call's attribution, and the senders add
them beside the customer header on every chat, embedding, rerank and classify request to the
gateway.

| Placeholder   | A request                                                                       | A job (`customer.for_org`)                                    |
| ------------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `{org}`       | the org it addresses                                                            | the row's org                                                 |
| `{namespace}` | the namespace the transport recorded                                            | the row's namespace                                           |
| `{subject}`   | the token's subject                                                             | the row's writer, else the agent whose own namespace it is in |
| `{agent}`     | `<agent>` of an agent's subject, `sa:<trust>:<agent>`                           | the same, of that subject                                     |
| `{tenant}`    | its trust's one required value of the claim `LOOM_INFERENCE_TENANT_CLAIM` names | the same                                                      |
| `{email}`     | a person's address: the subject, or its verified directory spelling             | the same                                                      |
| `{user_id}`   | the request's `user_id`, else the subject                                       | the row's `user_id`, else the subject                         |

- **Empty is left out.** A header whose template renders empty is not sent, so a bare
  placeholder is left out when its value is unknown; text around a placeholder is sent as
  written. A value is percent-encoded only where a header cannot carry it (a space, `%`,
  anything outside printable ASCII). A prefix claim (`tenant_id=acme-*`) names no tenant.
- **The agent goes whole.** The headers whose templates name `{agent}` or `{tenant}` are sent
  together or not at all: when any placeholder of theirs renders empty (an agent whose trust
  requires no exact tenant, a trust's own token), none of them goes, and the gateway applies
  its default identity rather than refusing half of one. The other headers stand alone.
- **No org, no call.** With identity headers set, a call that names no org is refused
  (`MODEL_CALL_UNATTRIBUTED`, logged), as it is with the customer header, unless it is the
  deployment's own (`customer.system()`): a `loom` command run without `--org` or a request
  whose identity was not recorded never goes out as the gateway's default. A `loom` command
  names its `--namespace` too, so its calls are for the agent whose namespace it is; while
  the headers name the agent, a command's call with `--org` and no namespace is refused the
  same way rather than billed to the gateway's default agent.
- **Writer first.** A job's call is for the principal that wrote the row: the subject in its
  `actor` (`human:` or `process:` and a token's subject). A pipeline's own actor
  (`process:extraction-worker`, `process:cli:<command>`, `provenance.PIPELINE_ACTORS`) is no
  principal. A deferred `memory.set_from_messages` stores its enqueuer on the queue row; the
  entity extractor, the embedding worker, projection repair and the code-link judge read the
  writer of the memory they work on. Observe-extract, profile folds and structured profiles
  take the newest principal writer among the observations or facts eligible for their prompt.
  Version orders these writes, so an update counts as a new write. Reflection and the period
  digest take the newest writer of the session's memories (`memory_repo.session_writers`);
  the conflict resolver takes the current row's writer. Only a row that
  records no principal (a conversation turn, a shared-scope vector) takes the agent whose own
  namespace it is in (owned by `sa:<trust>:<agent>`, the home its trust made on the agent's
  first token exchange). Only the deployment's own calls (`customer.system()`) name no one.
- **One identity per call.** A job that batches rows (the embedding worker, `loom reembed`, a
  model rebuild) groups them by the identity they render (`customer.groups`), so one call never
  carries two agents' texts, and one agent's rows in several namespaces share a call.
- **Reads.** A trust's claims (`loom.machine_identities`), a namespace's owner and an account's email are read at
  most once a minute per process. A failed read serves the copy in hand; with none the call is
  held (`identity.IdentityUnread`, one of `customer.HOLDS`): it waits rather than go out as
  someone else's, and a job leaves that work for 30 seconds, no attempt spent.
- **Only to the gateway.** The identity headers go only to a host
  `LOOM_INFERENCE_GATEWAY_HOSTS` lists, so setting them requires it. The model client's request
  hook (`server/clients/_http.py`) drops them from any other request, a redirect hop included:
  an operator's reranker, a namespace's classifier, Cohere or a provider elsewhere never learns
  whom a call is for. The reranker and classifier post through the model client too.
- **One identity refused.** An answer from a gateway host that
  `LOOM_INFERENCE_IDENTITY_REFUSALS_JSON` lists is the gateway refusing that identity, not a
  provider failure. Each entry names a status and at least one of the error body's `code`,
  `type` or `message` (under `error`), each matched exactly; no word in a message counts. The
  default matches by code an agent over its budget (`budget_exceeded`), denied
  (`agent_denied`) or named incompletely (`identity_incomplete`, `identity_malformed`), and
  waits on `agent_request_limit`; the metering's Anthropic-shaped budget answer carries no
  code, so it is matched by its type and its whole message. An answer the gateway also gives
  for its own failure (`token_unavailable`, `identity_missing`) is left out, so health still
  sees an outage or a misconfiguration. The model client's response hook (`gateway.answered`)
  raises `ModelCallRefused` for an entry under `refused`, so the request answers 403
  `MODEL_CALL_REFUSED` with the gateway's `gateway_status` and `gateway_code`, and
  `ModelCallDeferred` for one under `wait`, answered 503 `MODEL_CALL_DEFERRED` with the
  gateway's `Retry-After`. A request's call asked to wait two seconds or less waits it out once
  and the shared model client sends it again before it is deferred, including after a redirect.
  Neither answer is retried further or handed to the fallback model, `/v1/health` reads
  nothing from either, and the org's switch is untouched. Both are `customer.HOLDS`, so a job
  preserves the work without spending an attempt.
- **A job's hold.** After that answer, a job's calls (`customer.for_org`) for the identity to
  the same origin, path and model are held: refused by the request hook (`gateway.sending`) with
  nothing sent, for `LOOM_INFERENCE_REFUSAL_COOLDOWN_SECONDS` after a refusal and the answer's
  `Retry-After` (at most 5 minutes) after a wait. Its calls to another model or to the
  embeddings go on, and so do other identities'. The customer header is part of the identity. A success at that path and model, from a
  request or a job, ends the hold. A request always asks the gateway, which may take the
  identity back at any time. Any other answer is the provider's, as before: another 403 is a
  provider failure, never retried, and another 429 a rate limit, retried.
- **Skip, not stall.** A job leaves held work out of its next selections while the hold
  lasts (`server/workers/holds.py`), so its batch goes to other identities' work instead of
  filling again with work that would only be held. The extraction queue marks the request
  as `held` with the reason until the hold ends, storing a `running` lease that older
  workers can reclaim after rollback. It claims none of that enqueuer's other
  requests in the namespace meanwhile (by its `enqueued_by` column). The profile fold reads
  four times its slots, gives a held identity's slot to the next one read, and leaves it out
  of its scan; the structured profile worker leaves a held user out until the hold ends.
  The embedding worker records each held stub's `embed_retry_at` without
  spending an attempt. Projection repair excludes held failure IDs before its limit.
  Embedding selection excludes held rows before either limit, including
  when a healthy writer uses the same namespace and agent label. The batch embedders (the
  embedding workers, `loom reembed`, a model rebuild) hold one group at a time (`customer.GROUP_HOLDS`: an org switched off, an
  identity refused, asked to wait or unread), and write the vectors the other groups' calls
  bought. A model rebuild stages those healthy vectors but retains the page's cursor and
  counters until every held row can be rebuilt. Its saved retry deadline survives a worker
  change and prevents provider calls before the hold ends. Verification still rejects a
  lost source vector before publication.

**Why the namespace's owner.** Some rows a job works on record no principal (a conversation
turn, a shared-scope vector), and an agent's token has its own namespace as its home, so the
namespace names the agent the work is for. A row of a shared namespace that records no writer
goes out with the headers that rendered and no agent.

**Why a table of answers.** A gateway's refusal of one identity and its own failure can share a
status (a 403 for an agent not allowed, and for Loom's credential not allowed to act for agents),
and a word in a message is no contract. The table lists the exact answers a gateway gives for
one identity, by their stable code wherever the gateway gives one, and only those hold the
identity rather than read as the provider down.

## Fake providers

The fakes let a keyless install boot, tests run offline and CI stay deterministic. Do not
serve from them. Every fallback is visible:

- It is logged (ERROR for the embedder, WARNING for the LLM).
- `/v1/health` and the dashboard show a `problems` entry: `fake_embedder`,
  `embedder_key_missing`, `fake_llm`, `llm_key_missing`, or `provider_failing:<name>`
  when a real provider's last call failed (`server/observability/problems.py`).
- With `LOOM_REQUIRE_REAL_PROVIDERS=true` the fake entries are errors and health reads
  `degraded`. The HTTP status stays 200; only a ClickHouse outage is 503.

LLM features on the fake answer from a deterministic echo; some skip themselves (the
conversation titler).

## LLM-driven and deterministic parts

Every gate that decides what is stored or who may read it is deterministic:
authorization, the blacklist, sensitivity detection, conflict and duplicate verdicts,
ranking and the abstain floor. LLM stages produce or enrich content (facts, answers,
summaries, titles). Each is optional, bounded, and fails open to the deterministic path.

**Why.** An LLM-decided gate cannot be reproduced, audited or unit-tested, and it adds
cost and latency to every write. The deterministic gates run on the fake provider in CI
through the production code path. The cost: a cosine threshold cannot tell a revision
from a contradiction, which is why the conflict resolver exists. It judges after the
write, on the leader, so the write's own verdict stays deterministic.

| Feature                                       | LLM?          | Switch (default)                                                                                                                                                                                                                                                                                             |
| --------------------------------------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `memory.set`, `memory.search` core            | No            | —                                                                                                                                                                                                                                                                                                            |
| Conflict and duplicate verdicts               | No, cosine    | `LOOM_CONFLICT_THRESHOLD`                                                                                                                                                                                                                                                                                    |
| Conflict resolver (after the write)           | Yes           | `conflict_resolver` worker, `LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED` (on unless the LLM provider or the resolver model's provider is fake); `LOOM_CONFLICT_RESOLVER_MODEL` (`gpt-6.1-sol`)                                                                                                                    |
| `memory.set_from_messages` extraction         | Yes           | Always; deferred runs through the `extraction_queue` worker                                                                                                                                                                                                                                                  |
| Extraction verifier (coding captures)         | Yes           | `LOOM_MEMORY_EXTRACT_VERIFY_MODELS` (`claude-opus-5-5`; `""` turns it off); fails open                                                                                                                                                                                                                       |
| `memory.ask` synthesis                        | Yes           | `LOOM_MEMORY_ASK_SYNTH_MODEL`, `LOOM_MEMORY_ASK_SYNTH_MODEL_CHEAP`, `LOOM_MEMORY_ASK_MID_MODEL`                                                                                                                                                                                                              |
| `memory.search` HyDE and fallback helpers     | Yes, bounded  | Per-request `hyde`; `LOOM_MEMORY_SEARCH_FALLBACK_MAX_LLM_CALLS` (2); [none](../features/memory/search-and-ask.md#modes-and-recipes) under `retrieval_budget: "fast"`, `recipe: "fast"` or in `context.pack`                                                                                                  |
| `memory.search` rerank                        | Yes, opt-in   | Namespace `rerank_endpoint` or per-request `rerank` (`builtin:openai[:model]`, `builtin:cohere[:model]`, a `LOOM_RERANK_ENDPOINTS_JSON` name); `builtin:openai` makes [none](../features/memory/search-and-ask.md#modes-and-recipes) under `retrieval_budget: "fast"`, `recipe: "fast"` or in `context.pack` |
| Entity extraction                             | Yes           | `entity_extractor` worker, `LOOM_ENTITY_EXTRACT_ENABLED` (on)                                                                                                                                                                                                                                                |
| Conversation titles                           | Yes           | `conversation_titler` worker (on; skips on the fake LLM)                                                                                                                                                                                                                                                     |
| Observation mining, reflection, profile folds | Yes           | `observe_extract`, `reflection_distill`, `profile_consolidation` workers (off)                                                                                                                                                                                                                               |
| Structured profiles                           | Yes           | `structured_profile` worker (on; only for a namespace with a profile schema), `LOOM_STRUCTURED_PROFILE_MODEL`                                                                                                                                                                                                |
| Code summaries                                | Yes           | `code_summary` worker (off), `LOOM_CODE_SUMMARY_MODEL`                                                                                                                                                                                                                                                       |
| `code.ask`                                    | Yes           | `LOOM_CODE_ASK_LLM_MODEL`                                                                                                                                                                                                                                                                                    |
| Session briefs (`session.brief_save`)         | Yes, optional | `LOOM_SESSION_BRIEF_LLM` (on), `LOOM_SESSION_BRIEF_LLM_MODEL`                                                                                                                                                                                                                                                |
| Retrieval-quality judge                       | Yes, sampled  | `LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE` (0)                                                                                                                                                                                                                                                                       |
| Embedding recovery, calibration               | No            | —                                                                                                                                                                                                                                                                                                            |

Whether an agent calls `memory.set` at all is the client model's decision. The MCP tool
descriptions (`server/transport/mcp_descriptions.py`) describe intent, not a syntax.

### LLM-free reads

`LOOM_READS_LLM_FREE=true` makes every read op (`Operation.read_only`) run with no LLM
call, so a deployment can promise that reads never reach a model. The HTTP and MCP transports mark the
request while the handler runs (`server/core/llm_free.py`); the memory search reads the mark through the
same gate as the fast posture and `context.pack` (`_is_llm_free`), and every other read step that could
call a model reads `reads_llm_free()`:

- `memory.search`, `search.query` (the MCP `loom_search` tool), `context.pack` and every read built on
  them: no HyDE, graph triples, retry judge, LLM count intent or verb synonyms. The
  spelling-fix and set-expansion embeddings are skipped, so a plain search or pack embeds its query once.
- `code.search`: no HyDE, model-named concept expansion or query expansion; its memo keeps LLM-free
  answers apart.
- Reranking: a reranker is a model call, and an LLM-free read makes only the ones it names. A
  namespace's `rerank_endpoint` and code search's default (`LOOM_CODE_SEARCH_RERANK_DEFAULT`) do not
  run; a reranker the request names runs, except `builtin:openai`, which keeps the fused order.
- `tracker.duplicate_check` and `tracker.answer`: no rerank or uncertainty judge; `code.bisect_candidates`:
  no rerank. Each keeps the deterministic answer it already falls back to.
- `memory.ask` and `code.ask` answer 409 `READS_LLM_FREE` naming the setting unless the request
  sets `allow_synthesis=false` / `synthesize=false`, even when a deterministic route could answer it;
  with that flag they return the recalled evidence with no LLM call.
  A model's answer is the purpose of those calls, so the setting refuses it rather than answering
  without one. `memory.brief` asks for the evidence alone and returns it with its unknowns, no answer.
- The sampled retrieval-quality judge queues nothing: both transports record a search's quality inside
  the same mark.

Writes (`memory.set_from_messages` extraction, the conflict judge, `knowledge.synthesize`, session briefs)
and the background workers still call models.

## Prompts

Prompts are Python constants, versioned with the code:

| Prompt family                                                                  | Where                                                                                                                         |
| ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| Fact extraction, judge, retry addendum                                         | `server/memory/domain/extraction_prompts.py`; assembled in `server/memory/app/extract/prompts.py`                             |
| Search helpers (HyDE, iterative judge, derived intent, triples, verb synonyms) | `server/memory/domain/search_prompts.py`                                                                                      |
| Rerank                                                                         | `server/memory/app/rerank.py`                                                                                                 |
| `memory.ask` synthesis                                                         | `server/memory/app/ask/prompts.py`                                                                                            |
| Worker prompts                                                                 | The worker modules (`code_summary`, `conversation_titler`, `profile_consolidation`, `structured_profile`, `entity_extractor`) |

Which model runs each job is set in one place, the `Models` section of `server/settings/catalog.py`
([models per job](../operations/deployment.md#models-per-job)). Recalled memory
inside a prompt is fenced as data ([security](security.md#prompt-safety)).

**Extraction output repair** (`extract_from_messages` in
`server/memory/app/extract/extract.py`):

- a parse failure that looks like truncation retries with double the token budget (up to
  8192), then keeps the complete fact objects;
- any other parse failure retries once with a stricter addendum;
- a non-trivial input that yields no facts gets one more retry.

**Langfuse.** `scripts/codegen/push_langfuse_prompts.py --write` (with
`LOOM_LANGFUSE_URL` and `LOOM_LANGFUSE_ADMIN_TOKEN`) publishes the prompt constants under
fixed names and records each prompt's text hash and version in
`server/observability/langfuse_prompts.lock.json`. Nothing fetches prompts at run time.
The lockfile lets generation spans carry the prompt's name, version and hash
(`server/observability/genai.py`).

**Why prompts are code.** A prompt change is a behaviour change measured by the
benchmarks. In the diff, it is reviewed, benched and rolled back with the code that
parses its output. Editing a prompt needs a deploy.

## Cost accounting

Every provider call reports usage to a per-op accumulator
(`server/observability/cost_accumulator.py`). Both transports bind it per request; the
worker supervisor binds it per pass. Prices come from `PRICE_PER_1K_TOKENS` in the same
module, with cache-read and cache-write multipliers. An unlisted model bills $0 and logs
one warning. Gemini embeddings report no usage and bill $0.

The op's `loom.agent_events` row carries `upstream_cost_usd`, `embedding_tokens`,
`input_tokens`, `output_tokens`, `llm_calls` and the dominant model (`emit_agent_event`
in `server/observability/ledger.py`). Cache savings appear per answer
(`llm_cache_saved_usd` on `memory.ask`) and in the event's `args`
(`out_llm_cache_saved_usd`). See [cost and usage](../features/observability/cost-and-usage.md).

## Changing a default

The dev defaults are the fakes. Production sets `LOOM_EMBEDDING_MODEL` and
`LOOM_LLM_MODEL` explicitly; the commented provider block in `.env.example` uses
`text-embedding-3-small` at 1536 dimensions and `gpt-6-luna`.

**Run `make bench-quick` before changing either default** or opting into the Anthropic
provider. It runs a 30-question sample of the long-memory benchmark (`bench/longmemeval.py`)
against a running server with an answer judge and writes a CSV under `/tmp`. It needs
real providers. See [running benchmarks](../benchmarks/running.md).

## Invariants

- A process serves one embedder and one default LLM client, chosen at startup.
- Vectors of different `embedding_model` values are never compared.
- The served embedder falls back to the fake only when its key is missing, and says so on
  `/v1/health`. A secondary embedder never falls back.
- A search always has a threshold: namespace calibration, else boot calibration, else the
  scalar settings.
- No gate that decides storage or access calls an LLM.
- A fallback model's answer is never cached.
- Every provider call's usage lands on the `agent_events` row of the op or pass that made
  it.

## Source entry points

| What                      | Where                                                                                                                         |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Embedder selection        | `server/embedding/embedder.py` (`provider_for`, `get_embedder`, `probe_embedding_dim`)                                        |
| Embedding cache           | `server/embedding/_cache.py`                                                                                                  |
| Provider retry and guards | `server/embedding/_provider.py`, `server/llm/_retry.py`                                                                       |
| LLM selection             | `server/core/llm_provider.py` (`resolve_llm_provider`), `server/llm/client.py` (`get_llm_client`, `get_llm_client_for_model`) |
| Fallback model            | `server/llm/fallback.py` (`FallbackLLMClient`)                                                                                |
| Response cache            | `server/llm/cache.py` (`CachedLLMClient`)                                                                                     |
| Calibration               | `server/memory/app/embedder_calibration.py`, `server/memory/workers/recalibration.py`                                         |
| Re-embed                  | `server/memory/app/reembed.py` (`reembed_all`)                                                                                |
| Prompts                   | `server/memory/domain/extraction_prompts.py`, `server/memory/domain/search_prompts.py`                                        |
| Prompt publishing         | `scripts/codegen/push_langfuse_prompts.py`                                                                                    |
| Cost                      | `server/observability/cost_accumulator.py`, `server/observability/ledger.py`                                                  |
| Provider problems         | `server/observability/problems.py`                                                                                            |

## Related

- [Extraction](../features/memory/extraction.md): what the extractor produces.
- [Retrieval and ranking](../concepts/retrieval-and-ranking.md): where the thresholds act.
- [Langfuse](../features/integrations/langfuse.md): traces and prompt links.
- [Benchmarks](../benchmarks/overview.md): how changes are measured.
