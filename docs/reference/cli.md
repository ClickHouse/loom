# CLI reference

`loom` is the one command-line entry point for running, maintaining and
connecting to a Loom deployment. It is the `loom` console script of the server
package (`pyproject.toml`, `[project.scripts]` → `server.cli:main`); the parser
tree lives in `server/cli/app.py` and one module per command family under
`server/cli/`. The table at the end of this page is generated from that tree.

## Where it runs

| Where                                              | How                                                                | Use it for                                                                                             |
| -------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| Inside the server image                            | `docker exec loom-server loom <command>` (the web container)       | Operator commands against the deployment's own settings: `doctor`, `migrate`, `mint-token`, `reembed`. |
| Inside the workers container of a split deployment | `docker exec loom-workers loom <command>`                          | Long-running maintenance you want off the web tier: `code backfill`, `code stats`.                     |
| A checkout                                         | `pip install -e .` from the repository root, then `loom <command>` | `up`, `install`, `demo`, and development against a local stack.                                        |

Inside a container the settings are the container's environment. From a
checkout, `loom` first loads the `.env` in the working directory (or the file
`LOOM_ENV_FILE` names; empty for none) into the environment the way compose's
`env_file` does, without overriding variables your shell already exports. That
matters for provider keys: an embedding command run from a checkout without
them would fall back to the fake embedder and write unusable vectors.

Commands fall into two kinds:

- **Operator commands** read `server/settings/` and talk to ClickHouse directly
  (`migrate`, `reembed`, `code backfill`, `revoke-token`, the backfills and
  repairs). They run with the deployment's credentials, so run them where those
  credentials live.
- **Client commands** talk to a running server over HTTP with a URL and a token (`install`,
  `init`, `demo`, `memory-search`, `ingest-transcript`, `export-okf`, `import-okf`). They take
  `--server-url` (`--base-url` on `demo`) and, except `init`, `--token`, resolved in the plugin
  hooks' order: the URL from `LOOM_BASE_URL`, then `~/.loom/config.json` `url`, else
  `http://localhost:7777`; the token from `LOOM_API_KEY`, then `~/.loom/config.json` `token`,
  then `~/.loom/token` (moved by `LOOM_API_KEY_FILE`). `LOOM_CONFIG_FILE` moves the config file.
  For an agent (`LOOM_CLIENT_ID` with a workload token) they use only `--token` or
  `LOOM_API_KEY`, never a saved token, and refuse (exit 2) without one; `loom install` refuses
  whatever `--token` says, `loom init` refuses before it signs anything up, and `loom call`
  exchanges the workload token itself.

`loom mint-token` needs only `LOOM_JWT_SECRET`: it signs a token locally.

**Session commands** (`login`, `logout`, `whoami`, `call`, `token`) are the Python SDK's
own (`loom.cli`; `python -m loom …` with only the SDK installed), so `loom`
needs `pip install clickhouse-loom` for them. `loom login [--server URL]
[--profile NAME] [--no-browser]` signs in with a one-time code;
`loom call <op|/path> [key=text|key:=json …] [--data JSON] [--org] [--namespace]` reaches
any operation or route with that session, and `loom token create|list|revoke`
makes, lists and revokes your own tokens for CI and agents. See
[use Loom without the dashboard](../guides/without-the-dashboard.md).

## Conventions

**Dry run by default for rewrites.** A command that rewrites stored rows
reports what it would write unless it is given `--apply`; `--dry-run` is
accepted and names the default. The two flags are mutually exclusive
(`server/cli/_apply.py`, `add_apply_flags`). A successful dry run ends with
`dry run: nothing was written; re-run with --apply to write` on stderr, so a
command's `--json` output stays alone on stdout. Some commands spell the
same idea with their own flags (`loom entity-edges-invalidate-junk` takes
`--dry-run` (the default) or `--apply`; `loom reembed --dry-run` counts rows
without writing); the table below lists each command's flags.

**Every write names its principal.** A command's writes are stamped with the
actor `process:cli:<command>` (`server/memory/domain/provenance.py`, `cli_actor`), so
provenance and the audit log say which command wrote a row.

**Every query is attributed.** A command's ClickHouse queries carry the
`log_comment` tag `cli.<command>[.<subcommand>]`, and its reads run in the
`background` traffic class unless the command takes a `--traffic-class` of its
own. `loom serve` is the exception: it is the server, and its requests and
worker passes tag themselves.

**`--json`** on the commands that offer it prints one machine-readable result
on stdout and keeps the human report on stderr.

## Common commands

### Run and check a deployment

```bash
loom up                          # from a checkout: create .env, boot Loom, its extractor and ClickHouse, print how to connect
docker exec loom-server loom doctor          # pre-boot checks + the /v1/health problem list, each with its fix
docker exec loom-server loom doctor --json   # the same, machine-readable
loom doctor --client             # also examine this machine as a coding-agent client
docker exec loom-server loom migrate --status              # applied and pending migrations
docker exec loom-server loom migrate --dry-run --strict    # what a boot would apply; exit 1 on ledger drift
```

`loom up` waits for `/v1/health` (up to `--timeout`, default 300 s), mints a
writer token when auth is on, and prints the dashboard URL and first-call
snippets; `--no-build` reuses the cached image and a stack that already answers
health is left alone unless you pass `--rebuild`. `loom doctor` exits 1 on any
failing check. `loom serve` runs the control plane under uvicorn for local use
(the image starts it through `scripts/serve.sh`). Migrations and upgrades are covered in
[Upgrades](../operations/upgrades.md); health checks in
[Monitoring](../operations/monitoring.md).

### Tokens

```bash
# A writer token for one namespace, valid 30 days, printed on stdout
docker exec loom-server loom mint-token --subject my-bot --org dev \
  --namespaces local --roles writer --ttl-days 30

# A scoped token: per-namespace roles, in read-priority order
loom mint-token --subject triage --org acme \
  --namespace-role priv=admin --namespace-role pub=viewer --json

# Retire one token, or every token of a subject issued before an instant
docker exec loom-server loom revoke-token --jti <jti> --reason "leaked"
docker exec loom-server loom revoke-token --subject my-bot --issued-before 2030-01-01T00:00:00Z
```

A permanent revocation cannot be given an expiry or narrowed to restore tokens
retired before it. Choose a cutoff at or after its existing cutoff, or the
permanent revocation time plus the token clock-skew allowance.

`--save` writes the token to `~/.loom/token` (or a path you give) instead of
printing it. Without `--ttl-days` the token has no expiry and can only be
retired with `loom revoke-token`. `--read-scope own_user` limits reads to the
`user_id` each request names; `--consumer NAME` binds the
[consumer tag](http-api.md#the-consumer-tag) to the credential. See
[Tokens and grants](../guides/tokens-and-grants.md).

### Connect a client

```bash
loom install --client cursor --token "$LOOM_API_KEY" --server-url https://loom.example.com
loom install --client claude-code --toolset coding+tracker --dry-run
loom init --agent triage-bot --server-url https://loom.example.com   # self-signup: a namespace + token
```

`loom install` writes the MCP server entry into a client's config (or runs
`claude mcp add` / `codex mcp add`); with no `--client` it wires every
supported client it finds. See [MCP clients](../features/integrations/mcp-clients.md).
`loom init` saves the token to `~/.loom/token` and the server, org and namespace
to `~/.loom/config.json`, the file the client commands and the plugin hooks read.

### Re-embed after changing the embedder

```bash
docker exec loom-server loom reembed --dry-run          # count rows whose embedding_model is not the configured one
docker exec loom-server loom reembed --namespace support
docker exec loom-server loom reembed --delete-old        # re-embed, then delete the off-model rows
```

Loom never re-embeds on its own: after changing `LOOM_EMBEDDING_MODEL`, run
`loom reembed`. It regenerates vectors from stored content and keeps every
other column. `--in-database <named collection>` embeds inside ClickHouse with
`aiEmbed` (ClickHouse 26.7 or later) as one `INSERT … SELECT` per table, refused
while a customer or identity header is set or the inference gateway is required;
`--resume` continues after the row a previous run's `resume:` line names. See
[Maintenance](../operations/maintenance.md).

### Index a repository

```bash
docker exec loom-workers loom code init --org acme --namespace code-myrepo
docker exec loom-workers loom code backfill --org acme --namespace code-myrepo --embed
docker exec loom-workers loom code stats --org acme --namespace code-myrepo
```

`code backfill` is resumable. `--phase index` parses, chunks and derives
symbols and edges; `--phase embed` fills pending vectors; `--embed` does both
in one run. It refuses to embed with the fake embedder unless you pass
`--allow-fake-embedder` (development only). `--org` and `--namespace` default
to `LOOM_CODE_ORG` and `LOOM_CODE_NAMESPACE`. The full procedure, the compiler
tier and the PR-history backfills are in
[Code index operations](../operations/code-index-operations.md).

### Export and import

```bash
loom export-okf --namespace support --out ./support-okf            # a namespace as a Markdown bundle
loom okf-lint ./support-okf --strict                               # offline lint; exit 2 on a critical finding or, with --strict, a warning
loom import-okf ./support-okf --namespace support-copy
loom export-actions --format cedar -o loom.cedarschema             # the registry as a policy schema
```

`export-okf` leaves rows with `sensitivity=high`, or with no known sensitivity, out
unless you pass `--include-high`, because a mirror repository is a wider audience
than the namespace's grants. The raw chunks and digests linked to such a row, and raw
text that holds a high shape (a credential, card number, SSN or stated PIN), stay
home with it; `--include-high` releases them, and the server audits each released item.
See [OKF](../features/integrations/okf.md) and
[Exporting the action schema](./mcp.md#exporting-the-action-schema).

### Diagnose and repair

`loom report` prints the bug form's Environment field with the `loom doctor`
output; `--open` opens the form with the versions filled in. `loom drops`
prints the memory ingest drop ledger for a namespace. `loom erasure-re-verify`
re-checks failed erasure sagas. The backfill and repair commands
(`integrity-repair`, `reconcile-status`, `repair-value-bytes`,
`entity-edges-invalidate-junk`, `rechunk`, `lexical-stats-rebuild`, …) and
when to run each are in [Maintenance](../operations/maintenance.md) and
[Troubleshooting](../operations/troubleshooting.md).

## Reading the table

Each row is one command or subcommand: its full invocation, the first sentence
of its help text, and its arguments. Positional arguments are shown as
`<name>`; options by their long form. `loom <command> --help` prints the full
text, defaults and choices. The generator
(`scripts/codegen/docs_reference.py`, `render_cli`) reads the committed parser
snapshot `tests/unit/snapshots/cli_tree.json`.
`tests/unit/test_cli_surface_snapshot.py` keeps that snapshot and
`tests/unit/snapshots/cli_help.json` equal to the live parser, so a new flag
fails CI until both snapshots and this page are regenerated:

```bash
LOOM_UPDATE_CLI_SNAPSHOT=1 python -m pytest tests/unit/test_cli_surface_snapshot.py
python scripts/codegen/docs_reference.py
```

## Commands

<!-- BEGIN GENERATED: cli -->
<!-- Generated by `python scripts/codegen/docs_reference.py`; edit the source, not this region. -->
<!-- prettier-ignore-start -->

| Command | Summary | Arguments and flags |
| --- | --- | --- |
| `loom code` | Code intelligence plane operations. |  |
| `loom code init` | Create the bare clone + seed state. | `--org`, `--namespace` |
| `loom code backfill` | Full-tree indexing (resumable). | `--org`, `--namespace`, `--phase`, `--embed`, `--include-poisoned`, `--allow-fake-embedder`, `--no-fetch`, `--limit`, `--commit`, `--paths-file`, `--concurrency`, `--edges-second-pass`, `--no-edges-second-pass`, `--dialect-folder-pass`, `--embedding-model-suffix`, `--context-callees`, `--callers-from` |
| `loom code reconcile` | Content-based staleness check: paths whose chunks predate their last change at the branch head; --heal re-ingests them (watermark untouched). | `--org`, `--namespace`, `--heal`, `--limit`, `--max-commits`, `--no-fetch`, `--show` |
| `loom code compact` | OPTIMIZE ... FINAL a code vector table when its physical copies per key reach --min-ratio, and wait for the merge. | `--table`, `--min-ratio`, `--apply`, `--dry-run`, `--timeout-s` |
| `loom code reembed` | Write the namespace's live chunks under a second embedding model's label: in-database, resumable, no checkout. | `--org`, `--namespace`, `--embedding-model`, `--source-model`, `--prefix`, `--batch` |
| `loom code vector-twin` | Fill a namespace's Matryoshka-prefix twin of the chunk vectors inside ClickHouse: no re-embed, resumable. | `--org`, `--namespace`, `--embedding-model`, `--all-namespaces` |
| `loom code crossref` | SQL/shell test -> implementing C++ reference pass (writes code_test_map method=content_reference; idempotent). | `--org`, `--namespace` |
| `loom code coverage-import` | Import the newest per-test line coverage run of the configured source (writes code_test_map method=coverage and code_coverage_files). | `--org`, `--namespace`, `--dry-run` |
| `loom code export-pairs` | Export (query -> acted-on unit, hard negatives) retrieval pairs from real traffic (trace x outcome join) for embedder/reranker fine-tuning. | `--org`, `--namespace`, `--days`, `--out` |
| `loom code backfill-prs` | Merged-PR history from GitHub. | `--org`, `--namespace`, `--months`, `--max-prs` |
| `loom code repack` | Regenerate context packs for recently merged PRs in the current pack shape. | `--org`, `--namespace`, `--days`, `--limit`, `--dry-run` |
| `loom code describe` | LLM contextual one-liners for top-PageRank chunks. | `--org`, `--namespace`, `--top-pct`, `--limit`, `--suffix`, `--concurrency` |
| `loom code answers-purge` | Tombstone cached code.ask answers (after a synthesis-model upgrade, stale weaker-model answers would otherwise be served forever — the cache only self-invalidates on cited-file change). | `--llm-model`, `--all`, `--apply`, `--dry-run`, `--org`, `--namespace` |
| `loom code scip-full` | Full compiler-tier re-index by hand (the code_scip_full worker runs it weekly by default: heals the TU-cap degrades of per-merge deltas and any drift). | `--checkout`, `--jobs`, `--keep-delta`, `--apply-delta`, `--org`, `--namespace` |
| `loom code scip-lock-path` | Print the entry's compiler-tier lock file on this host. | `--org`, `--namespace` |
| `loom code entry-lock` | Run a command under the entry's compiler-tier lock (this host's file and the deployment's lease): exit 75 when another pass holds it, 3 when the lease is lost mid-run (the command is stopped). | `--org`, `--namespace`, `<command>` |
| `loom code scip-delta` | One compiler-tier delta pass (same code as the code_scip_delta worker), by hand: git_head vs scip_commit per configured repo, scip-clang over the changed TUs, apply. |  |
| `loom code entries` | One line per configured repository: org namespace checkout lock-file (the lock path resolved; a manual run takes it through `loom code entry-lock`). |  |
| `loom code warm` | Pre-seed the code.ask answer cache from a question list (one question per line, or JSONL with a 'question' field). | `<file>`, `--org`, `--namespace` |
| `loom code scip-retry` | Reset a failed SCIP batch to pending. | `--org`, `--namespace`, `<batch_id>` |
| `loom code backfill-reviews` | Inline review comments for merged PRs already in code_pr_history that have none stored yet (newest first, one GitHub request per PR). | `--org`, `--namespace`, `--prs` |
| `loom code backfill-assignees` | Fill code_pr_history.assignees (who the repo assigned to review) for merged PRs already stored, walking the newest closed PRs 100 per request. | `--org`, `--namespace`, `--pages` |
| `loom code backfill-items` | Fill code_gh_items (issues + PRs, any state) updated since a date, 100 per request, oldest update first; moves gh_items_cursor. | `--org`, `--namespace`, `--since`, `--pages` |
| `loom code stats` | Watermarks + live row counts. | `--org`, `--namespace` |
| `loom code export-matrix` | Maintenance matrix from merged-PR co-change: 'when this file changes, these usually change too', with evidence, as markdown for an instructions file (AGENTS.md / copilot-instructions.md). | `--org`, `--namespace`, `--prs`, `--min-together`, `--min-conf`, `--max-partners`, `--max-rows`, `--out`, `--json` |
| `loom code lint-instructions` | Agent-instruction files (CLAUDE.md, AGENTS.md, copilot-instructions, .claude/.md, skills) whose path or symbol references no longer resolve at the clone's head. | `--org`, `--namespace`, `--file`, `--glob`, `--symbols`, `--all`, `--json`, `--fail-on-stale` |
| `loom decisions` | Decision records mined from documents. |  |
| `loom decisions extract` | Mine one knowledge document into decision records: the model reads the document whole (or in windows of whole sections past LOOM_MEMORY_DECISION_EXTRACT_WINDOW_CHARS; a heading over only an image… | `--org`, `--namespace`, `--source-uri`, `--repo`, `--into`, `--max-per-section`, `--window-chars`, `--apply`, `--dry-run` |
| `loom knowledge` | Knowledge documents and their sources. |  |
| `loom knowledge sync` | Run one sync pass over a repository namespace's document sources now: list each source's changed files (every file with --full), write the changed ones as documents and retire those a full listing… | `--org`, `--namespace`, `--source`, `--full` |
| `loom knowledge migrate-originals` | Move document originals held in ClickHouse to LOOM_DOCUMENT_S3_BUCKET: each one whose catalog row still holds its bytes, and that a live document, upload or ingestion still needs (the rest are the… | `--org`, `--namespace`, `--dry-run` |
| `loom export-okf` | Render a namespace as an OKF v0.2 bundle (Markdown + frontmatter) — the git-reviewable projection of Loom memory. | `--server-url`, `--token`, `--org`, `--namespace`, `--out`, `--title`, `--include-tombstoned`, `--limit`, `--dry-run`, `--no-evidence`, `--no-links`, `--include-high` |
| `loom import-okf` | Write the concepts of an OKF v0.2 bundle into a namespace via memory.set. | `<path>`, `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--user-id`, `--session-id`, `--keep-sessions`, `--verify`, `--dry-run`, `--no-evidence`, `--no-lint`, `--force-import`, `--no-links`, `--prune` |
| `loom okf-lint` | Lint an OKF bundle directory offline — the compile gate a mirror repository's CI runs. | `<path>`, `--json`, `--strict` |
| `loom up` | Boot the local stack (Loom, its document extractor and ClickHouse) and print how to connect: creates .env and the ClickHouse config from their templates, runs docker compose up --build, waits for… | `--root`, `--file`, `--env-file`, `--no-build`, `--rebuild`, `--port`, `--timeout`, `--subject`, `--org`, `--namespace`, `--open` |
| `loom down` | Stop the local stack while preserving data; --delete-data performs a confirmed reset. | `--root`, `--file`, `--env-file`, `--delete-data`, `-y` |
| `loom ch-setup` | Prepare a self-managed ClickHouse or a ClickHouse Cloud service for Loom: create the loom database and the read-only reader user, or print the SQL for an admin. | `--print-sql` |
| `loom participant` | The Loom processes a provider change waits for. |  |
| `loom participant retire` | Retire the provider participant of a process that died without shutting down (its id is `participant_id` in that process's /v1/health). | `--participant`, `--yes` |
| `loom deployment` | The configuration every replica checks itself against. |  |
| `loom deployment publish` | Publish this env's configuration as the deployment's, replacing its root secret or LOOM_EMBEDDING_MODEL/DIM on purpose. | `--yes`, `--check` |
| `loom settings` | Every setting's value in force, and overrides of the live ones. |  |
| `loom settings list` | Each setting: value, where it came from, whether live. | `--prefix`, `--changed`, `--json` |
| `loom settings set` | Override a live setting for the whole deployment; outlives a restart. | `<name>`, `<value>`, `--expect` |
| `loom settings reset` | Remove the override: back to the env or the default. | `<name>`, `--expect` |
| `loom settings history` | Recorded changes, newest first: who, the value before and after. | `<name>`, `--limit`, `--json` |
| `loom secrets` | Credentials and signing keys stored sealed in ClickHouse. |  |
| `loom secrets reseal` | Seal again under LOOM_JWT_SECRET what only LOOM_JWT_SECRET_PREVIOUS opens. | `--dry-run`, `--check` |
| `loom signing-key` | The token signing keys beside LOOM_JWT_SECRET, kept in ClickHouse. |  |
| `loom signing-key rotate` | Add a key; it signs once every process has loaded it. | `--algorithm`, `--activate-in` |
| `loom signing-key retire` | Stop a key (or 'default') verifying tokens. | `<kid>` |
| `loom signing-key record-root` | Record this LOOM_JWT_SECRET as the deployment's root secret, after replacing it on purpose (keys sealed under the old one stay unusable). |  |
| `loom signing-key list` | List the keys. |  |
| `loom provider-lock` | The provider configuration lock. |  |
| `loom provider-lock clear` | Free the provider configuration lock of a holder whose process stopped (refused while it renews, heartbeats or has a query running). | `--holder` |
| `loom export-analytics` | Write one UTC day of usage and a namespace snapshot to S3 as Parquet, names hashed under LOOM_ANALYTICS_EXPORT_NAME_KEY. | `--bucket`, `--prefix`, `--day`, `--role-arn`, `--region`, `--expected-bucket-owner` |
| `loom reembed` | Re-embed vectors whose embedding_model != the configured embedder (run after changing LOOM_EMBEDDING_MODEL — Loom does not auto-re-embed). | `--org`, `--namespace`, `--batch-size`, `--in-database`, `--dry-run`, `--delete-old`, `--key-only`, `--resume` |
| `loom reextract` | Replay a namespace's raw evidence (memory.set_from_messages turns) through a running server's write path into ANOTHER namespace — the current extractor over the stored transcripts; the source is… | `--org`, `--namespace`, `--into`, `--base-url`, `--token`, `--dates`, `--concurrency`, `--limit-sessions`, `--dry-run` |
| `loom deploy-delta` | What a colour swap changed: the deployment's ledger rows (volume, errors, p50/p95, upstream spend per operation) and its ClickHouse statement shapes (runs, bytes, seconds) for a window on each side… | `--swap-at`, `--window-min`, `--ch-os-user`, `--cluster`, `--top`, `--json` |
| `loom entity-export` | Export a namespace's entity graph (co-occurrence edges + triples) to GraphML or JSON-LD for an external graph or RDF tool. | `--org`, `--namespace`, `--format`, `--output`, `--all` |
| `loom audit-namespace` | How a namespace is being used (namespace.audit): exact-key reads vs searches, hash keys, double-encoded and oversized values, hand-rolled index rows, leases stored as memories, version churn. | `--org`, `--namespace`, `--window-days`, `--samples`, `--json` |
| `loom mentor` | The store's advice to one agent (mentor.brief): what it called, the planes its namespace offers, the hints with their evidence, and how often past hints were followed. | `--org`, `--namespace`, `--agent`, `--window-days`, `--json` |
| `loom serve` | Boot the control plane (uvicorn). | `--host`, `--port`, `--log-level`, `--reload` |
| `loom migrate` | Apply schema migrations against the configured CH. | `--status`, `--dry-run`, `--strict`, `--target`, `--migrations-dir` |
| `loom reconcile-status` | Re-emit memory_embeddings rows whose status diverged from loom.memory (superseded / conflicted / duplicate marks the pool never saw), compared and re-emitted per identity row (org, namespace, agent… | `--org`, `--namespace`, `--all-namespaces`, `--since-days`, `--exclude`, `--limit`, `--apply`, `--dry-run` |
| `loom period-digest` | Write period digests — one derived episodic memory over K consecutive session digests of each identity in one namespace. | `--org`, `--namespace`, `--sessions`, `--max-identities`, `--max-periods`, `--apply`, `--dry-run`, `--rewrite` |
| `loom demote-activity-narration` | Find a namespace's extracted facts that narrate tool activity ('User read lines 1-1000 of X', 'Claude ran a command on …') with no durable outcome and demote (confidence x0.25) or label them through… | `--org`, `--namespace`, `--action`, `--classifier`, `--days`, `--limit`, `--apply`, `--dry-run` |
| `loom recalibrate` | Re-probe the embedder + run per-NS calibration immediately (otherwise this runs weekly via the recalibration worker). |  |
| `loom import-clickgap` | Import a ClickGap (Detector→Validator→Reviewer) SQLite export into Loom (lessons + typed entities + categories). | `--sqlite`, `--org`, `--namespace`, `--agent`, `--dry-run`, `--batch-size`, `--seed-recall-events` |
| `loom import-recall` | Import an external vector-recall agent-memory SQLite export into Loom. | `--sqlite`, `--org`, `--namespace`, `--agent`, `--base-url`, `--token`, `--dry-run` |
| `loom import-brain` | Import a markdown agent 'brain' directory into Loom: MEMORY.md (always-injected HOT layer) → profile.set sections; memory/contexts/*.md (read-on-demand WARM layer) → knowledge.add docs. | `--dir`, `--org`, `--namespace`, `--agent`, `--base-url`, `--token`, `--dry-run` |
| `loom claim-release` | Release a self-signup claim: revoke its grants, clear the owner, deactivate it. | `--org`, `--namespace`, `-y` |
| `loom mint-token` | Mint a JWT locally (requires LOOM_JWT_SECRET). | `--subject`, `--org`, `--namespaces`, `--roles`, `--platform-admin`, `--namespace-role`, `--ttl-days`, `--save`, `--read-scope`, `--read-user-id`, `--traffic-class`, `--consumer`, `--service-account`, `--user-token`, `--json` |
| `loom revoke-token` | Revoke one token by jti or subject (writes loom.revoked_tokens through the revocation module; needs the configured ClickHouse). | `--jti`, `--subject`, `--list`, `--issued-before`, `--scim`, `--reason`, `--expires-at`, `--json` |
| `loom sso` | Carry password accounts over to single sign-on. |  |
| `loom sso adopt` | Let SSO sign-ins reach the password accounts whose email is at --domain: each person then signs in with the identity provider to their own account, its namespaces and data. | `--domain`, `--org`, `--skip`, `--keep-tokens`, `--apply`, `--dry-run` |
| `loom sso link` | Let one person's SSO sign-ins reach their password account registered under another address (a personal one, a +tag). | `--email`, `--org`, `--namespace`, `--apply`, `--dry-run` |
| `loom sso unlink` | Undo the links of one email: its grants stop admitting the accounts. | `--email`, `--apply`, `--dry-run` |
| `loom sso password-link` | Print a one-time link (valid for LOOM_PASSWORD_LINK_TTL_SECONDS, a day by default) that sets the password of an email's SSO accounts, for after single sign-on is turned off. | `--email`, `--org` |
| `loom bot` | Bots and CI jobs that sign in with a key or their CI's identity. |  |
| `loom bot create` | Register a bot: its namespaces, and a key (--generate-key or --public-key) and/or a CI trust (--issuer, --audience, --claim). | `--name`, `--org`, `--namespace-role`, `--generate-key`, `--public-key`, `--issuer`, `--audience`, `--claim`, `--jwks-uri`, `--max-token-age`, `--max-access-token-ttl`, `--max-agents`, `--agent-claim`, `--platform-admin`, `--replace` |
| `loom bot list` | Every bot, active or disabled. |  |
| `loom bot disable` | Refuse its proofs and revoke the tokens it holds. | `--name` |
| `loom doctor` | Diagnose pre-boot misconfig (Python version, settings, JWT, CH reachability) and print the problem list /v1/health carries, each with its fix. | `--json`, `--client`, `--server-url`, `--token` |
| `loom report` | Print the bug form's Environment field: versions and the `loom doctor` checks. | `--open` |
| `loom drops` | Print the memory ingest drop ledger (loom.memory_drops) for a namespace, by stage/reason — what the pipeline discarded, truncated or gave up on. | `--namespace`, `--org`, `--hours` |
| `loom namespaces-gc` | List (default) or erase throwaway namespaces — finished bench runs under the bench prefixes that nobody registered and nobody has written to for --idle-hours — in one list-scoped erasure saga per… | `--org`, `--prefix`, `--namespace`, `--exclude-prefix`, `--idle-hours`, `--include-registered`, `--erase`, `--no-purge`, `--skip-external`, `--batch`, `--show`, `--requester`, `--worker-pass` |
| `loom erasure-re-verify` | Re-verify failed erasure sagas: print each one's last step-named reason and a fresh residue census (default, read-only); --resume re-runs them from their checkpoint past the recovery worker's… | `--org`, `--id`, `--resume`, `--skip-external`, `--completed-since`, `--orphan-graph`, `--erase`, `--batch`, `--show`, `--requester` |
| `loom tracker-probe` | Read-only first contact with the tracker source: counts and newest timestamps per upstream table per credential, breaker states, the configured tracker namespaces. |  |
| `loom tracker-contract-check` | Read-only: does each tracker source's schema meet the record contract (docs/features/tracker/record-contract.md)? | `--source`, `--namespace`, `--json` |
| `loom tracker-dupcheck-fit` | Refit tracker.duplicate_check's calibrated confidence for one tracker namespace now: replays the duplicate gold at filing time, fits, stores the fit. | `--namespace`, `--org`, `--negatives`, `--dry-run`, `--out` |
| `loom tracker-embed-reset` | Clear the tracker_embed pass cursors of one tracker namespace so the next pass re-walks the window and re-embeds every item whose text recipe changed. | `--namespace`, `--org`, `--kind`, `--apply`, `--dry-run` |
| `loom namespaces-adopt` | Give namespaces that hold live memory and no registry row an owner row: kind=bench under the bench prefixes, else memory; retention_days 0, no TTL. | `--org`, `--owner`, `--prefix`, `--namespace`, `--kind`, `--apply`, `--dry-run`, `--show` |
| `loom sensitivity-sweep` | Label live rows the sensitivity detector would have labelled on write but a path skipped. | `--org`, `--namespace`, `--since-days`, `--limit`, `--show`, `--only-shape`, `--apply`, `--dry-run` |
| `loom integrity-repair` | Read the row-level integrity gauges (dim-0 rows holding a vector, tombstoned_at/status drift, active rows past valid_to) and, with --apply, re-emit the dim-0 rows with their true dimension so search… | `--org`, `--namespace`, `--limit`, `--show`, `--apply`, `--dry-run` |
| `loom rechunk` | Re-chunk and re-embed a namespace's knowledge documents from stored content through the current chunker, so every document carries section metadata, chunk context and document frequency. | `--org`, `--namespace`, `--source-uri`, `--apply`, `--dry-run` |
| `loom export-actions` | Export the action schema derived from the operation registry — an MCP tools/list manifest with loom extras (json) or the Cedar schema (cedar) a policy gateway governs the surface with. | `--format`, `--output` |
| `loom lexical-stats-rebuild` | Recompute BM25 document frequency for a namespace from its live rows (the term-frequency views only see inserts; supersession and tombstones leave stale vocabulary behind). | `--org`, `--namespace`, `--no-knowledge`, `--no-memory` |
| `loom version` | Print installed Loom version. |  |
| `loom install` | Wire Loom into a local AI client's config. | `--client`, `--server-url`, `--token`, `--name`, `--toolset`, `--dry-run`, `--force` |
| `loom demo` | The first five minutes: ingest this repo's AGENTS.md (or --file) into the knowledge plane the way `make ingest` does, ask it a question, and print the answer with its evidence rows. | `--file`, `--question`, `--tags`, `--base-url`, `--token`, `--org`, `--namespace`, `--root`, `--search-only` |
| `loom init` | Create an identity on a Loom server. | `--agent`, `--server-url`, `--org`, `--purpose`, `--ttl-days`, `--token-path`, `--config-path`, `--no-files`, `--json` |
| `loom ingest-transcript` | Pipe a chat-history file (markdown / JSON) into Loom's conversation table as one thread. | `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--thread-id`, `--file`, `--dry-run` |
| `loom memory-search` | Run a memory.search from the shell. | `--server-url`, `--token`, `--org`, `--namespace`, `--agent`, `--user-id`, `--query`, `--top-k`, `--search-mode`, `--json-agent` |
| `loom set-host` | Designate the Loom host(s) for this deployment. | `<emails>`, `--env-file`, `--append`, `--dry-run` |
| `loom login` | Sign in to a Loom server on any browser with a one-time code. |  |
| `loom logout` | End the stored session on the server and forget it here. |  |
| `loom whoami` | Show who the stored session signs you in as. |  |
| `loom call` | Call any operation or API route with the stored session (`loom call --help`). |  |
| `loom token` | Make, list and revoke your own tokens for CI and agents (`loom token --help`). |  |

<!-- prettier-ignore-end -->
<!-- END GENERATED: cli -->

## Related

- [Operations reference](./operations.md): the ops the client commands call.
- [Maintenance](../operations/maintenance.md): re-embed, backfills and repairs.
- [Code index operations](../operations/code-index-operations.md).
- [Upgrades](../operations/upgrades.md): `loom migrate` in the upgrade procedure.
- [Local development](../development/local-development.md).
