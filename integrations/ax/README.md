# Loom on AX

[AX](https://github.com/google/ax) runs agents as sandboxed `Task`s on
[Agent Substrate](https://github.com/agent-substrate/substrate). The tasks are
cheap to create, suspend and throw away. Each sandbox starts empty, and AX has
no memory layer. Loom adds one: what a task learns outlives the sandbox, the
next task recalls it, and every task's run sits in Loom's run ledger.

| File                 | What it is                                                                                                 |
| -------------------- | ---------------------------------------------------------------------------------------------------------- |
| `task.yaml`          | A `Workspace` that writes Loom's MCP config and an `AGENTS.md`, and a `Task` that binds it                 |
| `egress-policy.yaml` | The task's egress policy: Loom with the token injected by the gateway, and GitHub for the clone            |
| `agent.py`           | The Loom calls an agent makes with the Python SDK: a run with a budget, checked between steps, then closed |

There are two ways in, and the example ships both. `agent.py` is the SDK path
and is what the `Task` runs. `.mcp.json` and `AGENTS.md` are for an image that
runs an MCP agent instead (Claude Code reads `.mcp.json` from its working
directory). Only the SDK path has been run inside an AX sandbox.

The token never enters the task. AX's API has no authentication yet, a Task's
`env` is printed by `ax get task`, and code in the sandbox reads its own
environment (AX's `SECURITY.md`). So the Task carries a placeholder, and
Substrate's egress gateway replaces the `Authorization` header with the token
from a Kubernetes Secret on the way out.

## 1. Give Loom a name inside the cluster

Egress rules name hosts by DNS name, never by address. Run Loom in the cluster,
or point a selector-less Service at it:

```yaml
apiVersion: v1
kind: Service
metadata: { name: loom, namespace: loom }
spec:
  ports: [{ name: http, port: 80, targetPort: 7777 }]
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: loom-1
  namespace: loom
  labels: { kubernetes.io/service-name: loom }
addressType: IPv4
ports: [{ name: http, port: 7777 }]
endpoints: [{ addresses: ["<Loom's address>"] }]
```

The hop from the egress gateway to Loom is cleartext HTTP, because injection
over HTTPS needs the actor to trust the gateway's CA (Substrate's egress trust
bundle) and AX's actor templates do not mount it. Keep that hop inside the
cluster.

## 2. Store a token per task as a Secret

```bash
TOKEN=$(loom mint-token --subject ax-fix-flaky-test --org acme \
  --namespaces ax-fix-flaky-test --roles writer --ttl-days 1)
kubectl create namespace loom-creds
kubectl -n loom-creds create secret generic loom-token --from-literal=token="$TOKEN"
```

One namespace per task or team, `writer` at most, and the shortest lifetime the
task needs. Then let the task's atespace (`default` here) read that namespace's
Secrets. The policy is the `k8s-credential-provider-namespace-policy` ConfigMap
in `ate-system`, and the provider reads it only at startup:

```bash
kubectl -n ate-system edit configmap k8s-credential-provider-namespace-policy
#   policies:
#   - atespace: default
#     allowedNamespaces: [loom-creds]
kubectl -n ate-system rollout restart deployment/k8s-credential-provider
```

## 3. Build the image

AX's published task-runner image is not publicly pullable, so build the runner
from AX's source (`CGO_ENABLED=0 go build ./cmd/ax-task-runner` for the
cluster's architecture) and put it in an image with the Loom SDK and your agent:

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "clickhouse-loom>=1.0"
COPY ax-task-runner /usr/local/bin/ax-task-runner
COPY agent.py /opt/loom-agent/agent.py
ENTRYPOINT ["/usr/local/bin/ax-task-runner"]
```

`agent.py` needs `clickhouse-loom>=1.0` and a 1.0 server.

Substrate accepts only an image pinned by digest, so `task.yaml` names it as
`<registry>/ax-loom-task@sha256:<digest>`. A tag is refused, and AX then falls
back to a default template that does not exist, so the task fails with "actor
template not found".

## 4. Apply

Fill in the `<...>` values in `task.yaml` and `egress-policy.yaml`, then:

```bash
ax apply -f task.yaml
kubectl ate create egress-policy fix-flaky-test -a default -f egress-policy.yaml
ax resume task fix-flaky-test
ax describe task fix-flaky-test
```

The Workspace comes first in `task.yaml`: AX creates the documents in order,
and a Task created before its Workspace starts with an empty one. The task is
created suspended, so the policy is in place before it first runs. Substrate
denies all egress from an actor with no policy, and AX creates none.

## Known issue: the Workspace's git clone

On current AX the clone runs before the sandbox has a network, so it fails and
the task runs without its repository. The fix is proposed in
[google/ax#482](https://github.com/google/ax/issues/482). Until it lands, leave
`spec.git` empty and have the agent clone the repository itself; egress is up
by the time the command starts.

## Budgets

`agent.py` opens its run with `budget_seconds`, and `run.get` answers
`over_budget: ["time"]` once the run goes past it; the agent decides what to do,
since Loom never stops it. The run is closed however the work ends: `completed`,
`cancelled` when it stops for the budget or is cancelled, `failed` on an error.
A stop for the budget still exits 0, so AX reports the task as finished; the run
ledger is where the stop shows.

A SIGTERM that arrives while the run is still opening stops the task by
default; if `run.begin` had already reached Loom, that run stays `active`, as
only MCP session runs are closed for idleness. Complete or cancel it with
`run.complete` if it matters.

A cost budget (`budget_usd`) needs a `session_id`, because a run's spend is the
turns its session made while the run was open. The Claude Code plugin records
those turns; a sandboxed agent has no plugin recording them, so to use a cost
budget it posts its own with `cost.record_turns` under that session. See
[runs](../../docs/features/runs.md#budgets).
