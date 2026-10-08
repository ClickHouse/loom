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
  (`source_trace` `profile.fold:<label>`).

```python
async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    await bot.profile.set("persona", "Terse. Cite ticket ids. Never promise refunds.")
    await bot.profile.append("preferences", "Prefers EUR invoices.", user_id="u-42")
    system = await bot.profile.render(user_id="u-42", read_scope="own_user")
```

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
| `LOOM_MEMORY_ATTRIBUTE_TAGGING` | Tags rows with protected attributes, which the fold leaves out ([Sensitivity and provenance](../../reference/configuration.md#sensitivity-and-provenance)). |

## Limits

- **Over-limit writes are refused, not truncated.** Watch `fill_ratio`.
- **Working memory is not recall.** A block never appears in `memory.search`,
  `memory.ask` or `loom_search`; only `context.pack` and the render/get ops return it.
- **A profile section is one row per label.** `profile.set` replaces it; prior versions
  stay readable through [`memory.history`](./history-and-provenance.md#versions-memoryhistory).
- **Render spans every agent by default** unless `scope="this_agent"`.

## Related

- [Writing memory](./writing.md): durable facts.
- [Conversations and sessions](../conversations-and-sessions.md): session briefs.
- [Unified search](../unified-search.md): `context.*` and `loom_search`.
