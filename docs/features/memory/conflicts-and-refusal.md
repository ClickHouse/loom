# Conflicts and refusal

Every memory write is judged against the facts the namespace already holds. A close but
different value is stored and flagged as a conflict, and the namespace owner decides
which fact stands: in the dashboard's Review queue, or with `conflicts.resolve`. Loom
picks a winner only where a policy or the judge says one row replaces another (see
[automatic retirements](#automatic-retirements)), and then records it so it can be
undone. Refusal works the other way: `blacklist.add` registers a pattern, and every
later write whose value matches it is refused. This page owns the write verdicts and the
namespace conflict policies; the model behind them is in [memory
concepts](../../concepts/memory.md).

![The review queue: a conflicting fact with Keep, Drop and Allow both](../../images/dashboard-review-queue.png)

## How a write is judged

`memory.set`, `memory.bulk_set` (and so `memory.set_from_messages`) and `memory.update`
compare the new value with the active rows of the same `memory_type` in the namespace:

- A canonical-form match is a **merge**: nothing is written.
- Cosine at or above the duplicate threshold is a **duplicate**.
- Cosine at or above the conflict threshold with a different value is a **conflict**:
  the new row and each peer get `status='conflicted'` and are cross-linked through
  `conflict_with`.
- With the LLM judge on (`LOOM_MEMORY_CONFLICT_JUDGE`: on by default while a real LLM
  provider and its key are configured, off otherwise; `false` turns it off), one call
  labels the closest peers as contradiction, elaboration, restatement, related or
  unrelated. The judge also reads peers down to
  `LOOM_MEMORY_CONFLICT_JUDGE_MIN_SIMILARITY` (0.7), which catches an update filed under
  a different key.

A pair on the namespace's allowlist is not flagged.

**Thresholds.** The conflict threshold comes from, in order: the memory type's
`dedup_threshold`, the namespace's `conflict_threshold`, the embedder calibration (with
`LOOM_ADAPTIVE_THRESHOLDS` on, the default), then `LOOM_CONFLICT_THRESHOLD` (0.85). The
duplicate threshold follows the same order without the type step, ending at
`LOOM_DUPLICATE_THRESHOLD` (0.97).

### Verdicts

Every write answers with `conflict_verdict`, the `peers` it was judged against (closest
first, at most five) and a `next` hint such as `memory.update(memory_key=<peer>, ...)`.

| Verdict      | Meaning                                                                                                                                       | Row written                                                        |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `clear`      | Nothing close.                                                                                                                                | Yes, `active`.                                                     |
| `merge`      | The same fact after canonicalisation is already stored.                                                                                       | No. The stored row is reinforced; `noop=true`, `merged_into_key`.  |
| `duplicate`  | Near-identical to a peer but not canonically equal.                                                                                           | Yes, `status='duplicate'` with `duplicate_of`.                     |
| `conflict`   | Close to a peer but different.                                                                                                                | Yes; both rows `conflicted` and cross-linked.                      |
| `refines`    | The LLM judge read it as an elaboration of a peer.                                                                                            | Yes; the refined peer is superseded.                               |
| `rejected`   | Close to a pinned peer, or a changed value for a pinned key.                                                                                  | No. 409 `PINNED_CONFLICT` (a dry run returns the verdict instead). |
| `pending`    | The LLM judge runs after the write.                                                                                                           | Yes, `active`; the settled verdict lands on the row later.         |
| `superseded` | Bulk writes under `auto_reinforce` with the judge on: a stored row of the same key and subject is the later statement this entry contradicts. | Yes, `superseded`, with `superseded_by` naming that row.           |

- `merge`, `duplicate` and `rejected` are deterministic and settled inline.
- `refines` and `pending` need the judge. It runs after the write unless the request
  passes `await_verdict=true`.
- `verdict_at` is `inline` or `deferred`.
- `dry_run=true` runs the whole check and writes nothing.
- The verdict is empty when no check ran: a non-searchable type or a `high` sensitivity
  row.

### Namespace conflict policy

`namespace.register` takes `default_conflict_policy`:

| Policy                     | Behaviour                                                                                                                                                                                                                                                                         |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `auto_reinforce` (default) | Conflicts are flagged; both facts stay live until resolved. With the judge on, a bulk write judges the stored rows of each key it writes for the same user (or, for the anonymous user, agent): the older row of a contradiction, or of an elaboration by the newer row, retires. |
| `manual_review`            | The same: both stay live and the pair waits for review.                                                                                                                                                                                                                           |
| `newest_wins`              | A judged contradiction retires the older row as `superseded` (history kept). A write for a key retires every older live row with that key, whoever wrote it.                                                                                                                      |
| `append_only`              | Every write is an event: no merge, duplicate or conflict verdict. A same-key rewrite stays last-writer-wins. The writer owns dedup.                                                                                                                                               |

Superseded rows are served only to reads that pass `include_superseded=true`, and a
served one names its successor.

### Pinned facts

`memory.pin` writes a fact and marks it pinned. A pin is a write-time rule, not a
ranking hint:

- A close write against a pinned peer is refused with 409 `PINNED_CONFLICT`;
  `pinned_blocker_id` names the row.
- A changed value for a pinned key is refused the same way unless the write itself pins.
- A byte-identical restatement passes and keeps the pin.
- In `memory.bulk_set`, an entry for a pinned key is skipped (`skipped_pinned`); one
  close to a pinned peer is listed in `rejected_pinned`.

To change a pinned value, call `memory.update` (keeps the pin) or `memory.pin` (re-pins).

## Resolving conflicts

| Operation                    | Role   | Does                                                                                     |
| ---------------------------- | ------ | ---------------------------------------------------------------------------------------- |
| `conflicts.list`             | viewer | Unresolved conflicted rows with `conflict_with`; `count_only` or `keys_only` for badges. |
| `conflicts.propose_revision` | viewer | Suggestions (`revise`, `contract` or `allow`) with a rationale; changes nothing.         |
| `conflicts.resolve`          | admin  | Apply a decision to a row and its peers, or to one `peer_id`.                            |
| `conflicts.allowlist_list`   | viewer | Pairs marked as known-distinct.                                                          |
| `conflicts.allowlist_remove` | admin  | Remove a pair; it is flagged again on later writes.                                      |

These are HTTP, SDK (`agent.conflicts.*`) and dashboard operations, not MCP tools.

`conflicts.resolve(memory_id, decision)`:

| Decision   | Effect                                                                                                                |
| ---------- | --------------------------------------------------------------------------------------------------------------------- |
| `revise`   | The chosen row wins: it returns to `active` and its peers are tombstoned.                                             |
| `contract` | The chosen row is wrong: it is tombstoned and its peers return to `active`.                                           |
| `allow`    | Both are distinct: every row returns to `active` and each key pair goes on the allowlist (`loom.conflict_allowlist`). |

`reason` and `resolved_by` go to the audit trail. A resolve's tombstones are ordinary
tombstones: `memory.restore` revives them within 14 days.

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="curator") as agent:
    page = await agent.conflicts.list(limit=20)
    for row in page["rows"]:
        print(row["memory_key"], row["value"], "vs", row["conflict_with"])
    await agent.conflicts.resolve(
        page["rows"][0]["memory_id"], "revise", reason="plan changed at renewal"
    )
```

### The Review queue

The dashboard's Review queue (`/me/admin/review_queue`, `g r`) lists conflicted pairs and
applies `conflicts.resolve`. Three more tabs appear when they have content: **Learnings**
([learnings](./procedures-and-learnings.md#learnings-review-queue)), **Auto-resolved**
(below) and **Exceptions** (the allowlist). See [the dashboard](../dashboard.md).

### Automatic retirements

When Loom retires memories on its own, each path that retired rows records them as
`memory.auto_supersede` audit rows (`reason` in brackets):

- the same-key judge on a bulk write (`same_key_judge`);
- `newest_wins` on a key written again (`same_key_newest_wins`);
- a `refines` verdict (`refines`), or a judged contradiction under `newest_wins`
  (`newest_wins_contradiction`);
- `newest_wins` inside one bulk batch, or behind a repaired single-valued triple
  (`newest_wins_contradiction`);
- a value update (`value_update`).

A write that retires rows through several paths leaves one record per path, and a path
that retires more than 200 rows splits them across several records. A record names each
retired memory, the row it retired behind and the reason; it holds ids, keys and
identity, never value text. On every path listed, a locked or pinned row, or one
already retired since it was read, is left as it is. A row is listed once a re-read
after the write finds its superseded version at the head; one a later write overtook is
not. One the re-read could not see is listed with `unconfirmed`: a write's result
never counts it among the rows it retired, recall is left as it was, and the projection
repair mirrors the row's head onto recall once it can read it (should even queueing
that fail, `loom reconcile-status` finds the divergence). A point write (`memory.set`,
`memory.update_by_id`, the deferred judge) writes its record beside the response.

The Review queue's **Auto-resolved** tab lists the last 30 days, a page of 20 records at
a time, and its Undo calls `memory.restore` with `if_superseded_by`: the row comes back
`active` and `memory.unsupersede` is recorded only while it is still retired behind the
memory the record names; a row deleted, revived or retired again since shows "No longer
retired". The Undo holds the key's write lock from its read of the row to its insert,
as a conditional write (`if_match_sha256`) does: a conditional revision made meanwhile
lands first and the Undo shows "No longer retired", or waits for the Undo. An
unconditional write takes no lock, and the later of the two wins. When another
conditional write holds the lock past its wait, the Undo answers 412 ("another
conditional write to this key is still running; re-read and retry"). An Undo opens again the
graph facts the restored row still holds (see the
[entity graph](../entity-graph.md)). To mark rows
already restored, the tab reads up to 2,500 `memory.unsupersede` records (5 pages of 500) back to the oldest retirement it shows. Past that, an older row still offers Undo,
and undoing a row that is already live shows "No longer retired". An Undo is not
remembered: a later write of the same key is judged again, and can retire the row again
if it is the later statement. A row the re-read could not confirm is marked "may not
have taken effect". The memory inspector's **Restore** on a superseded row sends
`only_if_superseded`, and `if_superseded_by` when it can see the successor: a read
narrowed below the namespace shows none for a successor outside its scope, and either
way a delete since is never undone.

## Duplicates

A duplicate is the same fact in other words. It is stored with `status='duplicate'` and
`duplicate_of` pointing at its canonical. `memory.search` puts the canonical in the
duplicate's slot, `memory.list` skips duplicates, and `memory.get` names the canonical in
`canonical_key` (`follow=true` serves it). `include_duplicates=true` shows them.

| Operation                          | Role   | Does                                                                                                     |
| ---------------------------------- | ------ | -------------------------------------------------------------------------------------------------------- |
| `memory.dedup_list`                | admin  | Clusters of one canonical and its duplicates.                                                            |
| `memory.dedup_resolve`             | admin  | `keep_canonical`, `keep_duplicate` or `keep_both` for one pair; `confirm_canonical_key` repeats the key. |
| `memory.dedup_collapse`            | writer | Every pair of a namespace (or key `prefix`) onto its canonical; no LLM; `dry_run` by default.            |
| `memory.resolve_orphan_duplicates` | writer | Re-activate duplicates whose canonical is gone; `dry_run` to preview.                                    |
| `memory.consolidate`               | writer | LLM-merge clusters of near-duplicates into one canonical each; `dry_run` to preview.                     |

`keep_both` puts the pair on the same allowlist conflicts use. `memory.consolidate` makes
one LLM call per cluster, skips rows younger than `min_age_seconds` (default one hour),
and records lineage ([history and provenance](./history-and-provenance.md#lineage)).

## Refusal: the blacklist

`blacklist.add(pattern, scope)` registers a pattern no write may store. `memory.set` and
`memory.update` answer 422 `BLACKLISTED` with the `pattern_id`, scope and reason. The
bulk and extraction paths skip the entry and count it.

| Field          | Values                                                                             |
| -------------- | ---------------------------------------------------------------------------------- |
| `pattern`      | Up to 4,096 characters.                                                            |
| `pattern_type` | `substring` (default, case-sensitive) or `regex`.                                  |
| `scope`        | `namespace` (default), `org` (needs org admin) or `global` (needs platform admin). |
| `reason`       | Stored on the pattern and returned with every refusal.                             |
| `sweep`        | Also tombstone the live rows the pattern already matches.                          |

`blacklist.add` is a writer op and an MCP tool (`loom_blacklist_add`), so an agent can
honour "never remember my home address" inside the conversation. A token bounded to one
user (`read_scope` `own_user` / `own_session`) gets `AUTH_INSUFFICIENT` on every
`blacklist.*` op: a pattern refuses every user's writes in the namespace.

A regex is checked at add time: at most 256 characters, no nested quantifier such as
`(a+)+`, and a probe that finishes inside 1 ms.

| Operation          | Role   | Does                                                                              |
| ------------------ | ------ | --------------------------------------------------------------------------------- |
| `blacklist.add`    | writer | Register a pattern; optionally sweep existing rows.                               |
| `blacklist.list`   | viewer | Effective patterns for the namespace, including org and global, with `hit_count`. |
| `blacklist.sweep`  | writer | Continue a sweep for an existing `pattern_id`.                                    |
| `blacklist.remove` | admin  | Remove a pattern.                                                                 |

**Sweeping existing rows.** `sweep=true` tombstones matching live rows, up to 5,000 per
call; `swept_truncated=true` means call `blacklist.sweep(pattern_id)` again. Each sweep
leaves one `loom.memory_drops` row, so `loom drops` shows what it removed. The sweep runs
in ClickHouse with RE2: a construct only Python supports (look-behind) matches nothing
there and is reported.

```bash
curl -sX POST http://localhost:7777/v1/blacklist.add \
  -H "Authorization: Bearer $LOOM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"org":"dev","namespace":"local",
       "pattern":"\\b\\d{3}-\\d{2}-\\d{4}\\b","pattern_type":"regex",
       "reason":"never store national id numbers","sweep":true}'
```

**Across replicas.** Each replica caches the patterns in process. Adding or removing one
bumps a one-row version watermark (`loom.blacklist_version`); replicas poll it every
`LOOM_BLACKLIST_VERSION_CHECK_INTERVAL_SECONDS` (2 s), with a hard cache TTL
(`LOOM_BLACKLIST_CACHE_TTL_SECONDS`, 300 s). For up to one interval, another replica can
still accept a newly refused value.

The dashboard's Guardrails page (`/me/admin/guardrails`) manages patterns. Refusing is
not erasing: to remove a person's data, use [erasure](./retention-and-erasure.md#erasure).

## Settings

| Setting                                         | Default                                 | Effect                                                                                                                                 |
| ----------------------------------------------- | --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_CONFLICT_THRESHOLD`                       | 0.85                                    | Fallback conflict cosine; 1.0 disables flagging.                                                                                       |
| `LOOM_DUPLICATE_THRESHOLD`                      | 0.97                                    | Fallback duplicate cosine.                                                                                                             |
| `LOOM_ADAPTIVE_THRESHOLDS`                      | true                                    | Calibrate both thresholds from the live embedder.                                                                                      |
| `LOOM_MEMORY_CONFLICT_JUDGE`                    | on with a real LLM provider and its key | LLM labelling of close peers: at most one call per write with a peer at the judge's band, inline on bulk writes; `false` turns it off. |
| `LOOM_MEMORY_CONFLICT_JUDGE_MIN_SIMILARITY`     | 0.7                                     | Lowest cosine the judge reads.                                                                                                         |
| `LOOM_BULK_CONFLICT_CHECK_MAX_ENTRIES`          | 200                                     | Entries per `memory.bulk_set` checked; 0 = none.                                                                                       |
| `LOOM_BLACKLIST_VERSION_CHECK_INTERVAL_SECONDS` | 2.0                                     | Cross-replica pattern refresh bound.                                                                                                   |

See the [configuration reference](../../reference/configuration.md#conflict-detection).

## Limits

- **A conflict is a flag, not a refusal.** Both rows are served until resolved. A reader
  should prefer the later `valid_at`, or ask.
- **Fake embedders give string-similarity verdicts.** The stock stack's fake embedder does
  not model meaning. With adaptivity off, re-bench the fallback thresholds after changing
  the embedder.
- **`conflicts.resolve` is admin.** A writer settles its own contradiction with
  `memory.update` on the peer the `next` hint names.
- **A pattern matches values, not keys**, and a substring pattern is case-sensitive.
