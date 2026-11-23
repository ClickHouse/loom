# Selected MCP source execution

Selected MCP source execution copies explicitly registered text resources into a
protected mirror. This page describes its authority checks, publication boundary,
progress, and limits.

## Source entry points

`server/knowledge/app/mcp_source_authority.py` provides:

- `validate_connection(org, owner, contract)`: checks the stored active account,
  generation fence, owner standing and cutoffs, sealed envelope, and current
  operator profile. Registration callers must hold their subject, account, and
  destination locks. This check grants no source or destination authority.
- `validate_registration(source)`: checks a proposed row's supported contract,
  connection binding, owner writer grant, and destination. The caller authorizes
  the human and holds the subject, account, and namespace locks through insertion.
  Collection copying requires exact stored copy consent, current operator profile
  approval, and an owner administrator grant. This grants no stored-source execution.
- `validate_source(source)`: requires the exact current active source row,
  supported contract version, selected resources, owner, connection generation,
  current writer grant, and allowed destination. It returns `SourceAuthority`.
- `SourceAuthority.client()`: builds an official MCP client restricted to the
  stored selection. Each outbound operation rechecks source authority and the
  account snapshot.
- `source_guard(source)`: holds the account lease before the namespace sources
  lease, rechecks current source authority, and yields it while the locks remain
  held. Protected live lookup can use this boundary for its final result release.
  The caller must separately authorize the requesting reader and selected resource.
- `publication_guard(source)`: uses that boundary for durable publication and
  refuses live mode or a missing current mirror.

The connection manager's sealing and profile-binding helpers remain the only
envelope authority. Worker validation does not impersonate a human caller or
relax foreground connection validation.

`server/knowledge/app/source_sync_mcp.py` (`turn`) follows the existing `Pass`,
`TurnResult`, pass deadline, stop checks, worker identity, and private accounting
contracts. Its caller owns the normal source-turn lease and worker actor scope.
The turn uses `ensure_mirror` under the account lease and `mirror_costs`. Both
reader policies publish into the protected mirror. Owner-only documents carry an
owner access list; collection copies carry an anyone access list under the
source's immutable audience and collection reader checks.

## Publication

The official client's configured read mapping selects `tools/call` when present;
otherwise the client uses `resources/read`. Tool descriptions never choose an
operation. Discovery validates approved tool schemas without enumerating resources
or changing the stored selection.

Each accepted read has a receipt matching the selected request identity, operation,
client configuration digest, and text hash. Resources and structured tool results
require verified result identity. An explicit tool read mapping with
`result_format=text` may return an unverified result identity: its receipt still
binds the actual selected request, body, configuration, and operation. Metadata
records `mcp_identity_verified` truthfully.

Bounded SDK text resource bodies are text even when their declared MIME type is
`application/json`. Blob resource bodies remain unsupported. The original is
stored as UTF-8 `text/plain`; `mcp_declared_mime_type` retains the upstream
declaration in protected metadata. There is no conversion or extraction step.

The address comes from `mcp_identity.resource_uri`, using the registered source ID
and exact selected resource identity. Remote result URLs cannot change it. The
same address identifies the document, its chunks, and citations. Metadata records
the profile, generation, selection digest, read operation, and local text hash.
The source revision is empty because no upstream revision guarantee is assumed.
After each successful selected read, the server stamps `mcp_read_at` with its UTC
read time and `mcp_scope_sha256` with the current bound scope. That digest includes
the server-controlled scope revision and excludes the label and pause flag.
`mcp_config_sha256` records the client configuration bound by the receipt.

New or changed content calls the existing `source_sync.write_document` and
knowledge upsert path, including original UTF-8 bytes, chunks, document metadata, indexing, imported
provenance, and source tags. The MCP commit guard is reacquired around original
storage and around chunk/document commit after embedding. Both locks remain held
through each storage boundary. A changed source, account, profile, owner standing,
writer grant, or destination refuses the commit.

Unchanged content uses the current document head and existing document row builder.
The head must be active, published, owned by this source's sync worker, and match
the exact mirror, selected URI, source ID, title, text, content digest, and original
metadata. The MCP publication guard holds both leases while re-reading the head,
validating authority, and inserting a row over its exact optimistic version. Only
metadata, access, and row version change. Original fields, authorship, provenance,
revision, ingestion fields, and chunks carry forward. No embedding runs. Unsafe
or missing state uses the existing guarded upsert. A pending head never reports a
successful refresh.

## Progress and invariants

`live` mode creates no mirror and writes no knowledge. `snapshot` copies the
selection once per binding or explicit refresh request. `selected_refresh` rereads
the same fixed selection on subsequent turns. Progress uses a local index plus a
digest of the source identity, owner, connection binding, contract, and selection;
it never stores or reuses an MCP session cursor.

Progress is stored through `TurnResult.kind_state` in the existing protected
mirror state. A failed or missing read leaves its index pending. A changed binding
or selection resets the index. A document bound holds new copies back while
allowing already stored selected copies to refresh.

An explicit per-item remote `MCPSourceError` invalidates only that selected prior
owned copy under the MCP publication guard. A metadata-only row clears
`mcp_read_at` and `mcp_scope_sha256` and sets access to `UNREAD`. Original bytes,
history, revision, and other selected documents remain intact. A later successful
read can refresh that same original. Cancellation, authority failures, and
unexpected exceptions do not trigger this invalidation. Lost authority blocks
reads and cannot be bypassed to write an invalidation.

After every selected read and publication succeeds, with no pending publication,
the turn records the existing protected listing timestamp under the commit guard.
This timestamp records progress only; it grants no document permission.
Protected read admission requires the exact selected document URI, a scope digest
equal to the current bound scope, and a server-controlled successful `mcp_read_at`
within the existing ACL stale TTL. Partial progress may publish independently fresh
selected documents. `TurnResult.full` stays false. Selected-set completion proves
no complete upstream enumeration, remote ACL synchronization, deletion, or revision ordering.
Absence, errors, partial results, and selection changes never delete documents.

The audience is immutable for a source ID. Moving from a private audience to a
collection audience requires another registration and an upstream read; a policy
edit cannot republish a private copy as a collection copy.

Source admission, worker dispatch, requesting-reader authorization, live result
assembly, and source lifecycle routing are separate callers of these interfaces.
