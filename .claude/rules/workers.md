---
paths:
  - "server/workers/**"
  - "server/**/workers/**"
---

# Background workers

- A worker is a `_Worker(...)` row in `server/workers/registry.py`, imported lazily when enabled. Its pass calls `set_query_tag(...)`.
- A pass is bounded (rows, bytes, seconds), resumable from a cursor, and idempotent when re-run after a crash halfway.
- A pass that reaches ClickHouse retries a transient failure within its pass budget rather than waiting for the next interval.
- An intended change to the enabled set, order, cadence or deadline updates the snapshot: `LOOM_UPDATE_WORKER_SNAPSHOT=1 .venv/bin/python -m pytest tests/unit/test_worker_registry_snapshot.py`.
- Two colours run side by side during a deploy; only the lease holder works. Anything a pass writes outside the lease is a race.
- Guide: [adding-a-worker](../../docs/development/adding-a-worker.md).
