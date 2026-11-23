# MCP source lifecycle format

MCP source metadata uses the existing connected-account and document-source stores.
The format preserves connection identity and opaque source payloads during lifecycle
work. It grants no source execution, document access or publication authority.
The [outbound client](https://github.com/ClickHouse/loom/pull/1194) handles protocol reads separately.
All exposed operations belong to Loom's existing operations registry and inbound
MCP endpoint; an external source is an outbound connection.

## Identity and storage authority

A connection belongs to one organization and canonical authenticated owner. Its
account kind is an opaque slot, `mcp:` followed by 32 lowercase hexadecimal digits.
The existing account key is `(org, subject, kind)`; a single account kind named `mcp`
would collapse independent connections. The account's existing connection generation
and durable fence distinguish a current binding from stopped historical bindings.
A slot is not a remote user, tenant, workspace or verified email identity.

The versioned connection envelope binds organization, owner, slot, generation,
approved profile identity and digest, endpoint and credential audience. Bearer
material is excluded from ordinary serialization and repr. Explicit envelope encoding
produces only the plaintext intended for the existing connected-account sealing
purpose. The codec admits anonymous or preissued-bearer shapes; it performs no
network access, endpoint approval, authorization enrollment or token refresh.
The existing stores remain the authority for encrypted account rows and fencing.
There is no second credential vault or connection registry.

A source retains its ordinary organization, namespace, name and source ID. The
additive `connection_slot` and `source_contract_version` columns permit lifecycle
inventory without opening credentials or parsing source content. Native rows use
an empty slot and version zero. An MCP source uses kind `mcp`, credential mode `mcp`
and a known version, with an explicit connection generation and approved profile
binding. Those markers describe storage; they do not admit execution.

The source contract splits metadata into `config_json` and its ordered, unique
resource selection into `items_json`. The selected identities have one stored
representation. IDs are opaque and scoped by the connection binding. They cannot
select a different owner, endpoint or tool. The codec bounds the envelope, selection
and individual UTF-8 IDs, refuses unknown fields and versions, and returns a fixed
error that excludes the supplied payload. Its reader-policy field is a declaration
requiring separate authorization, never a grant.

## Unknown formats and lifecycle writes

A nonzero contract version, MCP kind or MCP credential mode makes a row opaque to
native source codecs. Loading it retains the exact config and item strings, including
unknown versions or invalid JSON. Complete row writes preserve both strings and the
slot, version, source identity, owner, generation and encrypted columns. Lifecycle
work must not normalize an unknown payload into an empty native configuration.
The raw fields are excluded from repr and public source detail projections.

Native serving, mirror synchronization, source management and checked source access
refuse opaque rows. The access path repeats the discriminator check after loading a
connected source; current standing also refuses an unknown format before secret use
and after rereading the registration. No native connector is an MCP fallback.
This format alone supplies no generic read authority.

Account lifecycle matching compares organization, owner and exact account kind or
MCP slot. Stopping one slot cannot select every source with kind `mcp`. Existing
account-before-namespace lease order, connection fences and retirement records remain
in use. Historical selectors and residue counts inspect raw versions, including
bindings hidden by the latest row. Owner cleanup and restore inventory can discover
source-only history without a decryptable account row or a current approved profile.
Malformed slot keys refuse; cleanup never guesses a slot from opaque JSON.

Owner erasure inventories account and raw source-history identities, including an
owner with no surviving account row. It resolves verified aliases per organization,
preflights selected historical generations, then rereads standing under each account
lease before fencing and retirement. Explicit organization scope remains in force.
A permanent cutoff permits source-only cleanup; a dated cutoff cannot establish an
unknown connection's age and refuses. A lifted cutoff or newer current account is
preserved. This scoped deletion path shares per-owner lifecycle primitives with
restore; it never invokes deployment-wide offline restore. Remote revocation follows
all local cleanup.

The credential-free recovery marker retains an already inventoried registration
generation: the current account's registration, or an exact-owner historical
registration when no account survives. It stays inactive and contains no credential,
remote identity, scope or refresh state. Every family fence is stopped. Retaining
that registration keeps interruption recovery within the same bounded raw-history
workset, even when it already fills the configured generation limit.

The erasure call receives a separate in-memory object carrying the actual stopped
fence value from the marker registration's own prefix. The strict erasure guard
still requires equality with that durable claim. The stored registration identifies
recovery work; it grants no checkout authority. Retirement and all raw-history
censuses must finish before the recovery row disappears. Retrying neither adds a
registration nor skips a historical generation or its prefix validation.

Deployment admission limits live in the settings catalog: sealed plaintext bytes,
selected identities per source and UTF-8 bytes per selected identity. The app codecs
apply these bounds when writing and reading contracts. The pure version-one wire
models retain their fixed ceilings, so configuration can narrow admission without
accepting a format another lifecycle-aware build cannot parse. Lowering a limit
refuses an oversized stored contract; lifecycle preservation and erasure still use
its unchanged raw columns. The configured codecs create the hosted sealed fixture.

Disconnect and explicit erasure retain their existing meanings. Local bearer removal
does not prove remote token revocation. Restore is destructive offline lifecycle
work with fencing and a final census; it is not a reconfiguration or upgrade tool.
No migration deletes documents, originals, uploads, credentials, checkpoints or history.

## Admission and upgrades

The additive schema and lifecycle-aware build are preparation. They add no public
connection creation, MCP source registration, source worker dispatch or runtime
activation. No wire field is removed, so there is no `ignored_args` change.
Native operations continue under their existing contracts.

The preceding build does not understand generic slot cleanup or preserve the new
fields in a complete replacement write. Therefore applying the schema is insufficient
permission to persist generic authority. A later admission path must require the
lifecycle-aware build across every live deployment before creating or rebinding a
connection or source. It must also establish a maintenance and rollback floor for
standalone erasure, reseal and restore tools, which deployment heartbeats cannot fence.
Mixed-version tests must distinguish execution refusal from lifecycle compatibility.

The final replacement must retain native registrations as nonexecuting reconfiguration
metadata, preserve stored content and require an owner-authorized MCP binding. It must
not infer endpoints, reuse native credentials at a different audience or invoke source
removal, disconnect or restore as upgrade cleanup. Operator cutover is separate from
schema installation. Rollback after generic writes must retain lifecycle-aware tools.

## Access, retrieval and provenance boundary

Live retrieval uses the existing protected live-search path and writes no imported
knowledge or memory. Selected snapshot and refresh use the existing knowledge,
original, extraction, chunk, indexing, accounting and erasure paths with current
connection and selection checks. A persisted source identity must retain connection
and resource binding separately from its display citation. A citation is never
permission to fetch another endpoint.

Missing upstream ACL evidence defaults personal imports to owner-only access at every
read, cache, export, derived-result and delivery boundary. A team destination grants
no private-source authority. Intentional collection-copy publication requires explicit
source-sharing authority, current destination grants and disclosure that upstream
permissions are not mirrored. Preview explains current effective access and capability
limits; mutation and delivery revalidate them. Ordinary collection inheritance and the
automatic personal-namespace restriction remain in force.

Standard resource and tool capabilities do not prove complete enumeration, deletion,
revision history or reader permissions. Partial pages, errors and notifications cannot
retire content. Continuous synchronization needs its own qualified evidence contract;
a selected refresh must name that narrower behavior. These access and execution paths
require separate implementation and hosted qualification before generic admission.
