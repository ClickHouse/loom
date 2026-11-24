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
client configuration. A mapped read must return the exact selected identity.
Keep the signer and worker profile digest in agreement.
Any account, profile, selection or authority change requires a current grant.
An access check returns a content digest and fetch time without the body.

A grant is reusable until its expiry for the same read, within the worker's
capacity limits. It never authorizes a stored write. The issuer must check
selection and requester authority before issuing it and before delivering a
result. The worker rechecks its connection during the read and before reply.

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
