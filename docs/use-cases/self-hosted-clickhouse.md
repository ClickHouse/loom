# Self-hosted agent memory on ClickHouse

Loom runs on your own infrastructure, with Docker or on Kubernetes with its Helm chart, and
stores agent memory, documents, conversations and code indexes as tables in ClickHouse. A
deployment can choose Postgres at first boot for live memory and knowledge state. Data is
stored only in your stores (hosted model providers you configure receive the text they
process), and what your agents know is queryable with SQL.

## What you get

- **Docker or Kubernetes.** The one-line installer starts the local stack; the Helm chart
  runs N identical replicas behind one URL. [Deployment](../operations/deployment.md) ·
  [Kubernetes](../operations/kubernetes.md) · [Running N replicas](../operations/replicas.md)
- **ClickHouse OSS or ClickHouse Cloud.** [ClickHouse Cloud and Managed Postgres](../operations/clickhouse-cloud.md)
- **One store for vectors, text and audit.** Vector, lexical and graph search, filters,
  audit and erasure are SQL over the same rows. [Storage authority](../architecture/storage-authority.md)
- **Multi-user.** Orgs, namespaces, roles, tokens and single sign-on.
  [Identities and namespaces](../concepts/identities-and-namespaces.md) ·
  [Single sign-on](../operations/sso.md)
- **Local models.** The released stack embeds on CPU with a bundled local model, and the
  providers accept an OpenAI-compatible base URL.
  [Models and API keys](../getting-started/models-and-keys.md)

## Start

[Quickstart](../getting-started/quickstart.md) · [Prerequisites](../getting-started/overview.md#prerequisites) ·
[Upgrades](../operations/upgrades.md) · [Backup and recovery](../operations/backup-and-recovery.md)
