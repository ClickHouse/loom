---
paths:
  - "server/memory/**"
  - "server/knowledge/**"
  - "server/erasure/**"
  - "server/identity/app/namespace_snapshot.py"
  - "server/observability/audit.py"
---

# Memory plane

The design, its invariants and the code paths are in [memory architecture](../../docs/architecture/memory-architecture.md); read the section for the op you change.

## Writes

- A new version row of `loom.memory` or `loom.shared` (set, tombstone, restore, touch, `update_by_id`, `bulk_set`, promote, demote) goes through `build_memory_version_row` / `build_embedding_row` in `server/memory/store/rows.py`. Both tables are `ReplacingMergeTree(version)` read with `FINAL`, so a column the new row omits reads as zero for everyone.
- A new column reaches those builders and every projection that feeds them in the same change; a new `loom.memory` column also goes into `loom.memory_versions` and `memory_versions_mv` (`migrations` rule). History, redact and rollback read `loom.memory_versions`, never the merged rows.
- Every write that creates or changes memory leaves one hash-only audit row (`memory.set` and `memory.bulk_set` alike).
- `memory.set` and `memory.bulk_set` both run conflict surfacing, the blacklist and the pinned-peer check before the insert; a new write path that skips any of them is a bug ([conflicts and refusal](../../docs/features/memory/conflicts-and-refusal.md)).
- `memory.set` / `memory.update_by_id` honour `if_match_sha256`; the current hash rides `memory.get` / `memory.get_by_id`.
- Working memory (`working_memory.*`) is not embedded or ranked; a durable fact goes through `memory.set`.

## Ranking

- Change a similarity gate or rank weight only with a paired benchmark (`loom-bench`). With adaptive thresholds on (the default), the gates come from the embedder's own distribution at boot (`server/memory/app/embedder_calibration.py`); the scalar settings are the fallback.
- `memory.search_trace` returns the same ranking as `memory.search` with each component's score: use it to explain a rank.

## Deletion and erasure

- No TTL, retention sweep or cleanup removes durable memory (`loom.memory`, `loom.shared`, `loom.memory_versions`) its writer did not ask to remove. A cache, queue or trace that copies content may expire.
- A table that can hold memory content gets an erasure saga step and a `Substrate(...)` row in `server/erasure/store/census.py::_CENSUS`; a step with no census row goes in `STEP_ONLY_TABLES`, a table with no memory content in `NO_MEMORY_CONTENT`, each with its reason.
- The saga fails closed: a backend that cannot confirm a deletion records `failed`, and the final residue census fails the saga on any live row in scope ([erasure saga](../../docs/architecture/memory-architecture.md#erasure-saga)).
