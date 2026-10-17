---
paths:
  - "server/workers/**"
  - "server/**/workers/**"
---

# Background workers

- A worker is a `_Worker(...)` row in `server/workers/registry.py`, imported lazily when enabled. The supervisor tags each pass's queries `worker.<name>`; a loop it does not run calls `set_query_tag(...)`.
- A pass is bounded (rows, bytes, seconds), resumable from a cursor, and idempotent when re-run after a crash halfway.
- A pass that reaches ClickHouse retries a transient failure within its pass budget rather than waiting for the next interval.
- An intended change to the enabled set, order, cadence or deadline updates the snapshot: `LOOM_UPDATE_WORKER_SNAPSHOT=1 .venv/bin/python -m pytest tests/unit/test_worker_registry_snapshot.py`.
- Two copies can briefly overlap (a lease handover, an `on` process, two colours during a deploy): a pass is harmless when it runs twice at once — idempotent writes, leased claims, cursors in the store ([where it runs](../../docs/development/adding-a-worker.md#5-understand-where-it-runs)).
- Guide: [adding-a-worker](../../docs/development/adding-a-worker.md).
