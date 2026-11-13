# Long-term and shared memory for AI agents

Loom is a self-hosted MCP server and HTTP API that gives AI agents long-term memory they
share: a fact one agent saves in a session can be found by the next session, by another
agent, or by a person at the dashboard. This page shows how that works and where to start.

## What you get

- **One claim per key, in a namespace.** `memory.set` saves a fact; `memory.get` reads it
  back by key; `memory.search` and `loom_search` find it by meaning. A namespace is the
  unit of sharing: by default, every member reads every row in it.
  [Memory](../concepts/memory.md)
- **Memory that can be corrected.** A change makes a new version with its history. The
  same fact after canonicalisation merges into the stored row, the same fact in other
  words is stored as a duplicate, a contradiction is flagged for review, and a write
  against a pinned fact is refused.
  [Conflicts and refusal](../features/memory/conflicts-and-refusal.md) ·
  [History and provenance](../features/memory/history-and-provenance.md)
- **Search that abstains.** When nothing clears the similarity floor, `memory.search`
  answers `abstain=true` with a reason code and labels each weak hit `below_threshold`;
  `loom_search` drops them. [Search and ask](../features/memory/search-and-ask.md)
- **Long-term memory for your coding agent, shared across agents.** The Claude Code and
  Codex plugins load relevant context at session start and capture the session as it
  runs. Cursor, GitHub Copilot, Gemini CLI, Cline, Kilo Code, OpenCode, goose, Zed and other
  MCP clients connect to the same server, and an agent on a namespace finds the facts
  other agents saved there.
  [Connect a coding agent](../getting-started/connect-a-coding-agent.md) ·
  [MCP clients](../features/integrations/mcp-clients.md)
- **Per-user memory for your own agent.** The Python and TypeScript SDKs write and read
  memory per end user. [Your first agent](../getting-started/first-agent.md)

## Measured

On 100 paired LongMemEval-S questions with the same `gpt-5` reader, answers from Loom's
retrieved memories scored 94% against 90% from the whole history, with 91% fewer reader
prompt tokens (excluding the one-time cost of building the memory); the accuracy
difference is not statistically established.
[With and without Loom](../benchmarks/results.md#with-and-without-loom)

## Start

[Quickstart](../getting-started/quickstart.md) boots the local stack and saves a first
memory over HTTP, Python and MCP. [When Loom does not fit](../getting-started/overview.md#when-it-does-not-fit)
covers the cases an in-process memory library serves better.

Some ops on this page are marked experimental on the [status page](../reference/status.md).
