# Bi-temporal memory

Loom answers two time questions about a memory: which assertions applied at a
**valid time** V (when something was true in the world), and what Loom had
**recorded** by a **knowledge time** K. A correction, a delete or a merge never
rewrites what Loom believed earlier. Temporal consistency does not establish
truth: source evidence, accepted beliefs and the context shown to an agent stay
separate records.

## The authority: a commit journal

`loom.memory_temporal_commits` (MergeTree, ordered `(org, namespace, commit_seq,
commit_id)`) is the authority. One row is one complete change to one logical
memory. It names the assertion ids it retires and carries the full assertions it
adds, plus the operation, change kind, actor, source class, request and
idempotency identity, the payload hash at commit time and the erasure generation.
Rows are never updated, with one exception: `memory.redact` rewrites a redacted
value in place, so `payload_sha256` keeps the hash of what was committed.

An **assertion** is one claim about one memory over one valid-time span
`[valid_from, valid_to)`. `None` is an open end; `validity_basis` says whether a
bound was stated, extracted, inferred or unknown. Unknown is never the ingest
clock. Assertions also carry their temporal kind (`state`, `event`, `snapshot`,
`rule`: an event or snapshot never becomes an open-ended state), modality
(`asserted`, `planned`, `quoted`, `disputed`, …), qualifiers, status, confidence,
attestation, provenance and evidence ids. New semantic versions get new ids. A
revision names its parent and origin, so a timeline keeps its lineage.

| Change kind  | Valid-time effect                                                      | Produced by                                         |
| ------------ | ---------------------------------------------------------------------- | --------------------------------------------------- |
| `assert`     | The new assertion replaces what the key served under its qualifiers.   | `memory.set`, `bulk_set`, every plain revision      |
| `correct`    | Replaces `[from, to)`; keeps the supported segments around it.         | `memory.amend`, `memory.set change_kind=correct`    |
| `transition` | Ends the served state at `effective_at`; the new one starts there.     | `memory.amend`, `memory.set change_kind=transition` |
| `retract`    | Removes assertions from the served set; the world did not change.      | delete, clear, reject, supersession, expiry         |
| `assess`     | Status, trust, lock, pin, expiry or confidence; content and span kept. | verify, lock, pin, tag, conflict marking            |
| `redact`     | Content removed; the hashes stay.                                      | a redacted or anonymized row                        |

The reducer (`server/memory/domain/temporal_model.py`) is pure and unit-tested
against the oracle and a pointwise reference model. A correction copies only the
segments it touches. Claims under different qualifiers never supersede each
other. An assessment that attests content lands only on the assertions whose
value it hashes. A write that declares a new shape (modality, temporal kind,
qualifiers, date text) for unchanged content is a revision, not an assessment.

`memory.set` writes the value that applies now, because its row is the current
state: a transition takes effect now or earlier, and a correction's span includes
now. A future transition, or a correction of another span, is a `memory.amend`,
which keeps the current row on what applies now. An amendment changes the claim
and keeps the key's serving metadata (expiry, pin, lock, tags, sensitivity) as the
current row holds it.

## Publication

One logical publisher per namespace. A write plans its commits without a lock:
it reads the namespace head and what its keys serve, and builds one amendment per
changed key. It then takes the namespace lock (a KeeperMap lease when the server
has a Keeper, an in-process lock otherwise) only to re-read the head, re-plan if
another commit changed its keys in between, and stamp `commit_seq` and a
`recorded_at` that rises strictly with it. The commits are inserted synchronously
under the lock. **That insert is the publication boundary.** The projection and
the `loom.memory` row (the current-state projection) are written after it.

- Every memory writer goes through `insert_memory_versions`. A hook registered in
  `server/wiring.py` journals each batch before it is inserted.
  - A row that restates the head's content and validity is an assessment.
  - A changed value is an assertion, under a narrow rule set (`memory.amend`
    shapes history explicitly). A dated state fills only the span it names: it
    corrects that span, keeps every other segment, and stops at a state the key
    holds from later on, so late evidence fills the past without reverting the
    present. An undated state revises the state that holds now, keeping its start
    and stopping at a later plan; with nothing holding now it starts where the last
    closed segment ended (`inferred`, never projected onto `loom.memory`). An
    event or snapshot joins the key's others. A span a same-value assertion
    already covers is a restatement, or an assessment of that assertion when the
    write carries other metadata (sensitivity, tags, kind, modality).
  - A metadata write assesses the served assertions of its content only; other
    values and other qualifiers' claims keep their own metadata.
  - An interval that ends at or before its start is refused (422).
  - A tombstone, a redaction or an unserved status is a retraction.
- A key written before the journal existed is journaled at its first write: its
  live row becomes a `memory.journal_seed` commit (no retry key, no receipt)
  ahead of the change. A read at a knowledge time before that seed finds nothing
  of it (disclosed as `history_before_journal`). A row drawn before an erasure
  of its scope is never seeded.
- Concurrent writes resolve as `loom.memory`'s merge does: the later-drawn version
  wins. A row drawn at or below a version its key already committed (each commit
  records its `row_version`) is superseded: neither journaled nor written, and
  answered as a no-op with `left_out: superseded`. A restatement journals an empty
  `restate` commit recording its row's own drawn version (so every row has a
  version of its own, and history keeps it); each costs a commit under the
  namespace lock. A row is written only when the claim it shows is current, with
  the journal's validity: its own claim, the claim an assessment revised, or the
  one a restatement restates. One that applies only to the past, like late
  evidence, is journaled without a row; when it re-cut the current claim (a plan
  bounding it), that key's current row is reconciled once the batch is published.
- **Every writer says what it means.** Nothing is inferred from which columns of a
  copied row differ:
  - A **metadata write** (lock, unlock, pin, unpin, touch, verify, delete, status
    flips, conflict marks, expiry, apply, proposals, move, observe, entities,
    restore) declares the columns it sets. Its row is built, when it is planned,
    from the key's state in the journal (the current claim, and the current row's
    columns the journal does not keep when that row is as new as the claim), with
    only those columns its own; the assessment sets only those columns, on what
    holds now. A lock, pin, expiry, value or status committed after the read is
    never undone, and the write never needs a re-read.
  - A **value write** (set, bulk, update, restore of a value) writes its own row:
    its content, metadata and removal are its own, the later draw wins. The lock
    is the journal's (the strictest in force on what holds now). A new value under
    a `read_only` lock is left out as `locked`; memory.set and bulk, which never
    replace a pinned value they do not pin, are left out as `pinned` when a pin
    committed since their read.
  - `memory.lock` only tightens, checked against the journal's lock in force when
    it is planned (`locked` otherwise); unlock is its own operation.
  - The expiry sweep holds only while the claim it read is still expired
    (`superseded` once a touch extended it), and removes only claims that have
    expired. Removing what holds now (an expiry, a delete) removes the key's
    current row: ended history stays readable through `valid_as_of` and
    `memory.intervals`, never shown as the present again, and the reconciler never
    revives a removed row from ended history alone. A key that only ever held
    history written as history (a closed fact) shows its latest begun claim.
  - A lock binds only what holds now; a lock on an ended claim binds nothing.
    Every left-out write answers why (`left_out`: superseded, locked, pinned,
    restated). An embedding row lands only with a kept memory row the journal left
    embedded.
- A removal of a key whose lock in force refuses it is refused in planning (the
  journal's lock and deadline decide), with its row. A write that changes the
  memory it read (lock, pin, verify, touch, update by id) on a key the journal no
  longer serves is refused, never a re-assertion.
- Nothing takes rows out of service in place: `memory.clear` and `memory.reject`
  write versioned tombstone / rejected rows through the funnel, so the journal
  retracts exactly the rows removed. Usage counters stay operational and
  unjournaled.
- **Allocation:** the batch's last sequence and its recording time are recorded
  durably (the `published` watermark row) before its commits are inserted; a write
  whose record fails is refused. A sequence or recording time is therefore never
  handed out twice: not to a successor of an insert whose outcome is unknown, and
  not after erasure or retention removes the newest commits, so sequence and
  recording time keep rising together. Reads cut at the stable prefix
  (below); a read token waits for the cut to cover it.
- **On time:** a commit is part of the journal only if it landed within
  `SETTLE_S` (two minutes, past any publisher lease) of the time its publisher
  stamped. Reads, replay and the retry lookup ignore one that landed later, and
  the retry commits afresh.
- **Stable prefix:** an allocation whose insert is still in doubt may yet land on
  time, behind commits that came after it. A read therefore cuts below the first
  sequence missing above the newest settled commit (one recorded more than
  `SETTLE_S` ago): sequences and recording times rise together, so a gap below a
  settled commit can no longer land, and one above it holds the cut until it lands
  or settles. An answer at a cutoff never changes afterwards. A publisher that sees its
  own insert land late withdraws it and fails the write. An allocation whose insert
  never landed is a gap the projection worker and the reconciler step over once
  it is that old. Every time the journal compares (a commit's recording and
  landing times, an erasure's request time, when a request began) is on one clock,
  ClickHouse's, whose server timezone must be UTC (`loom.memory`'s zone-less
  `DateTime` columns are compared with the journal's UTC `DateTime64` ones): the
  publisher stamps with the server's time and converts the
  request's start with the host offset it measures at each publish. A clock behind
  the journal's last recording time is waited out when the gap is a second or
  less (replicas' clocks differ slightly) and otherwise refuses the write
  retryably, rather than stamp past the clock, which would date the batch after an
  erasure the fence missed.
- **Uncertain insert:** the publisher reconciles by commit id and retries the same
  rows; an outcome still unknown fails the write retryably.
- **Integrity:** the publisher checks it still holds the lease before inserting,
  and reads its sequences back after. If another publisher wrote one of them (a
  lease lost mid-insert), it deletes its own unacknowledged commits and fails the
  write retryably. A sequence that still holds two payloads (the withdrawal
  failed) is an integrity break: readers serve the prefix below it and
  `memory.temporal_status` names it. The next write to the namespace repairs it
  under the lock, and the `memory_temporal_projection` worker does so for a
  namespace nobody writes to: the commit that landed first (`inserted_at`,
  stamped by the server), the one a caller may have been told of, is kept, the
  others are deleted, and a warning is logged. A write planned before a repair
  plans again. Replay never passes a break.
- **Durable idempotency:** the transport's scoped key and the request body hash
  ride on each commit, past the 24-hour replay cache. A retry with the same body
  returns the original receipt whatever the key serves now; if a later commit has
  changed the key since, the retry's row is not written again, and if not, it is
  written with the memory id and expiry the commit recorded (a retry never renews
  a TTL the journal does not hold). Another body under the key is refused.
- **Erasure fence:** checked under the lock once the batch is stamped, again once
  the commits are in, and again once the current rows are in (a batch an erasure
  overtook withdraws its commits, takes its rows and vectors back out, and is
  refused). The last check runs whenever the current-row insert returns, even when
  it or the embedding write beside it failed: the rows may have landed. The insert
  and that check run in a task of their own, so a cancelled request still fences
  the rows it sent. A write is refused while an erasure of its namespace or user holds it
  (in progress, or failed and still retried: one predicate, shared with the
  reconciler), or when one was requested after the request (or worker pass) began.
- **Erasure cut:** reads never serve content recorded before an erasure of its
  scope was requested (the namespace, or a user anywhere in the org; the request's
  whole second counts as before it). The cut is read from the erasure ledger
  alone, never narrowed by which users a table holds, since that can change while
  a read runs. That is the guarantee, not the cleanup: a
  withdrawal that failed, an overtaken inline projection, or a replay whose
  re-check never ran leaves rows nothing can read. Stamping before the fence
  check makes it exact: an erasure requested after the stamp is later than every
  commit of the batch.
- **Replicas:** the read-back is exact on ClickHouse Cloud and a single node; on
  self-managed replication it is best-effort (sequence claims in Keeper would
  make it exact).
- **Search visibility:** a write's embedding rows are inserted only once the
  journal accepted its version rows, so a refused write is never searchable.
- **Receipts:** a write's output carries `commit_seq` and a `read_token`.

The journal is authoritative from a namespace's first write; there is no
journal-less mode. This trades automatic write failover for a tractable model:
memory writes need a Keeper, or an operator's declaration that one process writes
memory (`LOOM_MEMORY_TEMPORAL_PUBLISHER=process`). Without a Keeper only the server
process publishes: the CLI and any second process are refused, and process mode
refuses writes when `web_workers` is above 1 (each web process would allocate the
same sequences). A separate workers container is a second writer too: process mode
is for one server process in all; anything larger runs with a Keeper.

A batch that spans namespaces commits each in turn, and the current-state insert
runs after them. If a later step fails, the committed beliefs stand. Retrying with
the same Idempotency-Key recognises them and writes the current-state row; without
a retry, the projection worker's reconciler does (below).

Operational limits of this release:

- Publication is serialized per namespace: about 4–6 writes/s on one namespace
  (measured on ClickHouse 26.9 with a Keeper: ~10 statements and the Keeper lease
  per commit, ~120 ms under the lock), parallel across namespaces. A write that
  marks k conflict peers or restates commits k more. A write that waits past
  `memory_temporal_lock_wait_s` for its namespace's lock answers 503 "busy"
  (retryable): a single hot key under dozens of concurrent writers sees it.
- A redaction holds the namespace lock through its scrubs; writes to that
  namespace wait (and see "busy" past the lock wait). Under a Keeper lease the
  redaction checks the lease before each scrub; in process mode the lock cannot lapse.
- An allocation whose insert never lands holds reads below it for up to
  `SETTLE_S` (named `allocation_in_doubt`). So does a user erasure or retention
  that deletes commits recorded in the last `SETTLE_S`: until those sequences
  settle, reads cut below them. (Sequences a deletion voids are not recorded; a
  namespace erasure records its head and skips the wait.)
- A step of a redaction longer than its 60 s lease runs to its end; the next step
  checks the lease first.
- An amended value is vector-searchable once the embedding worker fills the
  current row's stub (lexically before).

## Projections

- `memory_temporal_assertions` (`ReplacingMergeTree(projected_seq)`) holds every
  assertion with `born_seq` and `retired_seq`; a retirement re-emits the whole
  row. A read at cutoff C admits `born_seq <= C AND (retired_seq IS NULL OR
C < retired_seq)`, then the valid-time test. Content and tag filters run after
  that set is fixed, so a retired assertion never reappears because its
  replacement fails a filter.
- `memory_temporal_watermarks` holds the highest contiguously applied sequence.
  The publisher applies the projection inline. If that fails, the watermark stays
  put; readers reduce the journal tail themselves (up to
  `LOOM_MEMORY_TEMPORAL_MAX_TAIL`), and the `memory_temporal_projection` worker
  replays it without skipping a gap.
- `memory_temporal_vectors` holds historical-search vectors keyed by model and
  the sha256 of the exact embed text (memory.set's: a procedural value's
  situation, name and steps, not its JSON). The worker copies a vector from
  `memory_embeddings` when the version matches, else embeds. A provider rebuild
  empties the table (it holds no text to re-embed); as after a model change, the
  worker refills the last week's assertions and older ones rank lexically, with
  `vectors_pending` saying so.
- Projection writers also clean up after an erasure that overtook them: replay
  drops the rows of a commit deleted meanwhile, the vector fill drops vectors no
  assertion backs, the reconciler tombstones a row whose identity has no commits
  left, and the saga deletes the journal before the projections. These remove the
  bytes; the erasure cut is what keeps them unread if a cleanup fails. The one
  residue a process loss can leave readable is a current row inserted in the
  moment between its write and the re-fence after an erasure has completed.
- `loom.memory` is the current-state projection: the assertion that applies now,
  else the latest that has begun (a closed fact stays the key's row); a key with
  only plans has no new row until one applies, and one the journal serves nothing
  for is tombstoned. A rewrite is bound to its journal cutoff and never replaces a
  commit past it. The worker's reconciler rewrites
  a row that does not carry what the journal says applies now: a version row whose
  insert failed after its commit, a row an amend could not write, a scheduled
  transition whose time has come. It compares everything the current row serves
  (value, type, status, tags, entities, validity, pin, lock, sensitivity,
  attestation, expiry, confidence), one definition for its SQL selection and its
  check, and only once the assertion projection covers the commits checked. Durable cursors drive
  it a page per pass, so an outage only delays it: per namespace, the sequence its
  rows were checked up to (once settled past the publisher lease); globally, the
  valid-time boundaries checked. A namespace that cannot be checked yet (an erasure holds it, its projection lags) keeps its own place in the boundaries and catches up later, without holding the others back. The projection worker finds lagging namespaces
  the same way, from the published and applied watermarks. The rewrite is one
  version above the head it replaces, so a newer write still wins,
  and its stub embedding row is filled by the embedding worker. A namespace or
  user under an in-progress erasure, or a failed one still being retried, is left
  alone; an abandoned one no longer holds it.

## Reads

| Request              | Meaning                                          |
| -------------------- | ------------------------------------------------ |
| no temporal argument | the current state, as today (`loom.memory`)      |
| `valid_as_of=V`      | the latest recorded interpretation at V          |
| `known_as_of=K`      | what Loom had recorded at K about the world at K |
| both                 | what Loom had recorded at K about the world at V |

These arguments work on `memory.get`, `get_by_id` (resolved through the journal,
so a memory deleted since is still found at an earlier K), `memory.search`,
`memory.ask` and `search.query`, alongside `memory.intervals` and
`memory.temporal_diff`. A future K is a 422. Facts and records take the same
pair (`valid_as_of`, `known_as_of`).

Each answer carries its cutoff:

- `cutoff_seq`, `head_seq` and `history_from`;
- `complete`, and the named gaps (`projection_not_ready`, `integrity_break_at`,
  `read_token_not_ready`, `read_token_after_known_as_of` (the token's write was
  recorded after K, so a read at K can't include it), `allocation_in_doubt`
  (commits above the cut wait on an insert in doubt below them),
  `history_before_journal`
  (the namespace holds data older than its journal), `ranking_current`,
  `vectors_pending`, `truncated`).

A multi-namespace search returns one cutoff per namespace; sequences are not a
global snapshot. Current permissions, read scope, high-sensitivity fencing and erasure apply at every K:
a key whose latest assertion or current row is `high` (or that an unprojected commit
made `high`) is withheld from search and list at every K, its earlier revisions too.
Reads group assertions per identity, and a key's snapshots per qualifier group.
A historical list orders by recency and, like get and search, records the exact
revisions it returned.

Search honours the live eligibility filters (type, tags, user, session, session
set, excluded keys, minimum similarity, quality filter) and narrows to matching
keys before its row limit; `run_id`, `entity_filter` and a hard `category` are a
422 (the journal records none of them). A historical `memory.ask` answers from
that search alone: no widening, completion, knowledge, record fallback, run notes,
precedent or source-turn pass, all of which read today's rows. Its output carries
the cutoffs.

Search fixes the eligible set exactly, then ranks with an exact cosine. Ranking
signals are today's (`ranking_current`): the content is historical, the scoring
is not. Bi-temporal reads write exposure receipts with the exact assertion ids,
value hashes and cutoff, so a past decision is reconstructed from what was
shown, not from what was available.

## Other planes

| Plane         | History                                                                                                     |
| ------------- | ----------------------------------------------------------------------------------------------------------- |
| Shared memory | `shared_versions` (mirror); a `scope=shared` search at K reads the version current at K.                    |
| Records       | `memory.record.query known_as_of` matches each record's revision at K against its own fields.               |
| Graph         | `recorded_at` and `closed_at` on edges and triples; `facts.* known_as_of` re-opens closes recorded after K. |

`search.query` with temporal arguments answers the memory plane bi-temporally.
Every other plane says `not_historical`, rather than serving today's state.

## Erasure

The `memory_temporal.delete` and `history_mirrors.delete` saga steps delete every
journal, projection, vector and mirror row in scope, and the residue
census counts them. The journal goes before the current rows are tombstoned, so it
never serves what the current state has lost. The `published` sequence row
survives (it is a bare number): erased sequences are never allocated again. History is the point of these tables, so a tombstone would
leave content in every retained slice.

Retention follows `loom.memory`'s rule (`LOOM_RETENTION_MEMORY_DAYS` and a
namespace's `retention_days`), so a namespace's retention covers its history. An
assertion expires at its `expires_at`, else N days after it was recorded, as its
row would. A commit is kept until every assertion it added or retired could have
expired (`retain_until`), so the journal is always a superset of the projection
and a retraction outlives what it retracts. A
vector goes when no assertion uses it any more. A read at a K older than the
window answers from what is left.

Redaction (`memory.redact`) names a value by a historical version or by a
journal assertion (`assertion_id`, for text only the journal holds: an amend, a
correction's segment). It scrubs every copy of that value the memory holds:
each historical `loom.memory` version, each journal assertion found in the
commit payloads (projected yet or not), the projection rows, the vectors no
unredacted assertion uses, and the derived embedding, chunk and cache rows. A
value the journal still serves is refused, as is a locked key.

## Why this shape

- **Commits with a projection, not timeline copies:** copying a key's timeline on
  every edit grows write volume quadratically. A bounded correction copies only
  the segments it touches.
- **A serialized publisher, not per-key version clocks:** `time.time_ns()`
  orders current versions, but it is not a publication protocol. One sequence per
  namespace gives every reader a cutoff it can name.
- **The journal, not `memory_versions`, as the time authority:** a version row
  cannot atomically represent a multi-segment correction, and in-place mutations
  bypass its view. `memory_versions` stays the per-row history `memory.history`
  and `namespace.rollback` read. A content scrub is one function that updates both:
  `memory.redact` rewrites the redacted version's text in `memory_versions` and in
  every journal assertion born from it, and deletes their vectors.
