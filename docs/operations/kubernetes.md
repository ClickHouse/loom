# Kubernetes

`deploy/helm/loom` runs Loom as [N identical replicas](./replicas.md) on Kubernetes: one
Deployment of web pods over one ClickHouse service, each pod a full replica. It works on
any conformant cluster; `values-eks.yaml` sets it up on EKS and `values-kind.yaml` runs
it on a laptop.

| Object                                | What it does                                                                                                                                                  |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Deployment `<release>`                | The web pods. Rolls one pod at a time and takes none down before its replacement is ready.                                                                    |
| Service, optional Ingress             | Routes to ready pods only. The readiness probe is `/v1/health/ready`.                                                                                         |
| Secret                                | The keys the Loom pods share (the extractor's read only its token). Bring your own with `secrets.existingSecret`. The rest of the env is in the pod template. |
| ConfigMap `<release>-settings`        | The settings file (`settingsFile`), mounted at `/app/config/loom.yaml` on every pod; each hook Job carries its own copy.                                      |
| PodDisruptionBudget                   | One pod at a time on node drains and upgrades.                                                                                                                |
| Optional HPA                          | CPU-based. Rate-limit shares resize as pods come and go.                                                                                                      |
| Optional `<release>-workers`          | Background workers in their own pods (`workers.enabled`). Off by default: one web pod holds the lease and runs them.                                          |
| Optional `<release>-publish`          | A pre-upgrade Job for a change that cannot roll ([below](#changing-what-cannot-roll)).                                                                        |
| Optional `<release>-analytics-export` | A daily CronJob and its own ServiceAccount (`analyticsExport.enabled`, [below](#analytics-export)).                                                           |
| Optional `<release>-extractor`        | The document extractor's Deployment, Service and NetworkPolicy (`extractor.enabled`, [below](#document-extractor)).                                           |
| Optional dev ClickHouse               | `devClickhouse.enabled`: one ClickHouse pod with embedded Keeper. For test clusters only.                                                                     |

## Before you install

- **ClickHouse.** One service for every pod: ClickHouse Cloud, or a self-managed server
  with the `deploy/clickhouse-config/` files mounted and Keeper
  ([requirements](./deployment.md#clickhouse-requirements)). Create the database:
  `CREATE DATABASE loom`. On a self-managed replicated cluster, point every pod at one
  replica.
- **A root secret.** `LOOM_JWT_SECRET`, at least 32 random bytes
  (`openssl rand -hex 32`). Every pod shares it, and it seals the signing keys and stored
  credentials in ClickHouse, so keep a copy outside the cluster.
- **A public URL.** `publicBaseUrl`, what clients reach Loom on. Pods refuse to start
  without it.

## Install

```bash
helm install loom deploy/helm/loom -n loom --create-namespace \
  --set publicBaseUrl=https://loom.example.com \
  --set clickhouse.host=abc123.us-east-1.aws.clickhouse.cloud \
  --set secrets.jwtSecret=$(openssl rand -hex 32) \
  --set secrets.clickhousePassword=... \
  --set secrets.extra.OPENAI_API_KEY=...
kubectl -n loom rollout status deploy/loom
```

The first pod runs the migrations under the migrate lease and the others wait for it,
so the first install takes a few minutes. The startup probe allows 15 minutes
(`probes.startup`), which also covers cloning the git mirrors.

The embedding settings go through `embedding.*`, and the root secret and its previous
value through `secrets.jwtSecret` and `secrets.jwtSecretPrevious`; the chart guards these
(below). The root secret can instead live in the `secrets.existingSecret` Secret, whose
contents the chart cannot check. The chart refuses all of them in `env`, `extraEnv`,
`settingsFile` and `secrets.extra`, and an `envFrom` source must not carry them either. Every other
setting goes in `env` (plain values) or `secrets.extra` (keys), named as in
the [configuration reference](../reference/configuration.md). `settingsFile` takes the same
settings as the YAML settings file holds them, lowercase and typed:

```yaml
settingsFile:
  memory_search_default_mode: rrf
  memory_ask_recency_weight: 0.3
```

The chart always renders the settings file (an empty `settings` map when `settingsFile`
is empty), mounts it at `/app/config/loom.yaml` on every pod and on the publish, reseal
and rollback-check Jobs (each from its own copy, rendered with the Job), and rolls the
pods when it changes; `env` wins over it. A pod of the previous ReplicaSet that starts
during a rollout still finds the file, and reads the new revision's copy: a pod of the
previous build logs a setting it does not have and ignores it, so a new setting can go
in the same upgrade, but it refuses to start on a value only the new build accepts (a
new choice of an existing setting, a widened bound). Set such a value in the upgrade
after the one that changes the image. A live setting can also change without a rollout
([runtime settings](./runtime-settings.md)). `values.yaml` documents the chart's own values.

### On a laptop

```bash
kind create cluster
helm install loom deploy/helm/loom -f deploy/helm/loom/values-kind.yaml \
  --set secrets.jwtSecret=$(openssl rand -hex 32)
kubectl port-forward svc/loom 7777:7777
```

This runs ClickHouse inside the release, the fake embedder and no repository-job
sandbox. Loom's pods meet the `restricted` Pod Security Standard; the dev ClickHouse
does not. Mint a token with
`kubectl exec deploy/loom -- loom mint-token --subject me --org dev --namespaces '*' --roles admin`.

### On EKS

`values-eks.yaml` assumes the AWS Load Balancer Controller, the EBS CSI driver with a
`gp3` StorageClass and ClickHouse Cloud. Replace its placeholders (hostname,
certificate, account, VPC range) and install with it:

```bash
helm install loom deploy/helm/loom -n loom -f deploy/helm/loom/values-eks.yaml
```

- **ALB to pod IPs.** `target-type: ip` sends to pods directly. Label the namespace
  before the install, so each new pod carries the controller's readiness gate and a
  rollout removes no old pod before the ALB sends to its replacement:

  ```bash
  kubectl label namespace loom elbv2.k8s.aws/pod-readiness-gate-inject=enabled
  ```

  The target group checks `/v1/health/ready` every 5 s. A stopping pod keeps serving
  for 20 s (`shutdown.readyDelaySeconds`) while the controller deregisters it, and the
  deregistration delay (50 s, `readyDelaySeconds` + `gracefulSeconds`) lets it finish
  what it holds. The 300 s idle timeout covers long calls, and Loom keeps idle
  connections open longer (`keepAliveSeconds`, 310 s), so the ALB never reuses one Loom
  has just closed. The image reads it as `LOOM_KEEP_ALIVE_S`.

- **`trustedProxies`.** Set it to the ALB's subnets, or every client counts as the ALB
  for the per-address sign-in limits. Not the whole VPC: any pod there could then set
  `X-Forwarded-For`.
- **Secrets.** The overlay names a Secret `loom-secrets`. With the External Secrets
  Operator and AWS Secrets Manager:

  ```yaml
  apiVersion: external-secrets.io/v1
  kind: ExternalSecret
  metadata: { name: loom-secrets, namespace: loom }
  spec:
    secretStoreRef: { name: aws-secrets-manager, kind: ClusterSecretStore }
    target: { name: loom-secrets }
    refreshInterval: "0" # sync on demand only (see below)
    dataFrom:
      - extract: { key: loom/production } # LOOM_JWT_SECRET, LOOM_CH_PASSWORD, OPENAI_API_KEY
  ```

- **Identity.** The service account carries an IRSA role annotation, for Bedrock, S3
  backups or the document bucket. With EKS Pod Identity, bind the role to the service
  account and drop it.
- **Zones.** Pods spread over zones, then nodes, and still schedule when a zone is short.
  The spread is kept at scheduling only: after a drain two pods can share a zone until
  the next rollout or the descheduler moves one.
- **Cache on gp3.** Each pod gets its own ephemeral volume for `/app/var` (git mirrors,
  the Go module cache), created with the pod and deleted with it. The StorageClass needs
  `volumeBindingMode: WaitForFirstConsumer`, so each volume is made in its pod's zone.
- **NetworkPolicy.** On, with ingress from the ALB's subnets only. Pods reach DNS,
  ClickHouse, the EKS Pod Identity agent and port 443 anywhere but the instance metadata
  service; the cluster needs a policy engine (the VPC CNI's network policy agent, Calico
  or Cilium). Also set the node groups' IMDS hop limit to 1. A ClickHouse inside the
  cluster needs its own rule, as engines differ on matching pod IPs by range:
  `networkPolicy.extraEgress: [{to: [{namespaceSelector: …, podSelector: …}], ports: [{port: 8123}]}]`.
- **ClickHouse Cloud's IP access list** must allow the cluster's NAT gateway addresses.
- **Image.** Pin `image.digest`, and `extractor.image.digest` with `extractor.enabled`;
  each release's run summary prints both, after each image passed a CVE scan.

## Upgrades and scaling

`helm upgrade` with a new `image.tag` rolls the pods one at a time. A new pod starts
serving only after its startup probe passes, and the old one is removed after that. The
mixed-version window is safe: builds N-1 and N run side by side on N's schema
([rolling upgrades](../architecture/rolling-upgrades.md)). Each pod template carries its
env, so a pod of the previous ReplicaSet that restarts mid-rollout keeps its own. The
Secret is one for every pod, so a change to it reaches each pod as it next starts; a
chart-managed Secret change rolls the pods through its checksum, and an `existingSecret`
change needs `kubectl rollout restart`. Keep a reloader off the Secret that holds
`LOOM_JWT_SECRET`: a root change has to go through a publish (below). For an upgrade whose
release notes ask for every pod at once (state that moves between builds), add
`--set strategy.rollingUpdate.maxSurge=100%`, and set it back on the next upgrade:
`--reuse-values` keeps it.

A stopping pod answers not-ready and keeps serving for `shutdown.readyDelaySeconds`
(15 s) while the Service and the balancer drop it. uvicorn then waits up to
`shutdown.gracefulSeconds` (30 s, the longest `task.pull` wait) for requests in flight,
and the lifespan drains and retires the pod. `terminationGracePeriodSeconds` is the sum
of the three `shutdown` values, 95 s by default. Behind a balancer with a slower check,
raise `readyDelaySeconds`. A pod that is killed (OOM, node loss) fails only the requests
it held; a client's retry lands on another pod.

Scaling is `replicaCount`, `kubectl scale` or the HPA. It changes no pod config, so it
rolls nothing. With `autoscaling.enabled` each revision records the Deployment's live
replica count (at least `autoscaling.minReplicas`), so turning the HPA on does not first
scale it down. A `helm rollback` writes the count its revision recorded, and the HPA
then corrects it: size `minReplicas` for the load a rollback must carry. The workers lease moves when its holder stops: within seconds on a
graceful stop, and within the 30 s lease TTL when a pod or node dies. Every pod is
labelled `deploy:k8s:<pod>`, so a new leader runs the workers whose last pass is
overdue rather than waiting out their interval again.

A pod that dies without stopping (node loss, eviction past the grace period) leaves its
provider participant row, which would hold the next embedding or model change from the
dashboard. With `retireLostPods` (the default) the workers leader retires it once its
heartbeat expired, no query of it runs and Kubernetes says the pod is gone: its object
deleted or replaced, or the pod `Failed` (evicted). That is a minute or two after a
deletion or an eviction, and after a node loss once Kubernetes deletes the node's pods
(five minutes of `NotReady` by default). It needs `get` on pods in the namespace (a Role
the chart creates for the release's service account) and that account's token, which only
the pods that may hold the workers lease mount. Without it, `/v1/health` names the row;
once the pod is gone, retire it with
`loom participant retire --participant <id> --yes` ([replicas](./replicas.md#stopping-a-replica)).

There is no migration Job. Every pod migrates at boot under a ClickHouse lease, so a
Job would only repeat that step, and a pre-upgrade hook would run the new schema while
every old pod still serves, outside the window the carry barrier protects.

## Changing what cannot roll

The embedding settings (`embedding.*`) and `LOOM_JWT_SECRET` must be the same on every
pod. A pod whose values differ from the deployment's published ones is not ready, so
these do not roll. The chart refuses an upgrade that changes them: it keeps a hash of
the embedding settings on the Deployment and compares the root secret with its Secret.

```text
Error: UPGRADE FAILED: ... this upgrade changes the embedding settings or LOOM_JWT_SECRET ...
```

To make the change on purpose, upgrade with `--set mustMatchChange.publish=<token>`, any
value the pods don't carry yet, such as `$(date +%s)`: the upgrade that brings a new token
publishes, and later ones that keep it (`--reuse-values`) don't. A pre-upgrade Job runs
`loom signing-key record-root` and `loom deployment publish --yes` with the new values.
From that moment the old pods are not ready and answer calls on the connections they
still hold with `503 CONFIG_DRIFT`, and the rollout replaces every pod at once
(`maxSurge: 100%`). The outage lasts until the first new pod is ready and registered with
the balancer: about 10 s with the image on the node, plus the image pull and the ALB's
registration otherwise. Change nothing else in that upgrade (no new image, no pending
migration), and pull the image onto the nodes first. The Deployments keep that surge
strategy until the next upgrade: follow the publish with a `helm upgrade --reuse-values`
(the same token, so no publish), or a later `kubectl rollout restart` surges every pod.
A `helm rollback` to the publish revision brings that strategy back too: follow it the
same way.
Then:

- **New root secret:** tokens the old root signed (kid `default`) stop verifying at once;
  mint new ones. Tokens signed with a keyring key (`loom signing-key rotate`) keep
  verifying. Set `secrets.jwtSecretPrevious` to the old root in the same upgrade, and what
  it sealed keeps opening: after the rollout a `post-upgrade` Job runs `loom secrets
reseal`, which seals the stored credentials, signing keys and rebuild recipes again under
  the new root (the publish Job checks first that it can, so a failing one publishes
  nothing). The previous root only opens sealed values; it never verifies a token. Remove
  it in a later upgrade once `kubectl exec deploy/loom -- loom secrets reseal --check`
  exits 0. Until then it also counts the SSO sign-ins in flight (a code, a device grant,
  an approval or link awaiting its click) whose identity-provider token only the old root
  opens: they are not rewritten, and expire within minutes; the check names when the last
  one does. A value it lists as opening with neither root was lost before this rotation:
  enter it again. Without it, the stored credentials stop opening: enter them again.
  If the old root leaked, also rotate the signing keys and retire the old kids
  ([tokens and grants](../guides/tokens-and-grants.md#revoke-and-rotate)).
- **New embedding model:** run `loom reembed` in a pod
  ([deployment](./deployment.md)).

Run the upgrade with `--wait` and a `--timeout` above the startup budget (15 minutes), and
never with `--atomic`: a rollback after the Job has published leaves every pod drifted. If
the upgrade fails after the Job ran (the Job half-done, or the apply refused), every pod
is drifted: run the same upgrade again with the same token, which publishes again. If
the upgrade timed out after the rollout started, the pods already carry the token and the
retry does not publish; while `secrets.jwtSecretPrevious` is set, every upgrade's
`post-upgrade` Job reseals what is left, and `loom secrets reseal` does it by hand.

Every `helm rollback` first runs `loom deployment publish --check` with the target
revision's env, and refuses a rollback whose root secret or embedding settings are not the
published ones: its pods would never be ready, and the Secret it re-applies would drift
every pod that restarts. Helm then leaves the release `pending-rollback`; a
`helm rollback loom <current revision>` passes the check and clears it. To go back past a
publish, upgrade with the old values and a new token instead.

Installing onto a ClickHouse whose deployment already published another root secret or
embedding space (a move from another install, a restore) brings up only drifted pods, as
hooks do not publish on install: follow the install with an upgrade that sets a token.

With `secrets.existingSecret` the chart cannot see the Secret's contents, so it does not
guard a new root secret there. A pod that starts after the Secret changed and before the
publish is drifted and never ready, so change it only together with a publish: with the
External Secrets Operator, update Secrets Manager (the new `LOOM_JWT_SECRET`, and the old
one as `LOOM_JWT_SECRET_PREVIOUS`; `secrets.jwtSecretPrevious` applies to the chart's own
Secret only), force a sync
(`kubectl annotate externalsecret loom-secrets force-sync=$(date +%s) --overwrite`), wait
for the ExternalSecret to be `Ready`, then upgrade with a new token, which rolls the pods
so they read it. The guard and the live replica count read the release with `lookup`,
which `helm template` (and so Argo CD) does not run: there the guard is off, and every
render with a token set would publish again, so never set `mustMatchChange.publish` there.
A publish is then `loom secrets reseal --dry-run && loom signing-key record-root && loom
deployment publish --yes` in a pod with the new env (the dry run checks the previous root
while the old one is still the recorded root), then `loom secrets reseal` once the new pods
serve. A `loom login` code shown while the root changes stops matching (its key derives
from the root): run `loom login` again. So does an SSO sign-in started on a pod with the
other root: start it again.

## Cache and git mirrors

Each pod clones its own mirrors at startup and keeps them fetched. A pod is ready before
its clones finish: until a mirror is there, history reads (blame, commit checks, PR
history) answer `no_git_mirror` and the rest of the code plane serves from ClickHouse.
A clone takes as long as the repository: a bare clone of a large monorepo (14 GB) is
about 12 minutes, two at a time per pod. To hold traffic until the mirrors are cloned,
set `env.LOOM_READY_REQUIRES_MIRRORS: "true"` and raise `probes.startup`.

The mirrors live in the pod's cache at `/app/var`, with the Go module cache, and are
lost with the pod. The default `emptyDir` (`cache.sizeLimit`, 20 GiB) counts against the
node's storage, and the kubelet evicts a pod that outgrows it. `cache.volume: ephemeral`
gives each pod its own volume from a StorageClass instead (`cache.ephemeral`). Size it
for full clones of every indexed repository plus the module cache. `workers.cache` is
merged over `cache` for the workers pods, which also keep a checkout per compiler tier:
about 30 GB beside the mirror for a ClickHouse-sized C++ entry with its build tree.

`cache.ephemeral.dataSourceRef` starts each new pod's volume from a snapshot instead of
empty, e.g. a `VolumeSnapshot` of a warm workers volume (on EKS this needs the CSI
snapshot controller and a `VolumeSnapshotClass` for the EBS driver). The mirrors and
checkouts then fetch only what is new, and the C++ tier skips its CMake configure.
Refresh the snapshot now and then: an old one still works, it just leaves more to fetch.

## Compiler tiers

Every compiler tier runs in the workers process: the per-merge deltas, the weekly C++
full re-index (`code_scip_full`, which also seeds a new entry), the SQL ↔ C++ crossref
and the checkout repacks. The image carries every indexer; scip-clang is in the amd64
image only, because upstream builds no arm64 Linux binary, so the chart schedules on
`kubernetes.io/arch: amd64` nodes by default. On arm64 set `nodeSelector` to
`{kubernetes.io/arch: null}` and mount your own scip-clang
(`env.LOOM_CODE_SCIP_CLANG_PATH`), or the C++ tier skips with `scip-clang missing`.

A C++ full index is the workers pod's peak. scip-clang runs `workers.scipClangJobs` (4)
translation units at once at a few GB each, and the default `workers.resources` (32 GiB
limit) is sized for that. Without the workers Deployment the web pod that holds the
lease runs them with one job inside its own limits. The index and its delta are written
under `/app/var/scip-work` on the cache volume (`LOOM_CODE_SCIP_WORK_DIR`), not `/tmp`:
several GB for a full run of a large tree.

## Repository-job sandbox

Repository jobs run repository code ([repository-job sandbox](./code-index-operations.md#repository-job-sandbox)).
Loom runs them under bubblewrap, which needs unprivileged user namespaces. The default
pod security does not allow them, so by default (`sandbox.required: false`) the compiler
tiers of the repositories the operator lists in `LOOM_CODE_REPOS_JSON` run as plain
processes in the pod, and a repository added from the dashboard is refused. List only
repositories you trust. With `sandbox.required: true` a pod is not ready until the sandbox
works, so set it only once the nodes are prepared:

1. Run Loom on nodes whose OS allows unprivileged user namespaces.
2. Install `dev/seccomp/loom-repo-sandbox.json` under the kubelet's seccomp directory
   (`/var/lib/kubelet/seccomp/`) and load `deploy/apparmor/loom-repo-sandbox` on those
   nodes, for example with a DaemonSet or the node image.
3. Point the container at them:

   ```yaml
   sandbox:
     required: true
     hostUsers: false
     securityContext:
       seccompProfile: { type: Localhost, localhostProfile: loom-repo-sandbox.json }
       appArmorProfile: { type: Localhost, localhostProfile: loom-repo-sandbox }
       procMount: Unmasked
   ```

`procMount: Unmasked` needs a user namespace (`sandbox.hostUsers: false`, Kubernetes 1.33
or later, where `ProcMountType` and `UserNamespacesSupport` are on), and the namespace's
Pod Security level must allow it (`baseline` and `restricted` do not). Run
`dev/sandbox_probe.sh` in a pod to check the result before setting `sandbox.required: true`.

## Separate workers

`workers.enabled: true` moves the background workers to their own Deployment
(`workers.replicaCount`, `workers.resources`). Its pods take no traffic and elect one
leader among themselves. The web pods run no workers. Use it when the workers need
more memory than a web pod, or to scale them apart from it. With `replicaCount: 2`
the second pod is a standby with warm mirrors, and a PodDisruptionBudget keeps one of
the two through a drain.

## Document extractor

`extractor.enabled: true` runs the document extractor service ([document
extraction](../architecture/document-extraction.md)) as a Deployment and a Service of
its own, `extractor.replicaCount` pods (two by default), each running one extraction at
a time. The Loom pods get `LOOM_DOCUMENT_EXTRACTOR_URL` pointing at it, unless `env` or
`extraEnv` sets one, and both sides read the bearer token from the release Secret's
`LOOM_DOCUMENT_EXTRACTOR_TOKEN` key: `secrets.extractorToken` (16 characters or more;
`openssl rand -hex 24`), or that key in `secrets.existingSecret`. The extractor pods
read that one key and nothing else of the Secret, mount no service-account token, run as
uid 10001 on a read-only root filesystem with every capability dropped, and carry labels
of their own, so no policy or Service that selects the Loom pods selects them. Their
NetworkPolicy, rendered whatever `networkPolicy.enabled` says, admits the release's Loom
pods on 8790 and allows no egress; with `networkPolicy.enabled`, the Loom pods' policy
allows egress to them. `extractor.env` takes the service's `LOOM_EXTRACTOR_*` limits
([extractor](../../extractor/README.md)), and `extractor.resources` bounds each pod.
Each side holds one token, so changing `secrets.extractorToken` restarts both
Deployments, and while they roll, a Loom pod and an extractor pod may hold different
tokens: extraction answers unavailable until both rolls finish.

## Analytics export

`analyticsExport.enabled: true` adds a daily CronJob that writes the previous UTC day's
usage and a namespace snapshot to an S3 bucket as Parquet, under a ServiceAccount of its
own ([exporting usage to a data warehouse](./analytics-export.md)).

## Checks

```bash
helm test loom -n loom                       # live, ready and /v1/health through the Service
kubectl -n loom exec deploy/loom -- loom doctor
kubectl -n loom logs -l app.kubernetes.io/instance=loom --tail=-1 | grep -E "WorkerSupervisor: starting|supervisor lease: acquired"
```

## Sign-in

SSO is configured as on any host ([single sign-on](sso.md)): the issuer and client id
in `env`, the client secret in the Secret.

```yaml
env:
  LOOM_OAUTH_LOGIN_PROVIDER: oidc
  LOOM_OAUTH_ISSUER: https://loom.example.com # the ingress host; the callback is built from it
  LOOM_OIDC_ISSUER: https://your-org.okta.com/oauth2/default
  LOOM_OIDC_CLIENT_ID: your-client-id
secrets:
  extra:
    LOOM_OIDC_CLIENT_SECRET: your-client-secret # or a key of secrets.existingSecret
```

Register `<LOOM_OAUTH_ISSUER>/oauth/callback` with the identity provider. Every
replica must run the same provider: set it in one place, and roll it out like any
other change; a sign-in whose steps land on two replicas that disagree is started
again. Run `loom sso adopt` (see the SSO page) from one pod, after the rollout:

```bash
kubectl -n loom exec deploy/loom -- loom sso adopt --domain example.com
```

## Model calls through an inference gateway

To send Loom's own model calls through a gateway that takes a workload token instead
of a provider key ([models and keys](../getting-started/models-and-keys.md#through-an-inference-gateway)),
mount a projected service-account token and point Loom at it:

```yaml
env:
  LOOM_OPENAI_BASE_URL: https://gateway.example/v1
  LOOM_ANTHROPIC_BASE_URL: https://gateway.example
  LOOM_INFERENCE_TOKEN_URL: https://gateway.example/oauth/token
  LOOM_INFERENCE_WORKLOAD_TOKEN_FILE: /var/run/secrets/inference/token
extraVolumes:
  - name: inference-token
    projected:
      sources:
        - serviceAccountToken:
            audience: gateway.example # what the gateway expects
            expirationSeconds: 3600
            path: token
extraVolumeMounts:
  - name: inference-token
    mountPath: /var/run/secrets/inference
    readOnly: true
```

The kubelet renews the file before it expires and Loom rereads it on each exchange,
so nothing restarts. The gateway trusts the cluster's OIDC issuer for this service
account (`serviceAccount.name`); that trust is set up on the gateway's side. The
NetworkPolicy's egress to port 443 already covers the gateway and the identity
provider.

A gateway that verifies the projected token itself takes it as the bearer: drop the
token URL and set the bearer mode. Loom rereads the file at most once a minute, and at
once after a 401. To name the agent each call is for, add identity headers
([models and keys](../getting-started/models-and-keys.md#naming-the-agent-on-every-call)):

```yaml
env:
  LOOM_INFERENCE_WORKLOAD_TOKEN_MODE: bearer
  LOOM_INFERENCE_WORKLOAD_TOKEN_FILE: /var/run/secrets/inference/token
  LOOM_INFERENCE_GATEWAY_HOSTS: gateway.example
  LOOM_INFERENCE_IDENTITY_HEADERS_JSON: '{"X-Agent": "{agent}"}'
```

## Related

- [Running N replicas](./replicas.md) and [deployment](./deployment.md#n-identical-replicas)
- [Upgrades](./upgrades.md), [monitoring](./monitoring.md)
