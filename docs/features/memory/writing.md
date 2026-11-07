# Writing memory

A memory is one claim stored under a key in a namespace. Each write is judged against the
namespace's existing facts before it lands, and the response carries the verdict: clear,
merge, duplicate, conflict or rejected. The verdicts and conflict policies are in
[conflicts and refusal](./conflicts-and-refusal.md); the model is in
[memory concepts](../../concepts/memory.md); the server internals are in
[memory architecture](../../architecture/memory-architecture.md).

## Choosing the operation

| You want to                                           | Operation                       | Notes                                                                           |
| ----------------------------------------------------- | ------------------------------- | ------------------------------------------------------------------------------- |
| Save a fact                                           | `memory.set`                    | The default. Idempotent on (identity tuple, `memory_key`).                      |
| Save many facts in one call                           | `memory.bulk_set`               | Up to 1,000 `entries`; per-entry verdicts. Not an MCP tool.                     |
| Let the server decide what a transcript taught        | `memory.set_from_messages`      | See [extraction](./extraction.md).                                              |
| Save a standing fact that later writes cannot replace | `memory.pin`                    | Same input as `memory.set`.                                                     |
| Correct a fact you found through search or ask        | `memory.update`                 | Resolves the key namespace-wide; keeps a pin.                                   |
| Correct one exact row by id                           | `memory.update_by_id`           | Addressed by `(org, memory_id)`.                                                |
| Change only the expiry                                | `memory.touch`                  | One key or up to 1,000 `memory_keys`; value untouched.                          |
| Retire a fact                                         | `memory.delete`                 | Tombstone; `memory.restore` revives it within 14 days.                          |
| Rename a key and keep the old one readable            | `memory.move`                   | `keep_redirect` (default true) leaves a redirect.                               |
| Guard a row against edits or deletion                 | `memory.lock` / `memory.unlock` | Unlock is admin.                                                                |
| Apply one change to many existing rows                | `memory.apply`                  | Dry run by default.                                                             |
| Clear an agent's or a user's rows                     | `memory.clear`                  | Admin; needs `confirm_subject`.                                                 |
| Import a curated Markdown file section by section     | `memory.import_curated`         | One memory per heading at `heading_level` (default 2).                          |
| Import facts another memory system already distilled  | `memory.import`                 | Up to 200 `entries`; see [importing](#importing-from-another-memory-system).    |
| Mark a candidate fact as wrong                        | `memory.reject`                 | Keeps it from being re-extracted.                                               |
| Try a fact out before it is served                    | `memory.set(proposed=true)`     | See [proposals](#proposals).                                                    |
| Store a row with exact typed fields                   | `memory.record.write`           | See [typed records](#typed-records).                                            |
| Store a document                                      | `knowledge.add`                 | Not a memory; see [knowledge](../knowledge.md).                                 |
| Park scratch state (a cursor, a lease)                | `working_memory.set`            | Not embedded or ranked; see [working memory](./working-memory-and-profiles.md). |
| Learn when a namespace's memory changes               | `webhook.register`              | Admin; see [change webhooks](#change-webhooks).                                 |

Each op is `POST /v1/<op>`, an SDK method, and, where exposed, the MCP tool
`loom_<op with dots as underscores>`. The [operations reference](../../reference/operations.md#memory)
lists roles and MCP toolsets.

## Keys and values

- **One fact per key, and a readable key.** Shape it `<scope>/<kind>/<id>`
  (`deploy/window/prod`). The key is how a later write revises the fact and how
  `memory.get` reads it. Search also uses key words as a tie-break (unless
  `LOOM_MEMORY_SEARCH_KEY_MATCH_MODE=off`). `namespace.audit` reports hash or UUID keys (`hash_keys`).
- **A revision rewrites the same key.** The prior version stays in history. A new value
  under a fresh key leaves two live claims for the conflict check to flag.
- **The value is the claim, in plain text.** `value` takes any JSON value, but recall
  embeds only the first 1,200 characters of its text. Put facets in `tags` and named
  things in `entities`.

The write succeeds whatever the shape. `advisories` names a shape recall cannot reach:

| Advisory       | Cause                                                                                    | Fix                                                                   |
| -------------- | ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| base64         | The value is base64-encoded bytes.                                                       | Send the text.                                                        |
| double-encoded | A JSON object serialized inside a JSON string (`json.dumps` twice).                      | Send the object, or a plain-text claim with facets in `tags`.         |
| oversized      | The serialized value is longer than `LOOM_MEMORY_VALUE_ADVISORY_CHARS` (default 30,000). | Write the claim as the value and put the document in `knowledge.add`. |

**Documents go to knowledge.** A value past a couple of kilobytes is usually a document.
Long values are split into verbatim chunks (`chunks_written`): any value whose stored JSON
runs past the 1,200-character excerpt it is embedded by, less its ` (#<key>)` suffix, which
a non-ASCII text reaches at about 200 characters, since each such character is stored as a
six-character escape. A knowledge document is chunked on its headings and searched with its
section. Store the document in [knowledge](../knowledge.md) and one short claim in memory.

**Size limits.** No fixed value cap. The request body is capped by `LOOM_MAX_REQUEST_BYTES`
(32 MiB, 413 `PAYLOAD_TOO_LARGE`). A caller may set `char_limit` on a row (422
`CHAR_LIMIT_EXCEEDED`); it persists for later writes and is not offered to MCP clients.

**Memory types.** `memory_type` (default `semantic`) decides whether a row is embedded and
searched. `episodic` and `profile` rows are not embedded and never appear in
`memory.search`. A `procedural` write that is not a procedure body is stored as
`semantic` (`retyped_from`). A `sensitivity="high"` row, or one detected as high (a
credential, card number, SSN or stated PIN), is never embedded and is readable by key
only. The full table is in [memory concepts](../../concepts/memory.md).

## What a write returns

`memory.set` returns `memory_id`, `version`, `content_sha256` and `status`, plus:

| Field              | Meaning                                                                                                    |
| ------------------ | ---------------------------------------------------------------------------------------------------------- |
| `conflict_verdict` | The verdict ([table](./conflicts-and-refusal.md#verdicts)). Empty when no check ran.                       |
| `peers`            | Rows the verdict was reached against, closest first, at most five: key, 200-character excerpt, similarity. |
| `next`             | A hint naming the call to make, such as `memory.update(memory_key=...)` on a conflict.                     |
| `noop`             | True when nothing was written: a merge or a dry run.                                                       |
| `merged_into_key`  | On a merge, the key that already holds the fact. Your key holds no row.                                    |
| `advisories`       | Value-shape advice.                                                                                        |
| `degraded`         | Providers that fell back during this write.                                                                |

- `dry_run=true` runs the full check and writes nothing: no row, embedding or audit hash.
- A write under a `run_id` that canonically matches a peer from another run is stored as
  a `duplicate`, not merged, so run-scoped reads find it.
- `memory.bulk_set` reports per entry in `written[]` and in the lists `merged`,
  `duplicates`, `conflicted`, `rejected_pinned`, `skipped_pinned` and `skipped_locked`.
  A blacklisted entry is skipped. Entries past `LOOM_BULK_CONFLICT_CHECK_MAX_ENTRIES`
  (200) are written unchecked and counted in `unchecked`.

## The checks, in order

The first refusal ends the call.

1. **Unknown arguments.** An unknown key is named in `ignored_args` (with `did_you_mean`).
   On a write, a close match to a real field is refused with 422 `UNKNOWN_ARGUMENT`, so
   `tag` for `tags` does not store an untagged row.
2. **Guards on the existing row.** A byte-identical value passes and reinforces the row.
   A changed value meets the lock (423 `LOCKED`) and then the pin (409 `PINNED_CONFLICT`).
3. **Value gates.** `char_limit` (422 `CHAR_LIMIT_EXCEEDED`), then the blacklist (422
   `BLACKLISTED`, see [refusal](./conflicts-and-refusal.md#refusal-the-blacklist)).
4. **Content-hash precondition.** `if_match_sha256` (409 `PRECONDITION_FAILED`).
5. **Conflict verdict** against active peers of the same type.

Every write that creates or changes memory leaves one hash-only row in the audit log (key,
id, content hash, actor, source class, verdict; never the value). See
[audit](../observability/audit.md).

## Racing writers: `if_match_sha256`

`memory.set`, `memory.get` and `memory.get_by_id` return `content_sha256`. Pass it back as
`if_match_sha256` and the write succeeds only if the live row still has that content.

```python
from loom import Agent
from loom.errors import PreconditionFailed

async with Agent(org="acme", namespace="support", agent="triage-bot") as agent:
    row = await agent.memory.get("customer/4812/plan")
    try:
        await agent.memory.set(
            "customer/4812/plan",
            "Customer 4812 is on the Enterprise plan since the March renewal.",
            tags=["customer", "billing"],
            if_match_sha256=row["content_sha256"],
        )
    except PreconditionFailed:
        ...  # re-read (the error carries current_sha256), decide, retry
```

`memory.update`, `memory.update_by_id` and `memory.move` (source row) accept the same
precondition. The Python SDK coalesces a burst of `set` calls from one identity into one
`memory.bulk_set` (from the third set inside 200 ms); a coalesced result carries
`coalesced: true` and no `content_sha256`. A set with `if_match_sha256` is never
coalesced.

A `memory.set` or `memory.update` that carries the precondition holds a lock on its key
from the check to the insert, so of two writers holding the same hash only one succeeds.
The lock is a KeeperMap key when the server has a Keeper, and per process otherwise.

## Example

Against the stock stack from the [quickstart](../../getting-started/quickstart.md) (drop
the `Authorization` header in single-user mode):

```bash
curl -sX POST http://localhost:7777/v1/memory.set \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"dev","namespace":"local","agent":"ops-bot",
       "memory_key":"deploy/window/prod",
       "value":"Production deploys run Tuesday and Thursday only; never Friday.",
       "tags":["deploy","policy"],
       "entities":["production"]}' \
  | jq '{conflict_verdict, noop, memory_key, content_sha256, next}'
```

With a real embedder, writing `"Production deploys run Monday to Thursday."` to another
key then answers `conflict_verdict: "conflict"`, `peers[0].memory_key:
"deploy/window/prod"` and a `next` naming `memory.update(memory_key='deploy/window/prod',
...)`. Over MCP the same write is the `loom_memory_set` tool.

## Changing and retiring facts

| Op                    | Behaviour                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.update`       | Resolves `memory_key` across the namespace when no identity is given (`"-"` counts as none, and `"*"` for `agent`). One live row: updated. None: `updated=false, reason="not_found"`. Several: 409 `AMBIGUOUS_KEY` with `candidates`. Runs the conflict check and keeps a pin. An expired row answers 409 `EXPIRED` unless `revive=true`.                                                                                                                              |
| `memory.update_by_id` | One row by `(org, memory_id)`. The namespace is checked after the fetch; a refusal answers `updated=false`, not 403.                                                                                                                                                                                                                                                                                                                                                   |
| `memory.delete`       | Writes a tombstone, looking past the caller's identity for the key (`found=false` when no live row exists). `memory.delete_version` instead reads only the caller's exact identity (a concrete agent) and, under the key's write lock, deletes only the version it names, else `tombstoned=false, reason="version_changed"`. Consolidated summaries lose the row: retired when no source survives, else `conflicted`. `memory.restore` revives the key within 14 days. |
| `memory.move`         | Renames a key, carrying value, type, tags, entities, validity and provenance. The source becomes a redirect (`canonical_key`, `follow=true`) or, with `keep_redirect=false`, a tombstone. See [optimize a namespace](../../guides/optimize-namespace.md).                                                                                                                                                                                                              |
| `memory.lock`         | `read_only` refuses value, expiry and deletion changes; `no_delete` refuses tombstone, redact, clear and consolidation. A writer may lock or tighten (`until`, `ttl_seconds`); `memory.unlock` is admin. `locked=false, reason="not_found"` is not a lock.                                                                                                                                                                                                             |
| `memory.apply`        | A `target` (keys, a filter or a search; filter and search need `limit` ≤ 1,000), an `action` (`delete`, `expire`, `lock`, `label`, `demote`), a `reason`, `dry_run` on by default. Each row goes through the single-row path; refusals come back as `refused` rows. Refuses `agent="*"`.                                                                                                                                                                               |

## Provenance fields

The server stamps `actor` (`human:<subject>` or `process:<subject>`, from the token) and
`source_class` on every row. A request may only downgrade `source_class` to `external`,
`imported` or `derived`; any other value is a 422. `external` matters: `memory.ask` keeps
unattested external rows out of synthesis. `source_resource_id` records the primary
source (a path, document id or URL). The values are in
[memory concepts](../../concepts/memory.md); reads are in
[history and provenance](./history-and-provenance.md).

## Validity time: `valid_at` and `valid_to`

`created_at` is when Loom recorded the row, `valid_at` when the fact became true, and
`valid_to` when it stopped. Default reads drop a row past its `valid_to`. Extraction fills
`valid_at` for past-tense state changes; most writers leave it unset.

`as_of` on `memory.get`, `memory.search` and `memory.ask` replays one of two axes:

| `as_of_basis`          | Returns                                                                                                                      |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `valid_time` (default) | What was true then: `valid_at`, else the event date the row carries, else `created_at`, at or before `as_of`.                |
| `knowledge_time`       | What Loom knew then: `created_at` at or before `as_of`, and not retired (`invalid_at`) or deleted (`tombstoned_at`) by then. |

`memory.timeline` orders by `coalesce(valid_at, created_at)`.

## Tags, entities and files

- `tags` filter recall (OR semantics) and carry per-tag policy from the namespace's
  `tag_registry`. `namespace.taxonomy` lists the namespace's top tags and entities.
- `entities` are stored lowercased, so `entity_filter`, the entity graph and
  `memory.timeline` find the row at once. Without them the `entity_extractor` worker
  fills them later.
- `files` names up to 32 repository files the fact is about; `memory.for_file` serves it
  before an edit. Unset, files are detected from the value.

## Expiry on a write

A row expires only when its writer or the namespace owner asks: `ttl_seconds`,
`valid_to`, or a configured retention window. `on_expire` is `tombstone` (default),
`demote` or `anonymize`. `clear_expiry=true` overrides type and namespace defaults. An
edit to other fields keeps a live expiry. See
[retention and erasure](./retention-and-erasure.md#writer-set-expiry).

## Importing from another memory system

`memory.import` writes up to 200 facts another memory system already distilled. Each
entry is the fact's text plus, optionally, `tags`, `valid_at` (when the source first knew
it), `source_trace` (its id or path there), `category` and `sensitivity`. The write is a
`memory.bulk_set` underneath, so each entry meets the same checks: identity dedup, the
lock and pin guards, the blacklist, the sensitivity rule, inline embedding and the
conflict verdict. Every entry gets the conflict check, whatever
`LOOM_BULK_CONFLICT_CHECK_MAX_ENTRIES` caps a `memory.bulk_set` at. In a namespace on
`newest_wins` that verdict can retire a live memory the imported item updates, as any write
there does; such an entry answers `written` (`refines`). The value-update linker of the
extraction path (`LOOM_MEMORY_EXTRACT_VALUE_UPDATES`) never runs for an import.

- **The key comes from the text:** `import:` and the first 24 hex characters of the SHA-256
  of the text as given, so the same text always lands on the same key. The key is a digest
  of the text, so anyone who can see a key can confirm a guessed text against it: do not
  import a secret as an item.
- **Re-runs write nothing new:** an entry whose key holds its text comes back `skipped`
  (`unchanged`) in any session the token may read (an `own_session` token reads only its
  own, see below), and the stored row keeps the tags, `valid_at`, `source_trace` and
  sensitivity it was written with; an entry naming another `sensitivity` answers `skipped`
  (`sensitivity_kept`), and `memory.set` changes the stored level. The import reinforces
  nothing, unlike a restatement through `memory.set` or `memory.bulk_set`, so re-runs never
  raise a memory's reinforcement; a fact already held under another key comes back `held`.
- **An item edited in the source** has new text and so a new key: the import adds it and
  leaves the old fact as it is (the conflict check may flag the pair), so delete or revise
  the old one yourself.
- **The text is embedded and indexed alone:** a key that is only the digest (`import:` and
  24 lowercase hex characters, nothing else) puts no `(#key)` suffix on the excerpt a row is
  embedded and lexically indexed by, and builds no key-axis vector, so an imported fact
  relates to other memories through its text, as a hand-written one does. A later
  `memory.set` or `memory.bulk_set` of the same key follows the same rule, so its vector
  stays comparable with the imported one; a key of any other shape keeps its suffix. Only
  the first 1,200 characters of an item's JSON encoding are embedded and indexed (a non-
  ASCII character takes six of them, one outside the Basic Multilingual Plane twelve), so
  split a longer item into several.
- **A key that was deleted, retired or rejected stays so:** before the write, the import
  reads each key's newest version in every session of the identity that the token may read
  (only its own session for an `own_session` token), tombstones and rows of every status
  included; the newest version across those sessions decides, whichever session holds it. A
  key its owner deleted comes back `skipped` (`deleted`), one a newer fact retired `skipped`
  (`superseded`) and one marked wrong (`memory.reject`) `skipped` (`rejected`); none is
  written again. A key holding a proposal comes back `skipped` (`proposed`): only
  `memory.proposal_decide` moves a fact out of its trial. A live key holding other text was
  corrected (`memory.set`, `memory.update_by_id`, a pin) or anonymized by an expiry
  (`on_expire=anonymize`) and comes back `skipped` (`revised`), before the bulk write's own
  pin and lock guards. A status the import does not recognise leaves the key as it is too,
  as `skipped` (`unknown`). The read and the write are not atomic: a delete that lands
  between them is written over. Text deleted, retired or rejected under another key is
  imported again under its `import:` key: the bulk write's dedup sees live rows only.
- **A tombstone has no way back through the import:** `memory.delete`, `memory.clear` and
  an expiry with `on_expire=tombstone` all write one, so clearing an agent and importing
  from the source again answers `skipped` (`deleted`) for every such item. Bring the keys
  back with `memory.restore` within 14 days of the delete (a re-import then answers
  `unchanged`), or remove the scope's rows with `erasure.request` and `purge=true`: a key
  with no rows left is imported as new.
- **Provenance:** every row is `source_class=imported`, `valid_at` is the source's time
  and `created_at` the import's. An entry with no `source_trace` is traced by its key.
- **Categories:** a `category` the namespace registered (`namespace.register_category`) is
  linked to the key once the key holds the fact, as `memory.assign_category` links one. A
  link already stored is left as it is, so a re-run does not bring back a link the owner
  rescinded, unless the rescind lands while the import runs. When the category is not
  linked, the result's `note` says why: `category_unregistered` (the namespace has no such
  category), `category_refused` (a token bounded to one user or one session links none,
  since a link has neither axis; it is refused `memory.assign_category` too),
  `category_not_linked` (the key does not hold the fact and has no link) or
  `category_rescinded` (a link the owner rescinded, left so). The note never changes the
  entry's outcome. A live link already stored for the key answers no note, and an entry its
  own request superseded (`newest_wins`) is linked like any written one. What an erasure
  leaves of the links: [retention and erasure](./retention-and-erasure.md#erasure).
- **`dry_run=true`** makes every check and writes no memory row, embedding, category link,
  audit row or usage event. It still pays for the embedding of each entry it sends to the
  bulk write, since the conflict check reads the vectors (and those vectors land in the
  embedding cache, `loom.embedding_cache`), and counts a blacklist pattern's hit. A
  `conflicted` or `refines` entry is previewed before a real write settles its peers, so it
  may land without the flag or the retirement if its peers change first.

Each result carries the entry's `index`, its `key`, an `outcome` and, when the entry was
not plainly written, a `reason`:

| `outcome` | `reason`                                                                                                                                                                                                                                                                                                     |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `written` | Empty, `duplicate` or `conflicted` (the status the conflict check stored the row with), or `refines`: under `newest_wins` the item retired the live memories it updates.                                                                                                                                     |
| `held`    | `merge` (the same fact after canonicalisation) or `identity_duplicate` (the same text under another key, named in `held_by`); nothing is written or reinforced.                                                                                                                                              |
| `skipped` | `unchanged`, `sensitivity_kept`, `deleted`, `superseded`, `rejected`, `proposed`, `revised` (the key holds other text: a correction, or an expiry anonymized it), `repeated` (an earlier entry of the request with the same text was written) or `unknown` (logged: a status the import does not recognise). |
| `refused` | `blacklist`, `pinned` or `locked` (only when the key is pinned or locked between the import's read and its write) or `pinned_peer` (too close to a pinned memory).                                                                                                                                           |

Under `newest_wins`, two entries of one request that state the same attribute differently
keep only the newer: the later `valid_at` (an undated entry counts as said at the time of
the import), then the later in the request. The other still answers `written`, and its row
is stored `superseded`.

An entry with the same text as an earlier one must be identical to it (otherwise the
request is a 422 naming both indexes), and answers as that entry did, except after a
write, when it is `skipped` (`repeated`).

`totals` counts the outcomes, and `dry_run` echoes whether the request was a dry run. A request
with more than 200 entries is refused with 422, and each call draws from its own
[rate-limit class](../../reference/http-api.md#rate-limits), `memory.import`. The op is
listed on no MCP toolset: an MCP client calls `loom_memory_import` by name. Fields:
[operations reference](../../reference/operations/memory.md#memoryimport).

## Proposals

`memory.set(proposed=true)` writes a fact on trial (`status='proposed'`). It is served
only to reads that pass `include_proposed`, and never retires, supersedes or flags an
established fact. The `relations` it names stay out of the fact graph, and accepting it
does not add them: state them again once it is established. A key that already holds a fact answers 409 `KEY_HOLDS_FACT`.
`memory.proposals` lists proposals with the graded runs that relied on each, and
`memory.proposal_decide` accepts or rejects them, by key: when several writers of the
namespace (agents, users or sessions) proposed the same key, one decision covers each of
their proposals. Loom decides only when the owner set
`proposal_policy.mode` to `auto` (`min_passes`, `max_fails`, `reject_after_fails`). See
[runs](../runs.md).

### An agent proposes, a person approves

The flow for a fact an agent should not make live on its own:

1. The agent writes it with `memory.set(proposed=true)`. Only reads that pass
   `include_proposed` see it; a run that relies on it names it in `memories_used`, and
   its grades (`run.grade`) are its trial.
2. The dashboard's **Review queue** (`/admin/review_queue`) shows a **Proposals** tab while
   one waits; for an admin of the namespace the drawer lists the queue then too. Each
   proposal shows its key, value, the agent that wrote it, the graded runs that relied on
   it and what the policy thresholds would decide.
3. A writer accepts it (it becomes `active`, or `conflicted` when it was contradicted on
   trial) or rejects it (`rejected`), optionally with a reason. A viewer sees the list
   without the buttons. The same decision is `memory.proposal_decide` for a script or
   another agent.

Knowledge has review steps of its own. A fact several writers back, or a rule a bot stated
about itself, waits as a learning and is published to the namespace's knowledge only when
a person promotes it, from the same page's **Learnings** tab (`learnings.review`;
[procedures and learnings](./procedures-and-learnings.md#learnings-review-queue)). A table
an ingested file held is served as a dataset only once a writer approves it, from the
Knowledge view (`knowledge.dataset_approve`; [datasets](../knowledge.md#datasets)).

## Typed records

Records are rows with exact fields, for data that has identity (a ticket, a customer).
They answer predicate questions without parsing text.

| Operation                 | Role   | Does                                                                                  |
| ------------------------- | ------ | ------------------------------------------------------------------------------------- |
| `memory_schema.register`  | admin  | Register a schema: up to 64 `fields`.                                                 |
| `memory_schema.propose`   | writer | Suggest a schema (`status='proposed'`) for an admin to accept.                        |
| `memory_schema.approve`   | admin  | `decision` `approve` or `reject`.                                                     |
| `memory_schema.list`      | viewer | Schemas of the namespace.                                                             |
| `memory.record.write`     | writer | Write one record; constraint violations are rejected.                                 |
| `memory.record.query`     | viewer | Filter by field values and typed predicates, optionally `as_of`; rows carry `status`. |
| `memory.record.history`   | viewer | Every version of one record with its `status`, newest first.                          |
| `memory.record.conflicts` | viewer | Field values that changed when a record superseded another.                           |
| `memory.aggregate`        | viewer | `count`, `sum`, `avg`, `min`, `max` over records, with `group_by`.                    |

- A field has a `type` (`string`, `number`, `integer`, `boolean`) and may be `required`,
  an `identity` field, an enum (`enum_values`), or constrained (`pattern`, `min_length`,
  `max_length`, `min_value`, `max_value`).
- Identity fields derive the key when none is given (`key_source="identity"`), so two
  writes of one entity land on one record. A name-like field (`name`, `title`, `label`,
  `*_name`, `*_title`, `*_label`) keys on its words without case, a leading article or a
  possessive; it drops accents and the other combining marks (the Japanese voicing mark,
  the virama, Thai tone marks; Indic vowel signs stay), so `かぎ` and `かき` share a name key.
  It is refused only when nothing is left once separators (spaces, `_`, `-`), combining
  marks, a leading article and a possessive are dropped. Any other field keys on its
  letters and digits a-z, 0-9 after case folding and compatibility decomposition with
  accents stripped, so `INV-2024/001` and `inv 2024 001` agree. A value with none of those
  (Cyrillic, CJK or Arabic text) keys on every letter, digit and combining mark it holds,
  after NFKC normalisation and case folding. No mark is stripped there: Indic vowel signs,
  Thai tone marks, the Japanese voicing mark and Arabic and Hebrew points tell words
  apart, so `かぎ` and `かき` get two keys, as do a word written with and without its Arabic
  vowel points. A value with any a-z or 0-9 after compatibility decomposition (`30 дней`,
  `2024年度`, `№`, `℃`) keys on those characters alone, so two values that differ only in
  their other characters share one key. Any other field with no letter or digit
  (punctuation, symbols or marks alone) is refused with 400 `MISSING_IDENTITY_FIELD`.
- Each field value carries a `state` (`value`, `unknown`, `rejected`, `absent`).
- `memory.set_from_messages` with `record_schema` and `memory_key` extracts one record.

## Change webhooks

A product built on Loom (a chat service that shows a user what the assistant remembers,
say) can be told when memory in a namespace changes instead of polling for it. A
namespace admin registers a URL with `webhook.register`; Loom then POSTs one signed event
per change. The event carries ids, the memory key and the identity tuple, never a value,
tags or evidence: the receiver reads the memory back with its own token
(`memory.get_by_id`), so what it may read is decided when it reads, as for any other read.
A key reaches every registered receiver, including one whose token may not read the row,
so a secret or a fact never goes in a key. Copies into the namespace (`memory.copy_to`,
`memory.cross_promote`) and `namespace.restore` from a snapshot send no event: after one,
the receiver reads the namespace again. The full list of writes that send none is below
the table.

| Event               | Sent after                                                                                                                                                                                                                                                                                                                                                                                                                  |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.written`    | `memory.set` or `memory.bulk_set` writes a key that held no live row, or a proposal is accepted                                                                                                                                                                                                                                                                                                                             |
| `memory.revised`    | a new value of a live key (for `memory.set`, also a new type, tags, expiry, confidence, `valid_at`, `valid_to`, sensitivity, files or source): `memory.set`, `memory.bulk_set`, `memory.update`, `memory.update_by_id`, `namespace.rollback`, the source of `memory.move` with `keep_redirect`, an expiry that demotes or anonymizes, a completed run's retention (`demote` or `expire`), the sensitivity sweep's new label |
| `memory.tombstoned` | `memory.delete`, `memory.delete_version`, `memory.clear` (one per row it clears), `conflicts.resolve` and `memory.dedup_resolve` for the side they tombstone, `blacklist.add` and `blacklist.sweep` for each live row the sweep tombstones, `namespace.rollback` back to a tombstone or removing a key born later, the source of `memory.move` without `keep_redirect`, an expiry that tombstones                           |
| `memory.restored`   | `memory.restore`, or `namespace.rollback` reviving a key that is tombstoned now                                                                                                                                                                                                                                                                                                                                             |
| `memory.redacted`   | `memory.redact` scrubs a historical version, and once for each profile key with versions built from that memory, naming its newest scrubbed version                                                                                                                                                                                                                                                                         |
| `memory.erased`     | an erasure saga completes (`erasure.request`, `collection.delete`, `code.repo.remove`)                                                                                                                                                                                                                                                                                                                                      |

Ops that write through `memory.set` (procedures, profiles, learnings, decisions) send its
events. A `memory.set` of the same value with the same type, tags, expiry, confidence,
`valid_at`, `valid_to`, sensitivity, `files`, `source_resource_id` and `source_class` (a
restatement, which only reinforces the row)
sends none; the same value with any of those changed is `memory.revised`. A retry with the
same fields is a restatement and sends nothing; a retry that recomputes a relative expiry
(`ttl_seconds`, a type's retention, the sensitive-data retention) moves `expires_at` and
is `memory.revised`. `memory.bulk_set` skips any entry whose value equals the live row's:
it writes no version and sends no event, even when the entry's tags, expiry or confidence
differ. In one `memory.bulk_set`, a key that an earlier entry of the batch wrote is
`memory.revised`; if the batch's lookup of live keys fails, its revisions are announced as
`memory.written`. A merge (`merged_into_key`) writes no version at your key and sends no
event; neither does a dry run or a refused write.

These writes send no event:

- the summaries `memory.delete` retires with the row they were built from
- `memory.consolidate` and the consolidation worker
- observations and their distillation
- period digests
- `memory.touch`, `memory.pin` and `memory.unpin`, `memory.lock` and `memory.unlock`,
  `memory.verify` and `memory.apply`
- `memory.reject`, and the quarantine `memory.outcome` makes after repeated negative
  outcomes. Both write the row again with status `rejected`, which takes it out of search:
  a receiver that mirrors what Loom serves reads the key again after one.
- `memory.resolve_orphan_duplicates`, which re-activates rows
- `memory.dedup_collapse`, which changes only the search mirror
- `memory.promote` and `memory.demote`, which change the namespace's shared copy, not the
  memory row
- the counters of `memory.outcome` and `memory.outcome_run`
- `memory.assign_category`, `memory.link` and `memory.reindex`
- a proposal (`memory.set` with `proposed`, which is not served until it is accepted) and
  its rejection
- the status change `conflicts.resolve` and `memory.dedup_resolve` make on the rows they keep
- the supersessions a write makes of other rows: the older same-key rows the `newest_wins`
  conflict policy retires, and the peers a `refines` verdict supersedes. The new row's own
  event is sent.
- what the conflict resolver lands later: the rows it retires (`superseded`, under another
  key too), and the `conflicted` flags it sets or clears. The write's own event was sent
  before.
- the `conflicted` and `duplicate` status the write's own check lands. The write's own event
  is sent.
- the undo of an automatic supersession through `memory.restore`
- copies into another namespace (`memory.copy_to`, `memory.cross_promote`)
- `namespace.restore` from a snapshot
- entity backfills
- typed records (`memory.record.*`) and saved chat messages (`conversation.*`). Neither is
  a memory row.

A receiver that mirrors what Loom serves reads `memory.auto_retirements` for the rows the
supersessions above retired: a write's own and the conflict resolver's.

Accepting a proposal, by `memory.proposal_decide` or the namespace's `auto` proposal
policy, sends `memory.written`.

```json
{
  "type": "memory.revised",
  "event_id": "5f0c…",
  "org": "acme",
  "namespace": "chat",
  "agent": "assistant",
  "user_id": "u-42",
  "session_id": "-",
  "memory_key": "user/pref/tea",
  "memory_id": "0b7e…",
  "version": 1790000000000000000,
  "actor": "human:alice",
  "request_id": "r-91c2…",
  "erasure_id": "",
  "occurred_at": "2026-09-20T12:00:00.000Z"
}
```

`memory.erased` carries the `erasure_id` the request returned and no key, user,
identity or requester (`actor` is `-`, `request_id` empty): a namespace erasure goes to that namespace's webhooks, a user's erasure to
every webhook of the org that takes the event, including namespaces that never held
the user. `erasure.status` of a user's erasure answers only a flat org admin or
platform admin whose token names the org, so a receiver's token cannot read the user
id from it.

| Operation            | Role  | What it does                                                                                                                                                                                                                                                                             |
| -------------------- | ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `webhook.register`   | admin | Registers `url`, `secret_ref` and `events` (default all six); the same URL again replaces it. At most `LOOM_MEMORY_WEBHOOK_MAX_PER_NAMESPACE` per namespace (10 by default): two registrations of one new URL at the same moment can make two webhooks, one past the cap; remove either. |
| `webhook.list`       | admin | The namespace's webhooks, with `secret_present` (whether the replica that answered holds a usable secret).                                                                                                                                                                               |
| `webhook.remove`     | admin | Removes one by `webhook_id`; none of its events is sent after the call, save a POST already in flight.                                                                                                                                                                                   |
| `webhook.deliveries` | admin | The last 3 days of events by state: `pending`, `delivered`, `dead`, with the last HTTP status or error.                                                                                                                                                                                  |

**Signing.** Each request carries the Standard Webhooks headers the task inbox webhook
uses: `webhook-id` (the event id; drop a repeat), `webhook-timestamp` (unix seconds;
refuse one more than a few minutes old to bound replay) and
`webhook-signature: v1,<base64 HMAC-SHA256>` over `"{id}.{timestamp}.{body}"`. The secret
lives only in the server's environment: `secret_ref` names the variable, one of the
org's own, `LOOM_WEBHOOK_SECRET_<ORG>__<NAME>`, encoded as for
[task inbox webhooks](../tasks-and-decisions.md); a `whsec_` prefix marks a base64 key,
whose line breaks and other whitespace are dropped.
A secret belongs to the org, not to one webhook: every webhook that names the same
`secret_ref` signs with it, so a receiver checks the signed body's `org` and `namespace`
before acting on an event.

**URLs.** `https` only, unless `LOOM_MEMORY_WEBHOOK_ALLOW_HTTP` is set (a local receiver
in development), and with no user name or password in it. The host must resolve to
public addresses: loopback, private, link-local (cloud metadata) and multicast targets
are refused at registration and at every delivery, unless
`LOOM_MEMORY_WEBHOOK_PRIVATE_HOSTS` names the host. Each delivery connects to the address
it checked and follows no redirect.

**Delivery.** The write leaves one row per event and webhook in
`loom.memory_webhook_outbox` and makes no network call; the `memory_webhook_deliver`
worker sends it. Any 2xx within 10 s (`LOOM_MEMORY_WEBHOOK_ACK_TIMEOUT_SECONDS`)
acknowledges it; the 10 s cover the name lookup, the connection and the status line, and
the answer's body is never read. Anything else is retried after 5 s, 30 s, 2 min, 10 min
and 30 min (`LOOM_MEMORY_WEBHOOK_BACKOFF_STEPS_SECONDS`) and then hourly
(`LOOM_MEMORY_WEBHOOK_MAX_BACKOFF_SECONDS`, which also caps the earlier waits), until
`LOOM_MEMORY_WEBHOOK_MAX_ATTEMPTS` attempts (60 by default, about 2.3 days of retries)
mark it `dead`, and any event a receiver has not taken 2.75 days after it happened is
marked `dead` with "not delivered in time". `actor` is the principal that
made the change, or `worker:<name>` for a change a background pass made (the expiry
sweep, the `delete_version` check).

- **Fairness.** A pass takes events in turns: every webhook's oldest due event, then
  every webhook's second, at most 10 of one webhook
  (`LOOM_MEMORY_WEBHOOK_POSTS_PER_WEBHOOK`), sent one after another, so a receiver that
  answers in 1 s takes about 10 events per pass with the defaults. A pass sends at most
  40 in all (`LOOM_MEMORY_WEBHOOK_MAX_POSTS_PER_PASS`), 4 at a time
  (`LOOM_MEMORY_WEBHOOK_DELIVER_CONCURRENCY`) and one at a time per webhook, so a slow
  receiver holds at most one of the 4. While fewer than 40 webhooks have work, every
  webhook with work gets its first POST of a pass before any webhook gets a second. A
  webhook whose last 5 POSTs failed (`LOOM_MEMORY_WEBHOOK_FAILURES_BEFORE_COOLDOWN`) is
  passed over for a cooldown that starts at a minute
  (`LOOM_MEMORY_WEBHOOK_COOLDOWN_SECONDS`) and doubles up to an hour
  (`LOOM_MEMORY_WEBHOOK_MAX_COOLDOWN_SECONDS`), checked before each POST; a 2xx ends it.
  The cooldown is kept per server process, so a restart or a new worker leader starts it
  afresh. A pass POSTs until every POST could have waited its whole ack deadline, for
  the larger of an even share of the pass (40 / 4 = 10 POSTs per slot) and one webhook's
  whole turn (10 POSTs one after another): max(40 / 4, 10) × 10 s = 100 s after the pass
  starts, and at the latest until
  `LOOM_MEMORY_WEBHOOK_DELIVER_RESERVE_SECONDS` (20 s) before its deadline
  (`LOOM_MEMORY_WEBHOOK_DELIVER_MAX_PASS_SECONDS`, 120 s); the reserve covers the one
  insert after the POSTs. At that point the pass cancels its POSTs still in flight: a
  receiver may already have handled one, but no outcome is recorded, so its event keeps
  the lease and is sent again under the same `webhook-id` once the lease ends. The reads before the POSTs come out of the POST budget, which
  counts from the start of the pass, so when the due-event read grows with the outbox
  backlog, raise `LOOM_MEMORY_WEBHOOK_DELIVER_MAX_PASS_SECONDS`. What a pass did not send
  goes next pass. With fast receivers the ceiling is about 40 events per pass, a pass
  every 5 s while there is work: some 8 events a second per deployment with the
  defaults. A deployment that needs more raises the per-pass and concurrency settings,
  and the pass deadline with them.
- **Waiting and dead events.** While the variable `secret_ref` names is unset or not
  usable (a `whsec_` value that is empty or not base64), the webhook's events wait, and the wait
  counts as no attempt; after 2.5 days they are marked `dead` with "no usable secret".
  An event of a working webhook that a receiver has not taken after 2.75 days (a
  receiver that is down, or a backlog that never reached its turn) is marked `dead` with
  "not delivered in time".
  A pass checks before each POST that the webhook was not removed since it began, so
  after `webhook.remove` only a POST already in flight goes out.
  The events of a removed webhook are marked `dead` by the next delivery pass that runs
  within about 30 minutes of the removal (`LOOM_MEMORY_WEBHOOK_REMOVAL_WINDOW_LEASES` widens
  it); after that they stay listed as `pending` until they expire. Registering the same
  URL again creates a new webhook that does not inherit them. An event whose memory was
  erased before it went out is marked `dead` and not sent: the key's history is read just
  before each POST, so only an erasure that lands during that POST can let one out.
- **Repeats.** An event is sent until a receiver takes it or it is marked dead (see
  above), and a delivered event can arrive more than once, in no order; a change can also
  go unannounced (a failed outbox insert, a registration not yet seen by the serving
  process, a rolling upgrade), so a receiver that mirrors memory re-reads the namespace
  from time to time. Order events by `version`, and read the row back rather than
  trusting the event's age. Two events can carry the same `version`: a `memory.clear` tombstone does not write a new version, so its
  `memory.tombstoned` event carries the version it tombstoned; on a tie,
  `memory.tombstoned` comes after `memory.written` or `memory.revised`. A
  `memory.redacted` event's `version` names the historical version it scrubbed, not a
  new head, so it does not move the key's order; for a profile key scrubbed with the
  memory it built from, that version can be the head, so read the key back. A repeat shares its
  `webhook-id`: a retry, two overlapping passes, and the same version of a memory
  announced twice to the same webhook (a `delete_version` check that runs again) all
  carry one id, so drop an id you have seen.
- **Keeping.** An outbox insert that fails is logged and that event is not sent; the
  write still stands. A webhook registered on one server process applies to writes on the
  others within `LOOM_MEMORY_WEBHOOK_REFRESH_SECONDS`. Events and their delivery record
  are read for 3 days and leave the disk within about 4: each day's events are dropped
  together once the day's last one is 3 days old. An erasure deletes the events in its scope; their delivery records
  name no key or identity and expire with the rest.

## Settings

| Setting                              | Default | Effect                                                                                                                        |
| ------------------------------------ | ------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_MEMORY_VALUE_ADVISORY_CHARS`   | 30000   | Size past which a value earns the oversized advisory. 0 disables.                                                             |
| `LOOM_MEMORY_SENSITIVITY_AUTODETECT` | true    | Detect credential, card, SSN and PIN shapes (`high`) and email, phone and IBAN shapes (`medium`) when `sensitivity` is unset. |
| `LOOM_MAX_REQUEST_BYTES`             | 32 MiB  | Request-body cap.                                                                                                             |

Change webhook settings (`LOOM_MEMORY_WEBHOOK_*`, including
`LOOM_MEMORY_WEBHOOK_DELIVER_ENABLED`, which stops delivery) are in
[configuration: MemoryWebhook](../../reference/configuration.md#memorywebhook).
Conflict and duplicate settings are in
[conflicts and refusal](./conflicts-and-refusal.md#settings). Per namespace,
`namespace.register` sets `conflict_threshold`, `default_conflict_policy` and
`proposal_policy`. See the [configuration reference](../../reference/configuration.md).

## Limits

- **Writes are eventually consistent and not transactional across tables.** A bulk write
  is not atomic.
- **A merge writes nothing at your key.** Read `merged_into_key`.
- **A `memory.set` that names no `sensitivity` drops the stored label.** Re-writing a
  `high` row without it stores the new version with only the detected level, embedded
  unless that is `high`, so restate `sensitivity` on a re-write. A `memory.bulk_set`
  entry that names none and repeats the stored value keeps its label.
- **Fake providers give string-similarity verdicts.** Judge verdicts only with real
  providers.
- **A misspelled argument is refused** (422 `UNKNOWN_ARGUMENT`); an unknown one with no
  close match is served and named in `ignored_args`.
- Error codes are in the [errors reference](../../reference/errors.md).

## Related

- [Search and ask](./search-and-ask.md)
- [Conflicts and refusal](./conflicts-and-refusal.md)
- [Extraction](./extraction.md)
- [History and provenance](./history-and-provenance.md)
- [Investigation agent memory](../../guides/investigation-agent-memory.md): a worked key
  and value shape.
