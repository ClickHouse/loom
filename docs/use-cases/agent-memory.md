---
title: Memory for coding agents across sessions and tools
description: Choose and evaluate shared memory for coding agents with cross-session recall, scoped access, corrected facts and lessons linked to current code.
---

# Memory for coding agents across sessions and tools

<a id="long-term-and-shared-memory-for-ai-agents"></a>

Loom is a self-hosted MCP server and HTTP API that gives AI agents long-term memory they
share: a fact one agent saves in a session can be found by the next session, by another
agent, or by a person at the dashboard. This page shows how that works and where to start.

## Choosing a memory tool for coding agents

The best memory tool for a coding agent depends on what it needs to remember. Keep
repository instructions in version control when every checkout needs the same rules.
Use a shared memory service when a correction, project decision or past fix must be
available across sessions, tools or teammates, with control over who can read it.

Check these requirements before choosing Loom:

| Requirement                    | What to check                                                                                                                                  |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Recall after a fresh session   | Save a fact, start another session and require the stored value and writer in the tool response.                                               |
| Shared context across tools    | Connect both clients to the same deployment and grant access to the intended namespace. A tool connection alone does not capture a transcript. |
| Corrections and source changes | Inspect memory history and conflicts; recheck a code-linked lesson when its source changes. A changed link does not prove the lesson false.    |
| Access and operations          | Test denied access separately, decide what each client may capture, and account for running the server and its configured providers.           |

Follow [coding-agent setup](../getting-started/connect-a-coding-agent.md) for the plugin
or MCP connection your client supports. Read its capture settings before using private
code or conversations. For a pipeline with separate implementation and review agents,
continue with [memory for a software factory](multi-agent.md#memory-for-a-software-factory).

Compare [memory architectures](../guides/compare-agent-memory.md) by deployment,
sharing, provenance and staleness handling before choosing a service or harness.

### Check one coding lesson across sessions

Use a disposable example rather than a secret or an unverified project rule:

1. Ask the connected agent to save `example/coding/review-policy` with the value
   “Require the test result before marking an example task complete.” Require a
   successful `loom_memory_set` response and check its `memory_id` in the dashboard.
2. Start a fresh session. Ask it to call `loom_memory_get` for that exact key and report
   the returned value and writer before drafting its next step. This checks stored
   recall without depending on the earlier conversation.
3. If the lesson concerns a real function, follow the [code-linked lesson walkthrough](memory-linked-to-code.md#start)
   to attach evidence to its qualified definition. Inspect the link after an edit;
   a saved rule still needs review against the current code.
4. In a team deployment, run the [shared recall and denied-access checks](../getting-started/evaluate.md#shared-memory-across-clients)
   with the intended identities. The successful read alone does not test isolation.
5. Delete the disposable key when finished. Keep the tool results, unresolved refusals
   and checks you actually ran with your evaluation.

An exact-key read checks storage and wiring. Evaluate semantic retrieval separately
with questions that do not name the key, including questions the store cannot answer.
Neither check establishes better code, fewer defects or lower total model cost. The
[evaluation guide](../getting-started/evaluate.md) separates these outcomes.

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
- **Memories linked to the code they are about.** A coding agent's lessons follow the
  definitions they name through renames and edits, and appear when an agent reads that
  code. [Memory linked to code](memory-linked-to-code.md)
- **Per-user memory for your own agent.** The Python and TypeScript SDKs write and read
  memory per end user. [Your first agent](../getting-started/first-agent.md)

For a team deployment, [evaluate cross-client recall and namespace isolation](../getting-started/evaluate.md#shared-memory-across-clients)
separately. A second client finding a shared fact does not prove that an identity
outside the team cannot read it.

## Measured

On 100 paired LongMemEval-S questions with the same `gpt-5` reader, answers from Loom's
retrieved memories scored 94% against 90% from the whole history, with 91% fewer reader
prompt tokens (excluding the one-time cost of building the memory); the accuracy
difference is not statistically established.
[With and without Loom](../benchmarks/results.md#with-and-without-loom)

## A research assistant that remembers your reading preferences

After the [quickstart](../getting-started/quickstart.md), connect Claude Desktop or
Cursor through the dashboard's [client setup](../getting-started/connect-a-coding-agent.md#how-do-i-give-cursor-or-another-mcp-client-shared-memory).
The tools work with the stock LLM: your chat app supplies the model that calls them.

1. Tell the assistant: “Use `loom_memory_set` to save my research preference under
   `example/research/source-policy`: include peer-reviewed papers in my reading list.”
2. Check that the tool response contains a `memory_id`. Open **Memories** in the
   dashboard and confirm the value and writer. If the tool refuses the write, resolve
   its reported problem before continuing.
3. Start a fresh chat. Tell the assistant: “Use `loom_memory_get` to read
   `example/research/source-policy`, then tell me which sources belong in my reading list.”
   Check that the tool returns `found: true` and the preference you saved.
4. Ask the assistant to delete that example key when finished, then confirm the
   dashboard no longer lists it as a live memory.

The second chat reads a stored preference without the first chat's transcript. This
checks persistence and client wiring; it does not test whether the assistant can judge
papers or answer research questions. For searches over the papers themselves, use
[document search](document-search.md).

## Start

[Quickstart](../getting-started/quickstart.md) boots the local stack and saves a first
memory over HTTP, Python and MCP. [When Loom does not fit](../getting-started/overview.md#when-it-does-not-fit)
covers the cases an in-process memory library serves better.

Some ops on this page are marked experimental on the [status page](../reference/status.md).

Use a [Microsoft Agent Framework context provider](../features/integrations/frameworks.md#microsoft-agent-framework)
to recall user facts before a run and record the conversation after it.

Store the [Claude API memory tool](../features/integrations/frameworks.md#claude-api-memory-tool)
file tree in Loom so agents in a namespace can share its contents.

Keep [OpenAI Agents session history](../features/integrations/frameworks.md#openai-agents-sdk)
in Loom across agent runs and handoffs.

For a visual workflow, [n8n nodes](../features/integrations/frameworks.md#n8n) connect an
AI Agent to Loom conversation history, durable memories and knowledge search.

Use the [Dify tools plugin](../features/integrations/frameworks.md#dify) to save facts,
recall a user's memories and search app documents from a workflow.

Use [Google ADK memory](../features/integrations/frameworks.md#google-adk) to recall
per-user facts and shared knowledge through ADK memory tools.

Use a [Haystack document store and retriever](../features/integrations/frameworks.md#haystack)
to search documents from a pipeline while keeping them in your Loom deployment.

Use [Agno user memories](../features/integrations/frameworks.md#agno) through its
native memory manager with a Loom database bound to each end user.
