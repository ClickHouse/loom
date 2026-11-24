---
title: Self-hosted agent memory on ClickHouse
description: Query self-hosted agent memory with SQL. Review decision history and writer provenance with read-only access scoped to an org and namespace.
seo:
  title: SQL-queryable agent memory on ClickHouse
---

# Self-hosted agent memory on ClickHouse

Loom runs on your own infrastructure, with Docker or on Kubernetes with its Helm chart, and
stores agent memory, documents, conversations and code indexes as tables in ClickHouse. A
deployment can choose Postgres at first boot for live memory and knowledge state. Data is
stored only in your stores (the hosted model providers and trace sinks you configure
receive what they process), and what your agents know is queryable with SQL.

## What you get

- **Docker or Kubernetes.** The one-line installer starts the local stack; the Helm chart
  runs N identical replicas behind one URL. [Deployment](../operations/deployment.md) ·
  [Kubernetes](../operations/kubernetes.md) · [Running N replicas](../operations/replicas.md)
- **ClickHouse OSS or ClickHouse Cloud.** [Choosing the ClickHouse](../operations/deployment.md#choosing-the-clickhouse) ·
  [ClickHouse Cloud and Managed Postgres](../operations/clickhouse-cloud.md)
- **One store for vectors, text and audit.** Under the default ClickHouse authority,
  vector, lexical and graph search, filters, audit and erasure are SQL in one database.
  [Storage authority](../architecture/storage-authority.md)
- **Multi-user.** Orgs, namespaces, roles, tokens and single sign-on.
  [Identities and namespaces](../concepts/identities-and-namespaces.md) ·
  [Single sign-on](../operations/sso.md)
- **Local models.** The released stack embeds on CPU with a bundled local model, and
  the embedding provider accepts an OpenAI-compatible endpoint you run.
  [Models and API keys](../getting-started/models-and-keys.md)

## Query agent memory with SQL

Use the [read-only ClickHouse views](../reference/sql-surface.md) to audit saved facts,
trace corrections, or pull usage into a reporting job. The ClickHouse user provisioned
by `loom sql-access` reads only its granted orgs and namespaces. Start with
[`sql_v1_memories`](../reference/sql-surface.md#sql_v1_memories) for current facts and
[`sql_v1_memory_history`](../reference/sql-surface.md#sql_v1_memory_history) for versions.
Agents can keep using MCP or HTTP for semantic recall and writes.

### Audit a changed decision with SQL

Try this on a disposable single-user quickstart deployment with bundled ClickHouse and
the default `dev/local` scope and database `loom`.

1. Save `deploy_window` using the [first HTTP memory write](../getting-started/quickstart.md#4-first-memory-over-http).
   Keep its `memory_id`. Pass that ID, `org: "dev"` and a replacement `value` to
   [`memory.update_by_id`](../reference/operations/memory.md#memoryupdate_by_id).
   Check that the operation returns `updated: true`.
2. From the Loom container, create a SQL reader scoped to
   this namespace. The [SQL access requirements](../reference/sql-surface.md#access) list
   the privileges the operator needs.

   ```bash
   docker compose --project-directory "$HOME/.loom" exec loom \
     loom sql-access grant --user loom_demo_audit --org dev --namespaces local
   ```

3. Open the bundled ClickHouse client as `loom_demo_audit` and enter the password printed
   by the grant.

   ```bash
   docker compose --project-directory "$HOME/.loom" exec clickhouse \
     clickhouse-client --user loom_demo_audit --ask-password
   ```

   Query the history of the decision written by the quickstart's `http` agent.

   ```sql
   SELECT version, actor, updated_at, value, value_withheld
   FROM loom.sql_v1_memory_history
   WHERE org = 'dev'
     AND namespace = 'local'
     AND agent = 'http'
     AND memory_key = 'deploy_window'
   ORDER BY version;
   ```

The history shows both versions, with each `value` returned as a JSON string. The
single-user example leaves `actor` blank. On an authenticated deployment, it identifies
the writer's token subject. The view returns NULL for a redacted `value` or any version
of a key ever labeled `high` and gives the reason in `value_withheld`. Under the
Postgres authority, the version appears once it has been
[published to ClickHouse](../reference/sql-surface.md#freshness).

Run `loom sql-access revoke --user loom_demo_audit` in the Loom container when finished.

## Measured

A synthetic load of memories with 2.4 versions per key and 3072-dimension embeddings (two
vectors per row) took about 13.6 KB per key in ClickHouse, almost all of it the embedding
row; a smaller embedder takes less, and parts with few rows carry somewhat more per row
until they merge.
[Memory at scale](../benchmarks/results.md#memory-at-scale)

## Start

[Quickstart](../getting-started/quickstart.md) · [Prerequisites](../getting-started/overview.md#prerequisites) ·
[Upgrades](../operations/upgrades.md) · [Backup and recovery](../operations/backup-and-recovery.md)
