# Outbound MCP source client

The outbound MCP source client reads resources through a configured remote HTTP endpoint.
It provides protocol discovery and normalized evidence without storing documents, authorizing
Loom readers or running an agent. Source lifecycle and publication remain responsibilities
of the [document source layer](document-sources.md).

## Source entry points

`server/clients/mcp_source.py` contains the client configuration, approved tool mappings,
capability discovery and evidence types. The official protocol SDK handles MCP messages;
the client constrains the HTTP transport and validates configured operations and results.

This client is an internal prerequisite. It adds no source registration operation, dashboard
control, worker dispatch or operator cutover. Connecting existing document sources continues
to use their current implementation. The client does not qualify an external service.

## Connection and endpoint boundary

A configuration identifies one exact HTTPS endpoint. Its caller supplies a separately
acquired upstream credential bound to that endpoint. The client does not accept a Loom
request, forward incoming headers, discover alternate endpoints or follow redirects.
A mandatory access validator runs around operations and HTTP response work. The source
layer is responsible for storing credentials sealed, binding the connection
to the authenticated owner and organization, checking current grants and revocation,
and approving the endpoint through operator policy before constructing a client.

The transport uses `server/core/webhooks.py` (`pinned_target`) to validate public DNS
addresses and preserve the configured Host header and TLS server name while connecting
to a validated address. Endpoint and credential-origin checks use the same HTTPX ASCII
hostname as dispatch. The pinned Host header and TLS server name must match it exactly;
trailing-dot aliases remain distinct. Proxy environment variables do not select another route.
Requests outside the configured endpoint are refused. A server response cannot select
a different server, credential or connection.

The supported profile is Streamable HTTP with an explicitly supplied upstream bearer
credential, or an explicitly anonymous endpoint. Interactive authorization, credential
refresh, legacy HTTP with SSE and local
stdio commands are outside this profile. The client launches no external command. An
expired or refused credential ends the operation; no native connector or broader
credential substitutes for it. Transport and token binding follow the
[MCP HTTP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
and [authorization contract](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).

## Read operations and capabilities

Standard resource listing and reading use `resources/list` and `resources/read` when the
server advertises resources. Tool-based reads require an explicit mapping approved by
the caller's control plane. A mapping names the exact tool, its argument and result
schemas, fixed scope arguments and result selectors. Search text, a selected resource
identity or a pagination cursor fills only its configured argument. It cannot replace
a fixed scope argument or choose another tool.

Discovery verifies the configured schemas before execution. Tool descriptions and
`readOnlyHint` are untrusted descriptive fields, never approval to call a tool. Unknown
capabilities remain unsupported; an action-only server supplies no usable document source.
Schema drift refuses the configured operation rather than guessing another result shape.
Returned resource links are evidence, never instructions to fetch an arbitrary URL.
Listing and search do not expand the explicitly approved resource identities. An empty
selection grants no read authority.

The qualified content profile is text resources and mapped structured tool output. Binary
resource blobs and tools that return only unstructured text are unsupported. Approved
tool schemas use a bounded subset with closed objects, bounded arrays and strings,
and no references, regular expressions or combinatorial validators.
Keywords must belong to the schema's declared type. External schema retrieval is
disabled independently of configuration validation.

Each read produces normalized evidence: resource identity, citation URI, title, media
type, content and any supplied revision. A read receipt binds the requested identity,
configured operation and returned body; listing metadata and search snippets have no
successful resource-read receipt. Identity is scoped by the source connection;
the same upstream identifier on another connection denotes separate evidence. A caller
that persists a document must retain the connection and resource identity with its
provenance. A citation address alone does not establish tenant or reader authority.

## Bounds and cancellation

Discovery, pagination, results, content and HTTP responses have finite limits. A session
has an overall deadline, including idle time, as well as request bounds. The SDK context
managers live in one task per open client. A failed or cancelled operation makes its
client terminal and closes and joins that task before returning, including when an SDK
request deadline expires while HTTP response data is still arriving. Another client's
SDK scope remains independent. A known background transport failure or an expired
deadline refuses discovery and result publication, including cached discovery.
Unexpected HTTP status or response media types are terminal before protocol handoff.
Optional GET and DELETE responses with status 405 remain supported.
Cancellation publishes no partial read evidence.
The transport retains cleanup ownership while checking authority after response headers
arrive and before handing the response to the protocol client.
Closing a request does not prove that remote processing has stopped. Errors expose bounded error
codes rather than upstream content, endpoints or credentials.

Only an omitted optional mapped cursor ends that listing. Every present mapped cursor
must be a nonempty bounded string; an explicit null is refused. Issued cursors are bound to their
operation and query, so a cursor from one query cannot authorize another. Reusing a
cursor in the same listing is refused even when another listing uses the same text.
A successful final page closes that listing operation. It does not establish a deletion
history, complete reader permissions, revision support or a subscription contract.
Partial pages, errors, refused access and notifications are never deletion evidence.
The client has no retirement or erasure authority.

## Storage and publication

The client keeps evidence in memory for its caller and writes no knowledge document,
original, chunk, memory, account, cache or source checkpoint. There is no persistent
connection cache shared between users. The caller decides whether the evidence serves
live retrieval, an explicitly selected snapshot or a supported refresh contract.

Live retrieval belongs in the existing protected live-search path. That path checks
current owner authority around reads and again before delivery, and applies private
result, accounting and publication rules. Calling this client does not grant permission
to bypass those checks or expose private content through diagnostics, exports or derived
memories. A team destination does not establish permission to private source evidence.
Without admitted upstream ACL evidence, a personal import remains owner-only. An
intentional collection-wide publication needs separate explicit authority.

The absence of a client-side durable cache is not a no-retention guarantee. The remote
server, downstream host, application logging and trace configuration have their own
retention. The integrating source layer must disclose those boundaries and protect
content-bearing outputs under the existing publication contract.

## Invariants

- Endpoint, credential audience, approved tool, fixed scope and identity cannot be
  widened by query arguments, resource content or server metadata.
- A protocol capability is not a permission grant or a synchronization guarantee.
- A read receipt proves the operation that produced evidence; it does not prove that
  a different user may read the result.
- A connection or live lookup imports nothing and deletes nothing.
- Unsupported authentication, transport, schema and access contracts fail closed.

## Upgrade boundary

This internal client changes no stored row, wire field or source kind. Builds continue
using the same schema, account history, source checkpoints, documents and originals.
There is no account migration, credential reuse, source retirement or runtime switch.
Integrating the client into source registration requires its own explicit storage,
rolling-upgrade, access and operator reconfiguration contract.
