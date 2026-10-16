---
paths:
  - "schemas/**"
  - "server/core/schema/**"
---

# Migrations and tables

- A migration is additive and takes the next free number on current `origin/main` (`git fetch` first). Two open PRs can claim one number: the one that merges second renumbers its file. A merged migration is never edited, renamed or deleted: deployed ledgers checksum every file.
- A renumber is more than a rename: update `tests/unit/test_schema_baseline.py::test_the_journal_files`, any code constant naming the file, and the docs and PR body that name the file, then `make schema-dump` on a ClickHouse migrated from empty in the new order.
- Every new MergeTree table carries `non_replicated_deduplication_window = 1000` in its `SETTINGS` (`tests/unit/test_table_settings.py`).
- `schemas/schema.sql` is generated: `make schema-dump` against a private ClickHouse migrated from empty, on the release CI tests. Never hand-edit it; `git diff` it and expect only your change.
- ClickHouse Cloud forbids `allow_experimental_nlp_functions`: the runner drops the flag there (`_nlp_available` in `server/core/schema/migrate.py`) and lexical search runs on plain tokens. Use that path; never add a second fallback.
- Builds N-1 and N run side by side on N's schema: a new column, enum value or wire format is written only once the previous build tolerates it ([rolling upgrades](../../docs/architecture/rolling-upgrades.md#the-rules)); `make test-mixed-version` checks it (needs docker).
- A sort-key or partition change is a rebuild, not an `ALTER` ([migrations](../../docs/development/migrations.md#changing-a-sort-key-or-a-partition-key)).
- A new table is classified for erasure in the same change: a saga step and a residue-census row in `server/erasure/store/census.py`, or `NO_MEMORY_CONTENT` / `STEP_ONLY_TABLES` with the reason (`test_every_table_is_classified.py`).
- A new `loom.memory` column is added to `loom.memory_versions` and to `memory_versions_mv`'s `SELECT` in the same migration, and to the row builders in `server/memory/store/rows.py`.
- No `TTL` on durable memory (`loom.memory`, `loom.shared`, `loom.memory_versions`): Loom deletes only what its writer asked to delete. A cache, queue or trace that copies content may expire, and still needs its census row.
- Checklist: [adding-a-table](../../docs/development/adding-a-table.md#checklist), [migrations](../../docs/development/migrations.md#checklist).
