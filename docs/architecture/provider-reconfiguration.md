# Provider reconfiguration

Loom stores dashboard model configuration as encrypted, versioned rows in
`loom.provider_configuration`. A configuration has one committed predecessor;
stale saves are rejected. Dashboard overrides take precedence over the server's
startup environment. Resetting to the environment restores that startup snapshot,
not a newly edited dotenv file.

## Activation and admission

Key rotation and LLM-only changes can activate without rebuilding vectors.
Changing an embedding provider, endpoint, model or dimensions creates a queued
transition. It must not activate against vectors from the previous space.

The runtime closes its local model-work admission gate when it observes an
unfinished transition, including a failed transition. Existing admitted work can
finish; new data-plane requests receive a retryable maintenance response, and
worker passes wait without consuming their execution deadline. Health, model
configuration and dashboard control routes remain reachable. A committed matching
transition permits recalibration and reopening admission. A later key-only revision
can satisfy this check through its committed predecessor chain.

This gate counts work within one process. It is **not a distributed writer barrier**
and does not prove that deferred writes or another replica have drained.

The refresh loop runs the coordinator outside that gate. Every server process and
native data-plane CLI command enrolls in `loom._provider_participants` before
loading committed settings or admitting work. A transition requires each existing
participant to acknowledge the same configuration ID after requests, worker
passes, registered finite child tasks and buffered writes have drained. A newborn
loads either the pending closed gate or the committed replacement before serving.

Each bounded rebuild step holds the shared configuration Keeper key. It cannot be
taken over merely because its TTL or a heartbeat expired. A stopped local owner
can be recovered only by acquiring its existing kernel-held proof file on the
persistent blob volume and confirming no residual ClickHouse queries, mutations
or asynchronous inserts. Missing files and unreachable remote owners prove
nothing: they keep the transition blocked. Do not delete the proof files or use
an ephemeral blob volume. A lost remote host requires operator fencing and
recovery; automatic timeout-based takeover would allow a paused writer to resume.

The first coordinator checkpoint freezes endpoint/model identity and valid
configured prefix widths. Restarting does not adopt changed prefix settings.
Rebuild clients use raw provider requests with explicit credentials, bypassing
both process and shared serving caches. Sibling requests finish cancellation
before a failed batch releases ownership. Activation clears process-cache vectors
and the old probed width before rebuilding serving calibration.

## Rebuild staging

The rebuild inventory accounts for all known live vector columns. History vectors
are preserved; caches and derived code prefixes have separate actions. Unknown
vector surfaces fail the preflight instead of being silently omitted.

The implemented staging primitives require an exclusive coordinator lease and
drained writers:

- Snapshot-phase preflight reconciles partial copies by receipt, source schema
  and UUID before any write. Unfinished source copies may be accounted for in the
  inventory but are rebuilt, never accepted as complete. Targets without completed
  matching sources and unexpected private tables stop recovery.
- Source copies retain every writable column and assign stable paging ordinals.
  A completion receipt binds a successful copy to its source UUID and schema
  fingerprint. An unconfirmed copy is rebuilt, not accepted from its row count.
- Model-keyed replacing tables select the highest revision for each logical key
  across old embedding labels. Equal revisions prefer a tombstone, then the old
  model label provides a deterministic tie-break. A later active revision retains
  an explicit restore. Selection keeps the complete chosen row rather than mixing
  independently aggregated fields. The source-policy version is part of the
  snapshot fingerprint, so old receipts cannot silently adopt a changed recipe.
- Snapshot cleanup checks ownership, UUID and local query status. Query inspection
  must use the same pinned ClickHouse node as creation; it is not a cluster-wide
  acknowledgement. Old coordinators must be fenced before recovery.
- Target tables are private `ReplacingMergeTree` tables keyed by source ordinal.
  Repeated batches have one logical row under `FINAL`. Their ownership marker binds
  them to a source snapshot and embedding-space fingerprint.
- Vector generation uses an explicitly supplied target embedder. It validates the
  complete batch before writing and does not change process-wide providers.
- The cache action invalidates only the owned private cache target and verifies
  it is empty, without calling an embedding provider. This also invalidates old
  endpoint-specific entries when the model label is unchanged. The served cache
  remains untouched until its replacement is published.
- Tracker pages hydrate title/body text through the namespace's existing source
  gate and normal indexing recipe. Matching includes tenant, kind, item number,
  repository source ID and stored text digest. Tombstones need no source read.
  A present source whose text changed refreshes its staged text digest with the
  exact authorized text sent to the target provider. Ownership and all other
  metadata remain frozen; snapshot rows are unchanged. Missing or inaccessible
  sources stop the page before provider calls or staging writes. Snapshot
  tombstones never trigger source reads or provider calls.
- Prefix derivation uses verified rebuilt full-code vectors, retaining their
  metadata inside ClickHouse and applying the normal prefix normalization rule.
  Stable ordinals separate configured projections. Verification compares full row
  multisets for each label, not only row counts. An empty frozen prefix policy
  clears obsolete twins. The manifest persists the policy; requests that disagree
  with it fail before changing staging. Runtime activation reconstructs code-search
  labels and table routes from the completed recipe, including through later key
  rotations. A mismatched provider/endpoint fingerprint keeps admission closed.
  An enabled prefix default retains its width when that width was rebuilt; absent
  widths are disabled. Vector capability caches refresh before admission reopens.
- Staged inserts select unchanged metadata directly from the source copy inside
  ClickHouse. Python transports only embedding-related updates, avoiding timezone
  reinterpretation and timestamp precision loss during metadata round-trips.
- Verification checks row coverage, null-safe metadata equality, model identity,
  vector dimensions, finite nonzero vectors, and cleared vectors on tombstones.
  A missing or malformed verification result is not success.
- Verification also checks the eventual live sorting key. Distinct staging
  ordinals must not silently collapse when old-model rows are rewritten to one
  model and published into a replacing or aggregating engine. Remaining collisions
  fail verification after canonical input selection.

The step runner persists progress only after an effect completes. Effects must be
idempotent because a process can stop after a database write but before its
checkpoint is saved. Failures retain the last cursor and expose a fixed error code;
the administrator's retry action requeues that same transition.

The progress checkpoint also carries a versioned recovery manifest, with the
configuration ID, embedding-space fingerprint, frozen prefix dimensions and
ordered full table inventory. Missing prefix policy is not interpreted as an
empty policy; older manifest versions fail closed rather than adopting runtime
defaults.
Each entry records the original schema and UUID plus the snapshot and target
UUIDs. Verification adds the prepared UUID and verified row count without
changing those frozen identities. The runner persists this manifest before
publication and passes it to publication and cleanup. Missing manifests,
unknown versions, partial inventories and reused UUIDs fail closed; recovery
does not infer table identity from a name. The manifest contains metadata only,
not credentials, vectors or source content.

Recovery inventory excludes a private copy only when its exact name and physical
UUID match the manifest. Unknown vector tables, another job's copies and same-name
replacements still fail preflight. Unjournaled copies need receipt reconciliation
before this check; a staging prefix alone never bypasses the inventory. This
inventory check does not prove that all journaled copies still exist.

Vector patches travel as typed JSONEachRow multipart file data, not a single
HTTP parameter field. Full-size pages can exceed ClickHouse's form-field limit
even when the SQL itself is small. Only ordinal and embedding-related fields
cross this write path; unchanged metadata is joined from the snapshot inside
ClickHouse, retaining DateTime64 precision. The upload creates no persistent
patch table, and synchronous writes retain the normal retry and deduplication
settings.

## Publication primitives

Candidate preparation clones the live schema into a private table and copies
only writable columns from verified staging. It checks both logical row count
and full-row multiset equality inside ClickHouse, including nulls and duplicate
counts, before recording completion. Resuming a completed candidate repeats
these checks; an equal row count alone does not prove an intact copy. This
helper currently accepts local MergeTree engines only: replicated and shared
engines require a separate replication-path plan.

On coded tables, preparation preserves the canonical DDL and the existing
Quantized method and codec chain, replacing only the table identity, ownership
comment and vector width. Cleared staging vectors become fixed-width zero
placeholders in these physical columns; dimension flags, tombstones and other
metadata remain unchanged. Full-row verification compares this explicit physical
projection, not an equal row count alone. Rabitq and TurboQuant widths must be
multiples of eight. Unknown codec arguments and operator-created vector indexes
or constraints require a separate dimension plan; preparation fails closed rather
than dropping codecs or publishing an incompatible layout.

The publication helper takes durable original and prepared table UUIDs. It moves
the live table to an owned backup name, then moves the prepared table to the live
name. A retry recognizes the before, intermediate and completed states by UUID.
It does not use `EXCHANGE`: the shared client's automatic network retry could
repeat an exchange after a lost response and restore the old table inadvertently.

The frozen recipe also journals dependent explicit-TO materialized views and their
replacement UUIDs. View DDL is encrypted because operator-authored definitions can
contain secrets. After every replacement table is live, publication re-creates
these views so future inserts bind to the replacement source rather than its
backup UUID. Lost DROP/CREATE responses resume against the journaled identities;
unknown identities stop the whole preflight. Views with implicit inner data are
unsupported: dropping them would discard data. The destinations of explicit-TO
views are preserved, and view rebinding completes before backup cleanup.
Discovery follows the source tables' `dependencies_database` and
`dependencies_table` arrays, not their asynchronous loading dependencies.
Missing view metadata and cross-database dependencies fail closed.

These one-way renames leave a temporary gap at the live name. All readers and
writers must remain drained until every table is published and verified. The
prepared table must already have its intended live schema, indexes and constraints;
the ordinal-keyed staging target is not itself a publishable replacement. Backup
cleanup and provider activation are separate coordinator steps.

Cleanup validates every live table against the journaled replacement UUID before
dropping any private copy. It refuses unknown or replaced copies and checks local
queries and unfinished mutations. Exact journaled snapshots, targets and backups
are removed with retry-safe drops; missing copies allow interrupted cleanup to
resume. A final identity census must find no remaining copies for this job. The
caller still owns the distributed fence and keeps admission closed throughout.

## Recovery and erasure

Local runtime activation waits for admitted requests and registered `TaskSet`
work to finish before changing embedding clients and reopening admission. The
check does not cancel deferred writes. It is not a distributed acknowledgement:
unregistered tasks, buffered writes and server-side queries require separate
drain checks, and completion alone does not prove that a write succeeded.

The concrete storage backend connects snapshot manifests, per-table rebuilding,
verification, replacement preparation, publication and cleanup to the durable
step runner. It receives an explicit embedder, frozen policy and barrier callback;
it does not create the coordinator lease or prove distributed quiescence itself.

Boot migrations share the provider configuration fence once its table exists.
Same-schema restarts skip migration DDL while a transition is pending; upgrades
with pending migrations must wait for that transition to finish. Retention DDL is
admitted through the model-work gate and is skipped on a maintenance restart.
Apply a changed retention environment after the rebuild by restarting again.
MCP idle-run sweeps do not start new summarization work during maintenance.

## Docker verification

`scripts/gates/docker_quickstart_smoke.py` installs the generated release bundle
into a fresh, disposable Compose project. It checks dashboard loading, memory,
shipped compiler toolchains, persisted compiler results, restarts and the documented
environment-update/recreation path through the default production entrypoint.

Its provider phase uses Chromium to submit the real Models & keys form, rotate a
key, change embedding dimensions, retry a failed rebuild and restore environment
configuration through the confirmation UI. It checks nonempty
memory/code vectors and preserved metadata, two-web-worker coordination,
maintenance responses, crash recovery,
container recreation and removal of private rebuild copies. The provider phase
redirects only the upstream LLM HTTP endpoint in a test-only Compose entrypoint;
embedding requests use a network-only compatible-provider fixture. Server-side
validation, authorization, encryption, coordinator and ClickHouse storage remain
real. Fixture credentials and outputs do not prove paid-provider access, model
quality or production-cluster behavior. CI runs this gate on amd64 and arm64.

For a local run, first install the frontend dependencies and Chromium
(`cd frontend && npm ci && npx playwright install chromium`), build the production
image, then run the gate with `--image <repository:tag>`. It removes only the
generated project's containers and volumes.
Unit tests exercise this orchestration with mocked storage effects, not a complete
Docker provider-change flow.

The coordinator advances queued jobs through snapshot, rebuild, verification,
publication, cleanup and activation. Failed jobs remain closed until an explicit
retry; it resumes their durable cursor. Dashboard progress exposes phase, table
and counters, not private recovery manifests or recipes.

Erasures arriving during a transition receive the same retryable maintenance
response as other data-plane writes. Detached erasure sagas started beforehand
are registered finite tasks and must finish before the snapshot. The residue
census also inspects every rebuild source, target, prepared copy and backup. It
counts physical rows, including old replacing versions and tombstones; copies
without usable scope columns count conservatively across scopes. Unknown copies
or unreadable counts fail verification rather than being interpreted as zero.
Copies are removed by rebuild cleanup, not edited underneath its frozen recipe.
An interrupted rebuild therefore cannot leave a snapshot outside erasure
accounting or report deletion while a copy can resurrect the subject's data.

## Current implementation boundary

Publication currently supports local MergeTree engines on one pinned ClickHouse
node. Replicated/shared engine preparation and cluster-wide residual-query
inspection still need their own supported plan, so a save or reset that changes the
embedding space is refused with 409 while any rebuild table uses another engine
and any re-embed table holds rows, before a transition is queued or admission
closes. The status response carries the same reason in `embedding_change_blocked`.
With no stored vectors there is nothing to rebuild, on any storage: the change commits as active
and each process drains, then switches, through its configuration refresh. Rows
written by a process before it switches keep the previous model's vectors. Direct database writers outside
Loom's registered server/CLI paths must be stopped by the operator; a census of
Loom processes cannot fence an arbitrary external SQL client. A lost remote
participant without a death proof is an explicit recovery boundary, not permission
to ignore it. Full Docker provider-change and crash-recovery E2E validation remains
a merge prerequisite; unit orchestration and storage-level tests are not that flow.
