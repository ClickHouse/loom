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
| Mark a candidate fact as wrong                        | `memory.reject`                 | Keeps it from being re-extracted.                                               |
| Try a fact out before it is served                    | `memory.set(proposed=true)`     | See [proposals](#proposals).                                                    |
| Store a row with exact typed fields                   | `memory.record.write`           | See [typed records](#typed-records).                                            |
| Store a document                                      | `knowledge.add`                 | Not a memory; see [knowledge](../knowledge.md).                                 |
| Park scratch state (a cursor, a lease)                | `working_memory.set`            | Not embedded or ranked; see [working memory](./working-memory-and-profiles.md). |

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
Long values are split into verbatim chunks (`chunks_written`), but a knowledge document
is chunked on its headings and searched with its section. Store the document in
[knowledge](../knowledge.md) and one short claim in memory.

**Size limits.** No fixed value cap. The request body is capped by `LOOM_MAX_REQUEST_BYTES`
(32 MiB, 413 `PAYLOAD_TOO_LARGE`). A caller may set `char_limit` on a row (422
`CHAR_LIMIT_EXCEEDED`); it persists for later writes and is not offered to MCP clients.

**Memory types.** `memory_type` (default `semantic`) decides whether a row is embedded and
searched. `episodic` and `profile` rows are not embedded and never appear in
`memory.search`. A `procedural` write that is not a procedure body is stored as
`semantic` (`retyped_from`). A `sensitivity="high"` row, or one detected as a credential,
is never embedded and is readable by key only. The full table is in
[memory concepts](../../concepts/memory.md).

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
| `memory.update`       | Resolves `memory_key` across the namespace when no identity is given. One live row: updated. None: `updated=false, reason="not_found"`. Several: 409 `AMBIGUOUS_KEY` with `candidates`. Runs the conflict check and keeps a pin. An expired row answers 409 `EXPIRED` unless `revive=true`.                                                                                                                                                                            |
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

## Proposals

`memory.set(proposed=true)` writes a fact on trial (`status='proposed'`). It is served
only to reads that pass `include_proposed`, and never retires, supersedes or flags an
established fact. The `relations` it names stay out of the fact graph, and accepting it
does not add them: state them again once it is established. A key that already holds a fact answers 409 `KEY_HOLDS_FACT`.
`memory.proposals` lists proposals with the graded runs that relied on each, and
`memory.proposal_decide` accepts or rejects them. Loom decides only when the owner set
`proposal_policy.mode` to `auto` (`min_passes`, `max_fails`, `reject_after_fails`). See
[runs](../runs.md).

## Typed records

Records are rows with exact fields, for data that has identity (a ticket, a customer).
They answer predicate questions without parsing text.

| Operation                 | Role   | Does                                                               |
| ------------------------- | ------ | ------------------------------------------------------------------ |
| `memory_schema.register`  | admin  | Register a schema: up to 64 `fields`.                              |
| `memory_schema.propose`   | writer | Suggest a schema (`status='proposed'`) for an admin to accept.     |
| `memory_schema.approve`   | admin  | `decision` `approve` or `reject`.                                  |
| `memory_schema.list`      | viewer | Schemas of the namespace.                                          |
| `memory.record.write`     | writer | Write one record; constraint violations are rejected.              |
| `memory.record.query`     | viewer | Filter by field values and typed predicates, optionally `as_of`.   |
| `memory.record.history`   | viewer | Every version of one record, newest first.                         |
| `memory.record.conflicts` | viewer | Field values that changed when a record superseded another.        |
| `memory.aggregate`        | viewer | `count`, `sum`, `avg`, `min`, `max` over records, with `group_by`. |

- A field has a `type` (`string`, `number`, `integer`, `boolean`) and may be `required`,
  an `identity` field, an enum (`enum_values`), or constrained (`pattern`, `min_length`,
  `max_length`, `min_value`, `max_value`).
- Identity fields derive the key when none is given (`key_source="identity"`), so two
  writes of one entity land on one record.
- Each field value carries a `state` (`value`, `unknown`, `rejected`, `absent`).
- `memory.set_from_messages` with `record_schema` and `memory_key` extracts one record.

## Settings

| Setting                              | Default | Effect                                                            |
| ------------------------------------ | ------- | ----------------------------------------------------------------- |
| `LOOM_MEMORY_VALUE_ADVISORY_CHARS`   | 30000   | Size past which a value earns the oversized advisory. 0 disables. |
| `LOOM_MEMORY_SENSITIVITY_AUTODETECT` | true    | Detect credential and contact shapes when `sensitivity` is unset. |
| `LOOM_MAX_REQUEST_BYTES`             | 32 MiB  | Request-body cap.                                                 |

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
