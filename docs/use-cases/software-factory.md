# Agent runs, traces and PR history

Loom records the work coding agents do (runs with their goals, outcomes and model costs,
searchable session conversations) and connects it to your code history: merged pull
requests, review comments and, with a tracker source, issues, incidents and CI runs. An
agent or a person can then ask what was tried before, what was decided and which change
introduced a regression.

## What you get

- **Runs.** `run.begin` and `run.complete` record a task's goal, status, the memories the
  agent relied on and what it wrote. [Runs](../features/runs.md)
- **Conversations and session briefs.** Recorded sessions are searchable, and a session
  brief lets the next session resume. [Conversations and sessions](../features/conversations-and-sessions.md) ·
  [Trace and resume long agent runs](../guides/trace-and-resume.md)
- **PR history.** History of a symbol, path or line, review briefs, a pre-PR duplicate
  check and bisect candidates. [History, PRs and releases](../features/code-index/history-and-prs.md)
- **Tracker records.** Duplicate issue checks, issue-to-PR links and flaky tests, read live
  from the team's own ClickHouse. [Tracker plane](../features/tracker/overview.md)
- **Traces and cost.** Loom emits OpenTelemetry and records its own model spend per
  operation; Langfuse and ClickStack are optional sinks.
  [Telemetry](../features/observability/telemetry.md) ·
  [Cost and usage](../features/observability/cost-and-usage.md)

## Start

[Connect a coding agent](../getting-started/connect-a-coding-agent.md) records sessions
through the plugins; the [code index](code-intelligence.md) needs a repository added.
