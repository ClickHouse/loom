# Kubernetes source worker

The standalone chart runs a source worker and its mTLS proxy in one Linux Pod,
separate from Loom. `enabled: false` renders no objects. With `enabled: true`,
the release creates one Deployment, a ClusterIP Service on TCP 8443, a
NetworkPolicy, a ServiceAccount with no RBAC grants or token mount, and an
immutable ConfigMap for the proxy. It creates no Ingress or credential objects.

Use Helm 3.16.2 or later and Kubernetes 1.30 or later. Run the chart in a
dedicated namespace with the Restricted Pod Security Standard enforced and a
CNI that enforces ingress and egress NetworkPolicy. Both container images are
operator-supplied `@sha256:` references. Build the worker from the same reviewed
revision as the chart. The proxy baseline is the digest in
[`../compose.yaml`](../compose.yaml); an alternate build must pass the same
proxy qualification with the unchanged configuration. Configure image pulls at
the node if the worker image is private.

## Inputs

Create these objects in the companion namespace before opting in. Values name
the objects; they do not contain config or credentials. Every listed key is
required. A missing object or key prevents volume setup. Invalid configuration
or credential syntax stops the worker; invalid proxy TLS files stop nginx.

| Value | Object and required keys | Mount |
| --- | --- | --- |
| `inputs.workerConfigMap` | ConfigMap: `config.json`, `grant-verifier.pem`, `vault-ca.pem` | Worker only, read-only at `/etc/loom-source-worker`. |
| `inputs.vaultTokenSecret` | Secret: `vault-token` | Worker only, read-only at `/run/loom-source-worker/credentials/vault-token`, mode `0440`, group `10001`. |
| `inputs.proxyTlsSecret` | Separate Secret: `server-chain.pem`, `server-key.pem`, `client-ca.pem` | Proxy only, read-only at `/etc/nginx/tls`. |

Use the [worker config and grant contract](../README.md). Set
`transport: tls_proxy_unix` in the worker config. Mount the Vault CA bundle and
set `vault.custom_ca` when the worker must trust that bundle instead of the
system roots. The chart requires the `vault-ca.pem` key even when the worker
uses system roots. Give the Vault token only read authority on the worker's
dedicated KV v2 connection prefix. The worker reads the token from the mounted
file, not an environment variable. Neither Loom nor nginx gets this token or
the upstream credentials.

Provision Secrets from the operator's encrypted secret backend. Configure
encryption at rest for Kubernetes Secrets, restrict API and node access, and
restrict who can create Pods or attach debug containers in the namespace. This
chart does not configure Vault, Kubernetes encryption, or Secret delivery. Use
versioned ConfigMap and TLS Secret names and update the references to reload
worker settings, verifier keys, or TLS assets. The worker rereads the token
file, so same-name token updates follow kubelet projection latency; revoke at
Vault rather than relying on projection as the revocation boundary. The single
Pod is replaced on changes to its specification; expect a readiness gap.

The worker and proxy run as UID/GID `10001`, with no Linux capabilities, no
privilege escalation, and read-only root filesystems. A non-root init container
creates a `0700` directory on a bounded memory volume. The worker creates the
`0600` Unix socket there; nginx sees only the read-only directory mount. nginx
has a separate 1 MiB memory volume at `/tmp`. Readiness checks the local socket
and proxy TCP listener; neither probe uses credentials or checks Vault or the
upstream service. A client still needs mTLS and a signed grant for each read.

## Network boundary

- `network.callers` selects approved caller Pods by exact namespace and
  nonempty `matchLabels`. The caller config uses the
  `sw-<release>.<namespace>.svc.<cluster-domain>` Service DNS name on 8443, a
  server CA and a client certificate. Put the Service DNS name in the server
  certificate SAN. Keep the client private key in the caller, not this chart.
- `network.dns.nameservers` sets the Pod's literal resolver IPs. The policy
  permits UDP and TCP 53 only to those addresses and any configured
  `network.dns.pods` peers. Use fully qualified endpoint names. Include resolver
  Pod peers where the CNI evaluates DNS traffic after Service translation.
- `network.httpsPods` and `network.httpsCidrs` allow explicit destinations and
  one TCP port per peer for Vault and approved MCP endpoints. For Pods use the
  target port the CNI sees. Use the smallest external CIDRs and keep them in sync
  with endpoint DNS. These rules match IPs and ports, not TLS names or HTTP
  paths. The worker's approved profile and certificate checks are separate.

All other Pod ingress and egress is isolated by the policy, subject to cluster
behavior: policies are additive, node traffic is special, and Service address
translation can affect matches. Prestage a namespace default-deny policy,
control label assignment, allow the companion on the caller's egress policy if
needed, and check both successful and denied traffic on the actual CNI. Use
infrastructure controls for node-local and metadata paths the CNI cannot
isolate. Do not permit broad policies or service-mesh injection in this
namespace. NetworkPolicy applies to the whole Pod, not individual containers;
nginx shares its egress policy but has no Vault token or worker config mount.

## Hosted qualification

Run on the exact reviewed revision, using synthetic credentials and endpoints.
The [static fixture](examples/qualification-values.yaml) is for lint and render
only. Do not deploy its placeholder worker image or network addresses.

```sh
set -eu
chart=source_worker/chart
values="$chart/examples/qualification-values.yaml"
cmp source_worker/proxy.conf "$chart/files/proxy.conf"
helm lint --strict "$chart" --kube-version 1.30.0
helm lint --strict "$chart" --kube-version 1.30.0 -f "$values"
helm template source-worker "$chart" --kube-version 1.30.0 > disabled.yaml
test ! -s disabled.yaml || ! grep -q '^kind:' disabled.yaml
helm template source-worker "$chart" --namespace companion-test \
  --kube-version 1.30.0 -f "$values" > worker.yaml
kubeconform -strict -summary -kubernetes-version 1.30.0 worker.yaml
```

Decode the rendered ConfigMap's `binaryData.nginx.conf` and compare it with
`source_worker/proxy.conf`. The chart holds a byte-for-byte copy because Helm
cannot load a file outside the packaged chart. Run the same render from an
isolated chart copy. Assert that opt-in fails without images or input names,
without approved callers, DNS or egress destinations, with a tag-only image,
with the same Secret for Vault and TLS, and without the packaged proxy file.

On a disposable, policy-enforcing hosted cluster, repeat strict validation for
the target Kubernetes version. Configure Restricted admission, default deny,
real hosted image digests and test-specific allowlists. Use an ephemeral KV v2
backend, independent issuer keys, synthetic PKI and controlled HTTPS MCP
endpoints. Run server-side dry-run and wait for Pod admission and readiness.
Record the exact chart revision, tool versions, CNI, image digests and checks.

| Check | Expected result |
| --- | --- |
| Missing input object, missing key, bad token permissions or invalid proxy TLS | No ready endpoints. No fallback listener or credentials. |
| Runtime mounts | Worker socket directory `10001:10001` mode `0700`, socket `0600`, Vault file mode `0440`; no token in proxy or Loom and no service-account token in either container. |
| Approved caller, trusted client certificate and valid signed grant | Bodyless HTTP/1.1 `POST /v1/live` and `POST /v1/check-access` work; only the live response has content. Responses carry `Cache-Control: no-store`. |
| No or untrusted client certificate; wrong server name or CA; plaintext; forged or missing grant | No protected result; no caller TLS bypass or plaintext route. |
| Extra headers, nonempty body, transfer encoding or unlisted method/path | Proxy forwards only the grant and fixed upstream Host; rejects disallowed requests without an upstream read. |
| Same labels in a different namespace, wrong caller labels, unapproved TCP destination/port, and unauthorized DNS | Policy blocks them. The approved DNS, Vault and MCP destinations still work. |
| Log, volume and restart inspection | Synthetic body and token markers absent from proxy and worker logs, termination messages and proxy temp files. No stale socket route after worker restart; readiness and mTLS recover after restart and reviewed rotation. |

Remove the hosted synthetic resources after recording results. Rendering and
schema checks do not qualify mTLS, file ownership, DNS translation or CNI
enforcement.
