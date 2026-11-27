---
seo:
  description: Choose a Loom deployment, manage access, and run backups, monitoring, and upgrades.
mode: wide
---

# Operations

Deploy Loom for a team, choose storage and identity settings, and maintain a running instance.

## Plan a deployment

- [Deploy Loom](deployment.md)
  Choose a deployment shape and configure storage, providers, and the public URL.
- [Run on Kubernetes](kubernetes.md)
  Install the Helm chart and select your ClickHouse and Postgres setup.
- [Set up single sign-on](sso.md)
  Configure OIDC for the dashboard, CLI, and MCP clients.
- [Choose the storage authority](../architecture/storage-authority.md)
  Decide where Loom stores live memory, knowledge state, and document bytes.

## Run the service

- [Monitoring](monitoring.md) lists health checks, metrics, and traces.
- [Troubleshooting](troubleshooting.md) starts with symptoms such as empty search results and connection failures.
- [Backup and recovery](backup-and-recovery.md) covers ClickHouse, Postgres where used, and document originals.
- [Upgrades](upgrades.md) explains version checks, migrations, and rolling updates.
- [Maintenance](maintenance.md) covers retention, rotation, and scheduled operator tasks.
- [Code-index operations](code-index-operations.md) covers indexing, freshness, and failures.

## Configure access and integrations

Manage [tokens and grants](../guides/tokens-and-grants.md), choose
[models and API keys](../getting-started/models-and-keys.md), and find settings in the
[configuration reference](../reference/configuration.md).
