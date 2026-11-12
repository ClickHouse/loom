# Curation loop

The curation loop turns what people ask into definitions an organization stands behind.
Searches that find nothing become knowledge gaps. A gap, or a query a team runs often on
its own ClickHouse, becomes a drafted definition in the learnings queue. A definition an
admin promotes is governed: `memory.search` ranks it first when a query names its term.
This page is the design of the loop. None of its ops, tables or settings are served until
the pull request that builds each part lands; that pull request rewrites its section to
describe what it built.

```mermaid
flowchart LR
    S["memory.search / knowledge.search<br/>/ loom_search (a miss)"] --> M["loom.search_misses"]
    M -->|"daily worker"| G["loom.knowledge_gaps"]
    Q["team ClickHouse<br/>system.query_log"] -->|"query-history source"| L
    G -->|"knowledge.gaps.draft"| L["learnings queue<br/>(a drafted definition)"]
    L -->|"learnings.review promote"| D["definition/&lt;slug&gt;<br/>governed"]
    D -->|"term leg"| R["memory.search ranks it first"]
```

## Governed definitions

A definition is a memory row with `memory_type = 'definition'` under the key
`definition/<slug>`, where the slug is the case-folded term with runs of characters other
than letters and digits replaced by `-`, followed by a short hash of the case-folded term,
so `C++` and `C#` get two keys. Its value is a JSON object, which `memory.set` stores
whole in `value` and in `value_json`, so the embedding and the content hash cover all of
it:

| Field      | Meaning                                                             |
| ---------- | ------------------------------------------------------------------- |
| `term`     | The name as people write it.                                        |
| `aliases`  | Other names for the same thing (an abbreviation, an older name).    |
| `body`     | The definition's text.                                              |
| `owner`    | Who answers for the definition: a person or a team.                 |
| `rests_on` | What it is based on: knowledge document `source_uri`s and SQL text. |

A definition is **governed** while an admin stands behind its current text. That is
recorded apart from the memory row, in `loom.governed_definitions`: one row per
`definition/` key holding the term, its aliases, the content hash of the text the admin
approved, who governed it and when. Only an admin op writes it: promotion (below) and an
admin's explicit govern or ungovern of a namespace-wide definition (`definition.govern`,
`definition.ungovern`). No memory write reaches it, so `memory.set`, `memory.pin`,
`memory.verify`, `memory.update` and the bulk and import paths cannot make a row governed.
A definition is governed exactly while its record exists and the row's live text hashes to
the approved one (`live_sha256` in `server/memory/domain/trust.py`). A version that
changes the text ends that, whoever writes it; one that copies the text (the entity pass,
a touch, a lock, a restatement) keeps it. A tombstone or a redact on N, a replayed redact
included, deletes the record before it commits and is refused when that delete fails, so
writing the approved text back after a delete never makes the row governed again and the
record keeps no redacted term; no record exists while an N-1 replica serves (the rollout
gate below). `definition.list`
(admin) lists the namespace's records, each governed or lapsed, so the admin sees what to
govern again. Only namespace-wide rows (`user_id`, `agent` and `session_id` all `'-'`) of
type `definition` under the `definition/` key can be governed, and the term leg serves one
only while its status is `active`.

No two governed definitions share a term or an alias, compared case-folded as the key is
built, so `SAT` and `Sat` are one name: governing one whose term or alias is already
another governed definition's is refused, and that check runs on every
govern, a renewed one included. Should two records still match one name (two governs
racing), the term leg serves the one governed last.

A caller may already write rows of type `definition`; they are governed only once an admin
governs them, but their write-time verdicts change with this design (below): no merge,
duplicate, refinement or pinned-peer refusal against another definition, which N-1 still
gives during a rollout. `definition` joins `KEY_IDENTIFIED_TYPES`, as `decision` is: two
definitions under different keys are two definitions however alike they read. Unlike a
decision, a definition is never judged against a close definition under another key: the
write-time check neither marks nor supersedes it, and the conflict resolver ([conflicts
and refusal](../features/memory/conflicts-and-refusal.md#the-conflict-resolver)) neither
flags nor retires it, since its key is its term and `MRR` is not a revision of `ARR`.
Conflict surfacing compares a row only with rows of its own `memory_type`, so a learned
fact that contradicts a definition is not refused by it. The term leg serves the governed
definition above such a fact; a cross-type check would add a candidate read to every
write, and is not part of this design.

### The term leg

`memory.search` gains one leg. It reads the addressed namespace's
`loom.governed_definitions` (only that one, whatever `namespace_scope` fans out over) on
the `(org, namespace)` key prefix, matches the query against each record's term and
aliases, reads the matched definitions whose text is still the approved one, and places
them at the top of the served list with `governed = true` on the hit. A term or alias
matches where it appears in the query with no letter, digit, `+` or `#` next to it. One
written all in capitals, or with a capital after its first letter, matches only as
written, so `IT` does not match "what is it" and `GitHub` stays exact; any other matches
case-folded, so `Churn rate` matches "what is our churn rate".

Governed definitions are served through this leg under any read scope: an admin published
them for every reader of the namespace, and a user-bounded reader who could not see one
would read the term as undefined. This narrows the read-scope rule that a bounded read
serves no row stamped `'-'` ([read scope inside a
namespace](../concepts/identities-and-namespaces.md#read-scope-inside-a-namespace)), for
governed definitions through this leg only; every other namespace-wide row keeps it. The
leg runs only on a search's first page, and is off when a field asks for other rows
(`memory_type`, which cannot name `definition`, `tags`, `category`, `scope`, an explicit
`agent`, `run_id` or `session_ids`, `as_of`). A `user_id` or `session_id` the read scope
pins, `exclude_keys` and `min_similarity` keep it on: it drops any key in `exclude_keys`.
It never serves a `sensitivity = 'high'` row, as no other leg does. Its read is bounded by a
setting; past the bound, the most recently updated definitions are read and the response
says the leg was cut. It runs beside the others under the same time budget, and its
latency is measured against the op's budget (`OP_BUDGETS_P99_MS` in
`server/settings/catalog.py`) in the pull request that adds it. `governed` is an added
output field: the SDKs and the MCP schema carry it.

The leg covers memory only. Knowledge has its own exact-key leg for identifiers a document
defines (`server/knowledge/domain/exact_keys.py`), and a definition's `rests_on` already
points at the documents behind it. `loom_search` serves the leg through its memory section.

**Ranking-neutral rule:** when no governed term matches, the leg adds nothing and the
served list is exactly what it was before. The memory-plane rule asks for a paired
benchmark when a similarity gate or a rank weight moves; this leg moves neither. It inserts
rows only on a term match. So the pull request that adds it shows, on the offline harnesses,
that served keys and order are identical for every query naming no term, and reports the
gain on a small set of term queries.

## Knowledge gaps

### Recording misses

Recording is an operator opt-in (`knowledge_gaps_enabled`, off by default), as
`search_trace_log_enabled` is: a miss stores query text. A namespace that refused capture
(`capture_generation_io = false`, read through `server/observability/capture_consent.py`)
records no misses, and an unreadable consent counts as a refusal; the setting's other
values leave recording to the operator's opt-in, so turning gaps on never needs prompt
capture. No miss is recorded while a replica below N serves (the rollout gate below). A
miss is a search that found nothing a caller could use, recorded by the op the caller
invoked, never by a search another op runs on its behalf:

- `memory.search` that abstained, or answered with `retrieval_risk` `high` or `abstain`,
  and served no governed definition, when it read the namespace-wide rows under a read
  scope of the whole namespace and no field removed candidates (for example a user,
  session, run, agent, type, tag, entity, category, scope, time, body length,
  `exclude_keys` or `min_similarity` away from its default);
- `knowledge.search` with no hit as close as the noise filter's threshold (`_close` in
  `server/knowledge/app/knowledge_search.py`), no exact-key hit and, in `lexical` mode, no
  hit at all, when no field removed candidates (for example `namespaces`, `tags`,
  `uri_prefix`, `since_seconds`, `session_ids`, `source_classes`, `formats` or
  `block_types` away from its default); documents are the namespace's under any read scope;
- `loom_search` (`search.query`) whose memory and knowledge sections both came back empty,
  with no leg failed, under a read scope of the whole namespace. Its own memory and
  knowledge searches record nothing, so a question that knowledge answers is not a gap
  because memory abstained.

A search that could not answer for another reason is not a miss: an expired page handle
(`abstain_code = 'page_expired'`), a degraded answer whose provider fell back, or one with
writes still waiting for extraction (`pending_extraction > 0`).

The **asker** is who the token says asked: the end user of an end-user token, else the
token's subject; a body field never names it. Each miss writes one row to
`loom.search_misses` under the namespace the search was addressed to: the identity tuple,
the asker, `actor`, the plane, the query and its normalised form. No memory key or document
URI is kept: one naming another user's row or document would outlive that user's erasure,
or the document's delete, in someone else's miss. The table has a 30-day TTL: it
is a trace, not memory.

Neither existing log is reused. `loom.search_trace_log` records every query rather than
misses and has no `user_id` to count askers or erase by. `loom.query_nodes` keeps one row
per query text with its sessions, not its people, and only for memory searches.

### Grouping

The `knowledge_gaps` worker runs once a day for each namespace with misses. It reads the
last `knowledge_gaps_window_days` (default 7) of misses and groups them by normalised
query. **Privacy threshold:** a group with fewer than `knowledge_gaps_min_askers` distinct
askers (default three, never below three, one value for the deployment) is dropped before
anything else, so every query a gap shows was
asked in that form by at least that many tokens' principals. The worker embeds the
remaining groups' queries (at most `knowledge_gaps_max_groups`), merges groups whose
vectors are at least `knowledge_gaps_merge_similarity` alike, and writes the result as a
new build of the namespace in `loom.knowledge_gaps`:

| Column            | Meaning                                                         |
| ----------------- | --------------------------------------------------------------- |
| `gap_id`          | A hash of the namespace and the gap's most asked query.         |
| `head`, `samples` | The most asked normalised query, and the others merged into it. |
| `askers`, `asks`  | How many distinct askers, and how many times.                   |
| `last_at`         | The day it was last asked.                                      |

Every build writes its gaps, then a marker row, and readers take the latest build that has
its marker, so a build with no gap left, or one whose gaps an erasure deleted, still
replaces the last one. Builds expire after 14 days. A pass whose read raced an erasure's
delete could write erased rows back, so after writing its gaps, and before the marker, the
worker deletes them and writes no marker when an erasure that may reach the namespace (any
user's, or the namespace's) was unfinished when the pass read or has finished since
(`ledger_reads.unfinished_reaching`, as the
[erasure saga](memory-architecture.md#erasure-saga) fences other writers); an
erasure that begins after that check deletes the gaps itself. A failed erasure keeps that
fence until it completes, and the namespace shows no new build meanwhile.

### Ops and dashboard

| Op                     | Role  | What it does                                                                                                          |
| ---------------------- | ----- | --------------------------------------------------------------------------------------------------------------------- |
| `knowledge.gaps.list`  | admin | The namespace's gaps, most askers first.                                                                              |
| `knowledge.gaps.draft` | admin | Drafts a definition for one gap with the LLM from its queries alone, and queues it (`source_kind = 'knowledge_gap'`). |

Both list what users asked, so neither honours a read-narrowed token: the check that
admits the `knowledge.` group to such a token (`honours_read_scope` in
`server/identity/domain/read_scope.py`) refuses `knowledge.gaps.` first. A draft queued
from a gap holds the drafted definition and the gap's id. The LLM writes it from the gap's
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
tracker org. It reuses the tracker's connection checks: the host vetting, a read-only
user (granted `SELECT` on `system.query_log`), the credential encrypted, a time budget and
a breaker on every read ([tracker plane](../features/tracker/overview.md)). The
registration names the team that owns its definitions. Each turn:

1. reads finished `SELECT` queries of the source's window, grouped by
   `normalized_query_hash`, ranked by distinct users and then by recency, and drops a group
   that fewer than `knowledge_gaps_min_askers` distinct users ran. Only the normalised text
   is read (`normalizeQuery`, which replaces literals), so no user's literal values reach
   the LLM or the draft;
2. skips groups already drafted (a draft's id derives from the source and the hash);
3. asks the LLM to draft a metric definition for at most a setting's number of groups, and
   queues each (`source_kind = 'query_history'`): the metric's name is the `term`, its
   meaning the `body`, the normalised SQL and the tables and columns it reads go in
   `rests_on`, and the registration's team is the `owner`.

Nothing reaches search until an admin promotes a draft.

## Promotion

`learnings.review` with `promote` on a `knowledge_gap` or `query_history` candidate first
checks the term and aliases against the governed definitions, so a refused promotion
writes nothing; then it writes the definition through `memory.set` for the whole
namespace, so the blacklist and the pinned-key check run as for any write ([learnings
review queue](../features/memory/procedures-and-learnings.md#learnings-review-queue)).
Then the same call governs the version the write committed (under the Postgres memory
authority, the version Postgres committed, published or not) as the reviewer's
server-stamped actor: the admin promoting a definition is the person standing behind it.
A govern (promotion's or `definition.govern`) writes its record, then reads the row again
and deletes the record when the row was tombstoned, redacted or changed meanwhile, so a
racing delete or redact leaves no record behind. A
draft for a term already governed revises that definition, and the promotion is the
approval of its new text. A definition promotes to memory only; a `destination` of
`knowledge` or `both` is refused for these candidates. A rejected draft leaves nothing
behind but its queue row.

## Erasure and audit

- Definitions are memory rows and take every path memory takes: tombstone, namespace
  erasure, history, audit.
- `loom.search_misses`: a namespace step, a user step deleting every miss whose asker or
  `user_id` is the user (a token subject spelled as the user's id goes too, which costs
  only a trace row), and a census row.
- `loom.knowledge_gaps` keeps counts, not askers, so no op or page can say who asked: a
  namespace step, and a user step that deletes every build of the user's org (derived
  rows the next daily pass rebuilds without the user's misses, which the miss step
  deleted first; the cost is a day without gaps), each with a census row.
- `loom.governed_definitions` holds terms and aliases, which are definition content: a
  namespace step and a census row.
- A query-history registration holds a host and a sealed credential: a namespace step and
  a census row.
- A draft carries only threshold-protected text (above), so it is erased with its
  namespace.
- `namespace.snapshot` does not carry governed records: a restored namespace's definitions
  are ungoverned until an admin governs them again.
- `learnings.review` writes the audit row of each promotion, the governing included; the
  admin's govern and ungovern write their own, and `knowledge.gaps.draft` writes one
  naming the gap.

## Cost bounds

- Recording a miss is one asynchronous insert; a hit records nothing.
- The gaps worker's grouping and its threshold run in ClickHouse (`GROUP BY` the normalised
  query, `HAVING` enough distinct askers), so at most `knowledge_gaps_max_groups` groups
  leave the store, and it embeds at most that many texts per namespace per day.
- `knowledge.gaps.draft` and a query-history turn make one LLM call per draft, a turn at
  most its setting's number. They and the gaps worker's embeddings run inside the
  namespace's daily cost cap (`server/observability/budget.py`), read fail-closed
  (`fail_open=False`) as the conflict resolver reads it: over the cap, or with the cap
  unreadable, a draft is refused and a pass or turn ends early, and the next one resumes.
- The term leg is one bounded read of the namespace's governed records per search, and a
  read of the matched definitions.

## Rolling upgrades

The tables are additive ([rolling upgrades](rolling-upgrades.md#the-rules)). N-1's erasure
saga does not know them, so nothing this design writes may exist while an N-1 replica
serves. Every op it adds (`knowledge.gaps.*`, `definition.*`, the query-history
registration's ops) and a `learnings.review` promotion of a `knowledge_gap` or
`query_history` candidate answers 503 while any live replica's schema is below N, as
`namespace.guide_set` does (rule 24), and the gaps pass and the query-history turn write
nothing then, and no miss is recorded. The operator turns `knowledge_gaps_enabled` on only
once no N-1 replica serves, as rule 23 does for assistant-turn learnings. So while N-1
serves no miss, gap, draft, governed record or registration is written, and N-1 sees a
`definition` row only as a plain memory row with no term leg.

A rollback to N-1 meets what N wrote. Before it, the operator turns
`knowledge_gaps_enabled` off and deletes the rows of `loom.search_misses`,
`loom.knowledge_gaps` and `loom.governed_definitions`, and every `knowledge_gap` and
`query_history` row of `loom.learnings_queue` whatever its status, as rule 23 does for
assistant-turn rules. The first N-1 replica to serve closes the gate, so no op or turn of
this design writes after it; the operator runs the same delete once more then, for what N
wrote in between. N-1's redact or erasure then leaves no term behind, N-1 has no
definition draft to promote, and governance is done again on N, as after a snapshot
restore.
Registrations stay, and N-1's namespace erasure does not reach them: a namespace erasure
run after the rollback is run again once N serves.

## Invariants

- A gap shows only queries that at least `knowledge_gaps_min_askers` askers asked in that
  form; a query-history draft only the normalised text of queries that many users ran.
- A miss is recorded only when the operator opted in; its asker comes from the token,
  never the body.
- A definition is governed only while an admin's record approves its current text, and no
  two governed definitions share a term or an alias; only governed definitions are placed
  above the served list.
- A query naming no governed term is served exactly as without the term leg.

## Source entry points

- `server/memory/domain/memory_types.py` (`KEY_IDENTIFIED_TYPES`)
- `server/memory/app/search/handler.py` (the legs of `memory.search`)
- `server/memory/app/learnings.py` (`handle_learnings_review`)
- `server/identity/domain/read_scope.py` (`honours_read_scope`)
- `server/observability/budget.py` (`namespaces_over_cap`)

## Related

- [Memory architecture](memory-architecture.md)
- [Learnings review
  queue](../features/memory/procedures-and-learnings.md#learnings-review-queue)
- [Conflicts and refusal](../features/memory/conflicts-and-refusal.md)
- [Tracker plane](../features/tracker/overview.md)
