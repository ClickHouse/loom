# Memory, recall, audit and erasure for autonomous agents

Loom gives autonomous agents memory they recall later, an audit record of who wrote what,
and erasure of a scope everywhere Loom keeps it. These are server operations, stored in
ClickHouse (or Postgres for live memory state).

## What you get

- **Memory and recall.** Versioned facts with provenance, search that abstains when
  nothing is close enough, and a ranking boost for memories that successful runs relied
  on. [Memory](../concepts/memory.md) · [Runs](../features/runs.md)
- **Refusal.** A pinned fact refuses contradicting writes, and `blacklist.add` registers a
  pattern no write may store. [Conflicts and refusal](../features/memory/conflicts-and-refusal.md)
- **Audit.** Each operation that changes memory leaves an audit row with a SHA-256 of its
  content, and a worker seals hourly windows into a hash chain, so an edit or deletion is
  detectable. The transport's audit writes are fail-open (a failed insert loses the row);
  under the Postgres authority a memory revision's audit row is stored with the revision.
  [Audit log](../features/observability/audit.md#limits-and-failure-modes)
- **Erasure you can check.** `erasure.request` erases a scope from every table that holds
  memory content (tombstones by default; `purge=true` deletes the rows of a `user_id` or
  `namespace_list` scope) and ends with a residue census and a receipt.
  [Retention and erasure](../features/memory/retention-and-erasure.md)
- **Rollback.** `namespace.rollback` returns every memory in a namespace to its version
  at a point in time.
  [History and provenance](../features/memory/history-and-provenance.md)
- **Headless operation.** A bot gets its own identity (it signs itself up where self-signup
  is open), checks its connection before its first run, and can be diagnosed when it goes
  quiet.
  [Headless bots](../guides/headless-bots.md)

Audit and erasure are engineering primitives, not a compliance certification.
[Security](../architecture/security.md)

Ops marked experimental may change between releases: see [status](../reference/status.md).
