# Working memory and profiles

Two planes hold text an agent is given every time instead of finding it by search:

- **Working memory**: small named blocks for leases, cursors, checkpoints and "where was
  I". Blocks are not embedded, ranked or searched.
- **Profiles**: always-loaded context about the user or the agent (persona, preferences,
  focus), rendered as one prompt fragment.

Use `memory.set` for anything a later session should find by asking. Use these planes for
what it should be told without asking. A scratchpad line written as a fact competes with
real facts in every search and conflict check.

## Working memory

A block lives in `loom.working_memory`, keyed by the identity tuple plus `run_id` and
`block_key` (default `scratchpad`, at most 80 characters). Blocks survive restarts and
have no TTL, so they work as a resume point for a long run
([trace and resume](../../guides/trace-and-resume.md)). A block leaves only when its
writer deletes it or an erasure covers it.

| Op                       | Role   | Does                                                                            |
| ------------------------ | ------ | ------------------------------------------------------------------------------- |
| `working_memory.set`     | writer | Replace a block's content; `char_limit` is enforced.                            |
| `working_memory.append`  | writer | Append `addition` with `separator` (default newline).                           |
| `working_memory.replace` | writer | Replace every occurrence of `old` with `new` inside a block.                    |
| `working_memory.delete`  | writer | Tombstone a block.                                                              |
| `working_memory.get`     | viewer | Read one block, with `fill_ratio`.                                              |
| `working_memory.render`  | viewer | Render the blocks in scope (or up to 20 named `block_keys`) as XML or Markdown. |

Field lists: [operations reference](../../reference/operations.md#working_memory).

### Size limits

| Limit                | Value                                                                     |
| -------------------- | ------------------------------------------------------------------------- |
| Default `char_limit` | 4,096 characters per block                                                |
| Largest `char_limit` | 16,384 characters                                                         |
| Over the limit       | 422 `WORKING_MEMORY_LIMIT_EXCEEDED`, with `value_length` and `char_limit` |
| Blocks per render    | `limit` 1–100 (default 20)                                                |

Every write returns `value_length` and `fill_ratio` (`len(content) / char_limit`), so a
caller can trim before a write is refused. A `replace` whose `old` text is absent answers
422 `SUBSTRING_NOT_FOUND`; one on a missing block answers
`WORKING_MEMORY_BLOCK_NOT_FOUND`. `working_memory.get` on a missing block answers
`found=false`.

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    await bot.working_memory.set(
        "cursor: ticket 4812\nlease: holds refunds queue", block_key="state", run_id="run-7"
    )
    await bot.working_memory.append("step 3 done", block_key="state", run_id="run-7")
    print(await bot.working_memory.render(run_id="run-7"))
    # <working-memory block="state">cursor: ticket 4812 ...</working-memory>
```

## Profiles

A profile section is a memory row with `memory_type='profile'` whose key is the section
`label`. `profile.set`, `profile.append` and `profile.replace` are thin wrappers over
`memory.set` that fix that type and a `char_limit`. Reading one section is `memory.get`
with the label as key; the Python SDK wraps this as `agent.profile.get(label)`,
`agent.profile.list()` and `agent.profile.delete(label)` (SDK methods, not wire ops).

| Op                 | Role   | Does                                                                                         |
| ------------------ | ------ | -------------------------------------------------------------------------------------------- |
| `profile.set`      | writer | Create or replace the section at `label` (`if_match_sha256` for racing writers).             |
| `profile.append`   | writer | Append to a section; retries with compare-and-swap when another client wrote first.          |
| `profile.replace`  | writer | Rewrite a substring inside a section (422 `SUBSTRING_NOT_FOUND` when absent).                |
| `profile.render`   | viewer | Render every section (or the named `labels`, in that order) as one XML or Markdown fragment. |
| `profile.activity` | viewer | Which clients read the profile, and when (`lookback_days`, capped at 30).                    |

Field lists: [operations reference](../../reference/operations.md#profile).

- **Limit.** Default `char_limit` 8,192 characters, measured on the value's canonical
  JSON; longer answers 422 `CHAR_LIMIT_EXCEEDED`.
- **Scope.** `profile.render` reads sections from every agent in the namespace by default
  (`scope="namespace"`), so a persona written in the dashboard reaches every MCP client.
  `scope="this_agent"` keeps to the caller's own. Under `read_scope` `own_user` or
  `own_session`, only that user's (or session's) sections and the namespace-level ones
  (`user_id` `-`) render.
- **Search.** Profile rows stay out of `memory.search`. The `loom_search` tool has a
  profile leg that scores sections by meaning when a question is about the user; each
  section's vector is refreshed off the request path after every write. See
  [unified search](../unified-search.md).
- **Folding facts into a profile.** The `profile_consolidation` worker (off by default,
  `LOOM_PROFILE_CONSOLIDATION_ENABLED`) folds a user's preference and decision memories
  into a versioned section (label `consolidated_profile`) with a model call. Rows tagged
  with a protected attribute are left out. Its provenance is `derived`
  (`source_trace` `profile.fold:<label>:<memory ids>`, the facts it read).

```python
async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    await bot.profile.set("persona", "Terse. Cite ticket ids. Never promise refunds.")
    await bot.profile.append("preferences", "Prefers EUR invoices.", user_id="u-42")
    system = await bot.profile.render(user_id="u-42", read_scope="own_user")
```

## Structured profiles

A chat bot that must know its user before the first reply reads one object in a shape
its operator defines, instead of free text. An admin gives the namespace a profile
schema; the `structured_profile` worker keeps each user's profile current in that shape
from the user's memories; `profile.structured` returns it with its status. A read never
calls a model.

| Op                     | Role   | Does                                                                                                                                         |
| ---------------------- | ------ | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `profile_schema.set`   | admin  | Set the namespace's schema (`json_schema`); audited. Replaces the old one.                                                                   |
| `profile_schema.get`   | viewer | The schema, its `schema_sha256`, who set it and when.                                                                                        |
| `profile_schema.clear` | admin  | Remove the schema; audited. Built profiles stay as memory rows.                                                                              |
| `profile.structured`   | viewer | One user's profile: `profile`, `status`, `built_at`, `memory_id`, `version` (and `version_text`, the same as a string, exact in JavaScript). |

The schema is a JSON Schema object with 1 to 64 `properties`, at most 16 KiB. Each
property has a `type` (`string`, `number`, `integer`, `boolean`, or `array` with `items`
of one of those and an optional `maxItems` up to 50) and a `description`, which the
model reads. A full answer holds at most 256 values, counting one per property and
`maxItems` per array, so it fits the model's output cap. Strings take `enum`, `pattern`, `minLength` and `maxLength`; numbers take
`minimum` and `maximum`, the constraints [typed records](./writing.md#typed-records) use.
Every property is in every profile, `null` when the memories do not settle it, so the
schema has no `required`; `additionalProperties` may only be `false`. A `title` is
accepted at any level and ignored.

```python
schema = {
    "type": "object",
    "properties": {
        "preferred_name": {"type": "string", "description": "What the user asks to be called"},
        "timezone": {"type": "string", "description": "IANA time zone, e.g. Europe/Berlin"},
        "languages": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Languages they speak",
        },
    },
}
async with Agent(org="acme", namespace="support", agent="chat", api_key=ADMIN_TOKEN) as admin:
    await admin.client.profile_schema.set({"json_schema": schema})

async with Agent(org="acme", namespace="support", agent="chat") as bot:
    out = await bot.client.profile.structured({"user_id": "u-42"})
    # {"status": "ready", "profile": {"preferred_name": "Sam", "timezone": "Europe/Berlin",
    #  "languages": ["en", "de"]}, "built_at": "...", ...}
```

| `status`    | Means                                                                                                                                                                                                 |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ready`     | Built from the user's current memories under the current schema; all `null` for a user with no facts.                                                                                                 |
| `stale`     | A memory or the schema changed since it was built; served as built until a pass rebuilds it.                                                                                                          |
| `pending`   | No live profile: not built yet, or the built row expired, was redacted (until the worker rebuilds it) or is labelled `high` (held until the key moves to a new version); every property reads `null`. |
| `no_schema` | The namespace has no schema, or its stored one no longer parses; `profile` is `{}`.                                                                                                                   |

- **Storage.** The profile is the user's `profile` row with key `structured_profile`
  (`agent` `-`), so `profile.render` and `context.pack` carry it too, and
  [`memory.history`](./history-and-provenance.md#versions-memoryhistory) lists its versions.
  Its provenance is `derived`; `source_trace` is
  `profile.fold:structured_profile:<memory ids>`, the memories it was built from. Only a
  row the worker wrote (its `actor`) is served: a row a caller writes to the key reads as
  no profile (`pending`), whatever its tags, and the worker rebuilds over it unless it is
  pinned, locked or labelled `high`.
- **Sources.** The user's current `semantic`, `preference` and `decision` facts, under the
  same filter as the free-text fold: never a `high` row, a duplicate (its canonical row
  counts), a fact about someone else (`about:other`), a protected attribute or a stored
  request to forget. Each fact is dated by when it became true (`valid_at`), else when it last
  changed, and a build reads the most confident and most recently changed first, so an
  edit is read as the newest statement.
- **Staleness.** Each profile carries the hash of its schema's properties (a `title` or
  the properties' order changes none), a digest of its sources (each one's id, content
  hash, confidence, and the date a build reads and orders by: `valid_at`, else the last
  change) and their count. A new or edited fact, a revert, a restore, a deleted fact, a
  changed confidence or date, and a change that moves a row into or out of the sources
  (it turns `high`, changes type, gains or loses an `about:other`, `attribute:` or
  `forget` tag, expires or passes its `valid_to`) changes the digest. A restatement or a
  reinforcement does too, since it moves the fact to the newest place in what the model
  reads: it costs one rebuild after the cooldown. So does a `memory.lock` or
  `memory.unlock` of a source fact: it writes a new version with the same content, which
  dates the fact at the lock and moves it to the newest place in what the model reads
  and into the newest of the facts a build reads. Of two facts that disagree under
  different keys, the one locked or unlocked last then reads as the most recent. A recall
  or a backfill, which keeps the row's last change, does not. The digest covers every source,
  while a build reads only the `LOOM_STRUCTURED_PROFILE_MEMORY_LIMIT` (100) most confident
  and newest: for a user with more facts than that, a change to one outside that set still
  costs a model call that cannot change the answer. A schema change is rebuilt from the
  next pass on, at most `LOOM_STRUCTURED_PROFILE_MAX_USERS_PER_PASS` profiles a pass. A
  deletion (fewer sources than the profile was built from) skips the cooldown and is
  rebuilt once the namespace is next scanned, at most
  `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS` (600) later while passes have budget
  left; `profile.structured` reports it `stale` at once. Other changes are rebuilt after
  `LOOM_STRUCTURED_PROFILE_COOLDOWN_SECONDS` (600). A user
  whose facts are all gone gets an all-`null` profile without a model call. An expired
  or redacted profile row reads as no profile (`pending`) and is rebuilt. A `high`
  profile row also reads as no profile, but the worker leaves it as it is: it holds
  like a pin until the key moves to a new version.
- **Output cap.** The model may write the `structured_profile` entry of `LLM_MAX_TOKENS`
  (32, changed through `LOOM_LLM_MAX_TOKENS_OVERRIDES`) plus the schema's estimate of a
  full answer: each key with its punctuation, and each value (one per property, `maxItems`
  per array) sized by its `maxLength` or its longest `enum` value at two characters a
  token, 48 tokens for a string with neither, 12 for a number or a boolean.
  `profile_schema.set` refuses a schema whose estimate passes 16,384 tokens. A string
  longer than its estimate can still be cut off; give a free-text property a `maxLength`
  to make room for it.
- **Validation.** The model's answer is held to the schema before it is written: a value
  of the wrong type, outside its enum or constraints, a number that is not finite (or an
  integer past 2^53), or a property the schema does not declare is dropped (`null`).
- **Passes.** A pass builds at most `LOOM_STRUCTURED_PROFILE_MAX_USERS_PER_PASS` (20)
  profiles. It reaches the namespaces with a schema in order from where the last pass
  stopped, and each gets an equal share. A namespace with nothing to build spends none,
  so the pass reads on to the next namespaces until its budget is spent or it has
  reached them all; what a namespace leaves goes to the namespaces that used their whole
  share, from the users their scan already listed. A pass may run
  `LOOM_STRUCTURED_PROFILE_MAX_PASS_SECONDS` (600) before the supervisor abandons it, and
  takes no user in the second half of that time: the build in flight has the second half
  to finish, and the users not reached stay listed for the next pass. One scan of a
  namespace lists up to ten times its share, and the next passes take from that list
  until it is drained, the schema changes, or it is
  `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS` (600) old; a scan that found every user
  behind (fewer than it asked for) is not repeated before then once drained, so a
  namespace is scanned at most once per that age while nobody else is behind. A user
  whose build failed (a model error, an answer that is not a JSON object) waits
  `LOOM_STRUCTURED_PROFILE_BACKOFF_BASE_SECONDS` (300), doubling up to
  `LOOM_STRUCTURED_PROFILE_BACKOFF_CAP_SECONDS` (21600). It is left out of the namespace
  scans made while it waits, so it is tried again at the first scan after the wait ends:
  at most the wait plus `LOOM_STRUCTURED_PROFILE_SCAN_MAX_AGE_SECONDS` (600) after the
  failure. A namespace whose scan or stored schema fails is counted and backs off the
  same way until the wait is over or its schema changes; a namespace that is waiting
  takes no place in the pass. A build the profile row refuses (locked, pinned, labelled
  `high`, blacklisted), or whose answer the model cut off at the output cap, is not
  retried until the user's memories, the profile row (an unpin or an unlock, also on a
  redacted row) or the schema change, or
  `LOOM_STRUCTURED_PROFILE_REFUSAL_RETRY_SECONDS` (21600) pass, so a lock that lapses on
  its own is retried. These waits and the scan lists are kept in the worker's process
  and start over when another process takes the worker lease. A pass that reaches every
  namespace with a schema and finds no user behind waits
  `LOOM_STRUCTURED_PROFILE_IDLE_INTERVAL_SECONDS` (600) before the next; one that reached
  only part of the rotation runs again at the worker's interval.
- **Erasure.** A build writes nothing for a user with no facts and no profile row, so a
  user erased after a scan listed them gets no new row when a later pass reaches them. A
  build also writes nothing when a fact it read is no longer current once the model has
  answered (erased or deleted while it ran). What is left is the short gap between that
  check and the write: a row written there while the erasure saga runs is a live row its
  final census finds, and the saga fails closed on it. `memory.redact` of a source fact
  scrubs every profile version whose trace names it, and is refused when the profile key
  is locked. A redact retried after a failure reaches the same profile keys. A profile version whose trace names no memory ids (`profile.fold:<label>`
  alone, as a build that does not record its sources writes it, including an older build
  during a rolling upgrade) is not reached and keeps the text until the user is erased
  ([security](../../architecture/security.md#sensitivity)).
- **Scope.** The read is always for one `user_id`. A token pinned to a user
  (`read_user_id`, an end user's token) reads only that user's profile, with or without
  `user_id` in the body; another user is 403. A token bounded to one session
  (`own_session`) is refused (`IDENTITY_INCOMPLETE` without a `session_id`,
  `AUTH_INSUFFICIENT` with one): a profile spans the user's sessions.
- **Model calls.** Only the worker calls the model, only for a namespace with a schema,
  never for an org whose model calls are switched off
  ([`org.set_model_calls`](../../reference/operations.md#org)), and through the
  configured provider or inference gateway.

## How `context.pack` loads them

`context.pack` renders `working_memory` first and `profile` second, ahead of the
query-dependent sections, so the start of the pack stays the same across questions and a
client can place a prompt-cache breakpoint after it (`stable_prefix_chars`,
`stable_hash`). Working memory is layered narrowest first: a pack for a run also carries
the session's blocks (`run_id` `-`), and a pack for a session also carries the agent's
blocks (`session_id` `-`, `run_id` `-`). A plugin's "where was I" note
(`session:<project>`) lives in the agent layer; with `session_block` set, other projects'
`session:*` notes are left out. Section order, budgets and the other `context.*` ops are
in [unified search](../unified-search.md).

## Settings

| Setting                         | Effect                                                                                                                                                      |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_PROFILE_CONSOLIDATION_*`  | The profile fold worker ([Consolidation](../../reference/configuration.md#consolidation)).                                                                  |
| `LOOM_STRUCTURED_PROFILE_*`     | The structured profile worker ([Structured profiles](../../reference/configuration.md#structured-profiles)).                                                |
| `LOOM_MEMORY_ATTRIBUTE_TAGGING` | Tags rows with protected attributes, which the fold leaves out ([Sensitivity and provenance](../../reference/configuration.md#sensitivity-and-provenance)). |

## Limits

- **Over-limit writes are refused, not truncated.** Watch `fill_ratio`.
- **Working memory is not recall.** A block is never ranked or embedded. Besides
  `context.pack` and the render/get ops, only a call that names a run or session carries
  blocks: `memory.ask` puts the run's blocks in its synthesis prompt, and `memory.search`
  returns the run's and the session's blocks in `scratchpad`. Both read inside the
  request's read scope.
- **A profile section is one row per label.** `profile.set` replaces it; prior versions
  stay readable through [`memory.history`](./history-and-provenance.md#versions-memoryhistory).
- **Render spans every agent by default** unless `scope="this_agent"`.

## Related

- [Writing memory](./writing.md): durable facts.
- [Conversations and sessions](../conversations-and-sessions.md): session briefs.
- [Unified search](../unified-search.md): `context.*` and `loom_search`.
