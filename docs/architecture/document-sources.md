# Document sources

Document sources read explicitly selected external resources through approved MCP
connections, or import recordings through the recording source path. This page
explains their namespace, protected-result, storage and erasure boundaries. The
[MCP document-source architecture](mcp-document-sources.md) owns remote approval,
connection binding and selected-resource execution. Follow the
[source setup guide](../guides/mcp-sources.md) and
[source CLI guide](../guides/mcp-source-cli.md) for enrollment and controls.
[Document extraction](document-extraction.md) describes parsing.

## Any namespace

A source destination is an active ordinary namespace or collection. A source
cannot target the automatic personal namespace: end-user erasure covers what that
person wrote, while source documents carry worker provenance. A private or team
collection remains subject to ordinary grants and inheritance. Registration does
not add destination grants or turn a private connection into a shared credential.

Namespace retirement and erasure fence source work before deleting content, so a
worker cannot write back what an erasure took. Inactive namespaces receive no new
source writes; explicit cleanup can still run. Source state belongs to its
namespace and registration identity, following the configured
[storage layout](storage-layout.md#cursors). A replacement registration cannot
resume another registration's authority from its name or cursor.

The Library's Sources entry opens source setup for the selected namespace or
collection; repository sources retain their repository entry.

## Items and links

An MCP registration saves an exact set of UTF-8 resource identities. Browsing shows
one bounded metadata window, with truncation and capability limits. Manual exact
IDs support servers that cannot enumerate. A resource identity is not normalized
into a provider URL, expanded into children or interpreted as a native folder,
page or project query. Approved read and search mappings supply service-specific
fields; tool descriptions and model arguments grant no authority.

Live mode reads on demand without importing. Snapshot reads the saved selection
once; selected refresh re-reads that selection through the source worker. None
claims complete enumeration, upstream ACL synchronization or deletion detection.
Partial pages, missing results and failed reads never authorize content deletion.

### Wire shape

MCP setup and lifecycle use `knowledge.connection.mcp_*` and
`knowledge.source.mcp_*`. Preview binds the exact selection, destination and reader
policy; save requires its current authority and any explicit collection-copy
consent. Updates replace the selected set under a monotonic scope revision.
Selection changes, including returning to an earlier selection, invalidate old
successful-read authority. A changed audience requires a separate registration.

The ordinary `knowledge.source.add` path accepts recording configuration. Native
document kinds, link parsing, native source updates and native OAuth enrollment
are outside the executable source contract. The old add input's removed fields
are reported through the transport's `ignored_args`; they do not configure an MCP
source. The source and OAuth cleanup is a breaking interface change.

### One document, one owner

Only a document's owning source may rewrite its copy. A source never overwrites
or retires content another writer owns. Mirror ownership uses the immutable
`source-id:<id>` tag, so a replacement registration with the same name owns none
of its predecessor's copies. Each mirrored source has its own address and access
proof under the existing document key; ordinary knowledge heads, revisions,
originals, chunks and indexing still apply.

An MCP citation identifies the registration and the digest of the exact selected
identity. It is a local source-copy address, never an outbound fetch target or an
assertion of an upstream URL. An unchanged owned document receives updated
metadata and successful-read proof without rewriting its content revision,
original or chunks. Retained bytes confer no current read authority.

Ordinary published namespace copies keep their existing ownership, namespace
access and writer-requested erasure rules. Retiring executable source support
does not silently delete them or convert them into protected copies.

## Credentials

An operator approves the remote HTTPS Streamable HTTP endpoint, authentication
profile, operations, mappings and bounds. A connection uses anonymous access or
a preissued bearer for that exact origin and audience. Loom credentials never
authenticate to the remote server. No provider OAuth enrollment, refresh fallback,
local command execution or native execution shim supplies failed MCP reads.

### Connected accounts

Each connection belongs to its canonical authenticated owner in one organization.
Its sealed binding retains the account slot, generation, endpoint, token audience
and approval digest. Source work checks current owner standing, destination
authority, revocation fences, approval and fleet readiness. Another reader's
namespace grant cannot borrow that connection. Remote identity is not verified
merely by a bearer, server title or returned account label.

Pause and disconnect stop local reads and retain content and account history.
They do not assert upstream credential revocation. The owner revokes a bearer at
its issuer when required. Retained native credentials are not reused or migrated
automatically; fresh MCP setup requires owner authorization and exact selection.

## Access follows the source

MCP stored copies use the protected mirror for both reader policies. Owner-only
access requires the current canonical owner and destination access. Collection
copy is an intentional local publication, requiring operator approval, namespace
administrator authority, explicit server-issued consent for that selection and
current destination-reader authority. Neither policy claims upstream ACL
equivalence. The audience is immutable for a registration.

The protected-result model has four parts: documents live in a companion
namespace no grant opens; each copy carries its current source access proof;
every checked leg validates that proof; and publication revalidates retained
contributions before delivery. Unsupported sources, missing proof and stale proof
admit nobody. Retained native permission rows cannot authorize MCP access.

Nothing derived from a mirrored document (its address, title, text, counts or
existence) enters a Loom record outside the mirror or an answer to a caller,
except through these boundaries:

1. A checked leg's answer to its reader, every field of which depends, of the mirror,
   only on the items that reader was admitted to, and otherwise only on the request and
   on whether the leg's reads succeeded.
2. The rows a marked request leaves, and every read of them (its own answer falls under
   exception 1, so a marked `memory.ask` still answers its `synth_*` fields to its
   caller):
   - Its spend (input, output and embedding tokens, and cost) depends only on what
     exception 1 lets the answer depend on: no model or embedding call runs, or is
     chosen, on a mirror candidate before the check. It counts in its namespace's totals
     and cap. Except to a verified platform admin without namespace-role restrictions,
     it shows only in the per-namespace-day total and in
     `cost.budget_status`'s live namespace or org total, and every other `cost.summary` dimension leaves
     it out. The fold carries ops, those tokens and cost only, and leaves marked
     requests out of `errors` and the latency figures. A viewer can still difference a
     live or daily total around another person's marked request and so learn that
     request's spend; that residual is accepted. Named-user caps also count marked
     spend. Their public status exposes the cap and whether the user or namespace
     ceiling is reached, but exact user spend and percentage are `null` except to that
     operator. User warnings expose the selected user, cap, warning share and threshold
     crossing, with exact spend withheld from every recipient, including an operator's
     configured inbox or webhook. Namespace warnings retain the allowed namespace
     total. These policy signals expose bounds; an admin changing caps can infer tighter
     bounds. That residual is accepted, not a claim of full noninterference or a grant
     to marked work, models or outcome fields.
   - Its work figures (statements, rows and bytes read, chunks injected, `llm_calls`,
     `response_tokens`, the stored duration), its `model` and the outcome fields a model
     call can change (`success`, `error_code`, `status_class`) go to a platform admin
     only, and the error-rate and model metrics leave marked requests out.
     The stored argument map keeps only numeric statement/read/injection work fields.
     Result identifiers, document tags, argument digests and error prose are omitted.
3. Owner-visible selected-source controls may report their authorized selection
   and read failures; they do not expose another owner's registration or content.
4. Privileged platform diagnostics and erasure recovery retain their existing
   codes-and-counts boundary. Ordinary receipts cannot reveal mirror contents.

The rule covers Loom's records, application logs, spans, metrics and caller-visible
answers. Database-native query, error, mutation, text, profile and trace logs remain
privileged deployment infrastructure, under the same trust as the plaintext tables
and backups. They can retain mirrored addresses, counts and values quoted in errors
after erasure; the database operator controls their access and retention, and any
export of them keeps that privileged boundary. This is the existing
[database-log boundary](assistant.md#stored-data-and-erasure) and
[erasure limit](../features/memory/retention-and-erasure.md#checking-an-erasure-yourself).
The SQL surface and analyst must refuse access to these logs. Loom's statement-capture
files, provider and extractor telemetry, and caller-readable exports remain subject to
the mirror rule.

A deployment with auth off (`LOOM_AUTH_REQUIRED=false`) has no reader identity to check.
It refuses to add a mirrored source with `AUTH_REQUIRED`, which wins over
`MIRRORED_SOURCES_OFF`, the answer of every deployment until
`LOOM_KNOWLEDGE_MIRRORED_SOURCES_ENABLED` is set. That setting turned off again once
mirrors exist makes the mirror legs answer nothing and stops the sync apart from the
cleanup of a stopped source. Auth turned off once mirrors exist does the same, and the
gate still refuses every mirror name; the cleanup only deletes.

### The mirror

A mirrored document is not stored in the source's namespace but in that namespace's
mirror: a namespace named `acl-<24 random hex>`, made under the namespace's source lease
with its first mirrored source and recorded in `loom.knowledge_mirrors` (org, namespace,
mirror). The gate admits no token to a name of that form (`acl-[0-9a-f]{24}`):
`check_authorized` and `CachedGate` refuse the name before any other rule, the auth-off
return included, and `namespace.register` and `namespace.restore` refuse the form.
`erasure.request` refuses an `acl-` name in its scope for every caller but a platform
admin with auth on, and `erasure.export` for every caller; with auth off both refuse it
for everyone. So no token searches, reads or exports a mirror, and the threat model's
rows hold for every token. A mirror has no directory row and appears in no namespace
listing or usage view; a deleted name keeps its mirror and counts as in use
(`namespace_census.in_use`, with the same code as any other reason, whatever the mirror
holds) until a namespace erasure, or the mirror-erase command, writes its
`loom.knowledge_mirrors` row `deleted`. A namespace erasure does that last, after the
mirror's rows (the row holds names only), so an erased name can be registered again and
gets a new mirror. A name that has ever had a mirror is always erased as a purge, a new
owner's erasure included, since the registry keeps every mirror it had. The migration
that enables the refusal first looks for namespaces of the reserved form, registered or
holding rows, and refuses to run, naming them, if it finds any.

A checked knowledge read adds a leg over the destination's mirror only through
current source discovery and authority checks. Retained rows alone do not make a
source eligible. Every other read path reads the namespace and finds nothing of
the mirror. So a
new op, a context-pack leg or an export that nobody taught the check shows no mirrored
document; a build without supported source authority cannot admit its protected
copies. A permission column on `loom.documents` read by every path would make the
opposite the default; [sharing](sharing.md#alternatives-not-taken) rejected
that for collections, since one missed path leaks.

Search discovers chunk keys and scores without returning chunk text. Each leg checks
candidates in batches of at most 64 until its admitted pool fills or its stream ends;
denied candidates consume no pool position or fusion rank. Content reads bind the
exact admitted namespace, address, chunk index, version and revision under the requested
embedding model. A missing key is omitted without substituting a later version.
Duplicate or unexpected rows discard all mirrored legs. Permissions are checked again
after content is fetched. The successful-read proof, source scope and connection
authority must still be current before content leaves the checked leg. A retained native ACL or
group row cannot substitute for supported source authority.

A marked private-first search gathers both possible passes from its authorized
namespace list and visibility settings before choosing which to return. It refreshes
all retained mirror permissions and reranks before applying the first-pass similarity
threshold. A provisional private hit that loses permission therefore cannot suppress
the public fallback. No reranking provider receives the provisional pools.

- **Legs with the check:** `knowledge.search`, `get`, `outline`, `list`, `download`,
  `synthesize`, `knowledge.inspect` (titles), source-specific protected reads, the
  `search.query` knowledge and repository-docs legs,
  `code.context`'s docs section, `context.pack`, `memory.stack`, `memory.ask` and the
  assistant (which calls the same handlers under its answer token, delegated from the
  caller's). A `get`, `outline` or `download` of a mirrored address the reader may not
  see answers `found=false`, as for an absent one.
- **Nothing to read:** decision mining and dataset extraction skip mirrored documents,
  since a decision row is a memory every reader of the namespace reads and a dataset is
  read by `knowledge.query` without the check.
- **A request that touches a mirror is marked.** Nothing a marked request writes outside
  the mirror depends on what the mirror holds, except as exception 2 allows: no cache
  shared across readers (the repository-docs listing cache, `memory.ask`'s precedents,
  the provider response and embedding caches) is read or written with text a mirror leg
  gave, and no field or row of its `agent_events`, trace, exposure, metric or span
  records, of recall credit (it writes none to `loom.memory_usage_events`), nor a
  generation's captured text, depends on the mirror. Every request that runs a mirror
  leg is marked, whether or not anything was admitted, since marking on admission would
  tell existence. The costs are accepted:
  - `knowledge.reads` names no address a marked request read, a namespace document's
    included;
  - every marked knowledge read drops out of `cost.summary` by `user_id`, `operation`
    and `agent`;
  - a marked request writes no recall credit or exposure row, so the usage signals
    (`recall_count`, `last_recalled_at`, the usage totals) leave out every marked read;
  - the assistant's support check reads a marked call as unknown, so a memory it cites
    from one gets the reason that the check could not run (`unchecked`), not
    `unsupported`;
  - the assistant's step list cannot say a marked tool call failed;
  - every session whose request runs a mirror leg is flagged, and a prompt-recall
    `context.pack` runs one, so while a namespace lists an active mirrored source every
    Loom hook of a session over it stops after its first recall, reads included, and a
    dashboard view of a flagged answer sends no `ui.vitals`
    ([the session rule](#the-session-rule)).

  The assistant front end's token record of a marked answer is a spend row under
  exception 2 that only the deployment's operator reads; the conversation itself lives
  with the caller (the dashboard's browser storage, or the message list an SDK or HTTP
  caller sends).

- **The flag.** The answer of every marked request carries `mirrored: true` as its first
  field or line, whether or not anything was admitted, for the same reason as the mark,
  and each admitted item carries it again, so a truncated copy still carries it. Every
  assistant answer on a namespace with an active mirrored source (the `assistant.ask` op
  and the chat route, `POST /v1/assistant/chat/completions`, which carries the flag in
  its stream's first event or its response's first field) is marked and flagged, since
  its answer is model text written from tool results and need not carry the field the
  tools answered. A mirror leg answers nothing to a client that does not declare it
  honours the flag. A participating client declares it only with its durable
  observer and automatic-capture guards installed. A header alone does not establish readiness.
  The assistant delegates its caller's protection promise; it cannot manufacture
  one. A caller without that promise receives no mirrored text through it.
- **The session rule** keeps a session that saw a mirrored answer from sending it on
  ([the session rule](#the-session-rule)).
- **The sync and the workers write nothing outside the mirror.** The mirrored sync's
  spend charges no namespace's cap, and
  `LOOM_KNOWLEDGE_SOURCE_MIRROR_DOCUMENTS_PER_PASS` bounds it. A mirrored source's sync
  state, the sync's audit rows, error text and per-file wait state are filed under the
  mirror's namespace and go with it when it is erased; whatever of a mirror leaves it (a
  source's status, a worker's pass notes, logs, spans and metrics, the extractor
  service's logs and traces) carries codes only (exceptions 3 and 4 apart).
  Worker pass summaries exclude mirrored turns' completed-source counts, document
  figures and cut flags. A failed mirrored turn contributes only `SYNC_FAILED`, for
  every cause; returned turn details remain private. Mirrored per-file failures emit
  no log record, since the number of records would disclose a document figure.
  A sync turn enters its private diagnostic scope before taking its source lease or
  listing files; retirement enters it before reading its queue. That scope suppresses
  client and provider instrumentation, database
  work figures, shared metric calls and log records, including their volume. Provider
  health and embedding caches receive no private contribution. The extractor receives
  a generic filename; its service emits no per-request access, stderr or fault logs.
  A pass that touched private work records codes without duration, outcome figures or
  failure counters, including when invoked from the CLI. Its retry history cannot
  advance a later ordinary pass's diagnostic streak. Ordinary work keeps its own cost
  accumulator; private spend is written directly under the mirror, without entering
  the shared event queue or its statistics.
- **No scope reaches a mirror.** Every statement a request runs that reads a table
  holding mirror rows other than by a namespace the caller names leaves out the
  `acl-[0-9a-f]{24}` form; the workers named here are the exception, since they must
  reach the mirror's rows. `resolve_org_scope` (its auth-off filter included),
  `every_namespace_scope` and `rollup_member_namespaces` (`server/auth/context.py`)
  leave it out for a request's caller; the sync, the erasure saga, the mirror-erase
  command (which finds mirrors by the `acl-` form in every store, not only by their
  registry rows, so a restore that loses a `loom.knowledge_mirrors` row orphans nothing)
  and the maintenance workers (reindex and embedding-model rebuilds, tier and retention
  moves, the blob and originals sweeps, which keep a mirror's rows referenced) reach a
  mirror through the stores, never the gate, under the rule of the bullet "the sync and
  the workers write nothing outside the mirror". Every `sql_v1_` view (each replaced by
  a migration), the analytics export's statements and the org-wide reads of
  `erasure.export` (`Shown.clause` carries the form) and of a `user_id` erasure's steps
  and census leave it out too; a saga whose scope holds only `acl-` names is not found
  for anyone but a platform admin; every scope a saga answers to anyone else, and every
  audit row a saga writes, leaves `acl-` names out (a mirror-only saga's audit rows show
  only that a platform admin erased something); so `audit.list`, the insights, usage and
  cost reads, the SQL surface and a person's export name no mirror. `check_authorized`
  refuses the form before its auth-off return, so no id-addressed check (`row_reach`) or
  `audit.list` of a named namespace opens one. The deployment-wide admin reads
  (`/v1/admin/`, outside the gate) leave a mirror's ledger rows out whole: until the
  fold below carries a marked request's spend, a mirror's spend is in no fleet or
  consumer total.
- **Private events have separate physical storage.** Ordinary events stay in
  `agent_events`; marked requests and mirror workers write `agent_events_private`.
  The ordinary table's CHECK excludes marked rows and mirror namespaces. An analyst
  keeps ordinary signed, namespace-scoped reads and cannot read the private table or
  its physical metadata. Provisioning verifies the CHECK on every writer, drains
  the finite set of prepared INSERTs and refuses any existing private contamination
  before granting access. Checked per-writer HTTP sessions read the native access
  catalog and prove that policies cannot hide the process or event populations.
  Unknown visibility, writer routes or metadata refuse. It preserves existing rows
  for operator resolution. Administrative application uses
  `loom ch-setup --apply-analyst-sql`; unchecked analyst SQL files refuse.
- **Private event reads are folded and masked.** The analytics export and
  `sql_v1_usage_daily` union both stores and fold marked spend into a namespace/day row
  with agent and operation `-`. `sql_v1_agent_events` and the signed
  `analysis_agent_events` view mask every field that can depend on an admitted item
  before predicates run: model, tokens, cost, work, `llm_calls`, success, error code
  and status class. Trusted budget and history-policy reads cover both streams too.
  User erasure scrubs both stores under `agent_events.scrub`; the private store has
  the ordinary ledger's retention and tiering policy. The
  [storage cutover](rolling-upgrades.md#analyst-event-storage-cutover) preserves
  ordinary analysis across builds. The [assistant boundary](assistant.md) describes
  the view and key-hiding requirements. Exception 2 accepts this fold beside
  `cost.summary`'s.

History predicates cannot use a marked call's outcome or projected arguments. A
potentially matching marked event in a rule's identity, operation and time scope makes
that leaf unknown before success or argument filters run. This includes an unresolved
argument reference and a negated condition. Breadth rules check marked participation
under the same principal pin because marked reads write no exposure row. The compiler
reads coarse participation and the ordinary aggregate in one statement. Protected
unknown carries one fixed reason and no count or timestamp; every affected enforcing
operation refuses, reads included. Shadow rules retain their non-enforcing behavior.
`policy.check` uses the same protected-history decision. Ordinary trace-backend errors
keep their existing read-availability policy.

A checked answer retains its access proofs through required policy witnesses and
other work that must finish before publication. All provider work for its complete
ordinary alternative finishes before cost and event snapshots. After required tail
work, publication renews the exact private dependencies of the already selected frame
and checks every retained deadline after the last renewal await. This boundary does
not render, rerank, call a provider or choose an ordinary alternative. A lost proof
refuses the whole frame; removing a citation from private prose is insufficient.
After awaiting the response headers, publication renews access again before sending
the body. A checked-proof or publication failure after headers aborts with a constant
failure and sends no unchecked body. Discarded candidate widths and optional sections
retain no publication guard; returned metadata, counts and cursor lookahead retain
their contributing proofs. A raw download owns the same required marked accounting
attempt as a registered operation. Cancellation and disconnects still propagate
through owned cleanup. These checks end at the server's response-send boundary.

The assistant's incremental stream has a terminal accounting boundary because final
usage is unavailable before its early text. Its server-owned first event is
`data: {"mirrored":true}` when marked. Partial text remains provisional until the
response closes and joins its producers, captures final usage and confirms its
required private cost row before `[DONE]` or a terminal error. Disconnect cleanup
retains and joins that required delivery. Whole answers and checked source frames
keep the accounting-before-publication boundary above. The answer token's distinct
`answer_mirrors` bit fixes mirror admission before model work, so a source activated
during a negative answer cannot introduce an unmarked tool leg. Missing admission is
false; ordinary delegation drops it. See the
[assistant boundary](assistant.md#the-bridge-each-callers-own-identity).

A multi-entry MCP batch refuses every response-bearing marked entry with the same
instruction to send it individually, including denied and empty entries. Notifications
remain silent. It discards each marked entry's complete frame before buffering it or
awaiting another entry. Single calls and one-entry arrays
retain checked reads. Each entry gets fresh local state before routing or screening;
the enclosing HTTP request keeps a monotonic mark through later ordinary entries,
streaming and cleanup. Middleware task boundaries do not clear that owner.

MCP pause, disconnect and unsupported source kinds make retained copies
unavailable without deleting their bytes. Source removal queues document retirement
only when explicitly requested by `delete_documents`; namespace or owner erasure
uses its own explicit erasure authority. Removal without copy erasure does not
preserve permission to read an unsupported or stopped protected source.

Explicit source retirement uses a durable source-id queue under the mirror.
Cleanup retains immutable ownership independently of a later registration with the
same name. The retirement worker runs with its own deadline, cursor and
`LOOM_KNOWLEDGE_SOURCE_RETIRE_DOCUMENTS_PER_PASS` bound, including when ordinary
syncing is disabled. It deletes no still-active matching registration. Completion
requires no live owned documents and no pending publication under the source's
turn lease. Erasure removes retirement state with the mirror's other state.
Cleanup exports no private document counts or cut flags.

A retained ordinary namespace copy and a protected mirror copy keep independent
ownership and access. Checked retrieval deduplicates only after access checks.
When an admitted ordinary copy wins, it supplies its own chunks and scores;
a mirror-only hit retains its protected classification.

### The session rule

A session-content op records a session's text, or a figure derived from it, that a hook
or client sends automatically. Explicit reader writes retain their ordinary
authority checks; participating clients also guard automatic writes derived from
a protected contribution. Each op declares
its class in the op registry (`server/operations/registry.py`), and an op both a hook
and an agent call is session content. They include `conversation.append` (which the
CLI's `ingest-transcript` calls), `conversation.save_message`, the `title` and
`metadata` of `conversation.create` and `conversation.update`,
`memory.set_from_messages`, `session.brief_save`, `observe.record`,
`observe.crystallize`, `working_memory.set`, `working_memory.append`, `run.begin`,
`run.complete`, `code.outcome`, `cost.record_turns` and `ui.vitals`, and every read a
hook sends with a session's prompt, files or transcript (`context.pack`, `code.search`,
`code.impact`, `memory.for_file`, `namespace.validate`). Framework callbacks also send
the latest user message through `memory.search` and persist conversation items through
`memory.set`, so both are session content. Its coalesced delivery through
`memory.bulk_set` has the same class. Automatic tool interception also sends
`code.test_gate`, `code.symbol`, `code.enclosing` and `tracker.duplicate_check` with
session-derived inputs. The rule decides; the
registry's classes and a test over what the shipped hooks send pin the full set.

Once a session has seen the flag, no Loom hook sends any op from it, reads included, and
no client sends a session-content op on its own. Explicit reader requests remain
subject to their ordinary authority checks, including dual-use reads such as
`code.search` and `context.pack`. Participating applications guard later automatic
writes through every retained contributing family, across clients and namespaces;
missing or lost durable state refuses rather than starting a clean session.

A session is also flagged when a subagent or a child process it started saw the flag: the child records it in the session
state it shares with the parent, which the parent's hooks read, since the child's final
text, reworded, need not carry `mirrored: true`. A session resumed, forked or compacted
from a flagged one is flagged, and so is one started from a flagged session's process
(`claude -p` or `codex exec` run through its shell): the CLI and the hooks set an
inherited marker in the environment and read it, and keep the flag in the session's own
state beside what they keep for resume and fork. A worker an orchestrator starts outside
that process tree (through `tmux`, `ssh` or a job queue) inherits nothing; what its own
session records is the orchestrating agent's choice, out of scope ([threat
model](#threat-model)). The CLI's `ingest-transcript` refuses a transcript that carries
the flag, or whose session's state records it.

The refusal: from the first time `LOOM_KNOWLEDGE_MIRRORED_SOURCES_ENABLED` is set until
it is off again and the mirror-erase command has run, every session-content op refuses a
client that does not declare the flag, in every org, whatever mix of clients and tokens
a session runs; the declaration is the client's promise, carried by the transport and
never by an op argument a model fills in. The exact transport declaration is one
`X-Loom-Session-Protection: v1` header; missing, repeated or unknown values declare
nothing. An assistant answer token carries its caller's declaration in the Boolean
`session_protection` claim the bridge fixes at delegation. An answer or its descendant
cannot replace that claim with its own header, and ordinary delegation does not inherit
the claim. A client that does not make
it (a third-party MCP client connected straight to Loom) is refused like any other. The
condition is the one row of `loom.knowledge_mirror_latch` or the process's own switch: a
process refuses while its switch is on, row or no row, and writes the row whenever it
reads the switch on and finds it absent, so neither a lost write nor a restore of a copy
without it lifts the refusal. Only the mirror-erase command deletes the row, last, once
no namespace of the `acl-` form holds rows in any store, and it refuses while any live
process reports the switch on. The check fails closed: a process that cannot read the
row refuses, and the first mirrored `add` waits until every live process reports the
switch on, as the schema check does. Since the refusal lifts only then, it says nothing
of when another org's mirrors went.

A process with the switch on confirms durable protection before startup can admit
clients or start workers. Its existing instance heartbeat reasserts that state, so a
lost row is repaired without waiting for a session-content call. Startup refuses an
unconfirmed write. The common gate runs after token verification. HTTP checks before
reading the operation body; MCP checks after resolving the operation and before
content validation, identity defaults, history, replay or implicit session runs. Its fixed `SESSION_PROTECTION_REQUIRED` refusal is HTTP 409 and is not
retryable without changing the client or completing the documented cleanup.

The latch is one fixed `id = 0` KeeperMap row on the default Keeper ensemble, with
`set_at` and no TTL, tenant identity or content. It is deployment protection, so tenant
erasure never removes it. A fresh negative check uses a checked HTTP session under the
service principal. It validates the local latch and lease engines, primes the latch,
and brackets an acknowledged nonempty strict write to a unique expiring lease key and
the latch read with the same live Keeper client identity. Every proof SELECT returns
its execution's server UUID, including an empty latch, and the barrier INSERT computes
the writer's UUID into its own lease row. The final exact holder and write UUID must
match. This matters because a retried initial HTTP session creation can leave the same
session name on two backends; the name alone does not pin execution. Query caches are
disabled within this protocol. Any missing metadata, changed identity, uncertain write,
expired nonce or cleanup failure refuses the negative decision. Only its exact own
lease is disposable; a cleanup fault leaves an expiring row and never admits a request.
A positive repair computes its fixed row key in the INSERT from a check of the writing
server's UUID against the validated backend. A differently routed write refuses; an
acknowledged strict insert or duplicate is followed by a fresh validated positive read.
No negative result is cached. The named-session transport alone is not a Keeper proof.

Mirrored sources are off for a deployment until an operator turns them on, after every
Loom client and hook that sends session content honours the flag. Each pull request that
adds a mirror leg, writer, client or hook lists what it writes and tests each against
these rules.

### The permission list

A mirror document's `access_json` is private access metadata, carried in the
ordinary document row and, under the Postgres authority, its head. For MCP
copies it binds the owning source, scope digest and server-observed successful
read time. The existing freshness bound applies to each document, including a
snapshot. A snapshot without explicit refresh eventually becomes unavailable.
No read returns the private access column as ordinary document content.

A denied, oversized or unavailable selected resource loses its own read proof;
its bytes remain. An observed approved-mapping schema failure stops the exact
current connection under its lease, invalidating all source reads on that
binding. Restoring a schema does not reactivate that connection. A fresh
owner-approved binding is required. Missing upstream ACL information never adds
readers or changes owner-only content into a collection copy.

### Who may read

Every checked leg requires a supported active source, current connection and
scope, fresh per-document proof and the selected reader policy. Destination
access alone is insufficient. An unbound machine reader cannot borrow an owner's
identity. Retained protected copies from unsupported sources remain unavailable;
old owner, email, group, domain or public entries do not repair missing current
authority.

Checks apply to search, reads, originals, history, exports, diagnostics, caches
and derived publication. Source replacement is never silently substituted into a
retained read plan. A denied id-addressed read answers as for a missing document.
A missing registry, access proof or required head admits nobody. Under the
Postgres authority a narrowing or retirement already committed in the head
applies before asynchronous publication.

Search checks permissions before filling the admitted pool or assigning fusion
rank. Denied candidates consume no rank, and admitted content is bound to its
exact address, chunk, version, revision and embedding model. Mirror chunks do not
contribute private corpus statistics to ordinary lexical ranking. A missing exact
content key cannot be replaced with a later version. Final publication retains
and revalidates only the proofs contributing to the selected response.

### Threat model

| Threat | Boundary |
| --- | --- |
| A shared destination exposes an owner's private selection | Both policies use protected storage; collection copy requires explicit publication authority and consent. |
| A caller borrows another owner's account | Connection binding and source controls require the current canonical owner. |
| A changed selection borrows old proof | A monotonic scope revision binds each successful read. |
| A stopped or unsupported source exposes retained bytes | Every read needs actual current supported authority. |
| A partial listing removes documents | Only explicit writer-authorized retirement or erasure deletes content. |
| A private result leaks through capture, caches or telemetry | Marked handling, durable session protection and the publication boundary cover the complete request. |
| A read path omits access checks | The mirror is inaccessible to ordinary token scope; a path without a mirror leg reads none of it. |

These boundaries end at response delivery. Readers and remote hosts control their
own retained copies, requests and conversations. Response timing, resource-limit
side channels and inference from the allowed aggregate spend totals are outside
the noninterference claim. An application must preserve every contributing
session authority across callbacks, tasks, resume and compaction; a separately
started process does not acquire that authority merely by receiving text.

## Meetings through the shared-folder store

Meeting notes and exported transcripts can be selected as documents only when an
approved MCP server exposes a supported text read. A folder name, transcript
shape or attendee paragraph grants no access, and does not expand the selection.
The document extraction and chunking paths retain supported transcript structure;
missing timing intervals are not invented.

The recording source path is separate. It accepts Zoom (`provider: "zoom"`)
and explicit recording IDs; MCP setup does not replace recording
credentials or make a document server a recording provider. Retained unsupported
recording configurations keep their bytes and history without native fallback.
Explicit removal deletes documents only when requested.

## Project-tracker issues

Feeding a project tracker's issues and comments into the tracker plane is a
separate design concern. MCP document import does not change its live record contract or create tracker tables. A tracker ingestion design must account
for these boundaries:

- The tracker plane reads a team's records live and copies none ([tracker
  plane](../features/tracker/overview.md)); issues with no team database would be kept
  by Loom, in its own tables, under the erasure census.
- The plane's statements filter by `repos` (and, in a standard namespace, by
  `is_private = 0`), never by org or namespace, so a Loom-kept table needs an (org,
  tracker namespace) predicate on every read.
- A source serves only with `github_issues`, `github_prs` and `edges` ([record
  contract](../features/tracker/record-contract.md)); the contract's `source_id` names a
  repository; and tracker namespaces belong to `LOOM_TRACKER_ORG` alone.
- An issue of a private team, or read through a person's connected account, is private
  (`is_private = 1`). The contract derives an item's id from `source_id`, which changes
  when an issue moves to another team; stable tracker ingestion would need the
  issue's own identity so a moved issue keeps its item.

## Surfaces

The [setup guide](../guides/mcp-sources.md) describes dashboard selection, access
preview and lifecycle controls. The [CLI guide](../guides/mcp-source-cli.md) owns
exact command and operation mappings, secret-file inputs, trusted session pointers
and request-UUID reconciliation. SDKs use the same authenticated operations and
must implement durable protected-response observation and automatic-write guards.
Protocol compatibility and generated operation methods alone do not qualify a
client application.

Live lookup reuses the common [live-source search](live-source-search.md) path.
It returns selected content without automatic import or memory save.
[Source push](source-push.md) describes retained registry data and local
maintenance; there is no native callback or renewal runtime.

## Data model

MCP connections reuse connected-account storage, canonical-owner lifecycle,
leases and generation fences. Source registrations reuse `knowledge_sources`;
versioned configuration and selected scope remain opaque to builds without
support. Documents, chunks, originals, revisions and accounting use the ordinary
knowledge machinery and configured storage authority.

The account registry is authentication control data, keyed by canonical owner
rather than a requesting session or destination namespace. Source rows retain
the identity tuple and worker provenance. The authenticated actor supplies audit
attribution; `created_by` is not credential authority. Owner erasure covers sealed
identity and credential history explicitly, rather than guessing a sign-in
subject from `user_id` or an email. Required lifecycle bindings refuse missing
composition instead of skipping cleanup.

Mirror mappings live in `knowledge_mirrors`. Private access metadata lives with
the document and its authority head. Retained listing, group, push, account and
source history rows remain subject to their existing storage and erasure
classification; retaining a row does not activate native execution or establish
MCP permissions. The deployment's protection latch contains no document content
and is separate from tenant erasure.

## Erasure and audit

- Every table that can hold memory content, document identities or private
  account material has an explicit erasure classification. MCP uses existing
  knowledge, original, revision, index and accounting cleanup paths.
- Namespace retirement fences source writes before erasure. Namespace erasure
  covers its mirrors as a purge. The mirror registry is marked deleted only after
  content cleanup, so a replay or restore still reaches retained mirror names.
  A later namespace registration gets a separate mirror.
- Each namespace step persists its monotonic `mirror_marked` receipt before
  counting or deleting mirror scope. Failure to mark counts nothing. Registry
  loss, resume and restore cannot downgrade that receipt. Restore promotion
  preserves its checkpoint and replay fences. Marked checkpoints use the high bit
  of their UInt64 version to supersede stale unmarked writers; timestamps order
  marked writes.
- Marked erasure replies expose codes only unless the current caller has the
  existing flat platform-admin diagnostic authority. Ordinary readers receive no
  mirror count, residue, census or verification timestamp. Placeholder fields
  never prove verified absence. Marked audit rows omit counts, and failed marked
  receipts withhold stage details and private failure prose.
- End-user `user_id` erasure and canonical-owner cleanup are distinct axes.
  Owner cleanup uses exact account identity and generation fences. Erasure must
  verify historical identity and sealed credential residue; account cleanup
  alone does not certify that every source copy or original is gone.
- No token reads or exports a mirror directly. `erasure.request` accepts a mirror
  name only from a platform admin with authentication on; `erasure.export`
  refuses it for every caller.
- Audit preserves authenticated actors and lifecycle decisions without credentials,
  selected private identities or content. Source pause, disconnect, schema refusal
  and upgrade are not erasure requests. Explicit remove with `delete_documents`
  requests copy retirement; without it retained copies remain subject to current
  access and ordinary retention policy.

## Rolling upgrades

Lifecycle preparation adds version and slot fields without converting existing
registrations. The MCP admission migration establishes a fleet code floor without
changing stored data or schema objects. Admission and current connection checks
require the whole live fleet to advertise that floor; unreadable fleet state
refuses. Neither migration activates connections, approves profiles, transfers
consent or deletes upgrade data. See the
[MCP cutover contract](mcp-document-sources.md#rolling-upgrades-and-operator-cutover)
and [rolling upgrades](rolling-upgrades.md).

Native document-source registrations remain retained and unavailable in this
build. Credentials, configuration, documents, originals, history, uploads and
checkpoints are not converted, reused or removed as upgrade cleanup. Existing
ordinary published copies keep namespace access and erasure rules. Retained
protected mirrors require actual current supported authority and stay unavailable
when their source is unsupported. Rollback does not translate MCP registrations
into native sources.

Restore still requires the documented account-authority and mirror-content
reconciliation before serving. Explicit mirror cleanup uses
`loom knowledge mirror erase --all` under the stopped-fleet procedure; it refuses
while a live process reports mirrors enabled. It covers orphan reserved mirror
namespaces across the stores, then removes the deployment latch last. A restored access row alone is not trusted current
authority. The explicit mirror cleanup procedure and deployment protection remain
separate from routine upgrades; migration is never a request to erase mirrors.
Follow [backup and recovery](../operations/backup-and-recovery.md) and the
[upgrade procedure](../operations/upgrades.md) for the supported restore boundary.

## Breaks

Native document-source setup, execution, source update/link parsing, provider
OAuth routes and native settings are removed. There is no provider fallback or
legacy execution shim. Owners need an approved MCP endpoint, fresh account
binding, selection and access preview. Recording, model providers, tracker and
code integrations keep their separate contracts.

Protected reads require actual participating clients. Session-content operations
continue to refuse clients without the durable protection protocol while the
[deployment refusal](#the-session-rule) applies. A sent header or generated SDK
method is insufficient evidence of application readiness.

## Settings

Use the generated [configuration reference](../reference/configuration.md) for
available settings and defaults. MCP requires authentication and
`LOOM_KNOWLEDGE_MIRRORED_SOURCES_ENABLED`; enrollment also requires
`LOOM_KNOWLEDGE_SOURCE_SELF_SERVICE_ENABLED`. Stored-copy execution requires
`LOOM_KNOWLEDGE_SOURCE_SYNC_ENABLED`. The existing
`LOOM_KNOWLEDGE_SOURCE_ACL_STALE_SECONDS` bounds successful-read freshness; its
name does not imply MCP ACL synchronization. Approved profiles and bounded
scheduler configuration are described in the
[MCP architecture](mcp-document-sources.md).

## Rejected

- Putting protected documents in the ordinary namespace would make one missed
  read check disclose them. A separate mirror makes absence the default.
- Deriving mirror names from namespace names would turn predictable names into
  probes. Mirrors use random reserved names.
- Inferring permissions from server descriptions, tool hints or missing ACLs
  would grant authority without evidence. Approval, selection and reader policy
  are explicit.
- Treating an incomplete catalog as deletion evidence would remove content the
  writer never asked to delete.
- Reusing native credentials or translating registrations would transfer account
  consent without an owner-approved MCP binding.

## Invariants

- Only checked legs read protected copies; no token scope opens a mirror.
- Every returned protected copy has current source, connection, scope and
  per-document authority, rechecked at final publication.
- A shared destination does not publish private content without explicit copy
  authority and consent.
- A source rewrites only its own copies. Replacement names confer no ownership.
- Nothing derived from protected content leaves its boundary except through the
  checked answer and documented accounting/diagnostic exceptions.
- Live retrieval imports nothing and saves no memory automatically.
- Unsupported sources and stale proof refuse access while retaining bytes.
- Only writer-authorized retirement or erasure deletes content; migrations and
  incomplete remote results authorize neither.

## Source entry points

- `server/knowledge/app/mcp_connections.py` and `mcp_source_sessions.py`: current
  connection binding and observed schema refusal.
- `server/knowledge/app/mcp_sources.py` and `mcp_source_controls.py`: preview,
  selected scope, lifecycle controls and reconciliation.
- `server/clients/mcp_source.py`: bounded official SDK protocol calls.
- `server/knowledge/app/source_sync_mcp.py`, `mcp_source_worker.py` and
  `source_sync.py`: selected reads, scheduling and common knowledge writes.
- `server/knowledge/app/source_access.py`: current copy authority and mirror legs.
- `server/knowledge/app/sources.py`: recording and retained-source lifecycle.

## Tests

Generic protected-result checks cover mirror isolation, exact content versions,
current authority, private diagnostics, session inheritance, cancellation,
accounting-before-publication and erasure receipt masking. MCP qualification must
also exercise selected-resource reads, scope changes, stale proof, schema refusal,
immutable audiences and unavailable retained sources under each supported storage
authority. Authored cases alone establish no qualification result.

The [qualification boundaries](mcp-document-sources.md#qualification-boundaries)
distinguish synthetic resource and mapped-tool tests from external server,
transport and client qualification. Exact-head hosted receipts are required for
actual results; no complete-listing, upstream-ACL or deletion guarantee follows
from protocol tests.

## Related

- [MCP document sources](mcp-document-sources.md)
- [Source setup](../guides/mcp-sources.md)
- [Source CLI](../guides/mcp-source-cli.md)
- [Document extraction](document-extraction.md)
- [Security](security.md)
- [Sharing knowledge](sharing.md)
- [Identities and namespaces](../concepts/identities-and-namespaces.md)
- [Rolling upgrades](rolling-upgrades.md)
