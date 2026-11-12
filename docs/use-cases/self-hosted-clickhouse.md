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

## Measured

A synthetic load of memories with 2.4 versions per key took about 13.6 KB per key in
ClickHouse, almost all of it the embedding row; the runs used local containers on a shared
host, so compare rows of one setup rather than read them as capacity figures.
[Memory at scale](../benchmarks/results.md#memory-at-scale)

## Start

[Quickstart](../getting-started/quickstart.md) · [Prerequisites](../getting-started/overview.md#prerequisites) ·
[Upgrades](../operations/upgrades.md) · [Backup and recovery](../operations/backup-and-recovery.md)
