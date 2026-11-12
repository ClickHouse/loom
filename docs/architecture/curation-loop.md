# Curation loop

This page is the design of the curation loop, which turns what people ask into definitions
an organization stands behind; none of it is built yet. Searches that find nothing become
knowledge gaps. A gap, or a query a team runs often on its own ClickHouse, becomes a
drafted definition in the learnings queue. A definition an admin promotes into the shared
layer is governed: `memory.search` ranks it first when a query names its term. The parts
land in this order: knowledge gaps (recording, grouping and the list), governed
definitions (the shared-layer changes and the term leg), then `knowledge.gaps.draft` and
query history, which queue definition drafts and so need the review path the second part
builds. None of its ops, tables or settings are served until the pull request that builds
each part lands; that pull request rewrites its section to describe what it built.

```mermaid
flowchart LR
    S["memory.search / knowledge.search<br/>/ loom_search (a miss)"] --> M["loom.search_misses"]
    M -->|"daily worker"| G["loom.knowledge_gaps"]
    Q["team ClickHouse<br/>system.query_log"] -->|"query-history source"| L
    G -->|"knowledge.gaps.draft"| L["learnings queue<br/>(a drafted definition)"]
    L -->|"learnings.review promote"| D["loom.shared<br/>definition/&lt;slug&gt;"]
    D -->|"term leg"| R["memory.search ranks it first"]
```

## Governed definitions

A definition is a memory row with `memory_type = 'definition'` under the key
`definition/<slug>`, where the slug is the case-folded term with runs of characters other
than letters and digits replaced by `-`, followed by a short hash of the case-folded term,
so `C++` and `C#` get two keys. Its value is a JSON object, which `memory.set` stores
whole in `value` and in `value_json` (the embedding reads its excerpt, as for any row):

| Field      | Meaning                                                             |
| ---------- | ------------------------------------------------------------------- |
| `term`     | The name as people write it.                                        |
| `aliases`  | Other names for the same thing (an abbreviation, an older name).    |
| `body`     | The definition's text.                                              |
| `owner`    | Who answers for the definition: a person or a team.                 |
| `rests_on` | What it is based on: knowledge document `source_uri`s and SQL text. |

A definition is **governed** while three things hold, checked when the term leg reads it:
a live `loom.shared` row of type `definition` under a `definition/` key exists; its
provenance (`promoted_from`) names exactly one source, the namespace-wide row under that
key, which is the slug of the value's `term` (`user_id`, `agent` and `session_id` all
`'-'`); and that source is live (`status = 'active'`, not tombstoned, superseded, or past
its `expires_at` or `valid_to`), not `sensitivity = 'high'`, and holds the same text as
the shared copy: the source's `content_sha256` equals the canonical-JSON hash of the
shared `value` (the hash the approved-content check below uses), so a store that orders an
object's keys differently is not a change. The source is read from the memory authority
(Postgres when it holds live memory, [storage authority](storage-authority.md)), and a
source whose head cannot be read fails closed: the definition is not served. A read-time
check covers every path into that state that it can see, rows promoted before this design
and through N-1 included; what it cannot see is named under rolling upgrades. The shared
layer already is the namespace's admin-approved copy of a fact ([the shared
layer](../concepts/identities-and-namespaces.md#agent-written-rows-and-the-shared-layer)):
`memory.promote` and `memory.demote` need `admin`, the shared row records its source
(`promoted_from`) and, on the review path, the candidate it came from
(`promoted_from_candidate_id`, which a direct `memory.promote` leaves to its caller),
`memory.lineage` walks it back, and every build knows it. So governance needs no table or
state of its own. An edit, a relabel to `high` or a tombstone of the source, by any writer
or worker on any build, ends governance while the source differs from what the admin
approved; a copy that keeps the text (the entity pass, a touch) keeps it, and a source
that returns to the approved state (a restore, an edit reverted byte for byte) is governed
again with no new promotion. A relabel back from `high` does not bring back the shared
embedding it retired: the term leg serves the definition again, and an admin promotes it
again to restore the copy's search row. `memory.demote` ends governance. Only
`memory.promote` promotes into `loom.shared` (the op and `learnings.review`'s path through
it; demote, erasure, a redact and a relabel to `high` only retire or scrub there, as the
changes below say); no worker promotes on its own. An admin read,
`memory.definitions.list`, lists the namespace's shared definitions with the read-time
check's verdict on each and the reason a lapsed one lapsed (its text changed, it is
`high`, gone, expired, past its `valid_to`, under a key that is not its term's slug,
promoted from a row that is not the namespace-wide one, or no longer `active`). A changed
text is promoted again; a source that is no longer `active` is restored (Undo in
`memory.auto_retirements`, whose reasons include `same_key_newest_wins` for a later write
of the same key under `newest_wins`) or cleared (`conflicts.resolve` for a `conflicted`
mark, `memory.dedup_resolve` for a `duplicate` one), which governs it again when its text
is the approved one, and is promoted again otherwise.

The shared layer needs eight changes for this, each in the pull request that adds the term
leg except where a change names another, because the term leg serves shared rows to every
reader:

- **Namespace erasure** gains a step and a census row for `loom.shared`. Today only user
  erasure reaches it, by provenance, and a definition promoted from the namespace-wide row
  names no user.
- **A redact** of a version of a definition's source (a past version: `memory.redact`
  refuses the live head) scrubs the shared row whose text is that version's, and that
  row's embedding, which a redact does not reach today, and blanks the text of every
  `knowledge_gap` and `query_history` candidate drafted for its key, promoted or not,
  which `learnings.list` shows to viewers; the pull request that adds those candidate
  kinds adds that part.
- **A relabel to `high`** of a promoted definition's source retires that row's shared
  embedding, keeping the row: `loom.shared` has no sensitivity of its own, so the ordinary
  legs would otherwise keep serving the copy, and no sweep removes a durable row its
  writer did not ask to remove.
- **A promotion of a definition carries the content hash it approves**, the hash of the
  candidate's drafted value on the review path, or one the caller must pass to
  `memory.promote` (a definition promotion without one is refused), and is refused when
  the source's text no longer has it, so a writer's change between the write and the
  promotion is never signed off as the admin's.
- **`promoted_by`** is stamped from the token's actor on every promotion of a definition
  (a candidate or source of type `definition`, whatever its `source_kind`), through
  `memory.promote` or `learnings.review`, and so are the queue row's `decided_by` and the
  audit subject on such a review, never taken from the body's `approver`, which both ops
  then name in `ignored_args`.
- **`check_against_shared`**, the promotion's conflict check, skips `definition` (only),
  so promoting `MRR` is not refused as a conflict with `ARR`; decisions keep the check.
- **`memory.promote` refuses a definition whose term or alias** is already another
  definition's, governed or lapsed (every live shared row of type `definition`), on every
  promotion path, so a lapsed definition that returns to its approved state never shares a
  name with one promoted meanwhile. Names are compared case-folded (the form the key's
  hash reads), so `SAT` and `Sat` are one name.
- **`memory.promote` refuses a definition** (type `definition` or a `definition/` target
  key) whose source is not the namespace-wide row, whose source key is not the slug key of
  its `term` (`definition/<slug>`), or whose `target_key` differs from that key, so a
  revision of a term always lands on the governed row's key. The read-time check above is
  what the term leg relies on; this refusal keeps such rows from being made.

The changes to `memory.promote` break existing callers: a `definition` promotion without
the approved hash, from a row that is not namespace-wide, or under a key that is not its
term's slug is refused, and `approver` no longer names a definition's promoter (the
response names it in `ignored_args`); the SDKs and the MCP schema carry the hash field,
and the implementing pull request names the break.

Should two shared rows match one name despite that refusal (two promotions racing, or one
promoted through a build without the check), the term leg serves the one promoted last, by
the shared row's `version` (its whole-second `promoted_at` can tie).

A caller may already write rows of type `definition`; they are governed only once an admin
promotes them, but their write-time verdicts change with this design: `definition` joins
`KEY_IDENTIFIED_TYPES`, as `decision` is, so two definitions under different keys are two
definitions however alike they read. Unlike a decision, a definition is never judged
against a close definition under another key: the write-time check neither marks nor
supersedes it (no merge, duplicate, refinement or pinned-peer refusal, and no supersession
by the value-update pass of `memory.bulk_set`, and no fold of a `memory.bulk_set` entry
into a byte-identical row under another key, which [conflicts and
refusal](../features/memory/conflicts-and-refusal.md) applies to every other type), and
the conflict resolver ([conflicts and
refusal](../features/memory/conflicts-and-refusal.md#the-conflict-resolver)) neither flags
nor retires it, and `memory.consolidate` leaves rows of type `definition` out of its
clusters (today it groups every active row of one type by similarity and tombstones the
merged keys), since a definition's key is its term and `MRR` is not a revision of `ARR`.
This narrows the rule that a pinned row blocks a close write from any user ([read scope
inside a
namespace](../concepts/identities-and-namespaces.md#read-scope-inside-a-namespace)), for
definitions against definitions only; the implementing pull request updates that page, the
memory-plane rule (`.claude/rules/memory-plane.md`), the write-time check and
consolidation in [memory architecture](memory-architecture.md) and [conflicts and
refusal](../features/memory/conflicts-and-refusal.md). N-1 still gives the old verdicts
during a rollout. Conflict surfacing compares a row only with rows of its own
`memory_type`, so a learned fact that contradicts a definition is not refused by it. The
term leg serves the governed definition above such a fact; a cross-type check would add a
candidate read to every write, and is not part of this design.

### The term leg

`memory.search` gains one leg, which runs wherever `memory.search` runs, inside
`memory.ask` and `context.pack` as well; the rule below that no later stage moves a
governed hit covers those ops' stages too. It reads the addressed namespace's live
`loom.shared` rows of type `definition` under a `definition/` key (only that namespace,
whatever `namespace_scope` fans out over), matches the query against each one's term and
aliases, and places the matched rows at the top of the served list with `governed = true`
on the hit, removing any other hit under the same key in the addressed namespace, so an
edited private copy is not served beside the governed text. A term or alias matches where
it appears in the query with no character next to it that continues its edge: next to a
Latin letter (accented ones included), digit, `_`, `+` or `#`, another of those; next to a
letter of another script written with spaces (Cyrillic, Greek), a letter or digit that is
not Latin; next to Hangul, whose particles attach to the word before them, or a script
written without spaces (Han, Kana, Thai, Lao, Khmer, Myanmar) or punctuation, nothing. So
`рост` does not match inside "простой" and `café` not inside "cafés", while `MRR` matches
in "什么是MRR", `ARR` in "ARR은" and `매출` in "매출은 얼마야". One written all in capitals, or with a
capital after its first letter, matches only as written, so `IT` does not match "what is
it" and `GitHub` stays exact; any other matches case-folded, so `Churn rate` matches "what
is our churn rate".

Governed definitions are served through this leg under any read scope: an admin published
them for every reader of the namespace, and a user-bounded reader who could not see one
would read the term as undefined. This narrows the read-scope rule that a bounded read
serves no namespace-wide row ([read scope inside a
namespace](../concepts/identities-and-namespaces.md#read-scope-inside-a-namespace)), for
governed definitions through this leg only; the implementing pull request updates that
page and the comment on `SCOPED_OPS` in `server/identity/domain/read_scope.py`, which says
every memory read of those ops carries the read scope. The leg runs only on a search's
first page (the governed keys it served there stay out of every later page of that
search), and is off when a field asks for other rows (a `memory_type` other than
`definition`, which the pull request adds to the field's values; `tags` or `category`
filtering, not with `tags_soft` or `category_soft`; an explicit `agent`, `run_id` or
`session_ids`; `as_of`; a body `user_id` or `session_id` under a whole-namespace read;
`min_body_chars`). A `user_id` or `session_id` the read scope pins, `scope`,
`exclude_keys`, `min_similarity`, a soft tag or category and `entity_filter`, which only
boost rows, keep it on; it drops any key in `exclude_keys`. It serves no definition whose
source is `sensitivity = 'high'` (the read-time check above). Its read is bounded by a
setting; past the bound, the most recently promoted definitions (by `version`) are read.
It runs concurrently with the other legs under its own time bound, which never shortens
theirs (a leg past its bound adds nothing); an added output field, `term_leg` (`complete`,
`capped` or `timed_out`), says which, and its latency is measured against the op's budget
(`OP_BUDGETS_P99_MS` in `server/settings/catalog.py`) in the pull request that adds it.
`governed` is an added output field: the SDKs and the MCP schema carry it.

The leg covers memory only. Knowledge has its own exact-key leg for identifiers a document
defines (`server/knowledge/domain/exact_keys.py`), and a definition's `rests_on` already
points at the documents behind it. `loom_search` serves the leg through its memory
section, and one rule holds for every later stage of `memory.search`, `loom_search` and
the ops that run `memory.search` inside them: no stage or floor moves, withholds or
outranks a hit marked `governed`, a stage added later included. Governed hits come first,
in the order their names first appear in the query (a tie by the later `version`), and
count within `top_k`, within a stage's cap on the memory section (the tracker verdict's
`verdict_leads`, which keeps `VERDICT_MEMORY_MAX` memory hits) and within the token
budgets (`body_budget_tokens`, `context.pack`'s block budget). When more governed hits
match than fit, the later ones in that order are not served; a budget may shorten a body
but never reorders the hits.

**Ranking-neutral rule:** when no governed term matches, the leg adds nothing and the
served list is exactly what it was before. The memory-plane rule asks for a paired
benchmark when a similarity gate or a rank weight moves; this leg moves neither. It
inserts rows only on a term match. So the pull request that adds it shows, on the offline
harnesses, that served keys and order are identical for every query naming no term, and
reports the gain on a small set of term queries.

## Knowledge gaps

### Recording misses

Recording is an operator opt-in (`knowledge_gaps_enabled`, off by default), and a miss
stores query text, so it reads the consent `loom.search_trace_log` reads for its query
column: the request's capture level (`current_capture_level()` in
`server/observability/genai.py`, which the transport resolves from the namespace's
`capture_generation_io`, else the operator's `langfuse_capture_io_level`, and narrows to
the most restrictive namespace a fan-out reads). A miss is recorded only when that level
is not `off`; an unreadable consent reads as `off`. A deployment that wants gaps therefore
turns on both: gaps, and capture at `query` or `all`. No miss is recorded while a replica
below N serves (the rollout gate below). A gap needs several distinct askers, so a
deployment that has one identity for every caller (`effective_identity_provider()` is
`single-user`, set explicitly or inferred when `auth_required` is off and no provider is
set) records no misses, gaps on or off, and a request without a token records no miss. A
miss is a search that found nothing a caller could use, recorded by the op the caller
invoked, never by a search another op runs on its behalf:

- `memory.search` that abstained, or answered with `retrieval_risk` `high` or `abstain`,
  and served no governed definition, when it read the namespace-wide rows under a read
  scope of the whole namespace and no field removed candidates (for example a user,
  session, run, agent, type, filtering tag or category, scope, time, body length,
  `exclude_keys` or `min_similarity` away from its default; a soft tag or category and
  `entity_filter` only boost rows);
- `knowledge.search` with no hit as close as the noise filter's threshold (`_close` in
  `server/knowledge/app/knowledge_search.py`), no exact-key hit and, in `lexical` mode, no
  hit at all, when no field removed candidates (for example `tags`, `uri_prefix`,
  `since_seconds`, `session_ids`, `source_classes`, `formats` or `block_types` away from
  its default); documents are the namespace's under any read scope;
- `loom_search` (`search.query`) whose every section (memory, knowledge, profile,
  conversations) came back empty, with no leg failed, no hit withheld (`withheld`), no
  tracker verdict, no namespace skipped (`namespaces_skipped`) and no `tags`, under a read
  scope of the whole namespace. Its own memory and knowledge searches record nothing, so a
  question that knowledge answers is not a gap because memory abstained.

Only a search's first page can record a miss (a call with a `page` handle never does), and
a search that could not answer for another reason is not a miss: a degraded answer whose
provider fell back, one with writes still waiting for extraction (`pending_extraction >
0`), one whose fan-out stopped short (`namespaces_skipped`), or one whose hits the memory
authority dropped (`stale_dropped > 0`), or one whose term leg did not finish (`term_leg`
other than `complete`, or a source read that failed closed), so a defined term is never
recorded as a gap because its leg was cut.

The **asker** is the person the token stands for, never a body field: the end user of an
end-user token; else the token's `creator` claim, the person at the root of a delegated
token or service account ([tokens and grants](../guides/tokens-and-grants.md)); else, for
a service account's token minted without the claim, the first subject that is not a
service account in its creator chain as the registry records it (`created_by` and its
lineage, `server/auth/service_accounts.py`; `sa_creator` there answers None alike for a
failed read and for no creator, and ignores `lineage`, so the implementing pull request
adds a lookup that raises on a failed read and walks `lineage`), and no miss when the
chain has none; else the token's subject. An agent of a bot (`sa:<bot>:<agent>`) and an
agent sign-up's service account (whose `created_by` names a namespace, not a person) stand
for no person and record no miss. Tokens a person mints for themselves therefore count as
that person. An asker that cannot be resolved (the registry unreadable, or a service
account with no creator) records no miss: an unknown principal is never counted as a
separate person. The asker is stored in the form a user erasure names: an end user as
their id, without the `user:` prefix, and an email lower-cased and folded onto the login
handle (`owner_email`) of the account the directory ties it to by a verified email,
whether that handle is an email or not, so one person's two spellings count once.
`canonical_subject` in `server/auth/aliases.py` keeps an email whose handle is also an
email unchanged, so the implementing pull request adds a helper beside it that returns the
handle; a spelling tied to two accounts, or a tie that cannot be read, records no miss.
Each miss writes one row to `loom.search_misses` under the namespace the search was
addressed to: the identity tuple, the asker, `actor`, the plane and the query's normalised
form (case-folded, with every run of characters other than letters, digits, `_`, `+` and
`#` made one space, so `C++`, `C#` and `arr_v2` stay apart as the term leg keeps them).
That form is still the query's words, kept as the trace of a question under the request's
capture level; only the threshold below decides what an admin sees. A query that reads as
`high` (`reads_high` in `server/memory/domain/sensitivity.py`, the rule namespace-wide
listings apply) records no miss, so a pasted credential never becomes a gap head, an
embedding or an LLM prompt. No memory key or document URI is kept: one naming another
user's row or document would outlive that user's erasure, or the document's delete, in
someone else's miss. A miss is inserted after the response, so a user erasure could run
between the search and the insert. An erasure reaches a miss when it is the namespace's
(or a list naming it) or a user erasure one of whose spellings (`aliases.spellings`, as
the user step matches them) is the miss's asker or `user_id`. The recording reads
`ledger_reads.unfinished_reaching` before the insert and records nothing when such an
erasure was unfinished when the search began or has finished since; it then inserts the
miss under an id of its own, waits until the row is visible, and reads the ledger again,
deleting the row when one now reaches it, as other erasure fences check after storing. An
unreadable ledger or directory counts as reaching. An erasure that begins after that read
finds the row and deletes it itself. The table has a 16-day TTL: it is a trace, not
memory. A pass reads the last 7 days and a build lives 7 days from its write, with two
days to spare, so a miss outlives every build that counted it and a user erasure always
finds the namespaces it must clear.

Neither existing log is reused. `loom.search_trace_log` records every query rather than
misses, its `actor` is the token's principal, not the person resolved as below (an end
user, a creator), and it has no user-scope erasure step. `loom.query_nodes` keeps one row
per query text with its sessions, not its people, and only for memory searches.

### Grouping

The `knowledge_gaps` worker runs once a day for each namespace with misses or a live
build. It re-reads the namespace's capture level through an uncached read that raises when
the consent cannot be read; `capture_consent.resolve` is not that read, since it caches
and reads an unreadable consent as `off`. A namespace whose level is now `off` gets an
empty build and its stored misses deleted, so a later consent does not count them again; a
level that cannot be read skips the namespace's pass, with no build and no delete, so a
failed read never deletes misses; otherwise it reads the namespace's misses of the last 7
days and groups them by normalised query. **Privacy threshold:** a group with fewer than
`knowledge_gaps_min_askers` distinct askers (default three, never below three, one value
for the deployment) is dropped before anything else, so every query a gap shows was asked
in that form by at least that many askers. The threshold is as strong as the principals
behind it: it does not bind whoever holds the signing key (`loom mint-token` mints any
subject), an end-user issuer or a trusted caller (each names its own users), or a
deployment where principals can be self-created (`LOOM_SELF_SIGNUP=open`), where an admin
could ask from accounts of their own. There it limits what one ordinary caller reveals,
not what an admin can learn. On any deployment, an admin who compares two daily builds can
tell that one more asker reached a query that day, and in a small namespace that may say
who; the threshold bounds what one build shows, not what a series of builds implies. The
reading admin's own asks count toward it too, so an admin who asked a query learns from
its gap that at least `knowledge_gaps_min_askers` minus one others did, which in a small
namespace narrows who. The worker embeds the remaining groups' queries (at most
`knowledge_gaps_max_groups`), merges groups whose vectors are at least
`knowledge_gaps_merge_similarity` alike, counting a merged gap's askers as the union of
the distinct askers each group's query returns (`groupUniqArray`), held in memory for the
pass and never stored, and writes the result as a new build of the namespace in
`loom.knowledge_gaps`:

| Column            | Meaning                                                         |
| ----------------- | --------------------------------------------------------------- |
| `gap_id`          | A hash of the namespace and the gap's most asked query.         |
| `head`, `samples` | The most asked normalised query, and the others merged into it. |
| `askers`, `asks`  | How many distinct askers, and how many times.                   |
| `last_at`         | The day it was last asked.                                      |

Every build writes its gaps, then a marker row, and readers take the latest build that has
its marker, so a build with no gap left, or one whose gaps an erasure deleted, still
replaces the last one. Builds expire after 7 days, and a namespace left with no misses in
the window gets an empty build, and `knowledge.gaps.list` ignores a build whose marker is
older than 7 days, so an old one is never served past its window even when passes stop
(the TTL only frees the disk). A pass whose read raced an erasure's delete could write
erased rows back, so after writing its gaps, and before the marker, the worker deletes
them and writes no marker when an erasure that reaches a miss it read (matched as for a
recording) was unfinished when the pass read or has finished since
(`ledger_reads.unfinished_reaching`, as the [erasure
saga](memory-architecture.md#erasure-saga) fences other writers); an erasure that begins
after that check deletes the gaps itself. The list then serves the previous build, or none
once the erasure deleted it. A failed erasure holds this fence, as it holds every
writer's, until it is run again and completes. An admin who sees a namespace's gaps go
when a user's erasure deletes its builds learns that the user asked there in the last 7
days; the threshold does not hide that.

### Ops and dashboard

| Op                                                | Role  | What it does                                                                                                                                                                                                                                                                  |
| ------------------------------------------------- | ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `knowledge.gaps.list`                             | admin | The namespace's gaps, most askers first.                                                                                                                                                                                                                                      |
| `knowledge.gaps.draft`                            | admin | Drafts a definition for one gap with the LLM from its queries alone, with `owner` left empty (an admin sets it by editing the namespace-wide row after promotion and promoting it again with `memory.promote` and its hash), and queues it (`source_kind = 'knowledge_gap'`). |
| `memory.definitions.list`                         | admin | The namespace's shared definitions, each with the read-time check's verdict and the reason a lapsed one lapsed.                                                                                                                                                               |
| `knowledge.query_history.add`, `.list`, `.remove` | admin | Register, list and remove a query-history source on the namespace.                                                                                                                                                                                                            |

The `knowledge.gaps.` ops list what users asked and the `knowledge.query_history.` ops
manage a source, not documents, so none honours a read-narrowed token: the check that
admits the `knowledge.` group to such a token (`honours_read_scope` in
`server/identity/domain/read_scope.py`) refuses both prefixes first. A draft queued from a
gap holds the drafted definition and the gap's id. The LLM writes it from the gap's
queries, so it carries only text from queries at least `knowledge_gaps_min_askers` askers
asked in that form: that threshold is why it may be shown to the namespace's viewers in
`learnings.list`, as every learning is, and why it outlives a user erasure. The Library
page shows a gaps tile for a namespace the viewer administers, with a **Draft definition**
action per gap.

## Definitions from query history

A query-history source reads `system.query_log` from a team's ClickHouse into one
namespace's learnings queue. It is registered on that namespace by its admin, in a
registration of its own: not a document source, whose config never holds a host and which
writes documents, and not a tracker source, which needs the record contract and the
tracker org. It reuses the tracker's connection checks: the host vetting, a read-only user
(granted `SELECT` on `system.query_log`), the credential encrypted, a time budget and a
breaker on every read ([tracker plane](../features/tracker/overview.md)), through
`knowledge.query_history.add`, `.list` and `.remove`, as `tracker.source.*` registers a
tracker source. The registration names the team that owns its definitions and the
databases it covers, so a source on a shared cluster drafts only from its team's own
tables. Each turn:

1. reads finished `SELECT` queries of the source's window whose `databases` are not empty
   and all lie within the registration's (a query touching any other database, or none, as
   a health check's `SELECT 1` does, is not read), from every replica when the
   registration names the cluster (`clusterAllReplicas`) and from the node it connects to
   otherwise, grouped by `normalized_query_hash`, counting initial queries only
   (`is_initial_query = 1`) by `initial_user`, ranked by distinct users and then by
   recency, and drops a group that fewer than `knowledge_gaps_min_askers` distinct users
   ran. A user here is a ClickHouse account, not a person: the threshold holds only where
   each person queries under their own account, and a shared account (a BI tool's, a
   service's) counts as one asker however many people stand behind it, while one person
   with several accounts counts once per account. The threshold is a floor against
   drafting from one account's queries, not an anonymity guarantee. Only the normalised
   text is read, with comments removed and literals replaced (`normalizeQuery`), so no
   user's literal values reach the LLM or the draft;
2. skips groups already drafted (a draft's id derives from the source and the hash);
3. asks the LLM to draft a metric definition for at most a setting's number of groups, and
   queues each (`source_kind = 'query_history'`): the metric's name is the `term`, its
   meaning the `body`, the normalised SQL and the tables and columns it reads go in
   `rests_on`, and the registration's team is the `owner`.

`clusterAllReplicas` is a table function: a cluster registration accepts the `REMOTE`
grant it needs beside the tracker's read-only list, which already allows `CREATE TEMPORARY
TABLE`, and the read runs under `readonly = 2`, as the tracker's `remoteSecure` reads do.
Nothing reaches search until an admin promotes a draft.

## Promotion

`learnings.review` with `promote` on a `knowledge_gap` or `query_history` candidate first
checks the term and aliases against every other live shared row of type `definition` (the
set `memory.promote` refuses on) and the draft for a credential-shaped value
(`value_reads_high`), and the rollout gate, so a promotion these refuse writes nothing.
Then it writes the definition through `memory.set` for the whole namespace (today's review
path writes under the approver's `user_id`; the implementing pull request writes the
namespace-wide row), declaring `source_class = derived`, the downgrade a caller may
declare (an LLM drafted it; the admin's promotion is the approval, not its authorship), so
the blacklist and the pinned-key check run as for any write, and promotes that row into
the shared layer through `memory.promote`, with the reviewer's server-stamped actor as
`promoted_by` and the candidate's id ([learnings review
queue](../features/memory/procedures-and-learnings.md#learnings-review-queue)). The admin
promoting a definition is the person standing behind it. A draft for a term already
governed revises that definition: the promotion replaces its shared row. A definition
promotes to memory only; a `destination` of `knowledge` or `both` is refused for these
candidates. A draft rejected before any promotion attempt leaves nothing behind but its
queue row. A promotion refused after the write (a racing edit, a racing term, or
`memory.set` answering `PUBLICATION_PENDING`, which today's review path swallows) leaves
the candidate pending and the namespace-wide row written, which no term leg serves until
its text is promoted; for a revision, the old definition lapses meanwhile and the admin
read lists it. Every attempt, the first and each retry, decides from the head it reads and
keeps no state of its own: a head that is live and `active` and whose text hashes to the
draft's is promoted without a write; a head holding the governed shared copy's text is
written with `if_match_sha256` against that head; no head is written with `if_absent`, a
precondition the implementing pull request adds to `memory.set` that refuses the write
when a live row holds the key, so a writer who creates the row first is never overwritten;
any other head is a writer's edit, or a head the read-time check would not serve
(`conflicted`, `duplicate`, expired), and the review is refused rather than overwrite or
promote it; a demoted term's live row is such a head too, since no shared copy is
governed, and the admin edits that row to the draft's text and promotes it with
`memory.promote` and its hash instead. A retry so completes the promotion; rejecting the
draft instead leaves the written row, which the admin restores from the row's history or
promotes.

A `memory_supporters` candidate of type `definition` promotes its supporter row through
`learnings.review` too, and today's path splits the supporter tuple on `/` into exactly
six parts, which a `definition/<slug>` key breaks; the implementing pull request splits
off the five identity parts and keeps the rest as the key.

## Erasure and audit

- Definitions are memory and shared-layer rows and take every path those take, with the
  shared layer's namespace erasure and redact added above: tombstone, demote, redact,
  namespace erasure, history, audit. A user erasure tombstones a shared row whose
  provenance names the user (`shared.tombstone`); a definition promoted from the
  namespace-wide row names none, so erasing the admin who promoted it leaves it governed,
  and an admin's name in its `owner`, like `promoted_by` on the shared row today, stays
  until an admin edits the definition.
- A redact of a past version of a definition reaches that version, the shared row holding
  its text, and every `knowledge_gap` and `query_history` candidate drafted for its key
  (the change above), not the gap it came from, which holds only normalised queries enough
  askers asked and expires after 7 days.
- `loom.search_misses`: a namespace step, a user step deleting every miss whose asker or
  `user_id` is the user, matching every spelling the directory ties to the named subject
  (`spellings` in `server/auth/aliases.py`, unverified ties included, as a revocation
  does), compared lower-cased on both sides (`lower(asker)` and `lower(user_id)` against
  the lower-cased spellings), since the tie may have changed since the miss was recorded
  and a subject such as `ASmith` is stored as given; a directory that cannot be read fails
  the step closed (a token subject spelled as the user's id goes too, which costs only a
  trace row), and a census row counting the step's own predicate, over the asker and
  `user_id` under every spelling it deletes, so a miss the step left fails the saga's
  verify; an end user's asker is stored as their id, the form the erasure names.
- `loom.knowledge_gaps` keeps counts, not askers, so no op or page names who asked: a
  namespace step, and, inside the user's miss step before it deletes the misses, a read of
  the namespaces the user has misses in and a delete of those namespaces' builds (derived
  rows the next daily pass rebuilds without the user's misses; the cost is a day without
  gaps there). A gap names no user, so the census counts the table in the namespace scope
  only; the pass's fence keeps a raced build from coming back.
- A query-history registration holds a host and a sealed credential but no memory content:
  a namespace step deletes it, and its table is listed under that step in
  `STEP_ONLY_TABLES`, a step without a census row.
- A direct `memory.promote` writes its audit row; a promotion through `learnings.review`
  writes the review's row, which names the candidate, and the implementing pull request
  adds the promotion's own row there. `knowledge.gaps.draft` writes one naming the gap.

## Cost bounds

- Recording a miss is one insert and the asker's resolution, run after the response and
  off the request's path: the rollout gate's read of the live replicas, a registry read
  per hop of a service account's creator chain, a directory read for an email-shaped
  subject, and a read of the erasure ledger before and after the insert, with the
  spellings of any user erasure it finds (a delete only when an erasure began in between).
  The recordings in flight are bounded by a setting; past it a miss is dropped and
  counted. A hit records nothing.
- The gaps worker's grouping and its threshold run in ClickHouse (`GROUP BY` the
  normalised query, `HAVING` enough distinct askers), so at most
  `knowledge_gaps_max_groups` groups leave the store, and it embeds at most that many
  texts per namespace per day.
- `knowledge.gaps.draft` and a query-history turn make one LLM call per draft, a turn at
  most its setting's number. They and the gaps worker's embeddings honour the org's switch
  for model calls and run inside the namespace's daily cost cap
  (`server/observability/budget.py`), read fail-closed (`fail_open=False`) as the conflict
  resolver reads it: over the cap, or with the cap unreadable, a draft is refused and a
  pass or turn ends early, and the next one resumes.
- A query-history turn's read of `system.query_log` is bounded by its own window (at most
  a setting's number of days) and time limit, separate from the tracker's live-read
  budget, and a timeout counts against the source's breaker like any tracker read.
- The term leg is one bounded read of the namespace's shared definitions per search, and
  one read of the matched definitions' source rows.

## Rolling upgrades

The tables are additive ([rolling upgrades](rolling-upgrades.md#the-rules)). N-1's erasure
saga does not know `loom.search_misses`, `loom.knowledge_gaps` or the query-history
registration, so none of them may hold rows while an N-1 replica serves. Every op the
design adds (`knowledge.gaps.*`, `memory.definitions.list`, `knowledge.query_history.*`)
and a `learnings.review` promotion of a `knowledge_gap` or `query_history` candidate
answer 503 while any live replica's schema is below N, as `namespace.guide_set` does (rule
24); the gaps pass and the query-history turn write nothing then, and no miss is recorded.
The operator turns `knowledge_gaps_enabled` on only once no N-1 replica serves, as rule 23
does for assistant-turn learnings. Governed definitions are memory and shared rows, which
N-1 knows: it serves them as plain rows with no term leg. What N-1 does to them is caught
by the read-time check: a promotion from another source, or an edit, relabel or tombstone
of the source, leaves a row the term leg does not serve, and the term leg's last-promoted
rule covers a promotion that skipped the term check. A promotion on N-1 also skips the
approved-hash check and takes `promoted_by` from the body, which no read-time check can
tell, and an N-1 `memory.set` ignores `if_absent`, a field it does not know, and writes
over a live row; the review path that sends it answers 503 while N-1 serves, and a direct
caller relies on it only once no N-1 replica serves. Once none does, an admin promotes
again the definitions promoted during the rollout (`loom.shared` rows of type `definition`
by promotion time). N-1, which does not know `definition` as key-identified, can merge,
supersede or retire one definition's source while writing another; the rows it retired are
listed by `memory.auto_retirements` (reasons `conflict_resolver`,
`newest_wins_contradiction`, `value_update` and `same_key_newest_wins`), restored with its
Undo, and promoted again; a source it marked `conflicted` is cleared with
`conflicts.resolve`, one it marked `duplicate` with `memory.dedup_resolve`, and promoted
again; a write it merged was never stored and is written again; a source an N-1
`memory.consolidate` tombstoned is brought back with `memory.restore` and promoted again.
An N-1 relabel of a definition's source to `high` leaves its shared copy served by the
ordinary legs: once N serves, an admin demotes every definition the admin read lists as
lapsed for `high`. N-1's redact and namespace erasure do not reach the shared row's text,
as today: one run during the rollout or after a rollback is run again once N serves.

A rollback to N-1 meets what N wrote. Before it, the operator turns
`knowledge_gaps_enabled` off and deletes the rows of `loom.search_misses` and
`loom.knowledge_gaps`, every query-history registration, and every `knowledge_gap` and
`query_history` row of `loom.learnings_queue` whatever its status, as rule 23 does for
assistant-turn rules; a team re-registers its source once N serves again. The first N-1
replica to serve closes the gate, so no op or turn of this design writes after it; the
operator runs the same delete once more then, for what N wrote in between.

## Invariants

- A gap shows only queries that at least `knowledge_gaps_min_askers` askers asked in that
  form; a query-history draft only the normalised text of queries that many users ran.
- A miss is recorded only when the operator opted in and the namespace's capture level is
  not `off`; its asker comes from the token, never the body.
- A definition is governed only while an admin's promotion of its namespace-wide row into
  the shared layer is live and the source still holds that text under its term's slug,
  active, live, within its validity and unlabelled `high`; the term leg serves at most one
  definition per name, the last one promoted; only governed definitions are placed above
  the served list.
- A query naming no governed term is served exactly as without the term leg.

## Source entry points

- `server/memory/domain/memory_types.py` (`KEY_IDENTIFIED_TYPES`)
- `server/memory/app/search/handler.py` (the legs of `memory.search`)
- `server/memory/app/promote.py` (`handle_memory_promote`)
- `server/memory/app/learnings.py` (`handle_learnings_review`)
- `server/identity/domain/read_scope.py` (`honours_read_scope`)
- `server/observability/budget.py` (`namespaces_over_cap`)

## Related

- [Memory architecture](memory-architecture.md)
- [Learnings review
  queue](../features/memory/procedures-and-learnings.md#learnings-review-queue)
- [Conflicts and refusal](../features/memory/conflicts-and-refusal.md)
- [Tracker plane](../features/tracker/overview.md)
