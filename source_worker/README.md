# MCP source worker

The companion runs outside the Loom service. It executes approved reads through
remote Streamable HTTP MCP servers. It accepts anonymous connections and
preissued bearer tokens bound to an approved resource audience and origin. It
does not run local server commands, acquire OAuth grants, refresh tokens or
infer read operations from tool descriptions. Resources and operator-approved
tool mappings use the same size, request, schema and deadline bounds.

The worker package contains no Loom server, database client or indexing code.
Loom integration, refresh and publication are separate from this package.

## Running the companion

Run `loom-source-worker` or build the image using this directory as its context.
The worker serves a private Unix socket. Put a TLS proxy in front of that socket
and limit it to the authorized Loom service. Disable proxy access, header, body
and trace logging and response caching. Forward only `POST /v1/live` and
`POST /v1/check-access`. Neither route accepts a query string or request body.
The worker verifies an issuer-pinned, short-lived Ed25519 grant in the
authorization header, scoped to the worker, operation, requester, connection,
tenant and selected resource. Do not forward this grant to an upstream server.

Mount these files and directories in the worker:

- `/etc/loom-source-worker/config.json`: identity, issuer, profiles and limits.
- `/etc/loom-source-worker/grant-verifier.pem`: public key used to verify grants.
- `/etc/loom-source-worker/vault-ca.pem`: optional credential service CA roots.
- `/run/loom-source-worker/credentials/vault-token`: credential service token.
- `/run/loom-source-worker/socket`: directory mounted by the worker and proxy.

Run the proxy and worker under the same numeric UID, `10001` in the image.
The worker requires an owned `0700` directory and creates the socket with
mode `0600`. Share only the socket directory with the proxy. Keep the
credential mount out of the proxy and Loom containers. The Vault token file
must be a regular file owned by root or the worker UID, with mode `0400` or
`0440` and readable by the worker. Give that token read access only to its
dedicated KV v2 connection prefix. Store each record
under the worker UUID and opaque connection UUID; the record binds the owner,
organization, profile and generation to that connection. The generation must
match the current secret version. Rotate or revoke at the credential backend;
do not copy the credential into a Loom registration or task. Limit egress to
the approved credential, DNS and upstream endpoints.

The required configuration fields are `transport` (`tls_proxy_unix`),
`worker_id`, `issuer`, `vault`, `read_limits` and `profiles`. A profile names the
permitted organizations, optional HTTPS citation origins and the approved
client configuration. A mapped read must return the exact selected identity.
Keep the signer and worker profile digest in agreement.
Any account, profile, selection or authority change requires a current grant.
An access check returns a content digest and fetch time without the body.

A grant is reusable until its expiry for the same read, within the worker's
capacity limits. It never authorizes a stored write. The issuer must check
selection and requester authority before issuing it and before delivering a
result. The worker rechecks its connection during the read and before reply.

## Resource profile and credential example

The names and IDs below are synthetic. Use the service's approved HTTPS
endpoint and the audience for which its token was issued. The stored audience
must exactly match `bearer_audience`, including any path. The configured
audience and endpoint must have the same HTTPS origin. For an anonymous profile,
omit `bearer_audience` and use an `anonymous` record without a token, audience
or expiration. Do not set `approved_resource_ids` in either profile; the issuer
selects one exact resource in each grant.

Save the worker configuration as `config.json`:

```json
{
  "transport": "tls_proxy_unix",
  "worker_id": "83d061b4-2542-4db7-adc0-4b81b3280c08",
  "issuer": "source-grants",
  "vault": { "origin": "https://vault.example:8200", "mount": "worker-connections" },
  "read_limits": {
    "concurrent": 8,
    "concurrent_per_org": 2,
    "timeout_seconds": 20,
    "content_bytes": 100000
  },
  "profiles": [
    {
      "organizations": ["tenant"],
      "link_origins": ["https://docs.example"],
      "config": {
        "endpoint": "https://mcp.example/rpc",
        "bearer_audience": "https://mcp.example/rpc"
      }
    }
  ]
}
```

Use the parser from the installed worker version to compute the profile digest.
It includes parsed defaults as well as the profile's tenant and link settings.
Run this offline with the intended configuration; it does not read the token.

```sh
python - <<'PYTHON'
from pathlib import Path
from loom_source_worker.main import _parse_settings

settings = _parse_settings(Path("config.json").read_bytes())
print(settings.profiles[0].digest)
PYTHON
```

For a first secret version, write this record under
`83d061b4-2542-4db7-adc0-4b81b3280c08/507d2572-b7e0-4a8c-8b87-195dd473ca29`
in the `worker-connections` KV v2 mount. Replace the two uppercase placeholders
and set `expires_at` to the issued token's actual Unix expiration in seconds.
When writing through the KV v2 API, send this record as its `data` object.
Production tokens belong in the secret store; keep them out of commands,
configuration and logs. The record's generation must match Vault's version.

```json
{
  "binding": {
    "worker_id": "83d061b4-2542-4db7-adc0-4b81b3280c08",
    "org": "tenant",
    "owner": "alice",
    "connection_id": "507d2572-b7e0-4a8c-8b87-195dd473ca29",
    "generation": 1,
    "profile_digest": "PROFILE_DIGEST"
  },
  "state": "active",
  "auth": "bearer",
  "audience": "https://mcp.example/rpc",
  "token": "UPSTREAM_TOKEN",
  "expires_at": 2000000000
}
```

Use the same profile digest in this grant payload. The issuer must derive each
field from current authority and selection, and use a fresh request UUID.

```json
{
  "version": 1,
  "request_id": "63f8514d-7bcd-4144-b43e-78785d04b08b",
  "operation": "live",
  "binding": {
    "worker_id": "83d061b4-2542-4db7-adc0-4b81b3280c08",
    "org": "tenant",
    "namespace": "collection",
    "source_id": "d07561a2-791b-4fef-a5f2-8bdaf903000a",
    "selection_generation": 1,
    "requester": "alice",
    "connection_id": "507d2572-b7e0-4a8c-8b87-195dd473ca29",
    "connection_generation": 1,
    "profile_digest": "PROFILE_DIGEST",
    "resource_id": "urn:example:document:one"
  }
}
```

Load the payload with `loom_source_worker.protocol.ReadGrant.model_validate_json`
and sign it with `issue_read_grant(grant, issuer="source-grants", key=signing_key,
lifetime_seconds=10)` on the issuer. Mount only the corresponding public key
in the worker. Send the resulting grant as the authorization bearer on the
matching route. For `/v1/check-access`, use operation `check_access`.

The worker returns `200` with `ok` on success, `403` for `denied`, `409` for
`reconnect`, `422` for `unsupported`, `429` for `overloaded`, `503` for
`unavailable`, and `504` for `timeout`. Connection errors, upstream 5xx and an
expired stateful MCP session return `unavailable`. Any retry needs current
authority and a new session; the worker does not retry inside the failed read.
The worker handler refuses a body or query with `400`. The TLS proxy rejects
query strings and other request targets outside the two routes with `404`, and
may return `413` when a body exceeds the proxy's limit.

## Docker Compose

`compose.yaml` starts the worker and its TLS proxy only when the `source-worker`
profile is selected. Place the worker configuration and public grant verifier
under `operator/worker/config`, the credential service token under
`operator/worker/credentials`, and the TLS server chain, private key and trusted
client CA under `operator/proxy`. Supply these files through deployment secret
management and keep them out of the image and repository.

The proxy is reachable only from the internal `worker-ingress` network. Attach
the approved grant issuer to that network and configure it to verify the proxy
certificate by name and present its own client certificate. The manifest does
not publish a host port. Only the worker joins the egress network. Restrict that
network to approved MCP hosts, DNS and the credential backend through host or
infrastructure policy; Compose network membership alone does not allowlist
outbound destinations.

The hosted package test uses owned SDK resource and mapped-tool servers with
synthetic accounts. The separate deployment smoke uses the Compose image and
proxy with synthetic certificates and a credential fixture. These tests do not
qualify production credential infrastructure or a particular external service.
