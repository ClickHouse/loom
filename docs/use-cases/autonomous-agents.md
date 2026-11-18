# Memory, recall, audit and erasure for autonomous agents

Loom gives autonomous agents memory they recall later, an audit record of who wrote what,
and erasure of a scope everywhere Loom keeps it. These are server operations, stored in
ClickHouse (or Postgres for live memory and knowledge state).

## What you get

- **Memory and recall.** Versioned facts with provenance, search that abstains when
  nothing is close enough, and a ranking boost for memories that successful runs relied
  on. [Memory](../concepts/memory.md) · [Runs](../features/runs.md)
- **Procedures.** Reusable how-tos the agent learns from doing: `procedure.add` stores one
  as a graph of steps, `procedure.match` finds the ones that fit a task, and the runs the
  agent reports teach Loom the proven path and the steps that fail. A revised graph is kept
  only if its runs do not do worse.
  [Procedures and learnings](../features/memory/procedures-and-learnings.md)
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

## Check one agent task

Use a disposable namespace on your deployment, with a writer token for the agent.
[Integrate Loom into an agent](../guides/agent-integration.md) covers the client and identity.

1. Open the task with `run.begin` and write a fact with `memory.set` carrying the returned
   `run_id` ([runs](../features/runs.md)). Add a [checkpoint](../guides/trace-and-resume.md)
   when the agent needs to resume after a stop.
2. Follow the [procedure example](../features/memory/procedures-and-learnings.md#example)
   to register, match and report an execution, then call `procedure.evolve` explicitly.
   The agent executes the steps; inspect the match's [trust status](../features/memory/procedures-and-learnings.md#matching).
   Close the task once with `run.complete`, following the [procedure run reporting rules](../features/memory/procedures-and-learnings.md#learning-from-runs)
   to count each execution once.
3. With viewer access, inspect `run.get` and read the namespace's `run.begin` and
   `run.complete` events through
   [`audit.list`](../features/observability/audit.md#reading-the-trail). The
   [offline verifier](../features/observability/audit.md#verifying-offline) checks stored
   row hashes; read the audit page's failure modes before treating the trail as complete.
4. As the namespace admin, [erase the disposable namespace](../features/memory/retention-and-erasure.md#erasure)
   and check for `status="completed"`, `residue_count=0` and `verified_at`. Save the
   receipt before the request ledger expires. Erasure retains audit rows and excludes
   backups; [retention and erasure](../features/memory/retention-and-erasure.md#limits)
   describes the boundary.

Some ops on this page are marked experimental on the [status page](../reference/status.md).
