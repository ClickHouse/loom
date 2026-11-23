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
  This helper refuses collection copying and grants no stored-source execution.
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
The turn uses `ensure_mirror` under the account lease, `mirror_costs`, and an
explicit owner access list.

## Publication

The official client's configured read mapping selects `tools/call` when present;
otherwise the client uses `resources/read`. Tool descriptions never choose an
operation. Discovery validates approved tool schemas without enumerating resources
or changing the stored selection.

Each accepted read has an identity-verified receipt matching the selected identity,
operation, client configuration digest, and text hash. Only text MIME types are
copied. Binary resources and plain tool text without verified result identity
refuse publication. There is no conversion or extraction step in this turn.

The address comes from `mcp_identity.resource_uri`, using the registered source ID
and exact selected resource identity. Remote result URLs cannot change it. The
same address identifies the document, its chunks, and citations. Metadata records
the profile, generation, selection digest, read operation, and local text hash.
The source revision is empty because no upstream revision guarantee is assumed.

Publication calls the existing `source_sync.write_document` and knowledge upsert
path, including original UTF-8 bytes, chunks, document metadata, indexing, imported
provenance, and source tags. The MCP commit guard is reacquired around original
storage and around chunk/document commit after embedding. Both locks remain held
through each storage boundary. A changed source, account, profile, owner standing,
writer grant, or destination refuses the commit.

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

After every selected read and publication succeeds, with no pending publication,
the turn records the existing protected listing timestamp under the commit guard.
This timestamp covers only the registered selected set and its owner permission
lists. `TurnResult.full` stays false. Selected-set completion proves no complete
upstream enumeration, remote ACL synchronization, deletion, or revision ordering.
Absence, errors, partial results, and selection changes never delete documents.

Without upstream ACL evidence, publication is owner-only. `collection_copy` is
refused even when the current operator profile permits it: the stored contract
does not prove durable writer consent for that publication. Admission must resolve
that missing authority before exposing this policy.

Source admission, worker dispatch, requesting-reader authorization, live result
assembly, and source lifecycle routing are separate callers of these interfaces.
