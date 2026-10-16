---
paths:
  - "schemas/**"
  - "server/core/schema/**"
---

# Migrations and tables

- A migration is additive and takes the next free number on current `origin/main` (`git fetch` first; two open PRs can claim one number). A merged migration is never edited, renamed or deleted: deployed ledgers checksum every file.
- `schemas/schema.sql` is generated: `make schema-dump` against a private ClickHouse migrated from empty, on the release CI tests. Never hand-edit it; `git diff` it and expect only your change.
- A sort-key or partition change is a rebuild, not an `ALTER` ([migrations](../../docs/development/migrations.md#changing-a-sort-key-or-a-partition-key)).
- A new table is classified for erasure in the same change: a saga step and a residue-census row in `server/erasure/store/census.py`, or `NO_MEMORY_CONTENT` / `STEP_ONLY_TABLES` with the reason (`test_every_table_is_classified.py`).
- A new `loom.memory` column is added to `loom.memory_versions` and to `memory_versions_mv`'s `SELECT` in the same migration, and to the row builders in `server/memory/store/rows.py`.
- No `TTL` on a table that holds memory content: Loom deletes only what its writer asked to delete.
- Checklist: [adding-a-table](../../docs/development/adding-a-table.md#checklist), [migrations](../../docs/development/migrations.md#checklist).
