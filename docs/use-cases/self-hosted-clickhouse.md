---
title: Self-hosted agent memory on ClickHouse
description: Evaluate self-hosted agent memory for coding agents. Verify local semantic recall and query decision history and writer provenance with SQL.
seo:
  title: Self-hosted agent memory with SQL
---

# Self-hosted agent memory on ClickHouse

Loom runs on your own infrastructure, with Docker or on Kubernetes with its Helm chart, and
stores agent memory, documents, conversations and code indexes as tables in ClickHouse. A
deployment can choose Postgres at first boot for live memory and knowledge state. Loom's
stores and model providers are separate deployment choices. What your agents remember
is queryable with SQL.

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

## Choose self-hosted memory for coding agents

For a team deployment, record where each component sends data before giving it repository
or conversation access. Review every destination against your network and data policies.

| Component      | What to verify                                                                                                                                                                                                                                                                             |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Storage        | Record who operates the databases, the document bucket and the backup destinations. Check the [storage authority](../architecture/storage-authority.md) and [backup and recovery plan](../operations/backup-and-recovery.md).                                                              |
| Loom inference | The released stack includes a local CPU embedder. For extraction, summaries and answers, check the selected [LLM and any per-job or fallback models](../getting-started/models-and-keys.md#keep-every-llm-job-on-your-chosen-provider).                                                    |
| Coding client  | Check the client’s model provider and capture settings. The client can pass retrieved memories to its model. Use the [coding-agent connection guide](../getting-started/connect-a-coding-agent.md) to configure capture for each client.                                                   |
| Telemetry      | Check the [trace exporter](../features/observability/telemetry.md#exporters) and any trace sinks. Loom sends aggregate self-hosted adoption reports by default. You can [disable those reports on each server and worker](../features/observability/telemetry.md#can-i-disable-telemetry). |

### Verify semantic recall on a private network

Start a disposable deployment from the [released Docker stack](../getting-started/quickstart.md#fast-path-released-image-no-clone).
Pull the images before restricting runtime traffic. Keep the bundled local embedder,
use a ClickHouse the server can reach on the private network, and restrict runtime
egress to the destinations you approved.

1. [Check `/v1/health`](../getting-started/quickstart.md#check-the-stack). With the bundled
   embedder, `fake_embedding` should be `false`. Record the model and provider settings
   with the health response.
2. [Save a test fact over HTTP](../getting-started/quickstart.md#4-first-memory-over-http),
   such as “Production deployments are allowed only on Tuesdays and Thursdays.”
   Check the exact key with `memory.get`. Ask “What days can we deploy changes to
   production?” with `memory.search` and `search_mode="semantic"`. Verify that search
   returns the saved key while the network restriction is in effect.
3. If the team needs extraction or generated answers, configure its approved LLM and
   run those examples separately. Check the destinations observed at runtime for both
   Loom and the coding client.

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

### Count active memories by agent and source

Use the same SQL reader to see how many active, unexpired memories each agent has by
provenance class. This report returns counts without memory keys or values.

```sql
SELECT
    agent,
    source_class,
    count() AS active_memories,
    countIf(value_withheld != '') AS withheld_values
FROM loom.sql_v1_memories
WHERE org = 'dev'
  AND namespace = 'local'
  AND status = 'active'
  AND (expires_at IS NULL OR expires_at > now())
GROUP BY agent, source_class
ORDER BY agent, source_class;
```

`withheld_values` counts active rows whose values the view suppresses for sensitivity or
redaction. It is a count within `active_memories`. In a reporting job, provision a reader
for the namespaces the job covers. The view reads the
[granted scope](../reference/sql-surface.md#what-a-read-costs) before applying the report's filters.

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
