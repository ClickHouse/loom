# Document source worker boundary

This design specifies the requirements for the external worker integration.

Loom serves stored document search and explicit live document reads through its
existing MCP endpoint. Stored search reads indexed copies. A live read asks an
approved source worker to read one selected resource under the current caller's
connection. It returns the source link and fetch time. It does not index the
result, save it as a memory or record its body as a diagnostic. The client that
receives the result controls what it retains.

The source worker owns upstream credentials, outbound MCP requests and account
consent. Loom owns source configuration, schedules, access policy, selected
copies and their normal erasure lifecycle. Loom stores opaque worker and
connection identifiers, their generations and the approved profile digest. The
worker resolves the connection within its own credential authority. Neither side
uses the other side's credential at the upstream endpoint.

Enrollment matches a short worker-signed statement to a challenge issued for
the authenticated owner and current approved profile. The statement contains
opaque connection metadata only. Loom verifies the worker's separate enrollment
key, consumes the challenge with the link write, and never uses an enrollment
statement to authorize a read. A newer connection generation replaces older
authority without erasing a copy or moving it to another account.

## Worker provisioning

The operator mounts a worker routes file and sets
`LOOM_KNOWLEDGE_SOURCE_WORKER_ROUTES_FILE` to its path. An empty setting leaves the
worker directory disabled. Live reads and worker metadata also require Postgres
knowledge authority so the store that serves the metadata erases it. Version 1 of the JSON file contains a `workers` list.
Each entry names a `worker_id`, HTTPS `origin`, grant `issuer`, and a `profiles`
map from exact organization names to profile entries. Each entry lists a `digest`,
approved `modes`, and optional `team_access`. A collection approval cannot add
an organization, profile, mode or team permission that the operator did not allow.

The `trust_ca_file`, `client_certificate_file` and `client_key_file` entries
configure mutual TLS. `grant_key_file` names Loom's Ed25519 key for signing
requests. `enrollment_verifier_file`, when present, names the worker's separate
public Ed25519 key. File paths are relative to the routes file or absolute. These
files authenticate communication between Loom and the worker. The operator does
not put upstream credentials or upstream endpoint configuration in this file.

A worker source uses the existing source registry. Its versioned configuration
contains opaque identifiers and generations. The items hold exact selected
resource identities. No new ClickHouse table or upstream credential is needed.
A worker source has mirrored access and an empty legacy kind, so a reader that
only understands legacy source configurations cannot serve it. The release must
qualify mixed builds before enabling worker registration or copy writes.

## Binding a live read

Loom proves the current caller and collection grant, finds the exact selected
resource and resolves the caller's approved worker connection. It signs a short
read grant for that worker. The grant binds the request, operation, organization,
namespace, source and selection generation, requester, connection and its
generation, profile digest and exact resource identity. The grant is for the
worker audience. It carries no credential, URL or tool arguments. The worker
accepts grants only from its configured Loom issuer and resolves that exact
connection with matching ownership and profile. It rejects an anonymous caller
or a grant for another worker, owner, organization, connection or generation.
For a team source, the reader enrolls a specific connection for the current
selection. A different connection for the same endpoint or profile cannot
stand in for it. The owner and reader connections must share a verified resource
realm before the reader can request access to the selected identity.

The worker validates the current connection before upstream use and before
return. Loom checks current source, selection, connection and reader standing
again before delivering a result. It locks the exact approval, account and team
link rows for the bounded handoff of each response frame. A revocation that has
committed before the handoff prevents that frame; revocation cannot recall bytes
already handed off. No upstream call happens while these locks are held.

`knowledge.source.live` is experimental. It requires a protected session, an
approved route and a current source selection. This change does not enable user
registration, so it does not establish a usable live setup on its own.
A live request lives with its API request;
it has a separate deadline and admission limit from refresh work and no durable
payload queue. Denial, reconnect, saturation, unavailable and timeout are distinct
outcomes. An authorized stored copy can be offered after timeout or worker outage
with its own age and provenance. Denial never falls back to a stored result.
The initial live read returns the explicit error without a stored substitute.

Each worker resolves approved profiles to its own endpoint and read mappings.
Resource reads and validated tool reads share the same bounds. Caller input and
server descriptions cannot expand the accepted operations or outbound addresses.
Returned data is evidence for the requested resource only. An account host can
implement this worker contract locally if it can resolve the exact requester
and connection for the requested operation without an active agent session or
credential export. An agent's own integration does not establish that authority.

## Account enrollment

An authenticated owner selects an externally provisioned connection. Loom
issues a short challenge for its exact organization, owner, worker, profile,
nonce, opaque connection ID and generation. The worker has a separate key for
signing its statement about the current account. Neither the challenge nor the
statement includes the token or endpoint. Loom verifies the statement and
checks approval again under the same store locks as it consumes the challenge.
A retired generation cannot be restored by replaying a proof. Team readers
still enroll their own accounts separately for the selected source.

The Postgres enrollment records contain only scoped identity, provenance,
bindings, generation and expiry. Expired challenges can be removed in bounded
batches. Account revocation retains its generation fence until erasure. Enable
writes only when owner erasure and worker approval take the same authority
locks and every runnable eraser covers these tables.

## Stored copies and permission evidence

Imports use the existing original, document, chunk, extraction, indexing and
accounting paths. The identity of a copy is its source UUID and the exact scoped
resource identity. A display link is provenance only. The copy's stable source
URI is never an outbound fetch target. Refresh retains the copy identity and
updates content or receipt time as needed.

Team collection membership is only one requirement. A reader also needs current
upstream permission evidence for their own connection to the exact resource.
Evidence binds the reader, collection, source and selection generation, worker,
connection generation, resource identity and expiry. An import receipt from one
reader cannot authorize another. Missing, expired, denied or disconnected
evidence fails closed. Evidence from reading current content cannot establish
access to historical content unless the approved upstream contract covers it.
Search, direct reads, originals, contextual results and publication share the
same access check; derived memory and cached results cannot extend it.

## Refresh and retirement

The queue stores metadata for a selected resource, never its body. A schedule
and a manual request derive the same kind of per-resource task. Reclaiming a
lease assigns a new fence from a noncycling sequence without changing the task
key. An old fence cannot return after a queue row is pruned. Changing the source,
selection, account, or approval generation invalidates existing work. A worker
can only claim the queue partition assigned to it. A claim page reports how
many exhausted rows it retired so the worker can distinguish progress from an
idle poll. The requester, destination, and exact selection still need to be
rechecked at acceptance.

Admission serializes on the source, allows one unfinished batch of at most 64
items, and keeps at most 128 queue entries. The next admission under a changed
assignment fences and cancels old unfinished work without touching its completed
receipts. Completed receipts expire after 24 hours. A later admission may remove expired queue metadata; it never removes a
stored copy, source checkpoint or pre-existing unmarked row. If the source is
busy or retained receipts fill the budget, admission leaves the due cursor in
place. The shortest cadence depends on selection size and the receipt budget:
12 minutes for one document and 24 hours for 64 documents. The estimate leaves
room for one extra batch. Manual requests share the budget and can defer a
scheduled batch. Elapsed periods can coalesce into one batch. For manual refresh, Loom
issues one bounded offer for the exact current selection, owner, and destination.
Submitting it repeats the original result while the offer is current; an expired
or superseded offer cannot create more work. If an offer expires while a batch
is being staged, admission rolls back the batch and records the observed time.
A backwards clock adjustment cannot reopen that offer. A busy queue leaves an
unexpired offer open.

The queue does not yet have a worker-facing API or a runner. Its lease and
checkpoint tests perform no document writes. Treat the upload, copy acceptance,
and cutover described below as requirements for enabling refresh.

Loom schedules and claims bounded refresh jobs. Each claim targets one worker and
the approved source revision. Settlement checks the deadline against the
Postgres clock after acquiring the row lock. A clock adjustment can affect an
unobserved deadline; replacement and cancellation permanently invalidate the
old claim.
The worker uploads an idempotent completion for the exact claim. The server
checks the fence, deadline, registration and current connection before accepting
writes, and advances the checkpoint only for an accepted result. “Sync now”
requests the same work as a schedule. Interruptions leave copies and originals
intact; stale completion does not renew permission or replace a copy. Partial
pages, missing items, timeouts and ambiguous errors do not prove deletion.

Refresh uses a metadata queue separate from ingestion jobs. It does not change
ingestion fairness or select work for the extractor. Postgres commits the
accepted receipt with the document head and its delivery obligation. Index
delivery can recover after that commit. A deployment without Postgres knowledge
authority refuses worker refresh; ordinary ingestion still follows its configured
store. Install the queue schema and its erasure support before allowing writes,
and admit work only when every runnable eraser can remove the queue's rows.

Disconnect invalidates live reads, claims and permissions without erasing stored
bytes. The existing erasure pipeline handles a separate deletion request. Older
builds must retain opaque registrations and must fail closed on worker-authorized
copies. Keep manual import available throughout the transition. A native source
keeps its stored copies, provenance and originals through upgrade. Reuse a copy
identity only when the owner authorizes a replacement connection and the old and
new resource identities have a verified one-to-one binding. An unmapped copy
keeps its old address and erasure history. Deployment requires compatible builds,
an approved worker binding and a separate operator cutover.
