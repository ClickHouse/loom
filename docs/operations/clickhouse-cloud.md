# ClickHouse Cloud and Managed Postgres

This guide runs Loom on Kubernetes over two managed services: ClickHouse Cloud for
projections, vectors, history and analytics, and ClickHouse Managed Postgres as the memory
authority. It covers the services, the chart's `postgres` values, the checks after the
install, rotating the Postgres password and the CA, and the SQL surface user. The chart
itself is in [Kubernetes](kubernetes.md), the authority in
[Postgres memory authority](postgres.md).

The overlay `deploy/helm/loom/values-clickhouse-cloud.yaml` layers on the EKS one. It is for
a deployment on an empty ClickHouse: the authority is chosen at a deployment's first boot,
and a ClickHouse that already holds memory keeps the ClickHouse authority.

## The services

- **ClickHouse Cloud**: a service with a database named `loom` and a user for Loom, its IP
  access list allowing the cluster's NAT addresses ([Kubernetes, On EKS](kubernetes.md#on-eks)).
- **ClickHouse Managed Postgres**: a service with **two HA standbys**, a database and a role
  that owns it, and the **direct** endpoint on port 5432
  ([Postgres, ClickHouse Managed Postgres](postgres.md#clickhouse-managed-postgres)). Grant
  the role `pg_read_all_stats`, so Loom can see which standbys are attached:

  ```sql
  GRANT pg_read_all_stats TO loom;
  ```

## Secrets

The overlay reads two Secrets beside `loom-secrets`: `loom-postgres`, key `password`, with
the role's password, and `loom-postgres-ca`, key `ca.crt`, with the CA bundle. Make them the
same way as `loom-secrets`, with an ExternalSecret.

`ca.crt` holds every root certificate the service's CA currently publishes, concatenated as
PEM; the driver loads the file as a bundle and trusts nothing else. Take the roots from the
CA's published list (the "CA Issuers" URL in the server certificate names the CA);
`openssl s_client -starttls postgres -showcerts` usually prints the chain without its
root. Convert a root fetched as DER first:

```bash
openssl x509 -inform DER -in root.der -out root.pem
```

Check the bundle against the chain the server sends, trusting nothing but the bundle, as
the driver does:

```bash
openssl verify -no-CApath -no-CAstore -CAfile roots.pem -untrusted chain.pem \
  -verify_hostname <postgres host> leaf.pem
```

A certificate the bundle does not cover fails closed: a pod that boots with it does not
start, and its log shows `SSLCertVerificationError`; in a running pod, every new
connection fails. When the CA announces a change to its hierarchy, update the bundle under
a new Secret name (below).

## Install

```bash
helm install loom deploy/helm/loom -n loom \
  -f deploy/helm/loom/values-eks.yaml -f deploy/helm/loom/values-clickhouse-cloud.yaml \
  --set publicBaseUrl=https://loom.example.com \
  --set clickhouse.host=<service>.<region>.aws.clickhouse.cloud \
  --set postgres.host=<postgres direct host>
```

With `postgres.host` set, the chart gives every pod that runs Loom (web, workers, the hook
Jobs and the analytics export):

- `LOOM_STORAGE_AUTHORITY=postgres`, `LOOM_PG_DURABILITY` and `LOOM_PG_POOL_MAX_SIZE` from
  `postgres.durability` and `postgres.poolMaxSize`;
- `LOOM_PG_DSN` with no password,
  `postgres://<user>@<host>:<port>/<database>?sslmode=verify-full&sslrootcert=/etc/loom/pg-ca/<key>`,
  so the password never sits in the pod spec;
- `PGPASSWORD` from `postgres.passwordSecret`, which the driver reads when the DSN has none;
- `postgres.caSecret` mounted read-only at `/etc/loom/pg-ca`;
- with `networkPolicy.enabled`, egress to `postgres.port`.

The chart refuses to render without `postgres.passwordSecret.name` or
`postgres.caSecret.name`, and when `env`, `extraEnv` or `secrets.extra` also sets one of
the names it renders (`LOOM_STORAGE_AUTHORITY`, `LOOM_PG_DSN`, `LOOM_PG_DURABILITY`,
`LOOM_PG_POOL_MAX_SIZE`, `PGPASSWORD`), or `settingsFile` the matching setting. Its values
win over the same names in `existingSecret` and `envFrom`.

### Connections

Each Loom process holds up to `postgres.poolMaxSize` connections, and a rollout runs the
old and the new pods side by side, so size the service for twice the steady count:

```text
poolMaxSize × (webWorkers × web pods + worker processes) × 2
  + booting processes + running erasures + CLI runs  ≤  max_connections − reserved
```

Web pods are `autoscaling.maxReplicas` with autoscaling on, else `replicaCount`; worker
processes are `workers.replicaCount` with `workers.enabled`, else one, which a web pod
runs. Each running erasure holds one more connection for its Postgres step. The overlay
sets `webWorkers` to 2, what the image picks for the chart's 2-CPU limit, and
`poolMaxSize` to 5: with three web pods and one workers pod that is 70 connections, which
fits a service whose `max_connections` leaves 100 for Loom. Raising `webWorkers` needs
`resources.limits.cpu` and memory (1 GiB per process) to match.
Replicas added past the chart's count by anything else are outside the sum.

## After the install

- Loom refuses to start when the service's `synchronous_standby_names` is empty. It
  starts with fewer standbys attached than it names, logging the WARNING
  `Postgres durability: fewer synchronous standbys are attached than required`, and
  commits wait until they are.
- `GET /v1/health` reports `storage_authority: postgres` and `pg_ok`.
  `GET /v1/health?include=postgres` adds a `postgres` block with `standby: synchronous`;
  the block is missing whenever `pg_ok` is false, when the check's `SELECT 1` fails for
  any reason or takes longer than 2 s.
- A role without `pg_read_all_stats` still starts, with the boot WARNING
  `Postgres durability: cannot see pg_stat_replication; standbys are unverified`, and
  `standby` reads `null`. The WARNING is the reliable sign; `standby` is also `null` when
  its read fails or takes longer than 2 s.

## Rotating the password and the CA

The password, in this order, back to back:

1. Update the password in the secret store and confirm `loom-postgres` has synced.
2. Run `ALTER ROLE loom PASSWORD '…'` on the service.
3. Run `kubectl rollout restart` on the web and workers Deployments together.

Connections already open keep working. Between steps 2 and 3, a call whose pod must open a
new connection (the pool closes connections idle for 300 s) answers 500, and the server
log shows `InvalidPasswordError` (SQLSTATE 28P01); retrying does not help until the pod
restarts. A pod that starts between steps 1 and 2 (a scale-up, an eviction, the analytics
export or a hook Job) fails to boot until step 2, and boots on its next restart.

The CA: create the new bundle under a new Secret name and `helm upgrade` with
`postgres.caSecret.name` set to it. The pod spec changes, so the pods roll.

## The SQL surface user

An org reads its agents' memory as SQL through a ClickHouse user that
`loom sql-access grant` makes ([SQL surface](../reference/sql-surface.md)). Run it in a web
pod with a ClickHouse admin credential that may create users, passing the admin password
from a file, never typed at an echoing terminal:

```bash
pod=$(kubectl get deploy -n loom -o name \
  -l app.kubernetes.io/instance=loom,app.kubernetes.io/component=web)
kubectl exec -i -n loom "$pod" -- sh -c \
  'read -r LOOM_CH_PASSWORD; export LOOM_CH_PASSWORD LOOM_CH_USER=default;
   exec loom sql-access grant --user acme_sql --org acme --namespaces support,research' < admin.pass
```

The command prints the SQL user's password once, to the terminal only: an exec stream is not
a container log. `--namespaces '*'` also covers namespaces made later; never give it on an
org that sign-ups share ([access](../reference/sql-surface.md#access)). `--all` instead of
`--org … --namespaces …` fits a deployment whose data belongs to one org. Revoke the user
before uninstalling: `loom sql-access revoke --user acme_sql`, run the same way.

## Related

[Kubernetes](kubernetes.md) · [Postgres memory authority](postgres.md) ·
[Deployment](deployment.md) · [SQL surface](../reference/sql-surface.md) ·
[Query Loom from Postgres](../guides/query-from-postgres.md)
