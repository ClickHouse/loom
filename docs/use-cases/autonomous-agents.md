---
title: Auditable memory for autonomous agents
description: Give autonomous AI agents self-hosted long-term memory. Resume a stopped job, reuse facts across runs, and audit who wrote each fact.
seo:
  title: Self-hosted memory for autonomous AI agents
---

# Memory, recall, audit and erasure for autonomous agents

Loom provides self-hosted long-term memory for autonomous agents. An agent can reuse
facts across runs, resume a stopped job from a checkpoint, and record the run and its
outcome for inspection. Loom stores this data in ClickHouse, with Postgres available
for live memory and knowledge state.

## Resume an agent after a restart

An agent that checks a catalog can save its last completed batch under the run ID
its scheduler assigns. A replacement process reads that checkpoint before starting
work. Save facts that should outlive the job with [`memory.set`](../concepts/memory.md).
Keep the job's current cursor in [working memory](../guides/trace-and-resume.md#which-plane-holds-a-checkpoint).

Try the checkpoint on the [single-user quickstart](../getting-started/quickstart.md#fast-path-released-image-no-clone).
Use a disposable run ID on the local deployment. Save the last completed batch:

```bash
curl --fail-with-body --silent --show-error http://localhost:7777/v1/working_memory.set \
  --json '{"org":"local","namespace":"default","agent":"catalog-worker","run_id":"catalog-restart-demo","block_key":"checkpoint","content":"batch-04"}'
```

From a fresh shell or agent process, read the checkpoint with the same identity
and run ID:

```bash
curl --fail-with-body --silent --show-error http://localhost:7777/v1/working_memory.get \
  --json '{"org":"local","namespace":"default","agent":"catalog-worker","run_id":"catalog-restart-demo","block_key":"checkpoint"}'
```

The response should have `found: true` and `content: "batch-04"`. Read again with an
unused run ID and expect `found: false`. The exact-key check requires no model call.
Delete the test checkpoint with [`working_memory.delete`](../reference/operations/working_memory.md#working_memorydelete).

On a shared deployment, [scope the agent's token](../guides/tokens-and-grants.md) and
carry the same namespace, agent, user and session identity into the replacement process.
Record the checkpoint after a batch completes. A crash between a side effect and its
checkpoint can cause a retry, so use a stable operation ID to deduplicate the side effect.
See [trace and resume](../guides/trace-and-resume.md) for the Python SDK and MCP equivalents.

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
