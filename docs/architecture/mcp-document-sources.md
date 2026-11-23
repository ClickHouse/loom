# MCP document sources

Needs authentication and `LOOM_KNOWLEDGE_MIRRORED_SOURCES_ENABLED=true`; enrollment
also needs `LOOM_KNOWLEDGE_SOURCE_SELF_SERVICE_ENABLED=true`. Loom reads explicitly
selected external resources through one outbound MCP client.
An operator approves a remote server and any tool mappings. A user connects their
own account, chooses content and mode, chooses an ordinary destination collection,
and previews effective access before saving a registration. Configuration supplies
service differences; tool descriptions and models do not decide permissions.

## Transport and operation approval

The client uses the official MCP SDK over remote HTTPS Streamable HTTP. Qualified
profiles support anonymous access or a preissued bearer bound to the exact approved
origin and audience. Loom credentials never authenticate to the remote server.
OAuth enrollment and refresh, local commands or stdio, legacy SSE, binary resources
and unstructured tool-result inference are outside this contract. Protocol support
does not establish that a particular service exposes readable content.

The operator profile fixes the endpoint, allowed organizations, authentication
profile, approved operation names, JSON schemas, fixed arguments, resource identity
fields and request/result/page/byte/time bounds. Its digest covers configuration
and publication policy. Enrollment exposes the approved ASCII HTTPS origin and
the limits of remote identity verification. A changed approval requires a new
binding. Caller IDs, returned content, tool hints and model arguments cannot add an
endpoint, tool, account, tenant or namespace.

Standard `resources/list` and `resources/read` work when advertised and supported.
A server exposing search or reads only through tools needs validated declarative
mappings. A mapping names an approved operation and exact input/output fields;
it does not infer semantics from descriptions or `readOnlyHint`. Action-only
servers can connect for inspection but cannot supply document reads without an
approved read capability. No native connector or provider-specific OAuth path
backs a failed MCP operation.

## Identity and storage authority

A connection belongs to the authenticated canonical owner in one organization.
A random request UUID reconciles a connect attempt to that owner's opaque slot.
Its sealed envelope binds organization, owner, slot, generation, profile digest,
endpoint and token audience. Current account standing, grants, revocation fences,
operator approval and fleet readiness are checked on use. The remote identity is
not presented as verified merely because a bearer or server title exists.

A registration has an immutable source UUID, account generation, destination and
reader policy. Each selected resource is an exact UTF-8 identity, without URL
normalization, recursive expansion or guessed aliases. Its local citation uses
the existing source-copy URI with a SHA256 digest of those exact bytes and the
source UUID. This address is stable within the registration, grants no access and
is never an outbound fetch target or an asserted upstream citation. Reconfiguration
allocates a new registration rather than reusing another account's history.

Connections use existing connected-account storage, leases, fences and owner
lifecycle. Registrations use `knowledge_sources`; their versioned raw configuration
and selection are opaque to builds that do not support the contract. Imports use
the existing knowledge document, chunk, original, revision, accounting, indexing
and erasure paths. The existing storage-authority choice applies to each path;
there is no separate MCP document store, agent executor or process-local authority.
Identity tuples, server-derived actors and source classes follow ordinary writes.

## Modes and selected scope

| Mode | Remote activity | Persistent content |
| --- | --- | --- |
| Live | Explicit bounded lookup or read, with final authority revalidation | No document import or automatic memory save |
| Snapshot | Read the explicitly selected identities once | Protected knowledge copies, originals and normal revisions |
| Selected refresh | Re-read that same selected set under bounded worker scheduling | Protected copies and current successful-read proof |

Browsing returns one bounded metadata window. It reports truncation and capability
limits; it does not claim a complete catalog. A user may supply exact IDs when a
server cannot enumerate. Search results are intersected with the saved selection
and are read before content is returned. Direct live reads require one exact saved
identity. Neither path silently expands the selection or imports content.

This contract has no continuous complete-enumeration, upstream ACL synchronization
or deletion guarantee. Partial pages, notifications, errors and absence from a
window never prove deletion. A source lacking revision evidence still permits
explicit re-read; Loom computes content identity and retains normal provenance.
An unchanged owned document receives a metadata/access refresh without rewriting
its original, chunks or content revision.

Stored-copy work requires the ordinary source worker,
`LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED=true`. Saving a registration while that worker
is disabled does not import its content.

Every imported document carries the current source-scope digest and server read
time. Scope changes increment a monotonic revision, including selection ABA.
A previous successful read cannot authorize a different selection revision. An
individual denied, oversized or unavailable resource loses its own read proof;
its bytes remain. An observed approved-mapping schema failure stops the exact
current connection under its account lease, invalidating reads from all sources
on that binding. The sealed credential, generation and retained content are not
erased. Restoring an operator schema does not reactivate that connection; the owner
must approve a new binding.

## Who may read

A destination grant alone does not publish private source material. Both reader
policies use the protected mirror and current source/connection checks.

- Owner-only copies require the current canonical owner and destination access.
  Missing upstream ACL evidence never grants team access. Unbound machine readers
  cannot borrow an owner's connection.
- A collection copy requires operator publication approval, namespace administrator
  authority, explicit confirmation of a server-issued exact-selection consent,
  and current destination-reader authority. It discloses that access is an
  intentional local publication, without claiming upstream ACL equivalence.

The audience is immutable for a source UUID. Publishing to another audience needs
a new registration and read; private history is not republished. Ordinary collection
grant inheritance and the automatic personal-namespace restriction remain in force.
Setup can select or create an ordinary private or team collection without turning
a personal source credential into a shared credential.

The existing successful-read freshness bound applies to every document proof.
Snapshot access therefore expires without an explicit refresh; retaining bytes does
not preserve access forever. Pause and disconnect stop use while retaining stored
content. Read, original, history, search, export, diagnostics, caches and derived
memory publication remain subject to the existing protected-result and publication
checks. A client must establish the supported durable session-protection protocol
before private results are returned. Merely adding a capability header does not
install its required automatic-capture barriers.

Live response content lives in request/client memory until that request or retained
contribution is released. This layer has no persistent live-result cache and does
not save a memory as a side effect. A remote host can retain requests under its own
policy; connecting does not establish a remote retention or deletion promise. Loom
uses its existing private-work diagnostic handling and configured log/trace
retention. Imported bytes, originals, revisions and account history follow existing
storage and writer-requested retention/erasure policy. Operators and clients must
account for their own response, trace and conversation retention.

## Lifecycle and bounded work

Connection APIs provide approved profiles, enrollment, list, inspect, discovery,
browse and disconnect. Source APIs provide preview, add, inspect, bounded list,
update, pause, resume, refresh, direct live read and remove. Inspect can reconcile
the original slot/request UUID without replaying an uncertain mutation. Clients
receive no authority beyond the authenticated API caller.

The background lane reads bounded metadata pages before hydrating a selected
source. Fair admission caps queued headers and concurrent turns globally, per
organization and per canonical endpoint. Each task reserves its own document
credits and has an outer deadline; cancellation joins owned work. Execution still
uses the existing source/account leases and publication fences. Scheduling headers
are hints, not authority. A separate opaque cursor in the existing worker-status
store leaves the recording lane's cursor intact. Physical query row, byte and time
limits apply even when the registry is larger than one returned page.

Source removal and owner erasure reuse existing cleanup and audit machinery.
Only an explicit writer request retires or erases content. Disconnect and observed
schema refusal stop local access without pretending to revoke an upstream token.
The account owner must revoke that credential at its issuer when required.

## Rolling upgrades and operator cutover

The lifecycle preparation schema adds version and slot columns without converting
existing registrations. The MCP admission migration changes no schema object or
stored data; it establishes a code floor. Admission and every current connection
validation require all live replicas to advertise that floor. An older lifecycle
build preserves opaque rows and blocks admission while it remains live. An
unreadable fleet refuses. Rolling storage remains readable by adjacent builds.

Native document-source registrations remain retained and unavailable in the
replacement build. Upgrade does not delete their credentials, documents, originals,
uploads, history or checkpoints, invent MCP endpoints, transfer account consent,
or use retired credentials. Explicit local retirement, erasure and offline reseal
remain available through their existing ownership rules. Recording sources and
unrelated model providers, tracker and code integrations remain separate.

The operator completes normal source qualification and the fleet rollout, configures
approved profiles, verifies supported protected clients, and performs the normal
feature cutover. Running migration or merging code alone does not activate a remote
connection. Rollback retains MCP bytes and refuses unsupported access; it does not
translate an MCP registration into a native source. Existing native credentials
require owner-led reconfiguration and fresh selection/preview/consent.

## Qualification boundaries

Deterministic servers exercise resource-based and mapped-tool reads through the
same official SDK engine. Functional qualification uses owned stores and synthetic
identities, real knowledge writes and protected readbacks. Scale qualification
uses a frozen synthetic registry and measures query work, bounded scheduling and
latency. In-process ASGI transport does not qualify TCP, TLS, DNS, proxy behavior,
external vendor capacity or every server marketed as MCP. No provider or model
calls are needed for these tests. Only actual exact-head hosted receipts establish
a qualification result; authored tests and protocol compatibility are not provider
certification.

## Source entry points

- `server/clients/mcp_source.py` owns the official SDK session and bounded protocol
  calls. `server/knowledge/app/mcp_profiles.py` validates trusted configuration.
- `server/knowledge/app/mcp_connections.py` binds owners, sealed credentials,
  current approval and the fleet floor. `mcp_source_sessions.py` persists observed
  schema refusal against the exact current binding.
- `server/knowledge/app/mcp_sources.py` and `mcp_source_controls.py` own preview,
  selected-source mutation and reconciliation. `mcp_browse.py` and `mcp_live.py`
  retain protected retrieval proofs.
- `server/knowledge/app/source_sync_mcp.py` reads selected content through the
  shared knowledge write path. `mcp_source_worker.py` joins the bounded scheduler
  to `source_sync.py` and its existing leases and state.
- `server/knowledge/app/source_access.py` checks current per-document proof;
  the existing publication machinery checks retained contributions at the tail.

## Invariants

A connection is never widened by a source selection or model argument. A catalog
header and a citation grant no authority. Live retrieval saves no document or
memory. Retained bytes are not a current access grant. A shared destination cannot
convert private source material into a team publication. No failed or partial
listing is deletion evidence. Schema refusal, pause and disconnect retain data;
writer-requested erasure remains the only erasure authority.

Copies already published into ordinary namespaces retain their existing namespace
access and erasure rules. Retiring an executable registration does not retroactively
change that publication or silently delete its documents. Unsupported protected
mirror registrations remain unreadable until an explicitly supported authority
contract exists.
