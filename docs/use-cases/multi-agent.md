# Shared context for multi-agent workflows

Loom is a self-hosted MCP server for multi-agent memory, with namespace-scoped tokens and
separate read and write roles. Agents share context through memory, task inboxes and
decision records within the namespaces they can access. Your agents and orchestrator run
the workflow; Loom keeps what they learn and hand over.

## What you get

- **Shared memory with provenance.** Every row records which agent wrote it, and under the
  default `auto_reinforce` policy a contradiction between two agents is flagged for review
  instead of silently overwritten. [Namespaces and sharing](../features/namespaces-and-sharing.md) ·
  [Namespace conflict policy](../features/memory/conflicts-and-refusal.md#namespace-conflict-policy)
- **Task inboxes.** Each agent has a durable queue with leases, retries and review rules;
  a claimed task has one owner at a time. [Tasks, decisions and policies](../features/tasks-and-decisions.md)
- **Decisions.** What was decided in a case like this, why, and whether it worked.
  [Decisions](../features/tasks-and-decisions.md#decisions)
- **Working memory.** Text an agent is given every time instead of finding it by
  search, separate from long-term memory. [Working memory and profiles](../features/memory/working-memory-and-profiles.md)
- **Context packs.** `context.pack` builds the bounded bundle an agent loads at the start
  of a task. [Unified search and context packs](../features/unified-search.md)
- **Session briefs.** The resumable state of one line of work: goal, status, decisions,
  dead ends and what is done. One agent saves it with `session.brief_save` and the next
  loads it with `session.brief_get`. [Session briefs](../features/conversations-and-sessions.md#session-briefs)
- **Framework adapters.** The SDKs present Loom through agent frameworks' own memory
  interfaces, including [Mastra threads, messages and resource working memory](../features/integrations/frameworks.md#mastra).
  [Agent frameworks](../features/integrations/frameworks.md)

## Two agents sharing a research note

On one person's laptop, the [single-user quickstart](../getting-started/quickstart.md)
lets two connected agents read the same namespace. Connect both clients with the
[connect guide](../getting-started/connect-a-coding-agent.md); for example, use Claude
Code to collect a finding and Cursor to write from it.

1. In the first client, ask it to use `loom_memory_set` to save
   `example/research/next-step` with the value “Check the paper's evaluation dataset
   before adding it to the reading list.” Check the returned `memory_id` and find the
   row in the dashboard.
2. In the other client, ask it to use `loom_memory_get` to read that exact key. Require
   the tool response, including `found`, `value` and `agent`, before it drafts a next
   step. It should read the first client's value and identify its writer.
3. Delete the example key from the client that wrote it when finished.

The agents share a saved fact; Loom does not run them or schedule their work. For agents
on different people's machines, use an [authenticated team deployment](../getting-started/quickstart.md#start-in-multi-user-mode),
with each person's own token granting access to the same namespace. A local single-user
run does not test those access controls.

## Memory for a software factory

When autonomous coding agents take an issue through implementation and review, Loom keeps
shared context, source-linked lessons and reported outcomes between those stages. Your
agents and orchestrator execute the work and enforce review and CI requirements.

Use an indexed repository and separate agent credentials with access to the shared
namespace. Check [shared recall and denied access](../getting-started/evaluate.md#shared-memory-across-clients)
before relying on the deployment for team work.

1. **Recover the task.** Load relevant context with `context.pack`, keeping the issue,
   acceptance criteria and current revision with the task. With a registered tracker,
   [check prior reports and candidate changes](../features/tracker/overview.md#from-a-bug-draft-to-candidate-prs).
   An unavailable or degraded duplicate check leaves that question unresolved.
2. **Inspect the source.** Read the current definitions, callers and affected code before
   changing it. Follow the [code intelligence walkthrough](code-intelligence.md); use local
   tools for uncommitted changes the index has not seen.
3. **Keep the reason.** Save a durable lesson or decision with its evidence and the
   qualified definitions it concerns. Follow the [repository tag and namespace rules](memory-linked-to-code.md#start)
   so the code-link worker can resolve it. Keep the current plan in working memory.
4. **Hand off the evidence.** Pass the issue, revision, lesson keys, completed checks and
   open questions to the next agent. The [planner and worker example](https://github.com/ClickHouse/loom/tree/main/sdk/python/examples#multi_agent_handoffpy)
   shows fenced task ownership, separate credentials and result provenance.
5. **Review against current code.** Inspect the diff and the lesson's link state. A
   `changed` link calls for checking the claim; it does not establish that the claim is
   false. Follow [check a lesson after a code edit](memory-linked-to-code.md#check-a-lesson-after-a-code-edit).
   Treat the agent's reported completion separately from review and CI results.
6. **Record the outcome.** Correct an outdated claim at the same memory key. Record which
   memories each run relied on and close the run once, following the [run and procedure reporting rules](autonomous-agents.md#check-one-agent-task).
   Keep failed checks and unresolved questions with the handoff.

The [Python software-factory reference](https://github.com/ClickHouse/loom/tree/main/sdk/python/examples#software_factory_memorypy)
implements this evidence handoff with separate implementation and review credentials.
Start with its indexed public repository and review-inbox setup. It compares the served
function body and reports operation receipts; it does not generate a patch, decide whether
a lesson is true or run code review. The example documents cleanup limits and supplies
no captured live output.

## Start

[Integrate Loom into an agent](../guides/agent-integration.md) covers the identity each
agent sends and what to write; [tokens and grants](../guides/tokens-and-grants.md) gives
each agent its own token.

Run the [planner and worker example](https://github.com/ClickHouse/loom/tree/main/sdk/python/examples#multi_agent_handoffpy)
to follow a task from posting through fenced completion and read its result with
provenance. It uses separate tokens and deletes the result memory after the handoff.

Some ops on this page are marked experimental on the [status page](../reference/status.md).
