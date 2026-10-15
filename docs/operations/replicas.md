# Running N replicas

Every Loom process is a full replica. Run as many as you need on one
ClickHouse service behind one URL, with no sticky sessions: each one serves
every op, verifies every token and writes to the shared service. The work that
must happen once runs on whichever process holds the ClickHouse lease, and
moves when that process stops:

| Once per deployment                       | Where it runs                                                                                              |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Schema migrations                         | the first process to boot a build with pending migrations, under the migrate lease; the others wait for it |
| Background workers                        | the holder of the `workers_supervisor` lease ([deployment](deployment.md))                                 |
| Publishing the deployment's configuration | the same lease holder (below)                                                                              |

Each process keeps its own git mirrors (a history read clones a missing one;
`LOOM_CODE_HISTORY_CLONE_ON_READ`), its own caches, and its own rate-limit
buckets.

## One env for all of them

Start every replica from the same env, with `LOOM_DEPLOYMENT=1`:

```bash
LOOM_DEPLOYMENT=1
LOOM_CH_HOST=...                 # the one ClickHouse service
LOOM_CH_PORT=8443
LOOM_CH_USER=...
LOOM_CH_PASSWORD=...
LOOM_JWT_SECRET=...              # the same root secret on every replica
LOOM_EMBEDDING_MODEL=...         # the same model and width on every replica
LOOM_EMBEDDING_DIM=...
OPENAI_API_KEY=...               # each replica makes its own provider calls
LOOM_TRUSTED_PROXIES=...         # the load balancer, so per-client limits see the client
```

`LOOM_JWT_SECRET` signs as kid `default` and seals every other signing key and
every stored credential, so with one value every token works on every replica.
Signing keys, revocations, grants and service accounts are rows in the shared
service; a rotation or a revoke reaches every replica within
`LOOM_JWT_KEYRING_REFRESH_SECONDS` and `LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS`.

## Configuration drift

Each process writes a heartbeat row to `loom.instances` every
`LOOM_INSTANCE_HEARTBEAT_SECONDS` (10 s by default): its build, a fingerprint
of its MUST_MATCH settings (the embedding space: `LOOM_EMBEDDING_PROVIDER`, `_MODEL`,
`_DIM`, `_BASE_URL` and `_SEND_DIMENSIONS`), a
fingerprint of its root secret and its start time. A row not renewed for three
intervals, or one its process marked stopped at shutdown, is not live.

The lease holder publishes its configuration to `loom.deployment_config`:
every setting its env sets explicitly plus the MUST_MATCH ones, without
secrets, and an HMAC fingerprint of its root secret. Every process compares
itself with that snapshot at boot and on every heartbeat. A different root
secret (its tokens and stored credentials would not open elsewhere) or a
different MUST_MATCH value (its vectors would be in another space) is drift:
the process is not ready, so the load balancer sends it nothing, and
`/v1/health` reports the `config_drift` problem with what differs. The values
compared are the env's: a model switch from the dashboard is the deployment's
(every replica reads it from ClickHouse), so it is no drift, and a replica
started with the same env after it is ready.
`/v1/health` `deployment_config` shows the last check and the live hosts.

The lease holder never publishes over a snapshot it drifts from: a replica
with the wrong root secret or embedding model that wins the lease stays not
ready, and the replicas on the published env stay ready. On the other settings
it rewrites the snapshot only when no other live replica still runs the stored
one, so a rollout with a mixed env never flips the published configuration
back and forth. Once the last replica on the old env is gone, the lease holder
publishes the new one within a heartbeat.

**Changing a MUST_MATCH setting or `LOOM_JWT_SECRET`** cannot roll: replicas on
the new value are not ready while replicas on the old one serve. Stop every
replica, change the env, run `loom deployment publish --yes` with the new env
(it lists what changes), then start them again (a full re-embed follows an
embedding change, `loom reembed`). After a secret change re-mint tokens and
store the credentials again.

Processes without `LOOM_DEPLOYMENT` (a branch or A/B server on the same
service) heartbeat too, but never publish, are never checked and do not count
as replicas.

## Rate limits

The configured caps are deployment-wide. Each replica's share is the cap
divided by the hosts with a live `loom.instances` row, so adding or removing a
replica resizes every bucket within a heartbeat. `LOOM_REPLICA_COUNT` is the
floor of that count, and the count while the rows cannot be read. Web workers of
one host share one bucket table ([deployment](deployment.md)).

## Code repositories

Every process re-reads `loom.code_repos` every `LOOM_CODE_REPOS_REFRESH_SECONDS`
(30 s by default), so a repository added or restricted on one replica applies on
every replica within that time.

A host that should serve no git history (an edge with no disk for mirrors) sets
`LOOM_GIT_HISTORY=none`: blame, commit checks and PR history then answer without
it.

## Stopping a replica

Stop it gracefully (`docker stop`, SIGTERM): it marks its heartbeat row stopped
and retires its provider participant row. A forced kill leaves both; the
heartbeat row stops counting after three intervals. A process that restarts in
the same container retires the dead one's participant row itself. If a provider
reconfiguration waits on a dead process's row from elsewhere, retire it with the
`participant_id` its `/v1/health` reported, once you have confirmed it is gone
(the command refuses one whose heartbeat is fresh or whose queries still run):

```bash
loom participant retire --participant <participant_id> --yes
```

## Moving from read-replica mode

Read-replica mode is retired: every Loom is a full replica. Before upgrading a
deployment that ran read-replicas:

- Give each former replica the main Loom's env, including its provider keys
  and `LOOM_DEPLOYMENT=1`: it now embeds, completes and reads git history
  itself, and the `embedding.embed` and `llm.complete` ops it used to call on the
  main Loom are gone.
- Remove `LOOM_READ_REPLICA`, `LOOM_READ_REPLICA_MAIN_URL`,
  `LOOM_READ_REPLICA_CONFIG_REFRESH_SECONDS`,
  `LOOM_READ_REPLICA_FORWARD_TIMEOUT_SECONDS`,
  `LOOM_READ_REPLICA_LLM_TIMEOUT_SECONDS` and
  `LOOM_READ_REPLICA_MAIN_PROBE_SECONDS`. A process that still has them ignores
  them and names them in its boot warning.
- `loom read-replica retire` is now `loom participant retire`.
