# Agent Substrate

Needs [Agent Substrate](https://ate.dev) v1.0.0 and Loom's Helm chart in one Kubernetes
cluster: Substrate's egress gateway adds each agent's Loom token to its MCP calls.

The token is never part of the actor's template or snapshots. Substrate v1.0.0 needs
Kubernetes 1.37 or later with a gVisor or microVM runtime on the nodes. The steps below also
need Substrate's Kubernetes credential provider, which
`ate-setup deploy ... --credential-provider='{"name":"k8s.io"}'` deploys.

The Substrate steps below follow Substrate's own documentation for v1.0.0; this page has
not been run end to end on a live cluster, so check them against your Substrate version.

## How the pieces fit

| Piece         | Where it runs                                                          | What it does                                                                 |
| ------------- | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Loom          | `helm install loom` in namespace `loom`                                | the memory server; its Service is `loom.loom.svc.cluster.local:7777`         |
| Agent         | a Substrate actor in an atespace                                       | calls Loom's MCP endpoint, `/mcp`, with a placeholder `Authorization` header |
| Egress policy | Substrate's control plane (`kubectl ate`), enforced by `atenet-egress` | allows the actor to reach Loom, and nothing else unless you add rules        |
| Loom token    | a Kubernetes Secret                                                    | one per agent; the egress gateway puts it in the `Authorization` header      |

Every request leaves the actor through Substrate's egress gateway (`atenet-egress` in
`ate-system`), which denies all egress an actor's policy does not allow. Loom therefore sees
connections from the gateway pods, not from the actors, and tells agents apart by their
tokens.

## 1. Install Loom in the cluster

Install the chart as [Kubernetes](../../operations/kubernetes.md) describes, in namespace
`loom` with release name `loom`, which names the Service `loom`. The agents need no
Ingress: they reach the Service by its cluster DNS name.

The chart's NetworkPolicy admits any source while `networkPolicy.ingressFrom` is empty, its
default. If you list peers there (the load balancer's subnets, for example), add the egress
gateway to the list, since a list you set replaces the default rather than adding to it:

```yaml
networkPolicy:
  enabled: true
  ingressFrom:
    # ...the peers you already allow...
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: ate-system
      podSelector:
        matchLabels:
          app: atenet-egress
```

## 2. A token per agent

Give each agent its own token, with a service-account subject named for the atespace and
the Substrate actor. Loom records that subject on each row the agent writes, and the token's
namespaces and roles decide what it reads and writes
([tokens and grants](../../guides/tokens-and-grants.md)).

Mint the token in a Loom pod, which holds the signing key, and store it in a Secret in a
namespace that holds only that atespace's tokens:

```bash
TOKEN=$(kubectl -n loom exec deploy/loom -- \
  loom mint-token --subject sa:research.research-agent --org acme \
    --namespaces research --roles writer --ttl-days 30)
kubectl create namespace loom-tokens-research
kubectl -n loom-tokens-research create secret generic research-agent --from-literal=token="$TOKEN"
```

Substrate's Kubernetes credential provider reads a Secret only from namespaces the
atespace is granted. An administrator grants the atespace the token namespace in the
provider's namespace policy and restarts the provider, which reads the policy at startup:

```bash
kubectl -n ate-system edit configmap k8s-credential-provider-namespace-policy
```

```yaml
data:
  namespace-policy.yaml: |
    policies:
    - atespace: research
      allowedNamespaces:
      - loom-tokens-research
```

```bash
kubectl -n ate-system rollout restart deployment/k8s-credential-provider
```

The provider grants an atespace whole namespaces, not single Secrets, so an atespace
granted a namespace can use every token in it: one namespace per atespace keeps each
atespace to its own agents' tokens.

The token expires after `--ttl-days`: before then, mint a new one, update the Secret and
restart the credential provider as above.

## 3. Configure the agent and create its actor

Substrate's documentation describes no MCP configuration for actors: configure the agent's
MCP client in its image or as literal environment values in the actor template. The server
entry is Loom's endpoint with a placeholder header, which the gateway replaces:

```json
{
  "mcpServers": {
    "loom": {
      "type": "http",
      "url": "http://loom.loom.svc.cluster.local:7777/mcp",
      "headers": { "Authorization": "Bearer placeholder" }
    }
  }
}
```

The key names follow the client ([MCP clients](./mcp-clients.md) has the entry for each).
Keep the token out of the template: Substrate stores templates and snapshots, and its
container environment takes literal values only.

Then create the atespace, the actor template that carries the agent's image, and the actor
from that template. Substrate's [API guide](https://ate.dev/docs/reference/api-guide/)
describes the template's fields; here its `metadata` names it `research-agent` in atespace
`research`. The template's `workerSelector` must match a WorkerPool in the cluster, and its
image is pinned by digest. The egress policy comes next:

```bash
kubectl ate create atespace research
kubectl ate create actor-template -f template.yaml
kubectl ate create actor research-agent --template=research-agent -a research
```

## 4. Allow the actor to reach Loom, with its token

An actor has one egress policy. This one, for the actor created above, allows HTTP to
Loom's Service and replaces the `Authorization` header the agent sends with
`Bearer <token>` from the Secret:

```bash
kubectl ate create egress-policy research-agent -a research -f - <<'EOF'
rules:
- http:
    hostnames: ["loom.loom.svc.cluster.local"]
    ports:
      numbers: [7777]
    effects:
      replaceHeaders:
      - header: Authorization
        prefix: "Bearer "
        credentialUri: ate-secret://k8s.io/default/loom-tokens-research/research-agent/token
EOF
```

- The rule matches the request's host, so it names the host of the MCP entry's URL:
  `loom.loom.svc.cluster.local`.
- The gateway replaces a header that is there. A request without an `Authorization`
  header goes out unchanged and Loom refuses it.
- A missing Secret, or an atespace without the grant, makes the gateway refuse the request.
- The chart serves HTTP inside the cluster, so on this rule the token crosses from the
  gateway to Loom unencrypted. To encrypt that hop, point the agent at a TLS endpoint in
  front of Loom whose certificate is publicly trusted, such as the chart's Ingress host,
  and allow that host with an `https` rule. The gateway then intercepts the agent's TLS,
  so the actor mounts Substrate's [egress trust
  bundle](https://ate.dev/docs/guides/egress-trust-bundle/) and points its MCP client at
  it (`SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS` or `REQUESTS_CA_BUNDLE`, by the client's
  runtime), and Loom sees connections from the ingress controller rather than from
  `atenet-egress`.

Rules for whatever else the agent needs, such as its model provider, go in the same
policy. `kubectl ate update egress-policy` replaces every rule and refuses a policy whose
metadata is not the current one, so read the policy with
`kubectl ate get egress-policy research-agent -a research -o yaml`, add the rules beside the
Loom rule, and send that file back with `kubectl ate update egress-policy ... -f`.

With the policy in place, start the actor:

```bash
kubectl ate resume actor research-agent -a research
```

## Several agents

- Agents in one Loom namespace share everything they write. A sub-agent that should read
  a team's namespace but write only its own gets a token with a role per namespace
  (`--namespace-role team=viewer --namespace-role research=writer`;
  [mint a token](../../guides/tokens-and-grants.md#mint-a-token)).
- Each actor gets its own token, Secret and egress policy; an actor in another atespace gets
  that atespace's own token namespace and grant ([step 2](#2-a-token-per-agent)).
- Hand work between agents with the [task inbox](../tasks-and-decisions.md#task-inbox).

## Related

- [Agent frameworks and orchestrators](./frameworks.md) for the shared MCP mount.
- Substrate's guides: [egress traffic](https://ate.dev/docs/guides/egress-traffic/) and
  [egress credential injection](https://ate.dev/docs/guides/egress-credential-injection/).
