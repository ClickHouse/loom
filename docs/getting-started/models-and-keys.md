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

The stock Docker installation explicitly selects `fake-ngram-3072` embeddings and the
`fake-echo-1` LLM. It boots without provider keys: storage, the dashboard and structural
code indexing work, but search uses character overlap rather than semantic embeddings.
The fake LLM does not produce meaningful extracted facts, summaries or generated answers.
These defaults test installation and wiring, not the quality reported in the benchmarks.

For semantic retrieval over saved content, configure a real embedding provider. For
automatic fact extraction, summaries and generated answers, configure an LLM too.
An LLM alone does not replace an embedding model; an embedding model alone does not
generate answers. No hosted embedding configuration means the stock stack uses its
fake embedder, not that it skips embedding entirely.

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

For dashboard embedding changes, Loom queues that rebuild automatically and keeps
data-plane requests paused until verification, publication and cleanup finish.
Models & keys shows the phase and progress; a failed job stays paused and offers
retry from its saved cursor. Keep the ClickHouse volume and the original
`LOOM_JWT_SECRET`, which seals the stored provider keys, when recreating containers. Rebuilds make provider calls and may
take time and incur fees proportional to the indexed content. The rebuild publishes
to local MergeTree tables only. The first embedding choice on an install with no
stored vectors, such as a fresh install leaving the placeholder models, switches
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
explicitly selects fake providers.

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
scheme gateways take. A gateway that accepts workload identity (RFC 8693 token
exchange) needs no key at all: Loom exchanges its workload's OIDC token there,
renews the answer five minutes before it runs out (halfway, for one living
under ten minutes), and sends it with every call. With no key to infer them
from, name the provider and the models:

```bash
LOOM_INFERENCE_TOKEN_URL=https://gateway.example/oauth/token         # https
LOOM_INFERENCE_WORKLOAD_TOKEN_FILE=/var/run/secrets/tokens/gateway   # rotated by the platform
LOOM_INFERENCE_TOKEN_SCOPE="chat embeddings"                         # if the gateway asks for one
LOOM_OPENAI_BASE_URL=https://gateway.example/v1                      # both roots required:
LOOM_ANTHROPIC_BASE_URL=https://gateway.example                      # the token is the gateway's
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
LOOM_EMBEDDING_MODEL=text-embedding-3-large
```

Loom refuses to start while either provider's root still points at that
provider's own API: a model named per request may be either provider's, and the
gateway's token would go there. In the dashboard, model
changes for OpenAI and Anthropic are checked through the gateway too.

On Kubernetes the token file is a projected service-account token with the
gateway's audience; the gateway must trust the cluster's OIDC issuer and match
Loom's service account (`system:serviceaccount:<namespace>:<name>`), not just
the issuer, since every pod of the cluster can get a token from it.

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
  recalibration worker's fixed pairs, the dashboard's provider check, the
  sampled retrieval judge) name `LOOM_INFERENCE_SYSTEM_CUSTOMER`.
- With the customer header set, a model call that names no org is refused
  (`MODEL_CALL_UNATTRIBUTED`) instead of being sent: it is a call path that
  forgot its org, and the gateway would bill it to no one.
- An org id is sent percent-encoded where it needs to be.

Cached answers make no call: an embedding or a model answer served from Loom's
cache is not metered again. The customer is still checked before the cache, so a
path that names none is refused even on a hit.

#### Switching an org's model calls off

An org admin can stop every model call for the org's data, with or without a
gateway:

```bash
curl -s -X POST "$LOOM_BASE_URL/v1/org.set_model_calls" -H "Authorization: Bearer $ORG_ADMIN_TOKEN" \
  -d '{"org":"acme","enabled":false,"reason":"customer turned AI off"}'
```

- While off, no chat, embedding, rerank or classify request is sent for the
  org, and no cached vector or answer is served to it either: the switch is a
  kill switch. A request that needs one is refused (403 `MODEL_CALLS_DISABLED`).
  The local embedder runs in-process and sends nothing, but it is switched off
  too. A call that names the org is checked even when Loom makes it for itself;
  only its own calls that name no org (the startup check, calibration) go on.
- `"enabled": true` switches it back on, and the waiting work runs.
- **A platform admin's lock.** A platform admin can add `"locked": true`: the
  org's own admins are then refused (403, naming who locked it) until a
  platform admin sends `"locked": false`. Ending an org's use of Loom switches
  it off locked. [`org.model_calls`](../reference/operations/org.md#orgmodel_calls)
  reads the setting, the lock and who set it.
- Every server applies a change within `LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS`
  (default 10; the process that took it, on its next call). A server that
  cannot read the switch for longer than
  `LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS` (default 60, 15 to 300) makes no
  model call for any org until it can.
- Switch an org off only once every replica runs a build that knows the
  switch: an older one keeps calling models for the org.

What waits, and for how long, while an org is off:

| Work | While the org is off |
| --- | --- |
| Memory embeddings (the embedding worker) | Not picked: the memories stay, without a vector, and are embedded once the org is back on. Never dropped. |
| Extraction requests (the extraction queue) | Stay pending, unclaimed. Past `LOOM_MODEL_CALLS_OFF_MAX_WAIT_DAYS` (default 7; 0 = no limit) each is marked failed with a `memory_drops` record, reason `model_calls_off`. |
| Entity tagging, profile folds, conversation titles, reflections, observation distillation | Not selected; done once the org is back on. They are derived from rows that stay, so nothing is dropped. |
| Projection repairs | Not scanned; retried once the org is back on. |
| Pending conflict judgements | Not re-judged. One older than the sweep's hour when the org comes back is past the sweep, as a judgement lost in an outage is: the write stands as made. |
| Code ingest, code summaries, code embedding recovery | The org's repositories are skipped, so its whole code index, lexical search included, stays as it was. A stub the recovery scan moved past is found by its periodic full scan. |
| Code-link judgements | Left due. |
| Tracker embeddings | The namespace is skipped before its cursor moves. |
| Retrieval-judge samples | Dropped: a sample is not owed work. |
| `loom reembed` | The org's rows are left out, counted (`skipped_switched_off`) and reported; run it again once the org is back on. |
| A model change's rebuild | Completes; the org's rows are staged without a vector (`rows_waiting`). Memory and code rows are embedded again by their workers once the org is back on; on the other surfaces those rows have no vector until they are written again. |

#### Without a workload token

Where the platform gives Loom no token (a VM, a container host), Loom signs
its own: set an audience instead of the token file, and the gateway trusts Loom
as an issuer.

```bash
LOOM_INFERENCE_TOKEN_URL=https://gateway.example/oauth/token
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
