---
title: Agent run history, duplicate checks and regression candidates
description: Trace AI agent runs to the memories they read and wrote, investigate slow Loom requests, and distinguish reported coding-agent costs from server model spend.
seo:
  title: AI agent observability and run history
---

# Agent runs, traces and PR history

Loom records agent runs and conversations, checks bug drafts for duplicates and ranks
candidate regression PRs without building the code.

Runs carry goals and outcomes, and model costs when the Claude Code plugin reports the
session's turn usage (on by default). The history covers merged pull requests, review
comments, GitHub issues and, with a tracker source, incidents and CI runs. Conversation
recording, PR history and the tracker are each off until turned on. An agent or a person
can then ask what was tried before, what was decided and which changes to investigate
for a regression.

To inspect requests associated with a saved decision, [trace its agent run with SQL](self-hosted-clickhouse.md#trace-a-saved-memory-to-its-agent-run).

Duplicate checks need a registered tracker source and a namespace the caller may read.
The [bug draft to candidate PRs](../features/tracker/overview.md#from-a-bug-draft-to-candidate-prs)
walkthrough covers tracker access and PR-history setup for both operations. Candidate ranking neither runs a bisect nor proves
which change caused the bug.

## Observe an AI agent run

Start with the record that answers the question you are investigating.

| Question                                   | Where to look                                                                                                                                                                                                                                   |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| What did the agent rely on or write?       | Read the run by `run_id`. The [run ledger](../features/runs.md#what-a-run-records) distinguishes keys the agent reported using, keys retrieved during the run, and memories written with that run ID.                                           |
| Where did one Loom request spend its time? | Capture `X-Request-Id`, then [join the request](../guides/debugging.md#2-join-the-request-across-systems) to the operation ledger, traces and ClickHouse query log. Trace detail depends on the configured exporter and sampling.               |
| What model cost is recorded for this run?  | Read `run.get` for reported model turns in the run's session and time window. An unpriced model can have token counts without a cost estimate. Check [Loom provider spend](../features/observability/cost-and-usage.md#costsummary) separately. |

Runs that overlap within a session can count the same turns. Review the
[run cost attribution rules](../features/runs.md#budgets) before adding costs across runs.

Run the [SDK ledger example](https://github.com/ClickHouse/loom/blob/main/examples/ledger_run.py)
in a disposable namespace. It records the fact the task reports using and attaches the
decision it saves to the run. Use [run details](../features/runs.md#operations) to check
those fields against the task output. For a real incident, keep the task's run ID and the
request ID for any slow operation so you can inspect both records.

## What you get

- **Runs.** `run.begin` and `run.complete` record a task's goal, status, the memories the
  agent relied on and what it wrote. [Runs](../features/runs.md)
- **Conversations and session briefs.** Recorded sessions are searchable, and a session
  brief lets the next session resume. Recording is off until the namespace owner turns it
  on with `conversation.set_logging(enabled=true)`. [Conversations and sessions](../features/conversations-and-sessions.md) ·
  [Trace and resume long agent runs](../guides/trace-and-resume.md)
- **PR history.** History of a symbol, path or line, review briefs, a pre-PR duplicate
  check and bisect candidates, as on the [code index page](code-intelligence.md).
- **Tracker records.** Duplicate issue checks, issue-to-PR links and flaky tests, read live
  from the team's own ClickHouse. Needs `LOOM_TRACKER_ORG` and a registered tracker
  source. [Tracker plane](../features/tracker/overview.md)
- **Traces and cost.** Loom emits OpenTelemetry and records its own model spend per
  operation; Langfuse and ClickStack are optional sinks.
  [Telemetry](../features/observability/telemetry.md) ·
  [Cost and usage](../features/observability/cost-and-usage.md)

## Measured

On public ClickHouse issues, `tracker.duplicate_check` ranked the older issue first for 91
of 104 recorded duplicates (0.875) and called 2 of 100 issues with no recorded link a
duplicate. The result is one tracker's, and its issues (stack traces, error codes,
consistently linked duplicates) are easier than many trackers'.
[Duplicate check on public ClickHouse issues](../benchmarks/results.md#duplicate-check-on-public-clickhouse-issues)

## Start

[Connect a coding agent](../getting-started/connect-a-coding-agent.md) installs the
plugins that capture sessions once recording is on; the [code index](code-intelligence.md)
needs a repository added and `LOOM_CODE_INGEST_ENABLED=true`.

Some ops on this page are marked experimental on the [status page](../reference/status.md).
