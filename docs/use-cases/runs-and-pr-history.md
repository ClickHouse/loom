# Agent runs, traces and PR history

Loom records the work coding agents do, as runs and searchable session conversations, and
connects it to your code history. Runs carry goals and outcomes, and model costs when the
Claude Code plugin reports the session's turn usage (on by default). The history covers
merged pull requests, review comments, GitHub issues and, with a tracker source, incidents
and CI runs. Conversation recording, PR history and the tracker are each off until turned
on. An agent or a person can then ask what was tried before, what was decided and which
change introduced a regression.

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

## Start

[Connect a coding agent](../getting-started/connect-a-coding-agent.md) installs the
plugins that capture sessions once recording is on; the [code index](code-intelligence.md)
needs a repository added and `LOOM_CODE_INGEST_ENABLED=true`.

Some ops on this page are marked experimental on the [status page](../reference/status.md).
