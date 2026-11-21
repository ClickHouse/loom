# Memory consent and derived text

A deletion contracts memory and records content refusal before its tombstone commits.
`memory.delete`, a successful `memory.delete_version`, natural-language forgetting and
`memory.clear` keep canonical JSON hashes and normalized literal fingerprints in
`memory_refusals`. Refusal records contain no deleted plaintext and have no TTL. Ordinary
revision, supersession, expiry and an internal move do not express permanent refusal.

A point deletion applies to the same user in its namespace, across agents and sessions.
A user erasure applies across the organization, including namespaces created later.
A namespace erasure applies to later intake by every user in that namespace. The policy
scope is separate from the original row's identity tuple, so a derived row keeps its
writer identity without narrowing the erased subject's refusal. Captured derived content
also records an indexed erasure refusal under that writer's namespace and user identity.

Canonical values are compared after JSON decoding. Literal comparisons normalize Unicode,
letter case and whitespace, and check passages within later strings. Wrapping a deleted
value in another JSON object does not bypass the check. These fingerprints compare
content; they do not recognize arbitrary semantic paraphrases. Verification work is
bounded and an incomplete check refuses the write.

ClickHouse writers share admission leases with deletion and erasure. A successful delete
records refusal under the exact head's write fence. Erasure blocks new intake while it
captures fingerprints and drains admitted sends before deleting content. Postgres stores
policy beside canonical memory. Every publisher takes an organization admission stripe;
erasure drains every stripe before capture. Native writers take the organization policy
lock before namespace, user, generation and head locks. Erasure takes that organization
lock exclusively, including when it spans several namespaces.
A failed policy read refuses intake. Raw evidence, deferred extraction, canonical writes,
embeddings and shared promotion use the same policy. A delayed projection rechecks its
source under admission before publishing.

Erasure captures ClickHouse revision history under either storage authority. A native
revision whose payload was cleared still supplies its typed identity for raw dependent
capture; ClickHouse history supplies its content before either copy is removed.

Canonical tombstones check erasure policy even though their content is no longer served.
Point deletion can retain its canonical history. Native authority commits check point
refusal before entering the journal. Delivery can publish those committed revisions after
point deletion, while rechecking erasure policy under the policy locks. Raw evidence,
chunks, extraction payloads and new vector publication still check point refusal.

A vector retirement keeps cache bytes
only while its authoritative source and policy permit them; point refusal produces an
empty marker. With no source or an erasure match, cleanup deletes older vector versions
across every partition, bounded by the caller's retirement version. It inserts no new row
and preserves equal or newer versions. Native publication proves source absence with a
durable read before borrowing a connection, then rechecks on that connection under policy
locks. Present heads remain locked until publication finishes.

Native delivery clears search-document debt only for acknowledged publications or a
source that no longer needs a document. A vector withheld after source selection stays
owed for another pass. Receipts match the complete identity and published version;
a newer metadata copy settles its original revision's delivery. Copy follow-ups confirm
the exact document in ClickHouse before settling debt.

Shared canonical retirement holds admission through its final send. It checks the
promoted source identities against erasure policy and requires the shared head to remain
present. Missing heads, erased content and unproven legacy sources are removed below the
retirement version without inserting plaintext or a marker. Definition promotion and
demotion acquire the namespace claim before borrowing a source connection.

Deferred extraction checks caller data in current and prior messages, names, instructions,
schema enums and pipeline arguments. Standard protocol fields do not become refusal
patterns. Enqueue, claim and every payload-carrying transition share this admission.
A transition also requires its exact source revision and payload hash to remain present.
A refused job ends through a control-only update bound to that same source; its transcript
is never copied into the terminal update. A missing source cannot be recreated.

An owed `memory.delete_version` check carries a hash of the exact checked state.
Recovery compares that hash before writing a missing tombstone. A numeric version
collision cannot turn a different fact into the target of permanent refusal.

Vector maintenance reports only acknowledged replacements. A legacy vector without a
source hash stays held when its complete source cannot be proved; `--delete-old` keeps
that organization's old vectors. Run `memory.reindex` with `force=true` to rebuild such
vectors from the complete authoritative values.

Permanent refusal also applies to `memory.restore` and snapshot restoration. Automatic
retirement can still be undone when the content has not been refused. Keeping the
historical row for inspection does not grant permission to publish it again.

## Session digests

A session digest contains complete committed source facts joined by newlines. Each fact
has a corresponding key in `derived_from`, under the same complete identity. A model does
not draft session digest text. Search and other digest consumers reconstruct the text
from the authoritative current heads and withhold any digest whose source is missing,
changed, retired, expired, sensitive, incomplete or awaiting review. A metadata touch
keeps an unchanged fact valid. A legacy digest without a complete proof is withheld.

Digest listing, outline and evidence export scan past withheld candidates to fill their
requested rows. `memory_digest_listing_page_size` bounds candidates per query and
`memory_digest_listing_max_pages` bounds queries per call; both default to 256. If that
budget cannot find enough verified rows or prove exhaustion, the operation returns
`BACKEND_UNAVAILABLE` rather than claiming the listing is complete.

Period planning selects the newest live digest per session before pagination. Source
verification applies to each digest row; a valid row cannot admit another model's stale
digest from the same session.

Period and contrast synthesis remain proposals. A model-generated restatement therefore
waits for independent review before ordinary fact retrieval serves it.

## Rollout

Stop and drain older APIs, workers and command-line writers before applying the additive
ClickHouse and Postgres migrations. Finish pending versioned deletion checks before
the drain. Without a checked-state hash, recovery closes a check that would need
a missing tombstone without deleting the fact; retry the deletion to check its current state.
Keep writes paused until every publisher enforces the
consent rules. If an older writer ran alongside a migrated store, keep traffic paused
until the affected deletion and erasure scopes have been verified again; a migration does
not certify writes admitted by the older build. Existing rows removed before policy recording was
introduced have no reconstructed consent record. Operators needing a historical guarantee
must establish the intended refusals from their deletion records before resuming intake.
Rolling back restores the earlier admission behavior. Audit records use their separate
retention policy and do not inherit namespace deletion windows.
