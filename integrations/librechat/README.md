# LibreChat

LibreChat v0.8.8 (MIT) as a chat client for Loom's
[assistant toolset](../../docs/reference/mcp.md#the-assistant-toolset). Each person's
tool calls carry that person's own Loom token. Other LibreChat versions are not qualified.

The guide (identity delegation, what the model receives, tool errors, streaming and
cancellation, tenant boundaries, what LibreChat stores and how it is deleted, the
qualification targets and results) is
[docs/features/integrations/librechat.md](../../docs/features/integrations/librechat.md).

| File                         | What it is                                                                                                     |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `librechat.yaml`             | the LibreChat configuration: the Loom MCP entry, per-user token, memory off, retention, no sharing             |
| `agent.yaml`                 | the agent Loom seeds into the LibreChat it runs beside it: name, model, instructions, version                  |
| `docker-compose.yml`         | LibreChat v0.8.8 and MongoDB, pinned by digest; no search index, no RAG service                                |
| `.env.example`               | LibreChat's settings, and the compose file's own `LIBRECHAT_PORT`, `LIBRECHAT_ENV_FILE` and `LIBRECHAT_CONFIG` |
| `qualification/qualify.py`   | the qualification harness: seeds a fixture, runs `cases.json` through LibreChat and through Loom               |
| `qualification/cases.json`   | the labeled cases                                                                                              |
| `qualification/fixture.json` | the memories, orgs and people the cases are labeled against                                                    |

Loom can also run LibreChat itself, headless, with the agent in `agent.yaml`
(`docker compose --profile assistant`, or the chart's `assistant.enabled`); the guide's
[deployed assistant](../../docs/features/integrations/librechat.md#the-deployed-assistant)
section covers it and its qualification. The steps below run LibreChat's own UI with a
token per person instead.

## Run it locally

Needs Docker, a model provider key, and a Loom checkout set up as in
[CONTRIBUTING.md's development setup](../../CONTRIBUTING.md#development-setup): its
virtualenv in `.venv`, ClickHouse running and `make migrate` done. Each step names its
working directory. Tested with Docker Engine on Linux.

1. Start Loom so the container can reach it. `librechat.yaml` expects Loom on the Docker
   host at port 7777 (`url` and `mcpSettings.allowedAddresses`, which lets LibreChat reach
   a private address). On Docker Engine on Linux, `host.docker.internal` is the bridge
   gateway, and `make server` and `loom serve` bind `127.0.0.1`, which a container cannot
   reach, so bind every interface. Loom refuses a non-loopback bind without
   `LOOM_PUBLIC_BASE_URL`, and with the checked-in dev signing secret, so the checkout's
   `.env` needs a private `LOOM_JWT_SECRET` first:

   ```bash
   # In the Loom checkout, with its virtualenv active (. .venv/bin/activate): make server
   # runs uvicorn from it. Skip the umask block if .env already has a private LOOM_JWT_SECRET.
   (
     umask 077  # every file below is created readable by its owner only
     touch .env && sed '/^LOOM_JWT_SECRET=/d' .env > .env.new && mv .env.new .env
     echo "LOOM_JWT_SECRET=$(openssl rand -hex 32)" >> .env
     chmod 600 .env
   )
   LOOM_AUTH_REQUIRED=true LOOM_BIND_HOST=0.0.0.0 \
     LOOM_PUBLIC_BASE_URL=http://host.docker.internal:7777 make server
   ```

   Replacing the secret stops every token minted under the old one. Binding `0.0.0.0`
   exposes Loom on the host's other interfaces; on a shared or public host, bind the
   Docker bridge's gateway address (`docker network inspect bridge`) instead, or firewall
   the port. Docker Desktop forwards `host.docker.internal` to the host itself, so a
   loopback bind may be enough there; that was not tested. For a Loom at a public
   `https://` URL, set `url` to `https://<host>/mcp?toolset=assistant` and remove
   `mcpSettings`.

2. In a second shell, from the Loom checkout, create LibreChat's own `.env` beside its
   compose file, with the four secrets filled in:

   ```bash
   cd integrations/librechat
   (
     umask 077  # the secrets never sit in a file others can read
     cp .env.example .env
     for v in CREDS_KEY JWT_SECRET JWT_REFRESH_SECRET; do
       sed "s/^$v=.*/$v=$(openssl rand -hex 32)/" .env > .env.new && mv .env.new .env
     done
     sed "s/^CREDS_IV=.*/CREDS_IV=$(openssl rand -hex 16)/" .env > .env.new && mv .env.new .env
     chmod 600 .env
   )
   ```

   Then set `ANTHROPIC_API_KEY` in that file.

3. Still in `integrations/librechat`, start LibreChat with `docker compose up -d`, then
   open `http://localhost:3080` and register.
4. Back in the Loom checkout (`cd ../..`), mint each person a viewer token; the CLI signs
   it with the `LOOM_JWT_SECRET` from step 1:

   ```bash
   .venv/bin/loom mint-token --subject alice --org acme --namespaces acme-handbook --roles viewer --user-token --ttl-days 30
   ```

5. In LibreChat, open the MCP settings for `loom`, paste the token into **Loom token**, and
   create an agent with the `loom` tools. Ask it what the namespace holds.

Stop it with `docker compose down` in `integrations/librechat`; `docker compose down -v`
also deletes the stored chats and tokens.

## Qualification

The harness spends model tokens (one answer per case; with the default model a mean of
$0.03 and a max of $0.064 counting every prompt token at list price) and never runs in CI.
Run it from the repository root, against a local Loom whose `.env` holds
`LOOM_JWT_SECRET` (it mints and revokes the fixture's tokens with the `loom` CLI), and
with LibreChat started on a fresh database. The published run used a Loom with
`LOOM_AUTH_REQUIRED=true`, `LOOM_EMBEDDING_MODEL=fake-ngram-1024`,
`LOOM_LLM_PROVIDER=fake` and `LOOM_BACKGROUND_WORKERS=off`, so the direct-path numbers
involve no model. The run below scores the document cases too, on whatever embedder the
server has; their published numbers come from a separate run, shown after it.

```bash
Q=integrations/librechat/qualification/qualify.py
export QUALIFY_CH_URL=http://localhost:8123/ QUALIFY_CH_USER=<user> QUALIFY_CH_PASSWORD=<password>
export QUALIFY_CH_DATABASE=loom  # the Loom server's LOOM_CH_DATABASE
.venv/bin/python $Q --loom-url http://localhost:7777 --loom-bin .venv/bin/loom seed
.venv/bin/python $Q --librechat-url http://localhost:3080 setup
.venv/bin/python $Q --loom-bin .venv/bin/loom revoke
.venv/bin/python $Q run --cancel-probe
```

The document cases measure the embedder too, and `knowledge.search` searches only the
chunks of the server's current embedding model: start a Loom with
`LOOM_EMBEDDING_MODEL=text-embedding-3-large` on an empty database, seed it, set up a
fresh LibreChat (the seed mints new tokens), and run only those cases into a file of
their own so `results.json` is kept:

```bash
.venv/bin/python $Q --loom-url http://localhost:7777 --loom-bin .venv/bin/loom seed
.venv/bin/python $Q --librechat-url http://localhost:3080 setup
.venv/bin/python $Q run --out integrations/librechat/qualification/results-documents.json \
  --only doc-release-reviews doc-migration-rollback doc-canary doc-contract-notice \
  doc-orders-ttl doc-cache-entries doc-revised-ack abstain-doc-credit abstain-doc-phone
```

- `--loom-url` is the address the harness reaches Loom at; with the bridge-gateway bind
  from step 1, it is that gateway address, not `localhost`.
- `QUALIFY_CH_*` name the ClickHouse behind that Loom: the harness counts each answer's
  tool calls in `agent_events` in `QUALIFY_CH_DATABASE`. The password stays out of argv and
  error messages.
- `revoke` waits 30 s afterwards, one `LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS`, so the
  server has the revocation before the revoked case runs; pass `--settle` if yours differs.
- `run` uses the Loom URL `seed` stored and writes `qualification/results.json`;
  `rescore` scores a saved run again against the current `cases.json` without a model
  (with `--citations`, it also checks every citation again against the seeded server), and
  `run --native-only` answers the direct path again into the saved run, with no model and
  no LibreChat.
- `seed` writes memories and documents into orgs `acme` and `globex`, so use a scratch
  deployment. Tokens, LibreChat passwords and API keys of the run are kept in
  `qualification/.state.json` (mode 0600); it, `results.json` and `results-*.json` are
  ignored by git.
