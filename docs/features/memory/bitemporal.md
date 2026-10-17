# Bi-temporal memory (opt-in)

Bi-temporal memory is an opt-in commit journal, off by default: every memory write is
journaled before its row is written, and a read can ask what applied at a valid time V
as Loom had recorded it at a knowledge time K. Without it, Loom keeps current-state
memory, a per-key version history (`memory.history`) and single-axis point-in-time
reads (`as_of`). It is turned on per deployment with
`LOOM_MEMORY_TEMPORAL_ENABLED=true`. The design is in
[bi-temporal memory architecture](../../architecture/bitemporal-memory.md).

## What it adds

- **Two times on every read.** `valid_as_of=V` and `known_as_of=K` on `memory.get`,
  `memory.get_by_id`, `memory.list`, `memory.search`, `memory.ask`, `search.query`,
  `facts.*` and `memory.record.query`. Either alone fills the other. A correction, a
  delete or a merge never rewrites what an earlier K answers.
- **Corrections of past spans.** `memory.amend` corrects, retracts or schedules one
  valid-time interval and keeps the rest. `memory.set` takes the write's intent
  (`change_kind`, `effective_at`, `temporal_kind`, `modality`, `qualifiers`, …).
- **Timelines.** `memory.intervals` returns a key's valid-time intervals,
  `memory.temporal_diff` what changed between two times, and `memory.temporal_status`
  the namespace journal's state.
- **Journal-planned rollback.** `namespace.rollback` serves each key's claims at `as_of`
  again as they were (a dated span keeps its span, a plan keeps its date), and a later
  `known_as_of` read still answers what was known before the rollback.
- **Read-your-write tokens.** A write answers a `read_token`; a read that passes it
  waits until the journal covers that write.

Most deployments need only current-state memory, `memory.history` and `as_of`. Turn the
journal on when you need "what was true at V as recorded at K", corrections of past
spans, or a rollback planned from the journal.

## Why it is off by default

| Cost                      | Journal off                             | Journal on                                                                                                      |
| ------------------------- | --------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `memory.set` p50          | {{SET_P50_OFF}} ms                      | {{SET_P50_ON}} ms ({{SET_RATIO}}×)                                                                              |
| Storage per revision      | a `memory_versions` row (about 0.15 KB) | about 0.6 KB of commit and assertion, plus a historical vector (about 6 KB at 1,024 dimensions, 18 KB at 3,072) |
| `memory.search` p50 (now) | {{SEARCH_P50}} ms                       | {{SEARCH_P50}} ms                                                                                               |

- **Write latency.** A journaled write makes about 10 more ClickHouse round trips: the
  plan reads, a deployment-wide per-namespace lease in Keeper, the commit insert and its
  read-backs, the erasure fence and the projection.
- **Storage.** The journal keeps every revision searchable as it was: commits,
  assertions and a vector per revision (the current-state embedding is one row per key,
  replaced on each write). Measured over 200 writes to 50 keys with a 1,024-dimension
  embedder; the vector scales with the embedder's width.
- **Reads.** A read that does not ask for a past time costs the same in both modes.

## What changes on the wire

| Surface                                                                              | Journal off (default)                                                                                      | Journal on                                                                                       |
| ------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Point-in-time reads                                                                  | `as_of` with `as_of_basis` (`valid_time` or `knowledge_time`)                                              | `valid_as_of` / `known_as_of`; `as_of` and `as_of_basis` are ignored and named in `ignored_args` |
| `memory.amend`, `memory.intervals`, `memory.temporal_diff`, `memory.temporal_status` | HTTP 404; not listed over MCP                                                                              | Served                                                                                           |
| `namespace.rollback`                                                                 | Version-row rollback from `loom.memory_versions` (`version_at`, `skipped_no_head`, `skipped_already_gone`) | Planned and committed in the journal (`left_out`, `read_token`, `current_rows_pending`)          |
| `memory.history`                                                                     | `loom.memory_versions`                                                                                     | `loom.memory_versions`                                                                           |
| Schema                                                                               | `schemas/schema.sql`; migration 039 dormant                                                                | `schemas/schema_temporal.sql`; migration 039 applied                                             |

With the journal off, the journal-only request fields are ignored and named in
`ignored_args`: `valid_as_of`, `known_as_of`, `read_token`, `validity_policy`, the
`memory.set` intent fields (`change_kind`, `effective_at`, `temporal_kind`, `modality`,
`valid_text`, `valid_precision`, `valid_tz`, `qualifiers`, `expected_revision`) and
`memory.redact`'s `assertion_id`. The journal-only response fields (`commit_seq`,
`read_token`, `left_out`, `past_only`, `current_rows_pending`, `temporal`, `assertions`,
`disputed`, `temporal_coverage`) are absent. The MCP listing omits the fields of the side
that is off. How a client sees an ignored field is in
[unknown and removed fields](../../reference/http-api.md#unknown-and-removed-fields).

Migration `schemas/migrations/039_bitemporal_memory.sql` carries
`-- requires-setting: memory_temporal_enabled`: with the setting off, `loom migrate`
neither applies it nor reports it pending, so the schema is exactly `schemas/schema.sql`.

## Turning it on

1. Set `LOOM_MEMORY_TEMPORAL_ENABLED=true` on every process of the deployment: every
   server replica and every worker. A deployment with the setting mixed behaves like a
   rolling upgrade (the journal's sweep adopts the writes of the processes that are
   off), but turn it on everywhere.
2. Run `loom migrate`, or let the boot migration run. Migration 039 applies now; with the
   setting on, `loom migrate --status` lists it as pending until it has.
3. Restart the processes.

Requirements: a Keeper (Loom already requires one; the journal adds a per-namespace
publisher lease), and the write cost above.

What existing data gets:

- A key written before the switch has no journal history. It is seeded into the journal
  on its next write.
- A `known_as_of` read before the switch reports a `history_before_journal` gap.
- A `namespace.rollback` to a time before the switch leaves such keys out and names them
  in the answer.

## Turning it off

Set `LOOM_MEMORY_TEMPORAL_ENABLED=false` on every process and restart. No migration is
reverted: the journal tables stay. The journal's operations and fields disappear from
the wire, and reads and `namespace.rollback` behave as in the default. Writes made while
it is off are not journaled; when it is turned on again, the journal adopts them the way
it adopts a previous build's writes during a rolling upgrade.

## Related

- [Bi-temporal memory architecture](../../architecture/bitemporal-memory.md)
- [Writing memory](./writing.md#validity-time-valid_at-and-valid_to)
- [History and provenance](./history-and-provenance.md)
- [Deployment](../../operations/deployment.md)
- [Rolling upgrades](../../architecture/rolling-upgrades.md)
