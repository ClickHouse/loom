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
retry from its saved cursor. Keep the ClickHouse and blob volumes, and the original
`LOOM_JWT_SECRET`, when recreating containers. Rebuilds make provider calls and may
take time and incur fees proportional to the indexed content. The rebuild publishes
to local MergeTree tables only: on replicated or shared storage, such as ClickHouse
Cloud, Models & keys locks the embedding fields and the server refuses an embedding
change before pausing anything. Change the embedding model in the environment instead
and re-embed each index as described above. Lost remote-process recovery remains a documented limitation in
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
`docker compose --env-file .env -f dev/docker-compose.dev.yml up -d --build --force-recreate loom`.

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
