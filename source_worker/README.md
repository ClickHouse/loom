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

| Path                                                | Purpose                                         |
| --------------------------------------------------- | ----------------------------------------------- |
| `/etc/loom-source-worker/config.json`               | Worker identity, issuer, profiles and limits    |
| `/etc/loom-source-worker/grant-verifier.pem`         | Ed25519 public key used to verify Loom grants    |
| `/etc/loom-source-worker/vault-ca.pem`               | Optional CA roots for the credential service     |
| `/run/loom-source-worker/credentials/vault-token`     | Worker-only credential service token            |
| `/run/loom-source-worker/socket/worker.sock`         | Local socket mounted only by worker and proxy    |

The image runs as UID and GID `10001`. Give it a socket directory it owns with
mode `0700`. Share only the socket directory with the proxy. Keep the
credential mount out of the proxy and Loom containers. Give the worker token
read access only to its dedicated KV v2 connection prefix. Store each record
under the worker UUID and opaque connection UUID; the record binds the owner,
organization, profile and generation to that connection. The generation must
match the current secret version. Rotate or revoke at the credential backend;
do not copy the credential into a Loom registration or task. Limit egress to
the approved credential, DNS and upstream endpoints.

The required configuration fields are `transport` (`tls_proxy_unix`),
`worker_id`, `issuer`, `vault`, `read_limits` and `profiles`. A profile names the
permitted organizations, optional HTTPS citation origins and the approved
client configuration. Keep the signer and worker profile digest in agreement.
Any account, profile, selection or authority change requires a current grant.
An access check returns a content digest and fetch time without the body.

The hosted package test uses owned SDK resource and mapped-tool servers with
synthetic accounts. It does not qualify production credential infrastructure
or a particular external service.
