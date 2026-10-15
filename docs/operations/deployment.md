# Deployment

Loom is one server image plus ClickHouse. The image serves HTTP, MCP and the `/me`
dashboard, and runs the background workers. Its containers keep no durable state of their
own, so you can replace or add them freely: ClickHouse holds Loom's data; uploaded document originals sit on a blob volume and the code index keeps
bare git mirrors on disk. The compose files under `dev/` are the supported production
files. Every setting named here is in the generated
[configuration reference](../reference/configuration.md).

## Deployment ladder

Each rung keeps the previous one's image and schema. What changes is where ClickHouse
lives and how many containers run.

| Rung                          | Start it with                                                                                          | ClickHouse                                         | Use for                                   |
| ----------------------------- | ------------------------------------------------------------------------------------------------------ | -------------------------------------------------- | ----------------------------------------- |
| Installed stack               | `install.sh` → `compose.yaml`                                                                          | bundled container, a self-managed server, or Cloud | evaluation, a single host                 |
| Local stack from a clone      | `loom up` or `make dev` → `compose.yaml` + `dev/docker-compose.dev.yml`                                | bundled container (`loom-ch`)                      | development                               |
| Web and workers apart         | `make dev-cloud` → `dev/docker-compose.cloud.yml`                                                      | a self-managed server or Cloud                     | teams; the production shape               |
| Zero-downtime pair (optional) | `./redeploy.sh`: two projects of `dev/docker-compose.colour.yml` behind `dev/docker-compose.proxy.yml` | a self-managed server or Cloud                     | deploys with no refused-connection window |

Sizing (web replicas, uvicorn processes, the workers container) is in
[failure and recovery](../architecture/failure-and-recovery.md), driven by the metrics in
[monitoring](./monitoring.md#what-to-alert-on).

## Choosing the ClickHouse

`compose.yaml` is the one shipped stack. Its `clickhouse` service runs under the
`bundled` Compose profile, so `COMPOSE_PROFILES` in `.env` decides the target:

| Target                      | Install                                                                         | `.env`                                           |
| --------------------------- | ------------------------------------------------------------------------------- | ------------------------------------------------ |
| Bundled container (default) | `curl -fsSL …/install.sh \| sh`                                                 | `COMPOSE_PROFILES=bundled`, no `LOOM_CH_*`       |
| Self-managed server         | `curl -fsSL …/install.sh \| sh -s -- --clickhouse http://loom@ch.internal:8123` | `COMPOSE_PROFILES=` and `LOOM_CH_*` from the URL |
| ClickHouse Cloud            | `curl -fsSL …/install.sh \| sh -s -- --clickhouse https://loom@<host>:8443`     | `COMPOSE_PROFILES=` and `LOOM_CH_*` from the URL |

- **The user.** Give Loom a user of its own (`loom` above) rather than an admin such
  as Cloud's `default`, which would let Loom manage every database and user on the
  service. The URL must name one.
- **The URL.** `http://` defaults to port 8123, `https://` to 8443 and TLS.
  Percent-encode `@`, `:`, `/` and `%` in the user or password; an empty password is
  `user:@host` or `LOOM_CH_PASSWORD=`. `LOOM_CH_URL` works in place of `--clickhouse`. A server on the Docker host is `host.docker.internal`: `localhost`
  inside the container is the container itself.
- **The password.** Set `LOOM_CH_PASSWORD`, or answer the prompt, which does not echo;
  one in the URL works too, but `ps` and the shell history see it. It is written only to
  `.env`, which is mode 600, double-quoted with `\`, `"` and `$` escaped so Compose reads
  it back exactly.
- **Setup.** With a server of your own the installer runs `loom ch-setup` before it
  starts Loom. It creates the `loom` database and the read-only `loom_reader` user
  (below), and checks the version and the grants. Run it again at any time with
  `docker compose --project-directory ~/.loom run --rm loom loom ch-setup`; what is
  already in place is left alone. A `loom_reader` whose profile or grants differ from what
  `ch-setup` gives it is rebuilt to exactly that (SELECT on `loom.*` only, the current
  caps), so a role, grant or `REVOKE` an admin adds to it does not survive a rerun; the
  installer reruns `ch-setup` on every install and upgrade. An existing `loom_reader` is
  replaced only when it has the shape `ch-setup` gives it (a password user on the settings
  profile of its own name) and logs in with `LOOM_CH_PASSWORD`; anything else of that name
  is left alone.
- **Without admin rights.** When the user cannot create the database, read the system
  tables the migrations read, or create the reader or its settings profile, `ch-setup`
  stops and prints the SQL for an admin, with the password as a placeholder.
  `loom ch-setup --print-sql` prints it without connecting. The SQL grants the Loom user
  everything the admin holds on `loom.*` through `GRANT CURRENT GRANTS` (ClickHouse Cloud
  does not accept `GRANT ALL`), `SELECT` on the system tables Loom reads, and
  `READ ON REMOTE`, which `clusterAllReplicas` needs to ask every replica for its version
  (a self-managed server shows it as `REMOTE` in `SHOW GRANTS`; writes through `remote()`
  are still refused). It creates the reader with `OR REPLACE`, so check that no other
  account has that name; `ch-setup` running the SQL itself refuses one it cannot prove is
  Loom's.
- **Other users' queries.** Loom reads `system.query_log` for each request's cost and
  `system.processes` to see whether its own statements are still running, so the Loom
  user sees every user's past and running queries on that server, literals included. On
  a server Loom shares, a self-managed admin can keep both to Loom's own:
  `CREATE ROW POLICY loom_own ON system.query_log, system.processes USING user IN ('loom', 'loom_reader') TO loom`;
  ClickHouse Cloud does not allow row policies on system tables.
- **Switching.** Re-run the installer with `--clickhouse` to move an install to your own
  server; Loom starts on an empty `loom` database there. The bundled container keeps
  running on its old volume until you stop it (the installer prints the command); copy
  the data first with [backup and restore](./backup-and-recovery.md) if you need it.

Every boot runs the same checks before it connects (the oldest replica's version, the
database, the grants) and stops with one line naming the fix. A process that does not
migrate (`LOOM_BOOT_MIGRATE=false`, a read replica) is checked only for `SELECT`, `INSERT`,
`ALTER UPDATE` and `ALTER DELETE` on `loom.*`; one that runs the background workers also
optimizes, truncates, drops and refreshes tables there, so give it everything on `loom.*`.
`loom doctor` reports the checks as `clickhouse_preflight`.

### Local stack from a clone

`loom up` (or `make dev`) creates `.env` from `.env.example` and
`deploy/clickhouse-config/named_collections.xml` from its template, then runs:

```bash
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
```

The override builds the image from the checkout and pins Loom to the bundled ClickHouse,
because a checkout's `.env` also serves `make server` on the host. Compose reads the
`.env` beside the first `-f` file, here the root one; `--env-file .env` only says so
explicitly.

| Service      | Container     | Ports                          | Role                                                                                     |
| ------------ | ------------- | ------------------------------ | ---------------------------------------------------------------------------------------- |
| `clickhouse` | `loom-ch`     | `8123` (HTTP), `9000` (native) | ClickHouse OSS (pinned image), database `loom`, user `loom`, password `loomdev`.         |
| `loom`       | `loom-server` | `7777`                         | HTTP, MCP, `/me` and the background workers in one process, built from the `Dockerfile`. |

The bundled ClickHouse starts with the files in `deploy/clickhouse-config/`:

| File                                    | Why                                                                                                                     |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `lemmatizers.xml` + `deploy/lemmagen3/` | The English lemmatizer for ad-hoc `lemmatize('en', …)` queries. Loom lemmatizes in Python.                              |
| `nlp_user_settings.xml`                 | The `loom_reader` user and profile (below), the per-tenant quota, and the experimental NLP and AI SQL flags.            |
| `keeper.xml`                            | An embedded single-node Keeper, so the `KeeperMap` tables work.                                                         |
| `named_collections.xml`                 | Named collections for ClickHouse's `ai*` SQL functions. Git-ignored; defines none as shipped; keys come via `from_env`. |

- **Read-only user.** Loom runs the reads a caller shapes (`code.grep`, a patterned
  `code.search`, `memory.aggregate`, `memory.record.query`) as `LOOM_CH_READER_USER`,
  which `compose.yaml` and the local stack set to `loom_reader` (set it in `.env` for
  `dev/docker-compose.cloud.yml`), so none of them can write,
  change schema or grant. It has SELECT on `loom.*` only (it cannot read
  `system.query_log`) and a read-only profile whose time, memory and rows-read caps each
  have a ceiling no statement can raise and a floor no statement can go under (time:
  0.05 s, `code.grep`'s statement floor), not even with a `SETTINGS` clause. Its password
  is `LOOM_CH_PASSWORD`. The bundled server defines it in `nlp_user_settings.xml`;
  `loom ch-setup` creates the same user and profile with SQL on any other server, from
  the current `LOOM_CH_CALLER_SHAPED_*` values. Raising one of those settings on the
  bundled server needs the XML profile raised too; elsewhere, re-run `ch-setup`. After
  changing `LOOM_CH_PASSWORD`, which the reader shares, drop the reader
  (`DROP USER loom_reader; DROP SETTINGS PROFILE loom_reader`) and re-run `ch-setup`: it
  replaces only a reader that logs in with the current password. The server probes the
  user at boot; `/v1/health` and `loom doctor` report `ch_reader_user_failing` when
  ClickHouse refuses it. Set empty (`LOOM_CH_READER_USER=` in `.env`), `ch-setup` makes
  no reader and the reads run as `LOOM_CH_USER`.
- **Ports.** If `8123` or `9000` is taken, set `LOOM_CH_HOST_PORT` or
  `LOOM_CH_NATIVE_HOST_PORT` in `.env`. These are compose variables for the host side
  only; containers always talk to `clickhouse:8123`.
- **Volumes.** `loom-ch-data` (ClickHouse) and `loom-blob-data` (document originals,
  `LOOM_BLOB_DIR`). `make down` keeps them; `make down-all` deletes them.
- **No external systems.** The local stack runs no Langfuse or ClickStack;
  the erasure steps for them do nothing until configured.
- **From source.** `make server` runs uvicorn on `:7777` with reload against the
  ClickHouse `.env` names (localhost:8123 when unset). Build the dashboard first with
  `make frontend-build`, or `/me` returns 503. Contributor setup is in
  [local development](../development/local-development.md).

### Web and workers apart

```bash
cp .env.example .env    # then set LOOM_CH_*, auth and providers (below); COMPOSE_PROFILES=
make dev-cloud
```

`dev/docker-compose.cloud.yml` builds the image from the checkout and runs two
containers from it against the ClickHouse in `.env` (run `loom ch-setup` against it once):

| Container      | Role                                                 | Key environment                                    |
| -------------- | ---------------------------------------------------- | -------------------------------------------------- |
| `loom-server`  | web tier on `:7777`: HTTP, MCP, `/me`                | `LOOM_BACKGROUND_WORKERS=off`                      |
| `loom-workers` | background workers; `/v1/health` on internal `:7778` | `LOOM_BACKGROUND_WORKERS=on`, `LOOM_WEB_WORKERS=1` |

A long worker pass never shares an event loop with client requests, so it cannot show up
as request latency. Both containers mount the same `.env`, the blob volume and the code
mirror volume. Whichever boots first applies pending migrations
([upgrades](./upgrades.md#how-migrations-run)).

## How settings are read

- Every setting is an environment variable `LOOM_<FIELD>`. Provider keys keep their own
  names (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`). The server reads them
  once at startup; a change needs a restart.
- `.env` in the working directory is read by default. `LOOM_ENV_FILE` names another
  file; an empty value reads none. `.env.example` is the commented template.
- The compose file's `environment:` wins over `.env`. `dev/docker-compose.dev.yml` pins
  the ClickHouse connection and `LOOM_SELF_SIGNUP=open`, so those lines in `.env` do
  nothing on the local stack. `compose.yaml` and `dev/docker-compose.cloud.yml` take
  them from `.env`.
- Validate before every start. This catches quoting errors in JSON-valued settings
  without starting anything:

  ```bash
  docker compose --env-file .env -f dev/docker-compose.cloud.yml config >/dev/null
  ```

- `/v1/health` and `loom doctor` report the resolved providers, the identity mode and
  every configuration problem with its fix.

## Required environment

A deployment anyone else can reach needs at least:

```bash
LOOM_CH_HOST=...
LOOM_CH_PORT=8443                  # 8123 for plain HTTP to a self-managed server
LOOM_CH_USER=...
LOOM_CH_PASSWORD=...
LOOM_AUTH_REQUIRED=true
LOOM_IDENTITY_PROVIDER=trust-network        # or gateway
LOOM_JWT_SECRET=<32+ random bytes>          # python -c 'import secrets; print(secrets.token_urlsafe(32))'
LOOM_PLATFORM_ADMINS=alice                  # login handles / SSO emails
LOOM_REQUIRE_REAL_PROVIDERS=true            # a fake or keyless provider makes /v1/health degraded
# providers: see below
```

### ClickHouse connection

| Variable           | Default     | Set it to                                                                                |
| ------------------ | ----------- | ---------------------------------------------------------------------------------------- |
| `LOOM_CH_HOST`     | `localhost` | Your service, outside the local stack.                                                   |
| `LOOM_CH_PORT`     | `8123`      | `8443` for ClickHouse Cloud or any HTTPS endpoint.                                       |
| `LOOM_CH_SECURE`   | unset       | `true` for HTTPS on a port other than `8443` (unset infers TLS from the port).           |
| `LOOM_CH_USER`     | `loom`      | A user that may create tables in the `loom` database. Cloud's default user is `default`. |
| `LOOM_CH_PASSWORD` | `loomdev`   | A real password. The default is a development placeholder.                               |
| `LOOM_CH_DATABASE` | `loom`      | Leave it. It is only the connection's default database; see below.                       |

**The schema always lives in a database named `loom`.** The migrations ledger
(`loom._migrations`) and the server's queries address `loom.<table>` directly, whatever
`LOOM_CH_DATABASE` says. Two Loom deployments cannot share one ClickHouse service by
renaming the database; give each its own service.

### Identity and authentication

| Variable                 | Default                                | Set it when                                                                                                             |
| ------------------------ | -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `LOOM_AUTH_REQUIRED`     | `true` (the stock `.env` sets `false`) | Keep `true` for anything other people can reach.                                                                        |
| `LOOM_IDENTITY_PROVIDER` | empty (inferred)                       | Always, in a deployment: `trust-network` or `gateway`. `single-user` turns auth off whatever `LOOM_AUTH_REQUIRED` says. |
| `LOOM_SELF_SIGNUP`       | `off`                                  | `open` enables browser sign-up at `/me` (`trust-network`).                                                              |
| `LOOM_SELF_SIGNUP_ORG`   | `dev`                                  | You want signed-up accounts in your own org.                                                                            |
| `LOOM_DEFAULT_NAMESPACE` | `local`                                | Renaming the one namespace `single-user` mode serves. Ignored when auth is required.                                    |
| `LOOM_JWT_SECRET`        | the checked-in development secret      | Always, when auth is on.                                                                                                |
| `LOOM_PLATFORM_ADMINS`   | empty                                  | Naming the operators. It is the only path to `platform_admin`.                                                          |
| `LOOM_TRUSTED_CALLERS`   | empty                                  | In `gateway` mode, a subject may send `X-Loom-On-Behalf-Of` to act for an end user.                                     |

- **`LOOM_JWT_SECRET`** signs every token (HS256; RS256 is available). A deployment
  (`LOOM_DEPLOYMENT=1`, set by the compose files) with auth on refuses to start with the
  checked-in value. Only the local stack (`LOOM_INSTANCE_LABEL=deploy:dev`) may use it;
  other processes are flagged by `/v1/health` (`dev_jwt_secret`) and `loom doctor`.
- **Rotating the signing key.** Replacing `LOOM_JWT_SECRET` invalidates every issued
  token and every private-repository token and tracker-source credential encrypted under
  a key derived from it. To rotate without that, set `LOOM_JWT_SECRETS_JSON` (a
  `kid → secret` map) and `LOOM_JWT_ACTIVE_KID`: new tokens use the active kid, old ones
  verify by the `kid` in their header.
- **`LOOM_PLATFORM_ADMINS`.** Tokens and grants carry only `viewer`, `writer` or
  `admin` on namespaces. A login handle promotes that password account; an email
  promotes only an OIDC sign-in whose provider verified it (a self-signup email never
  does). `loom set-host <handle-or-email>` writes the variable into `.env` (`--append`
  extends the list); the person signs in again to pick up the role.

### Public URL and clients

| Variable               | Default          | Set it when                                                                                                             |
| ---------------------- | ---------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `LOOM_PUBLIC_BASE_URL` | empty            | Always, off loopback: a non-loopback `LOOM_BIND_HOST` refuses to start without it. Empty uses the request's own `Host`. |
| `LOOM_BIND_HOST`       | `127.0.0.1`      | Loom must be reachable by others. In a container it is the address the port is published on.                            |
| `LOOM_TRUSTED_PROXIES` | `127.0.0.1`      | A proxy in front of Loom: its addresses or CIDRs. Only those peers' `X-Forwarded-For`/`-Proto` are honoured.            |
| `LOOM_ALLOW_INSECURE`  | `false`          | Never on a reachable port. Lets a non-loopback bind start with auth off or the dev JWT secret.                          |
| `LOOM_OAUTH_ENABLED`   | `false`          | You want one-click MCP connect (OAuth 2.1 browser sign-in instead of a pasted token).                                   |
| `LOOM_OAUTH_ISSUER`    | empty            | OAuth is on: the public HTTPS base URL the discovery metadata advertises. Empty derives it per request.                 |
| `LOOM_CORS_ORIGINS`    | empty (CORS off) | A browser application on another origin calls Loom directly. List exactly those origins; `*` refuses to start.          |

OAuth sign-in through your identity provider also needs `LOOM_OAUTH_LOGIN_PROVIDER=oidc`
and the `LOOM_OIDC_*` settings ([auth settings](../reference/configuration.md#auth)).

### Process and scale

| Variable                         | Default                     | Set it when                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| -------------------------------- | --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_BACKGROUND_WORKERS`        | `auto`                      | `off` on web replicas, `on` in the one workers container. `auto` elects one process per host by a file lock.                                                                                                                                                                                                                                                                                                                                        |
| `LOOM_WEB_WORKERS`               | image: one per CPU, up to 8 | uvicorn processes per container. Unset, the image starts one per CPU the container may use (its cpuset, lowered by a `--cpus` quota), at most one per GiB of its memory and at most 8. Set a number to leave cores for ClickHouse on a shared host. The standalone stack, whose ClickHouse shares the host, defaults to 2 and the web tier of `dev/docker-compose.cloud.yml` and `dev/docker-compose.colour.yml` to 1, unless `.env` names a count. |
| `LOOM_REPLICA_COUNT`             | `1`                         | Several web replicas: rate limits are per replica and divided by this count.                                                                                                                                                                                                                                                                                                                                                                        |
| `LOOM_LATENCY_BUDGET_MULTIPLIER` | `1.0`                       | ClickHouse is across a WAN: about `20`, or every call reports a budget violation.                                                                                                                                                                                                                                                                                                                                                                   |
| `LOOM_LOG_FORMAT`                | `text`                      | `json` for a log pipeline.                                                                                                                                                                                                                                                                                                                                                                                                                          |

Runtime overrides set through one web process bind every process of that server on its
next request. Budget overrides (`admin/budget.set`) stay within the server: for several
web replicas, set budgets with `LOOM_LATENCY_BUDGET_MULTIPLIER` and
`LOOM_LATENCY_BUDGET_OVERRIDES_BY_CONSUMER`. Rate-limit overrides (`admin/ratelimit.set`)
follow `LOOM_RATELIMIT_SHARED_PATH`: every server sharing that bucket table shares them.

A process started outside the compose files (`make server`, `loom serve`, a test server)
must set `LOOM_INSTANCE_LABEL=<who>:<what>:<port>` or it refuses to boot. The compose
files set `LOOM_DEPLOYMENT=1` and label their services `deploy:web`, `deploy:workers`
(with `:<colour>` in the zero-downtime pair) or `deploy:dev`, so leave `LOOM_DEPLOYMENT`
out of `.env`.

## ClickHouse requirements

Loom needs no server config of its own: the table settings it relies on are set on its
tables (`031_table_settings.sql`), and `loom ch-setup` makes the database and the reader
user with SQL.

| Requirement               | Why                                                                                                                                                                                                                                                 | ClickHouse Cloud                                 | Self-managed                                                                                                                                                                                                                                                  |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Version 26.4 or later     | the oldest server that applies every migration (26.3 still gates `stem()`); boot refuses an older one                                                                                                                                               | the service's version; the oldest replica counts | upgrade the server. CI tests only the latest release (`dev/clickhouse_release.sh`); a release bundle names the one that was latest when it was built                                                                                                          |
| Grants                    | everything on `loom.*` (`GRANT ALL`, or `GRANT CURRENT GRANTS` on Cloud): the migrations create, alter and drop tables and views, and Loom rebuilds tables and refreshes views at runtime; `SELECT` on the system tables it reads; `READ ON REMOTE` | the user `ch-setup --print-sql` grants           | an admin, or the same SQL                                                                                                                                                                                                                                     |
| Keeper                    | the `KeeperMap` migration lock, the task-inbox lease table, the run id claims, the OAuth exchange claims (one exchange per code or refresh token) and the grant change claims (one change per grant)                                                | present                                          | optional: without it the runner uses a lease row and skips `002_task_leases.sql`, `025_agent_run_claims.sql`, `028_oauth_claims.sql` and `029_grant_claims.sql`; OAuth exchanges then run unclaimed and grant changes are kept apart within each process only |
| `Quantized` codec (26.8+) | codes-first vector search: a fresh install creates its vector columns coded when `LOOM_EMBEDDING_DIM` is a multiple of 8                                                                                                                            | where the service version has it                 | nothing to configure; an older server gets plain vectors and full-precision scans                                                                                                                                                                             |
| `aiEmbed` (26.7+)         | only `loom reembed --in-database`                                                                                                                                                                                                                   | where the service version has it                 | needs a named collection (`named_collections.xml.template`)                                                                                                                                                                                                   |

- **Newer servers.** Features past the floor are probed where they are used: the
  `Quantized` codec, `aiEmbed`, lightweight `UPDATE` (Loom falls back to a mutation where
  it is off). Before 26.8 a vector read can leave wrong entries in the query condition
  cache, so every Loom session on an older server turns the cache off when it connects.
- **Run ids without Keeper.** Without Keeper (or while Keeper fails) a narrowed
  token's `run.begin` with a new caller-supplied `run_id` gets a server-made id
  instead, so a client that makes the id itself must use the returned `run_id` (the
  Claude Code and Codex plugins switch to it once run.begin answers). Known gap: a namespace-wide caller whose claim
  hits a Keeper error while a narrowed user wins the claim on the same new id at the
  same moment can end up sharing that id with them.
- **Another ClickHouse image.** An installed release bundle runs the ClickHouse release
  that was latest when it was built, and a newer Loom release moves it; the repository's
  compose files run `latest`, as CI does. Set
  `LOOM_CLICKHOUSE_IMAGE` in `.env` to run another, such as a release tag or
  `clickhouse/clickhouse-server:head` for a build of ClickHouse master. CI does not test
  master, and Loom uses experimental features master can change. A newer server can also
  write data in a format an older release cannot read, so switching back to an older release may
  need a fresh ClickHouse volume.
- **ClickHouse Cloud.** Loom's DDL uses plain `MergeTree` engines, which Cloud runs as
  `SharedMergeTree`, so replication and durability come with the service. The
  experimental NLP flag is read-only there; the runner drops it, and `stem()` runs
  without it.
- **Self-managed.** A single node is durable only to its own disk and has no
  point-in-time recovery; high availability is a Keeper ensemble and
  `ReplicatedMergeTree`, an operator project.

How Loom uses ClickHouse is in [ClickHouse](../architecture/clickhouse.md).

## Providers

For ready-to-use configurations and the distinction between agent subscriptions and
server API keys, start with [Configure models and API keys](../getting-started/models-and-keys.md).

The embedder turns text into vectors. The LLM extracts facts, tags entities, judges
conflicts and writes `memory.ask` answers. Both default to fakes that need no network.

| Embedder (`LOOM_EMBEDDING_MODEL`) | Width (`LOOM_EMBEDDING_DIM`) | Needs                                                  |
| --------------------------------- | ---------------------------- | ------------------------------------------------------ |
| `fake-ngram-3072`                 | 3072                         | nothing; character n-grams, for tests and wiring only  |
| `local` / `local:bge-small`       | 384                          | the `local-embedder` extra (fastembed, CPU); see below |
| `local:bge-base`                  | 768                          | same                                                   |
| `local:minilm`                    | 384                          | same                                                   |
| `text-embedding-3-small`          | 1536                         | `OPENAI_API_KEY`                                       |
| `text-embedding-3-large`          | 3072                         | `OPENAI_API_KEY`                                       |
| `gemini-embedding-001`            | 3072                         | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`)                 |

| LLM (`LOOM_LLM_PROVIDER`) | Needs                                | Default `LOOM_LLM_MODEL` |
| ------------------------- | ------------------------------------ | ------------------------ |
| `fake`                    | nothing; extraction produces nothing | `fake-echo-1`            |
| `openai`                  | `OPENAI_API_KEY`                     | `gpt-6-luna`             |
| `anthropic`               | `ANTHROPIC_API_KEY`                  | `claude-haiku-4-5`       |

- **Width.** `LOOM_EMBEDDING_DIM` must equal the model's width. It is fixed when the
  tables are created. The server probes the embedder at boot; a mismatch shows as the
  `embedding_dim_mismatch` problem.
- **Local embedder.** The image includes it only when built with a `local*` value of
  `LOOM_EMBEDDING_MODEL` (the compose files pass it as a build argument).
- **Inference.** When `LOOM_LLM_PROVIDER` is empty it is inferred once at load: from the
  model name (`gpt*`, `o1*`, `o3*`, `o4*`, `chatgpt*` → openai, `claude*` → anthropic,
  `fake*` → fake), else from the key present (`OPENAI_API_KEY`, then
  `ANTHROPIC_API_KEY`), else fake. An explicit value is never overridden. The boot log
  and `loom doctor` (`llm_provider`) say what was chosen and why.
- **Missing key.** A configured provider without its key falls back to the fake client
  and reports `embedder_key_missing` or `llm_key_missing`. With
  `LOOM_REQUIRE_REAL_PROVIDERS=true` those become errors and `/v1/health` reads
  `status: degraded`.
- **Fallback.** `LOOM_LLM_FALLBACK_MODEL` names a second model (any provider) that
  answers when the primary keeps failing after retries.
- **Changing defaults.** Run `make bench-quick` before changing the model or embedder
  ([benchmarks](../benchmarks/running.md)). A new embedder is a new vector space: run
  `loom reembed` afterwards ([maintenance](./maintenance.md#re-embed-and-re-extract)).

### Models per job

Every setting that names a model is in one file,
[`server/settings/_models.py`](../../server/settings/_models.py), one per job. Set any
of them in `.env` to move that job to another model. The provider comes from the name,
so a job can run on Claude while the rest run on GPT, as long as both keys are set. An
empty value uses `LOOM_LLM_MODEL`.

| Job                                          | Setting                                 | Default           |
| -------------------------------------------- | --------------------------------------- | ----------------- |
| Extraction, conflict judge, rerank, entities | `LOOM_LLM_MODEL`                        | provider default  |
| Answer when the primary keeps failing        | `LOOM_LLM_FALLBACK_MODEL`               | off               |
| Embeddings                                   | `LOOM_EMBEDDING_MODEL`                  | `fake-ngram-3072` |
| `memory.ask` synthesis (strong tier)         | `LOOM_MEMORY_ASK_SYNTH_MODEL`           | `LOOM_LLM_MODEL`  |
| `memory.ask` cheap tier                      | `LOOM_MEMORY_ASK_SYNTH_MODEL_CHEAP`     | off               |
| `memory.ask` mid tier                        | `LOOM_MEMORY_ASK_MID_MODEL`             | off               |
| Coding-capture durability check              | `LOOM_MEMORY_EXTRACT_VERIFY_MODELS`     | `claude-opus-5-5` |
| Profile fold                                 | `LOOM_PROFILE_CONSOLIDATION_MODEL`      | `LOOM_LLM_MODEL`  |
| Reflection distillation                      | `LOOM_REFLECTION_DISTILL_MODEL`         | `LOOM_LLM_MODEL`  |
| Session briefs                               | `LOOM_SESSION_BRIEF_LLM_MODEL`          | `gpt-6-luna`      |
| `code.ask` synthesis                         | `LOOM_CODE_ASK_LLM_MODEL`               | `LOOM_LLM_MODEL`  |
| `code.search` HyDE passage                   | `LOOM_CODE_SEARCH_HYDE_MODEL`           | off               |
| `code.search` concept expansion              | `LOOM_CODE_SEARCH_CONCEPT_EXPAND_MODEL` | off               |
| `code.bisect_candidates` rerank              | `LOOM_CODE_BISECT_LLM_RERANK_MODEL`     | `gpt-6-luna`      |
| File and module summary cards                | `LOOM_CODE_SUMMARY_MODEL`               | `LOOM_LLM_MODEL`  |
| Duplicate check rerank                       | `LOOM_TRACKER_DUPCHECK_RERANK_MODEL`    | `gpt-6-luna`      |
| Duplicate check judge                        | `LOOM_TRACKER_DUPCHECK_JUDGE_MODEL`     | `LOOM_LLM_MODEL`  |
| Sampled retrieval judge                      | `LOOM_RETRIEVAL_JUDGE_MODEL`            | `gpt-6.1-sol`     |

For example, a deployment with both keys set can run `gpt-6-luna` as `LOOM_LLM_MODEL`
and for the cheap `memory.ask` tier and HyDE, `claude-opus-5-5` for strong `memory.ask`
synthesis, and `gpt-6.1-sol` for `code.ask`.

Provider internals (retry, fallback, caching) are in
[LLM architecture](../architecture/llm-architecture.md).

### Memory behaviour

These defaults decide what a user sees first: whether a write is flagged and whether a
search abstains.

| Variable                                      | Default | Effect                                                                                                                             |
| --------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_ADAPTIVE_THRESHOLDS`                    | `true`  | Derives the search and conflict gates from the embedder's cosine distribution at boot. Leave it on.                                |
| `LOOM_CONFLICT_THRESHOLD`                     | `0.85`  | Fallback cosine at or above which a write is flagged `conflicted`. `1.0` disables. A namespace can override it.                    |
| `LOOM_MEMORY_CONFLICT_JUDGE`                  | `false` | With a real LLM, turn it on: one call per flagged write labels the peers, so restatements merge instead of piling up as conflicts. |
| `LOOM_MEMORY_SEARCH_MIN_SIMILARITY`           | `0.22`  | Fallback cosine floor under which hits are dropped.                                                                                |
| `LOOM_MEMORY_SEARCH_ABSTAIN_THRESHOLD`        | `0.3`   | Fallback top-hit similarity under which a search abstains.                                                                         |
| `LOOM_MEMORY_SEARCH_TRUST_WEIGHT`             | `0.0`   | Weight of provenance (`source_class`, `verified`) in ranking; `0` is off.                                                          |
| `LOOM_MEMORY_ASK_EXCLUDE_UNVERIFIED_EXTERNAL` | `true`  | Keeps unattested `external` rows out of `memory.ask` synthesis; they still appear as hits.                                         |
| `LOOM_ENTITY_EXTRACT_ENABLED`                 | `true`  | Background LLM entity tagging. Turn it off to save LLM spend on a quiet instance.                                                  |
| `LOOM_DEFAULT_DAILY_COST_CAP_USD`             | `1.0`   | Per-namespace rolling 24-hour LLM spend cap; `0` disables. A namespace overrides it with `daily_cost_cap_usd`.                     |

The fallback gates apply when adaptive thresholds are off or the embedder is a fake. If
you pin `LOOM_ADAPTIVE_THRESHOLDS=false`, re-bench them after every embedder change.
Over the cost cap, LLM-backed workers skip the namespace; `memory.ask` and
`knowledge.synthesize` abstain with `budget_capped`; `code.search` query expansion and
`code.ask` synthesis are skipped. Plain reads and writes are never refused for cost
([cost and usage](../features/observability/cost-and-usage.md)).

### Turning on an LLM-backed worker

Several workers are off by default because they spend on the LLM
([workers reference](../reference/workers.md)).

1. Configure a real LLM provider.
2. Set the worker's `LOOM_*_ENABLED` flag on the one workers container and recreate it.
3. For per-namespace workers, opt the namespace in (`namespace.register` or the
   namespace settings): `consolidation_enabled` for `profile_consolidation` and
   `entity_communities`.

Several Looms can share one ClickHouse service and serve the same data from
their own endpoints: another network or region, or more capacity. The main Loom
does the once-per-service work (migrations, workers, mirrors); the others are
[read-replicas](read-replicas.md), started or stopped at any time, serving the
same data and ops and accepting the same tokens.

**Run exactly one workers process per deployment.** The `auto` lock at
`LOOM_SUPERVISOR_LOCK_PATH` is host-local, so with several hosts set
`LOOM_BACKGROUND_WORKERS=off` everywhere but one. A second copy of the LLM workers
doubles their scans and their spend.

### Optional planes and integrations

Each is off until configured; `.env.example` has a commented block for each.

| Area                     | Start with                                                                                                         | Read                                                                                                        |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| Code index               | `LOOM_CODE_REPO` or `LOOM_CODE_REPOS_JSON`, `LOOM_CODE_INGEST_ENABLED`, `LOOM_MCP_CODE_TOOLS`                      | [Repositories](../features/code-index/repositories.md), [code-index operations](./code-index-operations.md) |
| Tracker records          | `LOOM_TRACKER_ORG`, `LOOM_TRACKER_SOURCES_JSON`, `LOOM_TRACKER_CREDENTIALS_JSON`                                   | [Tracker](../features/tracker/overview.md)                                                                  |
| Telemetry                | `LOOM_OTEL_EXPORTER`: `ch` (the `otel_*` tables), `otlp` (a collector); empty picks `otlp` when an endpoint is set | [Telemetry](../features/observability/telemetry.md)                                                         |
| Erasure in other systems | `LOOM_LANGFUSE_URL`, `LOOM_CLICKSTACK_DATABASE` and their credentials                                              | [Retention and erasure](../features/memory/retention-and-erasure.md)                                        |

## Identity modes

**OSS supports both single-user and multi-user deployments.** The stock Docker installer
selects `single-user` and binds to `127.0.0.1`: one person, no login, with multiple agents
sharing local context. For a shared team server, opt into `trust-network` or `gateway`
with `LOOM_AUTH_REQUIRED=true`, namespace grants and [TLS](#proxy-and-tls).
Agent count and user count are independent; multiple agents do not require multi-user mode.

`LOOM_IDENTITY_PROVIDER` selects the mode. When it is empty: auth off → `single-user`;
auth on with `LOOM_SELF_SIGNUP=open` → `trust-network`; auth on otherwise → `gateway`.
An explicit `single-user` value disables authentication even when
`LOOM_AUTH_REQUIRED=true`; change both settings when enabling team access.
The model is in [identities and namespaces](../concepts/identities-and-namespaces.md#identity-modes);
claims and grants are in [tokens and grants](../guides/tokens-and-grants.md).

| Mode            | Who authenticates people                  | Tokens come from                           | Account lifecycle                   | Use for                        |
| --------------- | ----------------------------------------- | ------------------------------------------ | ----------------------------------- | ------------------------------ |
| `single-user`   | nobody                                    | none; every call is the default identity   | n/a                                 | one person on one machine      |
| `trust-network` | Loom (email + argon2id password at `/me`) | sign-in (365-day token), `loom mint-token` | first claim wins; operator releases | a team that trusts its members |
| `gateway`       | your identity layer                       | your minter, signed with the shared secret | yours                               | an organisation with SSO       |

### Trust-network: self-signup

```bash
LOOM_AUTH_REQUIRED=true
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_SELF_SIGNUP=open
LOOM_JWT_SECRET=<...>
```

When switching an installed local stack, edit `~/.loom/.env` (or the checkout's `.env`)
and keep its existing random `LOOM_JWT_SECRET`; replacing it would invalidate tokens and
sealed credentials. Recreate the Loom container to load the changes, as shown in the
[quickstart](../getting-started/quickstart.md#2-open-the-dashboard). Preserve the data
volumes. Signing up does not automatically give a new account access to existing local
memories: provision the appropriate namespace grants through an operator.

People sign up at `/me`. The first person to claim a namespace name becomes its admin.
Agents sign themselves up with `loom init --agent <name>`, which returns a claim code a
person later uses to take ownership.

There is no email verification and no self-serve password reset. For a lost account the
operator runs `loom claim-release --org <org> --namespace <ns>`: it revokes the grants,
clears the owner and deactivates the namespace. The memories and the name stay, so a new
signup cannot take the name and become admin over the old data. `erasure.request` with
scope `namespace` removes the data but keeps the registry row. For a full account
lifecycle, use `gateway`.

### Single-user: laptop, no auth

```bash
LOOM_AUTH_REQUIRED=false
LOOM_IDENTITY_PROVIDER=single-user
LOOM_DEFAULT_NAMESPACE=local   # optional
```

Every call resolves to `LOOM_DEFAULT_NAMESPACE` in org `LOOM_SELF_SIGNUP_ORG` (default
`dev`). The server provisions it at boot as a real namespace with an admin grant, so it
stays administrable if auth is turned on later. Changing the setting leaves the previous
namespace's memories in place but out of view (the boot log names them).

> **Warning.** Never expose `single-user` mode to a network. Anyone who can reach the
> port is the admin of its namespace.

### Gateway: an upstream identity provider

```bash
LOOM_AUTH_REQUIRED=true
LOOM_IDENTITY_PROVIDER=gateway
LOOM_JWT_SECRET=<shared with the upstream minter>
LOOM_TRUSTED_CALLERS=oauth2-proxy,my-mcp-gateway
```

Your authentication layer (an OAuth2 proxy, an OIDC or SAML provider, a custom minter)
authenticates the person and mints a short-lived HS256 Loom token with `sub`, `org`,
`namespaces`, `roles` and `exp`, signed with the shared secret. Loom verifies it on every
call and never sees a password. A subject in `LOOM_TRUSTED_CALLERS` may send
`X-Loom-On-Behalf-Of` to act for another principal; the real caller is recorded on every
ledger row.

## Proxy and TLS

The image serves plain HTTP. Put a TLS-terminating proxy or load balancer in front.

- uvicorn honours `X-Forwarded-For` and `X-Forwarded-Proto` only from the peers in
  `LOOM_TRUSTED_PROXIES` (default `127.0.0.1`); set it to your proxy's address or
  network. `X-Forwarded-Host` is never read: client-facing URLs come from
  `LOOM_PUBLIC_BASE_URL`.
- With `compose.yaml` or `dev/docker-compose.cloud.yml` and a proxy on
  the host, the proxy reaches Loom through the published port, so Loom sees the Docker
  network's gateway as the peer, not `127.0.0.1`. Set `LOOM_TRUSTED_PROXIES` to that
  gateway, which
  `docker network inspect <project>_default -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}'`
  prints. Left at the default, forwarded headers are dropped, URLs say `http://` and
  every client shares the proxy's address for rate limits and sign-in lockouts. Every
  process on the host also arrives from that gateway, so trust it only on a host you
  control.
- A non-loopback `LOOM_BIND_HOST` refuses to start without `LOOM_PUBLIC_BASE_URL`, and
  with auth off or the dev JWT secret unless `LOOM_ALLOW_INSECURE=1`. With auth off, any
  request whose `Host` is not a loopback host or the public URL's host gets `421` (DNS
  rebinding). A request from a page on another origin (an `Origin` that is not the
  server's own, or `Sec-Fetch-Site: cross-site` / `same-site`) gets `403` unless
  `LOOM_CORS_ORIGINS` lists that origin: every request with auth off except following a
  link to a page, and every non-GET request with auth on, since in proxy mode the proxy
  adds the bearer itself. With `LOOM_PUBLIC_BASE_URL` set the `Origin` must be exactly
  that scheme, host and port; without it, an `Origin` with the request's `Host` (host
  and port, any scheme) counts. A proxy that rewrites `Host` needs
  `LOOM_PUBLIC_BASE_URL` set for the dashboard's own calls to count as same-origin.
- In a container the server listens on every interface, so the check trusts the
  declared `LOOM_BIND_HOST`, not the socket: it must match the address the compose
  file publishes the port on.
- Raise the file-descriptor limit on anything that holds many connections; the compose
  files set `nofile` to 65536.
- `/docs`, `/redoc`, `/openapi.json` and `/v1/api/openapi.json` answer without a token.
  They describe the API and return no data. There is no setting to turn them off; deny
  those paths at the proxy if the API surface should not be visible.

Carry the choices in `deploy/proxy/nginx.conf` over to your own front end:

| Setting                                 | Why                                                                                           |
| --------------------------------------- | --------------------------------------------------------------------------------------------- |
| `client_max_body_size 64m`              | Above `LOOM_MAX_REQUEST_BYTES` (32 MiB), so Loom's own JSON 413 answers an oversized request. |
| `proxy_read_timeout 600s`               | A strong-tier `memory.ask` can run for minutes.                                               |
| `proxy_buffering off`                   | Streams responses as Loom writes them.                                                        |
| `proxy_next_upstream off`               | Never replays a POST on another upstream.                                                     |
| `X-Forwarded-For $remote_addr`          | Replaces any client-sent chain, which this hop cannot tell from a forged one.                 |
| `X-Forwarded-Proto` from the terminator | Keeps the client-facing scheme, so Loom hands clients `https://` URLs.                        |
| `access_log off`                        | The request ledger (`loom.agent_events`) is the access log.                                   |

## Container images

The repository [`Dockerfile`](../../Dockerfile) builds the one image every rung uses:

| Stage              | Produces                                                                                                                                 |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `py-builder`       | the server with the `code` and `lemma` extras (plus `local-embedder` when `LOOM_EMBEDDING_MODEL=local*`)                                 |
| `frontend-builder` | the `/me` bundle in `frontend/dist/` (without it `/me` returns 503; `/v1/admin/*` still answers)                                         |
| `go-builder`       | a pinned, checksum-verified Go toolchain and scip-go for the Go compiler tier                                                            |
| `node-builder`     | pinned Node, scip-typescript and pnpm for the TypeScript compiler tier; scip-python for the Python tier                                  |
| runtime            | Python 3.12, non-root user `loom` (uid/gid 999), `git`, the `dev/` operator tools, the migrations, `bench/fixtures` for `code.calibrate` |

The runtime exposes 7777, has a built-in `/v1/health` healthcheck (30 s interval), and
runs `scripts/serve.sh`, which starts `uvicorn server.main:app` with `LOOM_WEB_WORKERS`
processes (unset: one per CPU the container may use, at most one per GiB of memory and at most 8). `LOOM_UVICORN_LOOP`
(`asyncio`, `auto`, `uvloop`) and `LOOM_GRACEFUL_SHUTDOWN_S` (default 10) are read by
that script. Unset, the loop is `asyncio`, in `loom serve` and `make server` too: the
workers and the code ops spawn git, and uvloop forks the whole process for each spawn,
holding its event loop for tens of milliseconds once the heap is a few GB.
`LOOM_BUILD_COMMIT`, stamped at build time, is what `/v1/health` reports as
`build_commit`.

The web processes share small files (rate-limit table, runtime overrides, graph marks) in
`/run/loom`, which the compose files mount as a tmpfs owned by the image's user. With a
plain `docker run`, mount it the same way, `--tmpfs /run/loom:uid=999,gid=999,mode=0700`:
a bare `--tmpfs /run/loom` is root's and open to everyone, so the server passes it over
for `/tmp/loom-<uid>` in the container's writable layer. There, as without a tmpfs, the
files stay in the writable layer, and a restarted container
does not remove an earlier one's; remove the container (`docker run --rm`) rather than
restarting it. With a read-only root filesystem and no tmpfs, several web processes have
nowhere to share: with `LOOM_WEB_WORKERS` unset `scripts/serve.sh` starts one and says
so, and with it set above 1, or with `LOOM_RUNTIME_DIR` pinned to a directory it cannot
use, it stops and says why. Mount the tmpfs, or pin `LOOM_RUNTIME_DIR` to a writable
directory only the server's user can enter (mode 0700: a looser one is refused, not
changed).

Every release publishes a multi-arch image (linux/amd64 and linux/arm64) to
`ghcr.io/clickhouse/loom`, tagged `X.Y.Z`, `X.Y` and, for a stable release, `latest`.
Images include build provenance and an SBOM. The one-line installer pins `X.Y.Z` in its
generated `.env`; pin an immutable version (or digest) in production rather than tracking
`latest` ([release process](../development/release-process.md)).

The matching GitHub Release carries `loom-stack.tar.gz`, its SHA-256 file, and
`install.sh`. The bundle is the supported source-free laptop deployment: it contains
Compose plus the ClickHouse configuration/dictionary the stack needs, while the UI and
server stay in the image. The repository serves `install.sh`; rerunning it preserves
`.env` (apart from advancing the pinned `LOOM_VERSION`) and volumes.

## Zero-downtime pair

`redeploy.sh` runs two copies ("colours") of the stack and switches traffic between them
at a proxy. It deploys the tip of `origin/main`, so an install that pins a release adapts
step 1.

```mermaid
flowchart LR
  C[Clients] --> P["nginx loom-proxy :7777"]
  P -->|"upstream.conf names one"| B["loom-blue :7781 + loom-blue-workers"]
  P -.->|idle or stopped| G["loom-green :7782 + loom-green-workers"]
  B --> CH[(ClickHouse)]
  G --> CH
  B --- V["shared: .env, blob volume, code mirrors, loom-shared"]
  G --- V
```

- `dev/docker-compose.colour.yml`, started once per colour as its own compose project
  (`LOOM_COLOUR=blue|green`, `LOOM_COLOUR_PORT=7781|7782`), gives each colour a web
  container (`loom-<colour>`, workers `off`) and a workers container
  (`loom-<colour>-workers`, workers `auto`, no published port). The colours share
  `.env`, the blob volume, the code mirrors and a `loom-shared` volume with the
  rate-limit table and the supervisor lock.
- `dev/docker-compose.proxy.yml` runs nginx (`loom-proxy`) on host `:7777` with
  `deploy/proxy/nginx.conf`. It forwards to the colour named in
  `var/proxy/upstream.conf` (template: `deploy/proxy/upstream.conf.example`). A swap is
  a file write plus `nginx -s reload`; no port is rebound. nginx re-resolves the colour's
  name through Docker's DNS, so a colour container that restarts on a new address is
  reached again, within about 30 s of coming back (nginx answers 502 meanwhile). The
  `nginx.conf` of a commit from before this re-resolution cannot load the `upstream.conf`
  a newer `redeploy.sh` wrote (`no resolver defined`): after checking such a commit out,
  run its `redeploy.sh` before the proxy restarts.

Once per host, before the first redeploy, load the AppArmor profile the colours run the
repository-job sandbox under, then check it in a throwaway container built from the
checkout about to be deployed
([repository-job sandbox](./code-index-operations.md#repository-job-sandbox) has both
commands):

```bash
sudo install -m644 deploy/apparmor/loom-repo-sandbox /etc/apparmor.d/loom-repo-sandbox
sudo apparmor_parser -r -W /etc/apparmor.d/loom-repo-sandbox
```

[`redeploy.sh`](../../redeploy.sh), run from the deploy checkout:

1. fetches `origin/main` and rebases onto it, refusing a dirty tree that is behind and
   asking before it ships changes to migrations, compose files, the proxy, the
   `Dockerfile`, `.env.example`, the dependency manifests or itself;
2. runs [`dev/apparmor_check.sh`](../../dev/apparmor_check.sh): the colour compose
   names the AppArmor profile `loom-repo-sandbox`, so the host must run AppArmor and
   have the profile loaded, or Docker would refuse to start the colour;
3. runs [`dev/redeploy_preflight.sh`](../../dev/redeploy_preflight.sh): the running
   `build_commit` against the target (exit 3 means nothing to deploy), who holds the
   port, and `loom migrate --dry-run --strict`;
4. builds and starts the idle colour while the active one serves;
5. waits, within `LOOM_REDEPLOY_READY_TIMEOUT_S` (default 180 s), for its `/v1/health`
   to report `status: ok` with an empty `schema.pending`, and for its workers container
   to be healthy;
6. runs [`dev/sandbox_probe.sh`](../../dev/sandbox_probe.sh): the repository-job
   sandbox probe in both new containers. When the sandbox does not run under the profile,
   or a container cannot be inspected, it removes the new colour's containers and exits,
   the old one still serving (until then `LOOM_CODE_SANDBOX_REQUIRED` keeps the new
   workers from running a repository job unsandboxed);
7. rewrites `upstream.conf` and reloads nginx;
8. tags the old colour's image `loom:prev` and stops the old colour (uvicorn drains for
   up to 10 s; `docker stop -t 45`). The old containers are **stopped, not removed**;
9. checks health through `:7777` and records the deploy in `var/deployed.json`.

| Command                    | Does                                                                                     |
| -------------------------- | ---------------------------------------------------------------------------------------- |
| `./redeploy.sh`            | the sequence above; exits without a change when already at `origin/main`                 |
| `./redeploy.sh --force`    | rebuild and swap even with no new commits                                                |
| `./redeploy.sh --rollback` | start the stopped previous colour, health-check it, swap the proxy back (no schema step) |
| `./redeploy.sh --status`   | which colour serves, each colour's build, container state and workers lease              |
| `loom deploy-delta`        | after about 30 minutes: ledger and ClickHouse statement shapes on each side of the swap  |

`LOOM_PREFLIGHT_STRICT=0` turns ledger-only rows and checksum drift into a report instead
of a stop. `LOOM_PUBLIC_HEALTH_URL`, when set, is probed last.

Rollback needs no schema step because every migration is additive
([upgrades](./upgrades.md#rollback)). The stopped colour is the rollback: never run
`docker container prune` on the host
([backup and recovery](./backup-and-recovery.md#the-rollback-colour)). The new colour's
workers take the supervisor lock within one `LOOM_SUPERVISOR_LEASE_RETRY_S` of the old
one stopping.

To adopt the pair from a single container: `redeploy.sh` refuses to start while
`loom-server` holds `:7777`. Bring up `loom-blue`, stop `loom-server`, then start the
proxy (`docker compose -p loom-proxy -f dev/docker-compose.proxy.yml up -d`).

## Production checklist

| Step            | Do                                                                                                                                                                                                                                            |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ClickHouse      | Cloud or a self-managed server, 26.4 or later, prepared with `loom ch-setup` ([requirements](#clickhouse-requirements)). `LOOM_CH_*` set; TLS on. Across a WAN, `LOOM_LATENCY_BUDGET_MULTIPLIER≈20`.                                          |
| Providers       | Real embedder and LLM with keys; `LOOM_REQUIRE_REAL_PROVIDERS=true`. Choose the embedder once. Only the LLM workers you want; check the cost cap.                                                                                             |
| Identity        | `LOOM_AUTH_REQUIRED=true` and `LOOM_IDENTITY_PROVIDER` set explicitly. Platform admins named with `loom set-host`. Know `loom claim-release` before you need it.                                                                              |
| Keys and tokens | A generated `LOOM_JWT_SECRET`; a rotation plan (`LOOM_JWT_SECRETS_JSON`). Service tokens minted with `--ttl-days` and a re-mint schedule; a token without `exp` is retired only by `loom revoke-token`.                                       |
| Proxy           | TLS terminator in front; container port not reachable directly; the nginx choices above; CORS off or listed.                                                                                                                                  |
| Web and workers | Web tier `LOOM_BACKGROUND_WORKERS=off`; exactly one workers process. `.env` validated with `docker compose … config`.                                                                                                                         |
| Deploys         | A pinned image or commit. With the pair, keep the stopped colour. Read the [upgrade procedure](./upgrades.md#upgrade-procedure) once before the first cut.                                                                                    |
| Host            | For the pair, an AppArmor host with `deploy/apparmor/loom-repo-sandbox` loaded and its throwaway-container check passed ([repository-job sandbox](./code-index-operations.md#repository-job-sandbox)).                                        |
| Backups         | Cloud's native backups, or `BACKUP DATABASE` / a backup tool on OSS ([choosing a backup](./backup-and-recovery.md#choosing-a-backup)). Rehearse with `./scripts/ops/backup_drill.sh`; after a restore run `python dev/verify_audit_chain.py`. |
| Monitoring      | Probe `/v1/health`; run `loom doctor` after each configuration change; wire [the alerts](./monitoring.md#what-to-alert-on); set `LOOM_OTEL_EXPORTER` and `LOOM_LOG_FORMAT=json`.                                                              |
| Size            | Scale web replicas and `LOOM_WEB_WORKERS` by signal; set `LOOM_REPLICA_COUNT` ([failure and recovery](../architecture/failure-and-recovery.md), [performance tuning](../guides/performance-tuning.md)).                                       |

## Related

- [Monitoring](./monitoring.md), [upgrades](./upgrades.md),
  [backup and recovery](./backup-and-recovery.md), [troubleshooting](./troubleshooting.md)
- [Security](../architecture/security.md): the threat model
- [Configuration reference](../reference/configuration.md): every setting
