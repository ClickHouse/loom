# Definition drafts

Needs `knowledge_gaps_enabled` for gap drafts or `knowledge_query_history_enabled` for
query history. Both paths put model-written definitions in the admin review queue.
The model never publishes a definition. This page describes evidence, private candidates
and review; [knowledge gaps](knowledge-gaps.md) describes recording and grouping searches.

## Evidence and access

The Library's **Draft definition** action calls `knowledge.gaps.draft`. It rereads the
gap's questions against the current seven-day miss window and requires at least
`knowledge_gaps_min_askers` distinct askers for each question. It sends only those
normalized questions to the model. Capture consent is read uncached, and unavailable
consent refuses the draft. A threshold change or a user erasure during work invalidates
the evidence and removes that attempt's candidate versions.

`knowledge.query_history.add`, `.list` and `.remove` need namespace admin access. In the
Library, **Query history** opens the source list and connection form. A registration has
a name, host, port, TLS choice, read-only account, database allowlist and optional cluster.
Its password is sealed at rest and never returned. The connection reuses the tracker's
host checks, pinned address and read-only grant review. A cluster also needs `REMOTE`.
Removing a registration stops subsequent turns without deleting an existing draft or
definition. Disconnect remains available when the operator disables query history.

The worker reads finished, initial `SELECT` queries whose nonempty database list lies
entirely in the registration's allowlist. A named cluster uses `clusterAllReplicas`;
otherwise only the connected node is read. Groups use `normalized_query_hash` and
`normalizedQueryHashKeepNames`, ordered by distinct `initial_user` accounts and recency.
The second hash keeps different numbered table names apart without splitting whitespace
or literal variants. `normalizeQueryKeepNames` removes comments and replaces literals,
preserving identifiers. Only this normalized SQL reaches the model.

At least `knowledge_query_history_min_users` accounts must support a group, never fewer
than three. Accounts are not people: one shared account counts once, while one person's
several accounts count separately. This floor provides no anonymity guarantee. A live
threshold increase or history-window change invalidates an in-flight result. Source
removal, a changed source UUID, feature disablement and namespace erasure also invalidate it.

Candidates use `source_kind = knowledge_gap` or `query_history`. Their model-written
text and query-history SQL remain private to namespace admins in `learnings.list`,
quality counts and both explicit and inferred lineage. Filtering precedes pagination and aggregation, including org-wide
quality reads. Read-narrowed tokens cannot call either operation prefix.

## One model attempt

The candidate ID derives from the organization, namespace, kind and evidence resource.
A gap uses its gap ID; query history uses the registration UUID and both query hashes.
A short candidate lease persists a pending claim before releasing the lease for the
model call. Concurrent and later requests return that candidate ID, including after a
decision, model error, invalid result or crash. Such failures retain a pending claim
whose rationale tells the admin to reject it and write explicitly. A policy hold before
generation removes only the unspent claim's versions so a retry can proceed when allowed.

The org's model-call switch and namespace daily cap are checked before claiming work;
an unreadable cap refuses it. Each attempt permits one bounded JSON completion. The
result validates term, aliases, body and table/column list sizes. Gap provenance in
`rests_on` holds the gap ID and the model's table/column lists. Eligible questions reach
only the model input; the application does not copy them into the candidate. The model
may repeat a question in its drafted text. After generation, the draft and gap ID can
outlive a user erasure of the source questions. Query-history provenance retains its
normalized SQL and the model's table/column lists. A final candidate lease rereads status so
completion cannot replace a rejection or restore scrubbed text. Evidence fences run
before and after model work and after insertion.

The worker limits sources, groups, attempts, query bytes, rows, memory and execution
time. It starts no further group after half the pass budget and saves the last source's
UUID before its turn, so a failing source does not prevent rotation. The fixed-size UUID
also keeps source names out of the persisted cursor. Connection failures use a bounded
per-source circuit breaker. Worker cost events retain namespace attribution.

## Review and publication

`learnings.review` accepts these candidates only for destination `memory`. Before a
write, it checks fleet capability, sensitivity, the shared-definition ceiling and name
conflicts. A draft revises the live shared definition whose folded term or alias matches
its term; ambiguous matches refuse. A new term uses the random `definition/` key persisted
with the claim. The shared rows are reread before writing so a concurrent rename cannot
redirect the write to a different term.

The review writes a namespace-wide, derived definition through `memory.set`, clearing
expiry and validity bounds. An exact, active, unexpired head needs no write, even when
pinned or locked. A changed value refuses a pinned or locked head. A revision requires
the current source text to match the shared copy; a writer's intervening edit refuses.
Retained expired heads use their hash for a conditional rewrite, and an absent head uses
`if_match_sha256="absent"`. Conditional callers serialize on the key; ordinary concurrent ClickHouse
writes resolve by version. These checks do not make writing and promotion atomic.

The review calls `memory.promote` with the approved canonical-value hash. The server
supplies candidate provenance internally and stamps the authenticated actor. A client
cannot supply trusted provenance or choose the deciding actor through `approver`.
Promotion publishes the entire definition, including normalized SQL, to readers of the
namespace, so the admin approves that text for all of them.

A committed write followed by publication or promotion failure leaves an ordinary
namespace-wide definition and a pending candidate. Ordinary retrieval can read that row.
A retry promotes an unchanged exact head without rewriting it. Rejecting the candidate
leaves the source row intact. There is no automatic undo: it could overwrite a later
writer's same-text metadata revision. The admin can restore intended text through memory
history and write operations or delete an exact version explicitly.

## Erasure, audit and upgrades

Namespace erasure deletes `loom.query_history_sources` through
`query_history_sources.delete`. The registration holds configuration and a sealed
credential, so it is classified under that step without a memory-content census row.
Candidates follow the learnings queue's erasure coverage. A historical definition redact
also scrubs matching private candidates by canonical text hash or the redacted version's
candidate tags, across every candidate version. It uses the review lease, refuses later
review of blank text and preserves a live head that still holds the redacted text.

The review audit identifies the authenticated deciding actor and candidate. Internal
`memory.set` and `memory.promote` emit their own audit events; the promotion event is also
the audit request ID that `memory.lineage` follows. Source mutations audit the scoped
registration name, never its password.

Private-kind production and promotion require every live server and worker heartbeat to
advertise the registration migration. Missing or unreadable fleet state fails closed.
Apply the migration and upgrade every server and worker before enabling either drafting
path. Keep both paths disabled while any serving build is older or unknown. An older
build does not filter private candidates or erase registrations. Fleet checks alone do
not protect a rollback: a request can pass its check before an older heartbeat appears.

Rollback requires a maintenance window before any older reader starts:

1. Disable both `knowledge_gaps_enabled` and `knowledge_query_history_enabled`. Stop new
   private drafting, review and source-registration requests and query-history worker
   turns. Quiesce their queued work and every admitted model call, review, registration
   and removal. Disabling a flag does not cancel an existing request or review.
2. Let those operations finish or terminate, then settle their uncertain server sends
   and retained writer leases through their bounds. Do not clean the tables while an
   admitted operation can still write a candidate, decision or registration.
3. With only a supporting build running for maintenance, remove every version of
   `knowledge_gap` and `query_history` candidates from `loom.learnings_queue`, and all
   `loom.query_history_sources` registrations. Wait for deletion to finish and verify
   both are absent on every storage replica a reader can use. Preserve ordinary
   candidates and published definitions. Only then start an older reader.

Keep private producers disabled throughout the downgrade. Enable them again only after
all serving processes support the registration migration. Governed memory and shared
rows retain their existing upgrade rules.

## Source entry points

- `server/memory/app/definition_drafts.py`: durable claims and bounded generation.
- `server/memory/app/definition_review.py`: fleet gate, promotion and candidate redaction.
- `server/knowledge/app/gap_drafts.py`: current gap eligibility and consent.
- `server/knowledge/app/query_history_sources.py`: registration and credential checks.
- `server/knowledge/store/query_history_remote.py`: read-only grouped query-log reads.
- `server/knowledge/workers/query_history.py`: bounded source rotation.

## Invariants

- Only an admin can read a private candidate or publish it through review.
- No model completion replaces a candidate decision or resurrects erased evidence.
- A source registration cannot outlive namespace erasure and continue producing drafts.
- A refused or interrupted promotion does not undo a writer's row.
