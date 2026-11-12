# Memory, recall, audit and erasure for autonomous agents

An autonomous agent needs memory it can trust without a person watching: facts it can
recall later, a record of who wrote what, and a way to delete data everywhere when asked.
Loom provides these as server operations, stored in ClickHouse.

## What you get

- **Memory and recall.** Versioned facts with provenance, search that abstains when
  nothing is close enough, and a ranking boost for memories that successful runs relied
  on. [Memory](../concepts/memory.md) · [Runs](../features/runs.md)
- **Refusal.** A pinned fact refuses contradicting writes, and `blacklist.add` registers a
  pattern no write may store. [Conflicts and refusal](../features/memory/conflicts-and-refusal.md)
- **Audit.** Each operation that changes memory leaves an audit row with a SHA-256 of its
  content, and a worker seals hourly windows into a hash chain, so an edit or deletion is
  detectable. [Audit log](../features/observability/audit.md)
- **Erasure you can check.** `erasure.request` removes a scope from every table that holds
  memory content and ends with a residue census and a receipt.
  [Retention and erasure](../features/memory/retention-and-erasure.md)
- **Rollback.** `namespace.rollback` returns every memory in a namespace to its version
  at a point in time.
  [History and provenance](../features/memory/history-and-provenance.md)
- **Headless operation.** A bot with no person to paste a token or notice a failure.
  [Headless bots](../guides/headless-bots.md)

Audit and erasure are engineering primitives, not a compliance certification.
[Security](../architecture/security.md)
