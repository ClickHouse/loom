---
title: Memory for coding agents across sessions and tools
description: Choose and evaluate shared memory for coding agents with cross-session recall, scoped access, corrected facts and lessons linked to current code.
---

# Memory for coding agents across sessions and tools

<a id="long-term-and-shared-memory-for-ai-agents"></a>

Loom is a self-hosted MCP memory server and HTTP API for coding agents that need to share
project decisions, corrections and lessons across sessions and tools. A fact saved by
one authorized agent can be retrieved by another; a lesson linked to indexed code can
surface with that code and carry a changed-source warning.

Use Loom when those agents need a shared service that your team operates. Keep rules
that belong in every checkout in version control. Connecting an MCP client supplies
operations; automatic session capture depends on the client's integration and settings.
[Connect a coding agent](../getting-started/connect-a-coding-agent.md) describes both paths
and shows how to [verify a memory handoff from Claude Code to Codex](../getting-started/connect-a-coding-agent.md#verify-shared-memory-between-claude-code-and-codex).
The [MCP memory check](../features/integrations/mcp-clients.md#verify-shared-memory-through-mcp)
tests save and recall through separate client sessions.

## What is agentic memory?

Agentic memory is information an AI agent can retrieve and update as it works. It lets a
new session reuse a tested workaround, respect an earlier decision, or correct a fact
when the evidence changes. The agent's context window holds the text it has available
for the current request. Persistent memory makes selected information available to
future requests without loading the entire conversation.

For example, an agent can save why a retry needs an idempotency key and link the lesson
to the function that enforces it. A later agent can retrieve the lesson with the
function and check the source before changing the retry. Use [memory history and provenance](../features/memory/history-and-provenance.md)
to see who saved the lesson and how it changed.

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
To measure the effect on completed work, [compare coding tasks with and without memory](../getting-started/evaluate.md#compare-coding-agents-with-and-without-memory)
using the same acceptance tests.

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

## Pass a coding task's review rule to the next session

Start the [single-user quickstart](../getting-started/quickstart.md#fast-path-released-image-no-clone).
The calls below use its loopback HTTP endpoint with authentication off. They save a
disposable example rule, not a claim about your repository's test policy. No model key
is needed for this exact-key check. An authenticated deployment instead needs the
[token and identity fields](../getting-started/quickstart.md#4-first-memory-over-http)
for the caller's permitted namespace; do not expose the single-user endpoint to a team.

Save the review rule for an example coding task:

```bash
curl --fail-with-body --silent --show-error \
  http://localhost:7777/v1/memory.set \
  -H 'Content-Type: application/json' \
  -d '{"memory_key":"example/coding/review-policy","value":"Require the focused test result before marking the example coding task complete."}'
```

Check the successful response's `memory_id` and value in **Memories**. A refused write
is not a saved rule. In a fresh shell, read it without the previous conversation:

```bash
curl --fail-with-body --silent --show-error \
  http://localhost:7777/v1/memory.get \
  -H 'Content-Type: application/json' \
  -d '{"memory_key":"example/coding/review-policy"}'
```

Require `found: true` and the stored rule. A connected coding agent can make the same
read with `loom_memory_get` before planning its next step. Keep the actual test output
as task evidence; recalling a rule does not run the tests or prove task completion.
Delete the disposable key in the dashboard when finished.

This checks persistence and wiring. It does not qualify semantic retrieval, a client's
automatic tool selection, cross-user isolation or better code. Use the
[evaluation checks](../getting-started/evaluate.md#shared-memory-across-clients) for team
sharing and denied access, and test key-free questions and abstention separately.

## Connect a remembered lesson to its evidence

| Coding need                                                           | Use Loom for                                                                                                           | Prerequisite and proof                                                                                                                                                                                                          |
| --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Another agent must reuse a project decision                           | Store the decision once and read it from an authorized session.                                                        | [Shared-memory evaluation](../getting-started/evaluate.md#shared-memory-across-clients) tests successful recall separately from denied access.                                                                                  |
| A lesson concerns a function that can change                          | Attach the lesson to a definition in an indexed repository and inspect its link state before reuse.                    | The [code-linked walkthrough](memory-linked-to-code.md#check-a-lesson-after-a-code-edit) names the indexing and worker prerequisites, checks across commits and the limits of those checks.                                     |
| The agent needs a feature's implementation or a change's dependencies | Query the centrally populated repository index instead of asking each agent to clone and search the entire repository. | [Code intelligence](code-intelligence.md) describes repository setup and supported lookups. An authorized, populated index is required; a registered repository alone is insufficient.                                          |
| Repeated exploration or decision-making may waste model context       | Retrieve relevant source and prior lessons, then measure completed tasks with and without retrieval.                   | [Coding-agent cost](coding-agent-cost.md#compare-completed-tasks-including-overhead) separates task success, model bills, indexing, server costs and elapsed time. Fewer prompt tokens alone do not establish lower total cost. |

A code link reports the source state, not whether a lesson is true. Uncommitted edits
do not update it. Read changed code and recheck the claim before applying it. A working
save/read example or a published integration does not establish independent adoption,
answer quality or a performance advantage.

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
