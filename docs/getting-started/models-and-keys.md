# Configure models and API keys

Your coding agent, Loom's internal LLM and Loom's embedding model are separate choices.
You can connect Claude Code to a Loom server using OpenAI models, or Codex to a server
using Claude. The agent does not need to use the same provider as Loom.

| Choice                                           | What it does                                                           | Where to configure it                                      |
| ------------------------------------------------ | ---------------------------------------------------------------------- | ---------------------------------------------------------- |
| Your agent: Claude Code, Codex or another client | Runs your coding or assistant workflow and calls Loom's tools.         | In that client; connect it with a Loom token.              |
| Loom's LLM                                       | Extracts facts, summarizes sessions and generates answers inside Loom. | The server's provider, model and API key settings.         |
| Loom's embedding model                           | Makes memories, documents and code searchable by meaning.              | The server's embedding model, dimensions and provider key. |

`LOOM_API_KEY` is a token for accessing **Loom**. It is different from
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `GEMINI_API_KEY`, which pay for the server's
model calls. Keep provider keys on the server; connecting an agent does not transfer its
subscription credentials or API keys to Loom.

## What works without provider keys?

The released Docker installation embeds with `local` (bge-small, 384 dimensions, on CPU
from files the image carries) and selects the `fake-echo-1` LLM. It boots without provider
keys or network: storage, the dashboard, structural code indexing and semantic search
work, though a hosted embedder retrieves better on paraphrase-heavy questions. The fake
LLM does not produce meaningful extracted facts, summaries or generated answers. A
checkout's `.env` selects `fake-ngram-3072` embeddings, which match on character overlap.
An upgraded installation keeps the embedder its `~/.loom/.env` names; switching it to
`local` is a model change like any other, and re-embeds what is stored.

For stronger retrieval over paraphrase-heavy content, configure a hosted embedding
provider. For automatic fact extraction, summaries and generated answers, configure an
LLM too. An LLM alone does not replace an embedding model; an embedding model alone does
not generate answers. With no embedding configured, a checkout's server uses its fake
embedder; it does not skip embedding.

Some hosted providers also fall back to fakes when their key is missing. Check
`fake_embedding`, `fake_llm`, `providers_degraded` and `problems` in `/v1/health`, then
save and retrieve a test fact and try an answer. An invalid key or inaccessible model can
still cause provider calls to fail; a running server is not proof of working model access.
Use `LOOM_REQUIRE_REAL_PROVIDERS=true` in environment-based deployments to make fake or
keyless providers a degraded-health condition.

## Choose a setup

| What your organization has                    | Loom LLM                  | Embeddings                                             |
| --------------------------------------------- | ------------------------- | ------------------------------------------------------ |
| OpenAI API access                             | An available OpenAI model | OpenAI; one API key can serve both roles.              |
| Anthropic plus an approved embedding provider | An available Claude model | OpenAI, Gemini or a compatible endpoint, with its key. |
| Anthropic API access only                     | An available Claude model | Add an embedding provider and its API key.             |

**One OpenAI API key is enough for both roles; an Anthropic key is not required.**
In Models & keys, choose one main LLM and leave per-job overrides blank. All enabled
LLM jobs use that model, including verification and judging. Advanced overrides are
optional and require another provider's key only when you explicitly select one.

Choose OpenAI, Google Gemini or an **OpenAI-compatible embedding endpoint**. OpenAI embeddings use
an embedding model such as `text-embedding-3-large`, not a GPT chat model.
Claude can handle Loom's LLM jobs, but requires a separate embedding provider and key.
Anthropic does not provide its own embedding model; Voyage is not a built-in Loom provider.
[Anthropic's embedding guidance](https://platform.claude.com/docs/en/build-with-claude/embeddings)

Loom's internal model jobs use provider API keys, not coding-agent CLI logins or
subscription credentials. Your external agent's choice of provider is independent.

## Set the server configuration

### Recommended: use the dashboard

As a platform administrator, open **Settings → Models & keys** (also available during Setup).
Choose your LLM and embedding provider, enter their model IDs and API keys, then click
**Test & apply**. Leave per-job overrides blank to use one LLM for every enabled job.
Saved dashboard settings override the environment and are encrypted in ClickHouse.
**No Docker restart or Compose command is needed.** Key and LLM updates apply live;
embedding changes start the rebuild described below. **Use environment
configuration** removes those overrides and restores this server's startup settings;
it does not reload an edited `.env` file. Changing the embedding space requires a rebuild.

The dashboard's embedding choices are hosted providers. To keep the released stack's
`local` embedder and add a real LLM, set only the LLM lines in `~/.loom/.env`
([Claude for the LLM](#claude-for-the-llm) shows them) and recreate the container: the
embedding space is unchanged, so nothing is re-embedded.

For dashboard embedding changes, Loom queues that rebuild automatically and keeps
data-plane requests paused until verification, publication and cleanup finish.
Models & keys shows the phase and progress; a failed job stays paused and offers
retry from its saved cursor. Keep the ClickHouse volume and the original
`LOOM_JWT_SECRET`, which seals the stored provider keys, when recreating containers. Rebuilds make provider calls and may
take time and incur fees proportional to the indexed content. The rebuild publishes
to local MergeTree tables only. The first embedding choice on an install with no
stored vectors, such as a fresh install that has stored nothing yet, switches
directly with no rebuild. Once vectors exist on replicated or shared storage, such
as ClickHouse Cloud, Models & keys locks
the embedding fields and the server refuses an embedding change before pausing
anything: change the embedding model in the environment instead and re-embed each
index as described above. Lost remote-process recovery remains a documented limitation in
[provider reconfiguration](../architecture/provider-reconfiguration.md).

The token settings are separate: they configure agent access to Loom, not model-provider credentials.

### Alternative: configure the environment

For automated deployments, edit `~/.loom/.env` (or the directory selected with
`LOOM_INSTALL_DIR`). For a source checkout, edit the repository's `.env`.
Replace existing entries instead of appending duplicate keys. These settings are the
startup baseline; saved dashboard overrides take precedence. Environment edits require
container recreation or a native server restart, as described below.

### OpenAI for both roles

```dotenv
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=<your OpenAI API key>
LOOM_EMBEDDING_PROVIDER=openai
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_REQUIRE_REAL_PROVIDERS=true
```

The examples use Loom's shipped model defaults. Choose a model enabled for your API
account. Setting a key alone does not change the stock installer configuration, which
names its providers explicitly (the local embedder and the fake LLM).

### Claude for the LLM

```dotenv
LOOM_LLM_PROVIDER=anthropic
LOOM_LLM_MODEL=claude-haiku-4-5
ANTHROPIC_API_KEY=<your Anthropic API key>
LOOM_REQUIRE_REAL_PROVIDERS=true
```

Then choose an embedding configuration:

```dotenv
# OpenAI embeddings
LOOM_EMBEDDING_PROVIDER=openai
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
OPENAI_API_KEY=<your OpenAI API key>
```

```dotenv
# Or Gemini embeddings
LOOM_EMBEDDING_PROVIDER=gemini
LOOM_EMBEDDING_MODEL=gemini-embedding-001
LOOM_EMBEDDING_DIM=3072
GEMINI_API_KEY=<your Gemini API key>
```

For another service that implements the OpenAI embeddings API, configure its API
root, model ID and native output width:

```dotenv
LOOM_EMBEDDING_PROVIDER=compatible
LOOM_EMBEDDING_BASE_URL=https://embedding-provider.example/v1
LOOM_EMBEDDING_MODEL=your-embedding-model
LOOM_EMBEDDING_DIM=768
LOOM_EMBEDDING_API_KEY=<your embedding provider API key>
# Enable only if the endpoint accepts the optional dimensions parameter:
LOOM_EMBEDDING_SEND_DIMENSIONS=false
```

The base URL is the API root; Loom appends `/embeddings`. Use HTTPS for remote
services. HTTP is supported for private self-hosted endpoints, but transmits the
key and content unencrypted. Compatibility requires indexed float-array responses
with the configured width; a provider using a different API needs its own adapter.
Changing the endpoint, model or width requires rebuilding existing embeddings.

If your organization currently has only Anthropic API access, obtain access to an
embedding provider before enabling semantic search.

### Keep every LLM job on your chosen provider

Loom supports different models for different jobs, but you do not need accounts with
multiple LLM providers. Add these settings **after** `LOOM_LLM_MODEL` to have the
normally provider-specific jobs use your selected model:

```dotenv
LOOM_MEMORY_EXTRACT_VERIFY_MODELS=${LOOM_LLM_MODEL}
LOOM_SESSION_BRIEF_LLM_MODEL=
LOOM_CODE_BISECT_LLM_RERANK_MODEL=
LOOM_TRACKER_DUPCHECK_RERANK_MODEL=
LOOM_RETRIEVAL_JUDGE_MODEL=
```

Compose and Loom's dotenv loader expand the verifier's model from the earlier entry.
The empty values above inherit the main LLM. This keeps the capture verifier enabled;
an empty verifier list would disable it. Remove any other cross-provider model overrides
you added previously, including fallback, synthesis and reranking models.

These settings matter: some shipped advanced defaults name Claude or OpenAI models
explicitly, and an override with no provider key can fall back to the fake client.
Using one model across jobs is a convenience configuration, not the configuration behind
every published benchmark. Tune individual jobs later using
[models per job](../operations/deployment.md#models-per-job).

### Through an inference gateway

A gateway that speaks the OpenAI or Anthropic API in front of the providers
(for budgets, cost attribution or one key for many services) takes Loom's
calls once its API roots point there; the model names stay what the gateway
serves:

```bash
LOOM_OPENAI_BASE_URL=https://gateway.example/v1   # chat and embeddings
LOOM_ANTHROPIC_BASE_URL=https://gateway.example   # Messages: <root>/v1/messages
OPENAI_API_KEY=<the key the gateway issued for Loom>
```

Off its default, the Anthropic key is also sent as `Authorization: Bearer`, the
scheme gateways take. With a gateway key, require the gateway, so a wrong root can
never send prompts and the key anywhere else. A token exchange requires it by itself.

```bash
LOOM_INFERENCE_GATEWAY_REQUIRED=true
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example   # comma-separated; exact names, any case
```

While the gateway is required, model calls and the memory content Loom posts
to classifiers and rerankers go only to the hosts `LOOM_INFERENCE_GATEWAY_HOSTS`
lists. A host matches exactly: no wildcard, and `gateway.example` does not
admit `api.gateway.example` or `gateway.example.attacker.test`; an IP address
matches only if it is listed.

- **At start**, Loom refuses to load with no `LOOM_INFERENCE_GATEWAY_HOSTS`,
  or while `LOOM_OPENAI_BASE_URL`, `LOOM_ANTHROPIC_BASE_URL`,
  `LOOM_EMBEDDING_BASE_URL`, `LOOM_INFERENCE_TOKEN_URL` or an entry of
  `LOOM_RERANK_ENDPOINTS_JSON` names another host, while the embedder is
  Gemini's, or while `LOOM_CODE_SEARCH_RERANK_DEFAULT` or
  `LOOM_KNOWLEDGE_SEARCH_RERANK_DEFAULT` is `builtin:cohere`.
  Both API roots count, the one for a provider you do not use included (their
  defaults are the providers' own APIs): a model named per request may be
  either provider's.
- **At call time**, a model call to any other host is refused before it
  leaves, on every redirect hop too, whatever named it: a model named per
  request, a `LOOM_CODE_SEARCH_VECTOR_MODELS` entry. A refused call is not
  retried, and `/v1/health` reports the provider as failing.
- **Namespaces**: `namespace.register` refuses a `classify_endpoint` that is not
  https at the default port of a listed host (422), and one stored before is
  never called: memory.set keeps the caller's type, with a warning in the log.
  `builtin:cohere` is refused as a rerank choice (422), and a namespace that
  stored it reranks with nothing, with a warning in the log.
- **Dashboard**: saving a model configuration with an endpoint at another host
  (or the Gemini embedder) answers 422, and the model check answers
  `gateway_required`. A stored one that the check refuses is not applied, its
  serving vector settings included, and `/v1/health` reports
  `model_configuration_refused`. The check reads only the embedding, so a refused
  configuration always embeds differently from the settings in force, and the
  vector tables may hold that other space: every data-plane request on that
  server is refused (503 `GATEWAY_REQUIRED`, which neither SDK retries, with no
  `Retry-After`) and the server is not ready, until a configuration the check
  accepts replaces it or it is reset to the environment's. A rolling restart then
  stops at that server: replace the configuration before rolling.
- **Rolling it out:** a previous build ignores both settings, so turn them on
  once every replica runs this one. A deployment that already exchanges a token
  refuses to start on this build until `LOOM_INFERENCE_GATEWAY_HOSTS` lists its
  gateway, and while it embeds with Gemini, names a rerank endpoint at another
  host or defaults code search to `builtin:cohere`; requests naming
  `builtin:cohere` get a 422, and namespaces that stored it or an off-gateway
  `classify_endpoint` stop reranking or classifying until it is changed. Set the
  hosts and move the embedder behind the gateway (`LOOM_EMBEDDING_PROVIDER=compatible`,
  `LOOM_EMBEDDING_BASE_URL` at the gateway) first, or the rollout stops at the
  first upgraded replica.

A gateway that accepts workload identity (RFC 8693 token
exchange) needs no key at all: Loom exchanges its workload's OIDC token there,
renews the answer five minutes before it runs out (halfway, for one living
under ten minutes), and sends it with every call. With no key to infer them
from, name the provider and the models:

```bash
LOOM_INFERENCE_TOKEN_URL=https://gateway.example/oauth/token         # https
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example                         # every root's host
LOOM_INFERENCE_WORKLOAD_TOKEN_FILE=/var/run/secrets/tokens/gateway   # rotated by the platform
LOOM_INFERENCE_TOKEN_SCOPE="chat embeddings"                         # if the gateway asks for one
LOOM_OPENAI_BASE_URL=https://gateway.example/v1                      # both roots required:
LOOM_ANTHROPIC_BASE_URL=https://gateway.example                      # the token is the gateway's
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
LOOM_EMBEDDING_MODEL=text-embedding-3-large
```

Loom refuses to start while either provider's root, or the token endpoint,
is at a host `LOOM_INFERENCE_GATEWAY_HOSTS` does not list: a model named per
request may be either provider's, and the gateway's token would go there. In
the dashboard, model changes are checked through the gateway too.

On Kubernetes the token file is a projected service-account token with the
gateway's audience; the gateway must trust the cluster's OIDC issuer and match
Loom's service account (`system:serviceaccount:<namespace>:<name>`), not just
the issuer, since every pod of the cluster can get a token from it.

A gateway that verifies the workload's token itself takes it as the bearer, with no
exchange: set the bearer mode instead of a token endpoint. Loom reads the file at most
once a minute, and again at once when the gateway answers 401, so a rotated token is
picked up without a restart:

```bash
LOOM_INFERENCE_WORKLOAD_TOKEN_MODE=bearer
LOOM_INFERENCE_WORKLOAD_TOKEN_FILE=/var/run/secrets/tokens/gateway   # sent as it is
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example                         # required, as with the exchange
LOOM_OPENAI_BASE_URL=https://gateway.example/v1
LOOM_ANTHROPIC_BASE_URL=https://gateway.example
LOOM_EMBEDDING_PROVIDER=compatible                                   # embeddings through the gateway too
LOOM_EMBEDDING_BASE_URL=https://gateway.example/v1
```

Name the provider and the models as in the exchange example. The token goes only to the
gateway's hosts: the bearer mode requires the gateway, as the exchange does. A compatible
embedding endpoint given its own `LOOM_EMBEDDING_API_KEY` sends that key instead.

#### Naming the customer on every call

A gateway that meters or budgets per customer reads the customer from a header.
Name the headers, and every chat, embedding, rerank and classify request carries
the org whose data it holds, and the feature that made it:

```bash
LOOM_INFERENCE_CUSTOMER_HEADER=X-Customer-Id   # the org
LOOM_INFERENCE_FEATURE_HEADER=X-Feature        # optional, with the customer header: hyde, memory.search, worker.embedding
LOOM_INFERENCE_SYSTEM_CUSTOMER=loom            # calls that belong to no org
LOOM_INFERENCE_CUSTOMER_REQUIRED=true          # a cell serving several customers: refuse to start without the header
LOOM_INFERENCE_CUSTOMER_FORMAT='cloud:{org}'   # the gateway's customer id for an org; default the org id
LOOM_INFERENCE_FEATURE_MAP_JSON='{"hyde": "search", "*": "memory"}'  # Loom's feature names to the gateway's
```

- A request names the org it addresses, as authorized for its token; a caller
  cannot name an org its token does not reach.
- A background job names the org of the rows it works on, and makes one call
  per org: the embedding worker, `loom reembed` and a model rebuild split their
  batches by org.
- The deployment's own calls (the startup check of the embedder, the
  recalibration worker's fixed pairs, the dashboard's provider check) name
  `LOOM_INFERENCE_SYSTEM_CUSTOMER`.
- The sampled retrieval judge (`LOOM_RETRIEVAL_JUDGE_SAMPLE_RATE`) sends the
  judged org's query and memories, so each judged search adds a call that
  names that org.
- With the customer header set, a model call that names no org is refused
  (`MODEL_CALL_UNATTRIBUTED`) instead of being sent: it is a call path that
  forgot its org, and the gateway would bill it to no one.
- An org id is sent percent-encoded where it needs to be.

Cached answers make no call: an embedding or a model answer served from Loom's
cache is not metered again. The customer is still checked before the cache, so a
path that names none is refused even on a hit.

#### Naming the agent on every call

A gateway that bills or limits each agent reads whom a call is for from headers. Map each
header to a template, and every chat, embedding, rerank and classify request to the gateway
carries it:

```bash
LOOM_INFERENCE_IDENTITY_HEADERS_JSON='{"X-Tenant": "{tenant}", "X-Agent": "{agent}", "X-Actor": "{email}"}'
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example   # required: identity headers go only to these hosts
LOOM_INFERENCE_TENANT_CLAIM=tenant_id   # the workload trust claim {tenant} names (the default)
```

- The placeholders are `{org}`, `{namespace}`, `{subject}`, `{user_id}`, `{agent}` (the agent
  of a workload trust's token, `sa:<trust>:<agent>`), `{tenant}` (the value that trust requires
  of its `LOOM_INFERENCE_TENANT_CLAIM` claim) and `{email}` (a signed-in person's address).
- A header whose template renders empty is not sent: with bare placeholders, a call with no
  agent behind it (a person's, or the deployment's own) carries no agent header. The headers
  naming `{agent}` or `{tenant}` go together or not at all, so an agent never goes without its
  tenant.
- A call that names no org is refused (`MODEL_CALL_UNATTRIBUTED`) unless it is the deployment's
  own: run `loom` commands that call models with `--org`, and with `--namespace` while the
  headers name the agent (one with no namespace is refused rather than billed to the
  gateway's default agent).
- Background jobs name the identity of the rows they work on: the writer of a memory or of a
  deferred extraction, else the agent whose own namespace the row is in. A job batching rows
  makes one call per identity.
- No other host gets the identity headers: a reranker, a classifier or a provider outside
  `LOOM_INFERENCE_GATEWAY_HOSTS` never learns whom a call is for.
- `LOOM_INFERENCE_IDENTITY_REFUSALS_JSON` lists the answers your gateway gives for one
  identity, each by its status and the exact `code`, `type` or `message` of its error body
  (under `error`). Match by `code` where the gateway gives one. The default matches an
  inference proxy's per-agent codes and the budget refusal of the metering behind it:

  ```json
  {
    "refused": [
      { "status": 429, "code": "budget_exceeded" },
      { "status": 429, "type": "rate_limit_error", "message": "budget exceeded" },
      { "status": 403, "code": "agent_denied" },
      { "status": 400, "code": "identity_incomplete" },
      { "status": 400, "code": "identity_malformed" }
    ],
    "wait": [{ "status": 429, "code": "agent_request_limit" }]
  }
  ```

  The second entry is matched by type and message because the metering's Anthropic-shaped
  answer on `/v1/messages` carries no code.

- Never list an answer the gateway also gives for its own failure: `token_unavailable` (it
  cannot fetch a credential, which a credential-service outage also answers) or
  `identity_missing` (the request named no agent and the gateway has no default, a
  configuration problem). Listed, `/v1/health` would stop seeing the outage or the
  misconfiguration. Deployment-level answers stay provider failures too: `caller_*`,
  `scope_not_granted` (a route the token's scopes do not cover), `route_not_found`,
  `upstream_*`, `misconfigured`, `no_identity`.
- An answer under `refused` makes that request answer 403 `MODEL_CALL_REFUSED`, and one under
  `wait` 503 `MODEL_CALL_DEFERRED` with the gateway's `Retry-After`; a wait of two seconds or
  less is waited out once first, and the call asked again. Nothing retries either answer,
  `/v1/health` does not read them as a provider failure, and background jobs hold that
  identity's calls to the same model, for `LOOM_INFERENCE_REFUSAL_COOLDOWN_SECONDS` (10
  minutes by default) after a refusal and the `Retry-After` after a wait, while other work
  goes on. Any answer the table does not list is the provider's, as always.
- How each value is found: [identity headers](../architecture/llm-architecture.md#identity-headers).

#### Switching an org's model calls off

An org admin can stop every model call for the org's data, with or without a
gateway, from the dashboard's Guardrails page or over the API:

```bash
curl -s -X POST "$LOOM_BASE_URL/v1/org.set_model_calls" -H "Authorization: Bearer $ORG_ADMIN_TOKEN" \
  -d '{"org":"acme","enabled":false,"reason":"customer turned AI off"}'
```

- While off, no chat, embedding, rerank or classify request is sent for the
  org, and no cached vector or answer is served to it either: the switch is a
  kill switch. A request that needs one is refused (403 `MODEL_CALLS_DISABLED`):
  `memory.set` of a memory that is embedded on write, or in a namespace with a
  classify endpoint, `memory.search`, and `tracker.similar`,
  `tracker.duplicate_check` and `code.bisect_candidates` when they embed, judge
  or rerank, among them. A read by key (`memory.get`) and a delete make no
  model call and answer.
  The local embedder runs in-process and sends nothing, but it is switched off
  too. A call that names the org is checked even when Loom makes it for itself;
  only its own calls that name no org (the startup check, calibration) go on.
- `"enabled": true` switches it back on, and the waiting work runs.
- **A platform admin's lock.** A platform admin can add `"locked": true`: the
  org's own admins can then still switch it off, but switching it back on or
  unlocking it is refused (403, naming who locked it) until a platform admin
  sends `"locked": false`. A platform admin's change that sends no `locked`
  keeps the lock. [`org.model_calls`](../reference/operations/org.md#orgmodel_calls)
  reads the setting, the lock and who set it.
- **Offboarding an org** is manual: a platform admin sends
  `"enabled": false, "locked": true`. The org's cached query vectors expire
  after 30 days. No Loom path switches an org off by itself.
- Every server applies a change within `LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS`
  (default 10; the process that took it, on its next call). A server that
  cannot read the switch for longer than
  `LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS` (default 60, 15 to 300) makes no
  model call for any org until it can.
- Switch an org off only once every replica runs a build that knows the
  switch: an older one keeps calling models for the org.

What waits, and for how long, while an org is off:

| Work                                                                                                      | While the org is off                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| --------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Memory embeddings (the embedding worker)                                                                  | Not picked: the memories stay, without a vector, and are embedded once the org is back on. Never dropped.                                                                                                                                                                                                                                                                                                                                                        |
| Extraction requests (the extraction queue)                                                                | Stay pending, unclaimed. Once a request has waited `LOOM_MODEL_CALLS_OFF_MAX_WAIT_DAYS` (default 7, 1 to 29) since it was queued and since the org was switched off, it is marked failed with a `memory_drops` record, reason `model_calls_off`. The record is written synchronously first: a request whose record cannot be written stays pending. The limit stays under 30 days because the queue's 30-day TTL would remove the request first, with no record. |
| Entity tagging, profile folds, period digests, conversation titles, reflections, observation distillation | Not selected; done once the org is back on. They are derived from rows that stay, so nothing is dropped.                                                                                                                                                                                                                                                                                                                                                         |
| Projection repairs                                                                                        | Not scanned; retried once the org is back on.                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Pending conflict judgements                                                                               | Not re-judged. One older than the sweep's hour when the org comes back is past the sweep, as a judgement lost in an outage is: the write stands as made.                                                                                                                                                                                                                                                                                                         |
| Code ingest, code summaries, code embedding recovery                                                      | The org's repositories are skipped, so its whole code index, lexical search included, stays as it was. A stub the recovery scan moved past is found by its periodic full scan.                                                                                                                                                                                                                                                                                   |
| Code-link judgements                                                                                      | Left due.                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| Tracker embeddings, tracker link scoring                                                                  | The namespace is skipped before its cursor moves.                                                                                                                                                                                                                                                                                                                                                                                                                |
| Retrieval-judge samples                                                                                   | Dropped: a sample is not owed work.                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `loom reembed`                                                                                            | The org's rows are left out, counted (`skipped_switched_off`) and reported; run it again once the org is back on. `--delete-old` keeps the old vectors of every org whose rows it left.                                                                                                                                                                                                                                                                          |
| A model change's rebuild                                                                                  | Completes; the org's rows are staged without a vector and counted in the rebuild's progress (`rows_waiting`). Once the org is back on, memory embeddings, code chunks and code summaries are embedded again by their recovery workers; a row of any other table has no vector until it is written again or a later rebuild covers it.                                                                                                                            |

#### Without a workload token

Where the platform gives Loom no token (a VM, a container host), Loom signs
its own: set an audience instead of the token file, and the gateway trusts Loom
as an issuer.

```bash
LOOM_INFERENCE_TOKEN_URL=https://gateway.example/oauth/token
LOOM_INFERENCE_GATEWAY_HOSTS=gateway.example
LOOM_INFERENCE_TOKEN_AUDIENCE=loom-inference   # what the gateway's entry for Loom names
LOOM_PUBLIC_BASE_URL=https://loom.example      # the issuer is <this>/v1
LOOM_OPENAI_BASE_URL=https://gateway.example/v1
LOOM_ANTHROPIC_BASE_URL=https://gateway.example
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
LOOM_EMBEDDING_MODEL=text-embedding-3-large
```

Each exchange sends a new token that lives five minutes: issuer
`<LOOM_PUBLIC_BASE_URL>/v1`, subject `loom`, the audience above, signed RS256
with the newest RS256 key of the signing keyring. Create one first with `loom
signing-key rotate --algorithm RS256`, unless `LOOM_JWT_SECRET` is an RSA key,
which signs as RS256 already. That key signs Loom's own tokens too from then
on, which every Loom of the deployment verifies; an HS256 secret is never used,
since the gateway could not verify it. Spend is attributed to that one subject.

The gateway's entry for Loom names that issuer, the audience, subject `loom`,
RS256, and the keys at `<LOOM_PUBLIC_BASE_URL>/v1/.well-known/jwks.json` (OIDC
discovery is at `/v1/.well-known/openid-configuration`). The gateway must be
able to fetch that URL over https; where it cannot reach Loom, publish a copy of
the key set somewhere it can. Each Loom publishes a key at its next keyring
refresh after `rotate` writes it, and the key signs only `--activate-in` later
(300 s by default): rotate with an `--activate-in` longer than the gateway
keeps its copy of the keys, or than copying them takes. A retired key stays
trusted at the gateway until its copy is refreshed. `loom signing-key retire`
refuses to retire the last active RS256 key when `LOOM_INFERENCE_TOKEN_AUDIENCE`
is set in its own environment, so run it with the servers'. Loom (the server
and every `loom` command) refuses these settings without an https
`LOOM_PUBLIC_BASE_URL`, with `LOOM_JWT_AUDIENCE` as the audience, or with an
issuer its own verifier trusts, and its own verifier never accepts a token with
the gateway's audience.

## Apply changes and check the result

**Dashboard changes:** wait for Models & keys to report the active configuration. If a
rebuild is running, follow its progress there; retry a failed job from the same page.
Do not recreate containers to apply a dashboard change.

**Environment changes only:** for the released stack, recreate the server container:

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
curl -fsS http://localhost:7777/v1/health
```

Use your installation directory and port if different. Recreate the container: a plain
`docker compose restart` does not reload its environment from `.env`. Native servers
must restart with the updated environment. For the checkout stack, run
`docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build --force-recreate loom`.

Health reports `fake_llm`, `fake_embedding`, `providers_degraded` and actionable
`problems`. With both real providers configured, the fake flags should be false. This
checks configuration and health; also save and retrieve a test fact and try an answer to
confirm that your account can call the chosen models.

Changing the LLM or rotating an API key does not require rebuilding embeddings.
Changing the embedding model does, even when its dimensions stay the same. For
environment changes (rather than the dashboard workflow), existing
memory, document, tracker and code indexes need their corresponding re-embedding or
re-ingest procedures; `loom reembed` alone is not a universal rebuild of every index.
If dimensions change, check the existing schema's vector constraints before applying
the new configuration. Preserve your data and follow
[maintenance](../operations/maintenance.md#re-embed-and-re-extract) and
[code-index operations](../operations/code-index-operations.md).
