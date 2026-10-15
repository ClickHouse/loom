# Loom on AX

[AX](https://github.com/google/ax) runs agents as sandboxed `Task`s that are
cheap to create, suspend and throw away. Each sandbox starts empty, and
AX has no memory layer. Loom adds one: what a task learns outlives the sandbox,
the next task recalls it, and every task's run sits in Loom's run ledger with
what it cost.

| File        | What it is                                                                                                 |
| ----------- | ---------------------------------------------------------------------------------------------------------- |
| `task.yaml` | A `Workspace` that writes Loom's MCP config and an `AGENTS.md`, and a `Task` that binds it                 |
| `agent.py`  | The Loom calls an agent makes with the Python SDK: a run with a budget, checked between steps, then closed |

## 1. Mint a token per task

```bash
loom mint-token --subject ax-fix-flaky-test --org acme \
  --namespaces ax-fix-flaky-test --roles writer --ttl-days 1
```

Treat the token as readable by anyone who can reach the AX server. As of AX
`v1alpha1` its API has no authentication, a Task's `env` is part of the spec
`ax get task` prints, and code in the sandbox reads its own environment (AX's
`SECURITY.md`). So:

- one namespace per task or team, never a shared one;
- `writer` at most, never `admin`;
- the shortest lifetime the task needs (`--ttl-days 1`), and a new token for a
  new Task (AX tasks are immutable once created).

## 2. Build the image

The default AX task-runner image has Python and git but not the Loom SDK. Extend
it as AX's `docs/runner.md` describes and keep its entrypoint:

```dockerfile
FROM gcr.io/ax-substrate/ate-images/ax-task-runner@sha256:<the digest AX's examples pin>
RUN pip install --no-cache-dir clickhouse-loom
```

An agent that reaches Loom through MCP only (Claude Code reads the `.mcp.json` the
Workspace writes) needs its own CLI in the image instead.

## 3. Apply

Fill in the `<...>` values in `task.yaml`, then:

```bash
ax apply -f task.yaml
ax resume task fix-flaky-test
ax watch task fix-flaky-test
```

The Workspace comes first in the file: AX creates the documents in order, and
a Task created before its Workspace starts with an empty one.

## Budgets

`agent.py` opens its run with `budget_usd` and `budget_seconds`. `run.get`
answers `over_budget` (`cost`, `time`) once the run goes past either, and the
agent decides what to do; Loom never stops it. `cost_usd` counts the turns the
Claude Code or Codex plugin recorded for the run's session, so an agent without
a plugin reports its own spend with `cost.record_turns`, or has only the time
budget. See [runs](../../docs/features/runs.md#budgets).
