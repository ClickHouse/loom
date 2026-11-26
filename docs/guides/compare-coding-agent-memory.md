---
title: Compare self-hosted memory tools for coding agents
description: Compare Git instructions, file-backed MCP memory, and Loom. See which setups share decisions, track source changes, and support project access controls.
seo:
  title: Compare coding-agent memory tools
---

# Compare self-hosted memory tools for coding agents

Choose memory by what the next coding session needs. Put reviewed rules in a Git-tracked
`AGENTS.md`. Use a file-backed MCP memory server when clients on the same host need a
shared store of observations. Use Loom when readers need separately issued credentials,
warnings tied to indexed source changes, or a SQL audit of who changed a decision.

## Three setups on the same repository

The fixture contains a Python retry function. A writer saves that `should_retry`
retries on HTTP 503 and points to the source revision. A fresh reader retrieves the
same decision. The fixture then changes the function to retry on HTTP 429, without
changing the saved decision. After reading it again, the reviewer records a correction.

| What the reader checks                      | Git-tracked `AGENTS.md`                                                                           | File-backed MCP reference server                                                                           | Loom                                                                                                   |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Did the decision survive a fresh session?   | A fresh checkout contained the exact note and source revision.                                    | A separate MCP process retrieved the exact observation from the shared JSONL file.                         | A separate MCP session retrieved the same memory ID, value, and source revision.                       |
| Did the read warn that the source changed?  | The note was unchanged. Comparing its recorded revision to the current revision exposed the edit. | The read still returned the original observation. Comparing the recorded revision in Git exposed the edit. | After indexing, the original lesson returned `changed` and the exact commit that altered the function. |
| Could an operator attribute the correction? | `git log` reported both commit authors for the note.                                              | The graph returned both observations. Those records carried no writer or revision fields.                  | A scoped SQL reader retrieved the two values, version numbers, and authenticated writer identities.    |

The Git author is commit metadata. The Loom SQL actor is the subject of the credential
that performed the write. The reference server's second observation was explicitly
appended by the reviewer. It does not establish which statement is current.

## Check access across projects

The authorized reviewer retrieved the decision by key over HTTP. A third identity
with viewer access to a different project namespace in the same organization made the
same request and received `403`. The other two setups use access to the repository or
storage file. The fixture does not test repository or filesystem permissions.

When separate teams share a deployment, check both an allowed handoff and a refused
read before adding project data. Use the [cross-client access checks](../getting-started/evaluate.md#shared-memory-across-clients)
to test semantic search, lookup by memory ID, and expired credentials as separate cases.

## Operating trade-offs

- **Git instructions.** Put rules every checkout needs under existing repository review
  and access controls. Readers need the relevant checkout. Record a source revision
  when a decision refers to code that can change independently of the instruction file.
- **File-backed MCP memory.** Give both clients access to the same durable storage path
  and back up that file. The test used sequential processes on one host. Configure
  filesystem permissions for the people and processes allowed to read it.
- **Loom.** Run the server and ClickHouse, provision scoped credentials, and configure
  the repository index if you need code-change checks. Back up the stores you operate.
  A code change is surfaced after indexing. The reviewer still validates the current
  behavior before using the lesson.

If the team is deciding between options, run a small pilot on a repository it maintains.
Keep the same tasks, starting revisions, and acceptance tests. Count model use, indexing,
maintenance, and human review time before assigning a budget. See the
[paired coding-task scorecard](../getting-started/evaluate.md#compare-coding-agents-with-and-without-memory).

## Shared memory between Claude Code and Codex

In two local trials, Claude Code stored a different value over MCP and a fresh Codex
session retrieved it by key. The clients used the same local model and separate writer
and viewer credentials for the same namespace. The reader received only the key and
namespace. An independent MCP read confirmed each write before the reader started.

Tool configuration affects the handoff. An initial Claude Code attempt made no tool
call. Both trials succeeded when the prompt named the write tool as registered by
the client and the test allowed only that tool. The reverse attempt stopped when
Codex's approval policy refused the write. A successful read therefore verifies the
reader's access. A handoff also requires the writer to execute the write.

Use the [cross-client handoff procedure](../getting-started/connect-a-coding-agent.md#verify-shared-memory-between-claude-code-and-codex)
with your own clients, model, and credentials. Check the tool result for the exact
value and writer identity as well as the agent's final answer.

## Reproduce the comparison

Clone the [Loom repository](https://github.com/ClickHouse/loom) and run these commands
from its root. Use Docker, Node.js 24 or later with npm, Git, `jq`, `curl`, and `uv`.

```bash
uv sync --locked --python 3.12 --extra dev --extra code
bash examples/run_coding_agent_memory_comparison.sh
```

The runner creates a temporary repository, storage file, local server, and ClickHouse
container. It installs `@modelcontextprotocol/server-memory@2.2.1` as the MCP reference
arm and prints the two source revisions, the fields returned by the graph, the
project-access check, and the observed author and version records. If a default port is taken, set
`COMPARISON_CH_HTTP_PORT`, `COMPARISON_CH_NATIVE_PORT`, or `COMPARISON_API_PORT` to
unused ports before running it.

The comparison runner checks the protocol and storage for one fixture. It exercises
separate MCP sessions without a coding model. The comparison gives no estimates of model
accuracy, semantic recall, latency, throughput, or hosted cost.

[Read how Loom tracks code-linked decisions](../use-cases/memory-linked-to-code.md) or
[query memory history with SQL](../use-cases/self-hosted-clickhouse.md#audit-a-changed-decision-with-sql).
