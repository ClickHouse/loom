# Quickstart

This page boots Loom on a laptop and saves a first memory over HTTP, Python and MCP. It
needs no API key and no account. A fresh install of the released stack embeds with a small
local model the image carries, so search returns meaningful hits with no network (an
upgraded one keeps the embedder its `.env` names); its LLM is fake, so extraction and
`memory.ask` answers produce nothing. A checkout's `.env` uses fake providers for both.

**The stock Docker installation is single-user:** no login, localhost-only access and
a default `dev/local` namespace. One person can connect multiple agents without enabling
multi-user mode. **Team deployments are also supported in OSS**, with authenticated
accounts and namespace access controls; [start in multi-user mode](#start-in-multi-user-mode)
or follow [Deployment → Identity modes](../operations/deployment.md#identity-modes).
Both modes support the same Loom features; real LLMs and embeddings need provider
configuration in either mode.

## Prerequisites

| Need                           | For                                                                      |
| ------------------------------ | ------------------------------------------------------------------------ |
| Docker with the compose plugin | The stack: ClickHouse and the Loom server.                               |
| Python 3.12 or newer           | Contributor-only `loom` CLI. The SDK supports 3.11+.                     |
| Node.js 20 or newer            | The TypeScript SDK (optional).                                           |
| Free port `7777`               | Loom. Checkout development also exposes ClickHouse on `8123` and `9000`. |
| `jq`                           | Reading the `curl` output in step 4 (optional).                          |

## 1. Boot the stack

### Fast path: released image, no clone

Prerequisite: Docker Desktop or Docker Engine with Compose 2.20 or later.

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh
```

Run it from your home directory, or another directory only you can write to. A failed
download stops with curl's error and a non-zero exit; after a failed install, fix what it
reports and run the whole command again. The installer downloads `loom-stack.tar.gz` from
the latest GitHub Release, verifies its SHA-256, installs the Compose and ClickHouse
configuration under `~/.loom`, pulls the matching `ghcr.io/clickhouse/loom:X.Y.Z` and
`ghcr.io/clickhouse/loom-extractor:X.Y.Z` (the document extractor service) images, and
waits for `http://localhost:7777/v1/health`. The server image contains the API, workers,
migrations and the built React dashboard at the server root; no Python or Node
installation is needed. Python, TypeScript and Go compiler indexers are included and
enabled. A repository indexed with the TypeScript indexer gets its dependencies installed
automatically. Both images support Linux `amd64` and `arm64`, including Docker Desktop on
Apple Silicon.

The stack runs its own ClickHouse container. To use a ClickHouse you already run, a
self-managed server or a ClickHouse Cloud service (26.4 or later, with a Keeper), pass its
URL; the installer asks for the password when the URL has none, creates the `loom`
database and a read-only reader user, or prints the SQL an admin has to run
([choosing the ClickHouse](../operations/deployment.md#choosing-the-clickhouse)):

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && sh loom-install.sh --clickhouse https://loom@abc123.us-east-1.aws.clickhouse.cloud:8443 && rm loom-install.sh
```

Each release's `install.sh` installs that release, so the copy `latest` serves installs the
latest release, and a saved copy installs the same version every time it runs. Over a newer
install, an older release's copy refuses to install, rather than start an older Loom and
ClickHouse on newer data; to downgrade on purpose, follow
[downgrading an installer install](../operations/upgrades.md#downgrading-an-installer-install).
Pin a release, or inspect everything before starting:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/download/v1.2.3/install.sh -o loom-install.sh && sh loom-install.sh && rm loom-install.sh

curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz
curl -fLO https://github.com/ClickHouse/loom/releases/latest/download/loom-stack.tar.gz.sha256
curl -fL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh
sha256sum -c loom-stack.tar.gz.sha256
tar -tzf loom-stack.tar.gz

# after reading what the checks and the listing print:
LOOM_INSTALL_BUNDLE="$PWD/loom-stack.tar.gz" \
  LOOM_INSTALL_CHECKSUM="$PWD/loom-stack.tar.gz.sha256" sh loom-install.sh
```

`LOOM_PORT`, `LOOM_BIND_HOST`, `LOOM_IMAGE_REPOSITORY`,
`LOOM_EXTRACTOR_IMAGE_REPOSITORY` and `LOOM_CLICKHOUSE_IMAGE` given to the installer are
written to the install directory's `.env`, so later `docker compose` commands there and
later installer runs use the same port and images. A `LOOM_BIND_HOST` other than
loopback publishes Loom beyond this host, and the server refuses to start that way
without `LOOM_PUBLIC_BASE_URL` and sign-in: install with `LOOM_SKIP_START=1`, set them
in `.env` (below), then run the installer again.

Each release runs the ClickHouse release that was latest when it was built, so rerunning
the same Loom version keeps its ClickHouse; installing a newer Loom version moves it,
unless `.env` pins `LOOM_CLICKHOUSE_IMAGE` (delete that line to follow the releases
again). Rerunning the installer updates the managed Compose/configuration files and
reconciles the containers. It preserves `~/.loom/.env`, adding the extractor's URL and a
token to one without, and the named Docker volumes. To stop without deleting data:

```bash
docker compose --project-directory "$HOME/.loom" down
```

Add `--volumes` only for an intentional, irreversible reset.

### Start in multi-user mode

For a fresh released-image installation with accounts and sign-in from the first boot,
prepare the stack files without starting containers:

```bash
curl -fsSL https://github.com/ClickHouse/loom/releases/latest/download/install.sh -o loom-install.sh \
  && LOOM_SKIP_START=1 sh loom-install.sh && rm loom-install.sh
```

Edit `~/.loom/.env` before starting Docker. Replace the existing identity and auth entries,
and add the signup setting:

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

Keep the generated `COMPOSE_PROJECT_NAME` and `LOOM_JWT_SECRET`. Then pull and start
the stack (with your own ClickHouse, run `ch-setup` first, as the installer said):

```bash
docker compose --project-directory "$HOME/.loom" pull
docker compose --project-directory "$HOME/.loom" run --rm loom loom ch-setup  # your own ClickHouse only
docker compose --project-directory "$HOME/.loom" up -d
```

After the [health checks](#check-the-stack) pass, open `http://localhost:7777/` and
sign up. SDK and MCP clients need tokens; use the org and namespaces granted to your
account, not the single-user `dev/local` defaults in the examples below.

This enables multi-user authentication, not public networking: the stack still binds
to localhost. Before sharing it with a team, configure [operator access](../operations/deployment.md#identity-modes)
and [TLS](../operations/deployment.md#proxy-and-tls). Configure [real models and API keys](./models-and-keys.md)
as well; selecting multi-user mode does not replace the fake LLM.

For an already running single-user stack, follow the [switching instructions](#2-open-the-dashboard)
instead; container recreation preserves the existing data volumes.

### Contributor path: build a checkout

Prerequisite: Docker Compose 2.34 or later, which the checkout's compose file needs to
pull ClickHouse daily.

```bash
git clone https://github.com/ClickHouse/loom && cd loom
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
loom up
```

`loom up` does three things:

1. Creates the two files git does not track: `.env` from `.env.example`, and
   `deploy/clickhouse-config/named_collections.xml` from its template. It never
   overwrites either; it gives an existing `.env` without an extractor token one.
2. Runs the dev compose stack with `--build`.
3. Polls `http://localhost:7777/v1/health` until the server is ready, printing the
   pending-migration count. It gives up after `--timeout` (300 s) with the last health
   body and log lines.

The first contributor boot builds the image and applies every schema migration, so allow
a few minutes. `loom up` then prints the dashboard URL (`http://localhost:7777/`), a `curl`
call and a Python snippet that save a first memory, and a line that wires Claude Code.
When auth is on, it also mints a writer token and puts it in those snippets.

| Flag         | Does                         |
| ------------ | ---------------------------- |
| `--rebuild`  | Rebuild after a code change. |
| `--no-build` | Reuse the cached image.      |
| `--open`     | Open the dashboard.          |
| `--port`     | Use another Loom port.       |
| `--env-file` | Point at another env file.   |

Re-running `loom up` on a running stack only re-checks health. `make dev` does the same
without the CLI and runs:

```bash
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --build
```

With `compose.yaml` first, Compose reads the repository root's `.env` for the
`${VAR:-default}` substitutions; `--env-file .env` only says so explicitly. `loom down`
stops the stack and preserves its volumes. `loom down --delete-data` permanently
deletes this installation's named volumes only after you type the generated Compose
project name; `--yes` is the non-interactive confirmation. The Makefile targets
do the same: `make down` preserves data and `make down-all` deletes it.

### What the stock `.env` boots

The checkout and release-bundle `.env` templates both select single-user mode.
The release bundle sets `LOOM_IDENTITY_PROVIDER=single-user` explicitly; the checkout
leaves it empty and infers the same mode from `LOOM_AUTH_REQUIRED=false`.

| Setting                                                                      | Value         | Effect                                                                    |
| ---------------------------------------------------------------------------- | ------------- | ------------------------------------------------------------------------- |
| `LOOM_AUTH_REQUIRED=false`, `LOOM_IDENTITY_PROVIDER=single-user` (or empty)  | `single-user` | No login. Default namespace `dev/local`. SDK and MCP calls need no token. |
| `LOOM_LLM_PROVIDER=fake`, `LOOM_LLM_MODEL=fake-echo-1`                       | fake LLM      | Extraction, entity tagging and `memory.ask` synthesis produce nothing.    |
| `LOOM_EMBEDDING_MODEL=local`, `LOOM_EMBEDDING_DIM=384` (release bundle)      | bge-small     | Semantic search on CPU, from the model files the image carries.           |
| `LOOM_EMBEDDING_MODEL=fake-ngram-3072`, `LOOM_EMBEDDING_DIM=3072` (checkout) | fake embedder | Search ranks by character n-grams. Enough to check wiring, not recall.    |

Both installers generate a unique `COMPOSE_PROJECT_NAME`, a random JWT signing secret, a
random extractor token, and `LOOM_BIND_HOST=127.0.0.1`. Separate clones therefore do not
silently share ClickHouse volumes, and Loom plus ClickHouse are reachable only from this
computer.

For real providers, open **Settings → Models & keys**, choose your providers and models,
enter their API keys, then click **Test & apply**. One OpenAI API key can serve both the
LLM and embeddings. No Docker restart is needed; embedding changes trigger a verified
rebuild, with progress and retry shown in the dashboard.

For environment-based deployment instead, replace the provider lines in `~/.loom/.env`
(released stack) or `.env` (checkout). Setting `OPENAI_API_KEY` alone changes nothing:
the stock provider lines name their providers explicitly. Moving off the local or fake
embedder re-embeds what is stored.

```bash
LOOM_EMBEDDING_MODEL=text-embedding-3-large
LOOM_EMBEDDING_DIM=3072
LOOM_LLM_PROVIDER=openai
LOOM_LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=sk-...
```

For Claude, OpenAI, hosted embeddings, subscription-only clients and deployments with
one LLM provider, follow [Configure models and API keys](./models-and-keys.md).
Only after editing the released stack's `.env`, apply it with
`docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom`;
a plain restart does not reload the container's environment. Advanced settings are in
[Deployment → Providers](../operations/deployment.md#providers).

### Check the stack

```bash
curl -fsS http://localhost:7777/v1/health | jq '{status, fake_embedding, fake_llm, providers_degraded, problems}'
docker compose --project-directory "$HOME/.loom" exec loom loom doctor
```

- `/v1/health` says `status: ok` when ClickHouse is reachable and no problem other
  than `code_tier_failing` (a repository's failing code-index tier) is an error.
  `fake_embedding`, `fake_llm` and `providers_degraded` say which mode you are in;
  `problems` lists each issue with its fix; `schema.pending` lists unapplied migrations.
- `loom doctor` prints one line per server-side check. `--json` gives JSON. From a source
  checkout stack, run `docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml exec loom loom doctor`.

## 2. Open the dashboard

`http://localhost:7777/` opens straight into the dashboard in `single-user` mode;
in multi-user mode, sign up or sign in first. In `single-user` mode
the dashboard and agent defaults use `dev/local` (`LOOM_DEFAULT_NAMESPACE` renames the
namespace). This is a convenience mode, not a user-isolation boundary: anyone who can
reach the server can access its data and administration. Keep it localhost-only.

To try sign-up instead (`trust-network`: email and password, and the first person to
claim a namespace name becomes its admin), replace these entries in `~/.loom/.env`
(released stack) or `.env` (checkout):

```dotenv
LOOM_IDENTITY_PROVIDER=trust-network
LOOM_AUTH_REQUIRED=true
LOOM_SELF_SIGNUP=open
```

Setting only `LOOM_AUTH_REQUIRED=true` is insufficient when `single-user` is explicitly
selected: that mode keeps authentication off. Keep the installer's generated
`LOOM_JWT_SECRET` and existing volumes, then recreate the server to load the environment:

```bash
# Released stack:
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
# Checkout stack (run from the repository root):
docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml --profile bundled up -d --force-recreate loom
```

The dashboard then opens on its sign-in page, which has a **Sign up** tab, and every SDK
and MCP call needs a token. The identity modes are in [Identities and
namespaces](../concepts/identities-and-namespaces.md#identity-modes). Naming
`trust-network` is also what lets a non-loopback `LOOM_BIND_HOST` start with sign-up open,
so anyone who reaches the port can then sign up. Before making the server reachable by a
team, follow the [deployment guide](../operations/deployment.md#identity-modes) for
operator access and [TLS](../operations/deployment.md#proxy-and-tls).

## 3. Get a token

In single-user mode a token is optional. Minting one anyway keeps your scripts working
when auth is turned on.

- **Dashboard:** **Settings → Tokens → New token**. By default the token reads everything
  you belong to and saves to the namespace you are in. _Only these namespaces_ limits it to
  the ones you tick (none at first); the ticked ones are searched in the order listed, and
  you change it with a row's arrows, or on a desktop by dragging the row's grip. The result
  shows the token once, with the setup for your MCP client or a script.
- **CLI, as the operator:** run it inside the server container, which holds the signing
  secret:

  ```bash
  docker compose --project-directory "$HOME/.loom" exec loom loom mint-token --subject my-bot --org dev --namespaces local --roles writer
  ```

  Add `--ttl-days 30` for anything that leaves your machine. `loom mint-token --help`
  lists the scoping options (`--namespace-role`, `--read-scope`, `--traffic-class`).

- **A bot that signs itself up** (trust-network only):
  `loom init --agent nightly-triage --server-url http://localhost:7777` creates the
  namespace `agent-nightly-triage`, saves a writer token to `~/.loom/token` and the URL
  to `~/.loom/config.json`, and prints a claim code. A person enters it once at
  `/oauth/agent-claim` with a password of their own to take ownership.

Save the token where the SDK, the CLI and the plugins look for it:

```bash
mkdir -p ~/.loom && (umask 077; printf '%s' "<token>" > ~/.loom/token)
export LOOM_API_KEY="<token>"
```

Claims, roles and grants are in [Tokens and grants](../guides/tokens-and-grants.md).

## 4. First memory over HTTP

Every operation is a `POST /v1/<op>` with a JSON body that names the identity
`(org, namespace, agent)`:

```bash
curl -sX POST http://localhost:7777/v1/memory.set -H 'Content-Type: application/json' \
  -d '{"org":"dev","namespace":"local","agent":"quickstart",
       "memory_key":"deploy_window","value":"Tue and Thu only; never Friday."}'

curl -sX POST http://localhost:7777/v1/memory.search -H 'Content-Type: application/json' \
  -d '{"org":"dev","namespace":"local","query":"when can we deploy?"}' \
  | jq '.results[0].content_excerpt'
```

With auth on, add `-H "Authorization: Bearer $LOOM_API_KEY"` and use your account's org
and namespace; a header with an empty token is refused. The `memory.set` response
carries `memory_id`, `content_sha256` and `status`. The search response carries
`results` and `abstain`, which says whether the best hit is strong enough to answer from.
Search reads every agent's memories in the namespace, including those added in the
dashboard; name an `agent` to read only that agent's.
`GET /v1/operations` lists every operation, and <http://localhost:7777/docs> lets you try
each one from the browser. Shapes are in the [HTTP API reference](../reference/http-api.md).

## 5. First memory from Python

```bash
pip install clickhouse-loom    # or, from a clone: pip install ./sdk/python
```

A release not yet on PyPI installs from its assets
([installing from a GitHub Release](../development/release-process.md#installing-from-a-github-release)).

```python
import asyncio

from loom import Agent


async def main():
    async with Agent(
        org="dev",
        namespace="local",
        agent="quickstart",  # names the writer in the audit log
        base_url="http://localhost:7777",
    ) as a:  # reads LOOM_API_KEY; none needed in single-user mode
        await a.memory.set("deploy_window", "Tue and Thu only; never Friday.")
        for hit in await a.memory.search("when can we deploy?"):
            print(hit.content_excerpt)


asyncio.run(main())
```

Where the SDK finds its server and credential is in
[Python SDK → Credentials](../reference/python-sdk.md#credentials).
The TypeScript SDK installs with `npm install @clickhouse/loom`
([from a clone](../reference/typescript-sdk.md#install)).

The client surfaces are in the [Python SDK](../reference/python-sdk.md) and
[TypeScript SDK](../reference/typescript-sdk.md) references.

## 6. First memory from a coding agent

For Claude Code, install the Loom plugin. It adds hooks (context at session start,
capture at the end) and skills on top of the MCP tools:

```bash
claude plugin marketplace add ClickHouse/loom
claude plugin install loom@loom
```

On a single-user server on this machine, keep the default server URL and leave the token
empty when Claude Code asks for the plugin's settings.

Without the plugin, add the MCP entry directly:

```bash
claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding" -s user
```

Both reach a single-user server on this machine without a token of yours.

With auth on, create a token in **Settings → Tokens** and choose **Claude Code** under
**Connect**: the dialog shows the setup for the plugin and for a `claude mcp add` entry,
with the token in it. Then:

1. Overwrite `~/.loom/config.json` with the dialog's `config.json` block plus the
   namespace the token saves to (`"namespace": "<namespace>"`): an `org` or `namespace` a
   single-user session left in the file outranks the token, and without one the plugin
   writes to the token's first namespace other than `*`, or `coding`. Add back any
   `code_repo` or `agent` you had set.
2. Leave `LOOM_API_KEY` (step 3 may have exported one: `unset LOOM_API_KEY`) and the
   plugin's `token` option empty, or set them to the new token: the plugin reads both
   before that file
   ([configuration](../features/integrations/claude-code.md#configuration)).
3. Remove a `claude mcp add` entry made without a token (`claude mcp remove loom -s user`)
   before running the dialog's command, which refuses a name that exists.

`loom install --client <name>` writes the MCP entry for Claude Desktop, Claude Code,
Codex, Cursor, VS Code, Cline, Windsurf and Zed; with no flag it wires every client it
finds. [Connect a coding agent](connect-a-coding-agent.md) covers Codex and Cursor.

Start a new session and say _"Remember that I never deploy on Friday."_ The agent calls
`loom_memory_set`. In a fresh session, ask _"When can I deploy?"_ The agent calls
`loom_search` and answers from the stored fact. The model decides when to save and
search from the tool descriptions; there are no trigger words.

## 7. See it in the dashboard

| Page              | Shows                                             |
| ----------------- | ------------------------------------------------- |
| `/memories`       | The namespace's memories, with writer and time.   |
| `/admin/activity` | The audit log: which agent wrote which key, when. |
| `/admin/usage`    | Cost per operation and agent.                     |

The rest is in [Dashboard](../features/dashboard.md).

## Optional: index a repository on the released stack

Adding a repository from the dashboard is refused on this stack with `SANDBOX_UNAVAILABLE`:
it needs the [repository-job sandbox](../operations/code-index-operations.md#repository-job-sandbox),
which this stack leaves off because Docker's default confinement blocks it. Only list
repositories you trust: their build steps (TypeScript installs, Go module downloads,
CMake) run without the sandbox, in the `loom` container, whose environment holds the JWT
secret and provider keys.

`LOOM_CODE_REPOS_JSON` is one line holding every repository as a JSON array
([Configure the entry](../operations/code-index-operations.md#configure-the-entry)).
In `~/.loom/.env`, uncomment that line (add it if it is missing) and edit it.
`dev` is the org a single-user stack runs as, and the org multi-user accounts sign up
into (`LOOM_SELF_SIGNUP_ORG`); if you changed that, use your account's org here and in
`--org` below:

```dotenv
LOOM_CODE_REPOS_JSON=[{"org":"dev","namespace":"code-myrepo","repo":"acme/myrepo","git_branch":"main","public":true}]
```

- Loom picks the compiler-tier indexers from the repository's build files (`go.mod`,
  `package.json`, `pyproject.toml`, a root `CMakeLists.txt`, …); `scip_producer` names
  one instead ([Compiler tier](../operations/code-index-operations.md#compiler-tier)).
  C/C++ also needs a configured build tree, so without one a C/C++ repository gets only
  the parser tier.
- For a private repository, add a GitHub token (`LOOM_CODE_GITHUB_TOKEN` in `.env`, or
  `github_token` on the entry) and drop `"public":true`: it is for repositories public on
  their host, and lets anyone in the org join the index as a reader at once from
  **Repositories**. Decide before the first `loom code init`: it sets the namespace's join
  policy from `public`, and editing `public` later does not change it (a maintainer
  changes it with `code.repo.set_access`).

Then recreate the `loom` container and run the first index:

```bash
docker compose --project-directory "$HOME/.loom" up -d --force-recreate loom
docker compose --project-directory "$HOME/.loom" exec loom loom code init --namespace code-myrepo --org dev
docker compose --project-directory "$HOME/.loom" exec loom loom code backfill --namespace code-myrepo --org dev --embed
```

`backfill --embed` runs on the released stack's local embedder. Under a fake embedder (a
checkout's `.env`, or an upgraded install that still names `fake-ngram-*`) it refuses and
exits; to try the wiring anyway, add `--allow-fake-embedder`. Choosing a model later in
**Settings → Models & keys** re-embeds the code index; after an `.env` change it needs its
own re-embedding, as [Models and keys](./models-and-keys.md) describes.

In multi-user mode a listed repository has no maintainer, so without `"public":true` a
reader's join request waits with nobody to approve it. After `loom code init`, give
someone access as the operator with a token minted in the container: `admin` makes them
the maintainer, who approves readers from then on, and `viewer` lets them read. The
subject is the namespace the account signed up with (its login handle, the subject of
its tokens); a self-signup email is never verified, so a grant to it admits no one:

```bash
ADMIN_TOKEN=$(docker compose --project-directory "$HOME/.loom" exec -T loom loom mint-token --subject operator --org dev --namespaces code-myrepo --roles admin --ttl-days 1)
curl -s http://localhost:7777/v1/grants.create -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"org":"dev","namespace":"code-myrepo","subject":"your-namespace","role":"admin"}'
```

## Optional: ask a question about a document

```bash
loom demo
```

`loom demo` uploads this repository's `AGENTS.md`, ingests it with `knowledge.ingest` (the
stack's reference extractor parses it) and asks a question about it with
`knowledge.synthesize`, printing
the answer and the chunks it cited. With the fake LLM it prints the top
`knowledge.search` hits instead. `--file` and `--question` point it at your own document.

## Common problems

| Symptom                                           | Cause and fix                                                                                                                                                                                  |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_from_messages` writes no facts               | The LLM is the fake one. Follow [Models and keys](./models-and-keys.md) to configure one.                                                                                                      |
| Search results look arbitrary                     | The embedder is a fake one (`fake-ngram-*` in `.env`). Switch it as [Models and keys](./models-and-keys.md) says, which re-embeds what is stored. Do not delete your volumes to change models. |
| 401 `AUTH_INVALID` after turning auth on          | No token, or one signed with another `LOOM_JWT_SECRET`. Re-mint with the Compose command in [Get a token](#3-get-a-token).                                                                     |
| The dashboard returns 503                         | The server runs from source (`make server`) without a built frontend. Run `make frontend-build`.                                                                                               |
| Released stack does not become healthy            | Run `docker compose --project-directory "$HOME/.loom" logs --tail 100 loom clickhouse`. See [Troubleshooting](../operations/troubleshooting.md).                                               |
| Adding a repository answers `SANDBOX_UNAVAILABLE` | The stack runs without the repository-job sandbox. Index the repository through `LOOM_CODE_REPOS_JSON` ([Index a repository](#optional-index-a-repository-on-the-released-stack)).             |
| Port `8123`, `9000` or `7777` is taken            | Remap the ClickHouse host ports ([local stack](../operations/deployment.md#local-stack-from-a-clone)).                                                                                         |

## Next

- [Evaluate Loom](evaluate.md): turn this smoke test into a workload-specific quality,
  latency and cost evaluation.
- [Connect a coding agent](connect-a-coding-agent.md): Claude Code, Codex or Cursor
  against this stack.
- [Your first agent](first-agent.md): identity, abstain handling, sessions, context packs
  and runs from the SDK.
- [Deployment](../operations/deployment.md): running Loom for a team.
