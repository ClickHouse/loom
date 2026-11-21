# Shared context for multi-agent workflows

Loom gives the agents in a multi-agent workflow one place to share context: memory every
agent in a namespace reads, task inboxes for handing work from one agent to another, and a
record of decisions. Your agents and orchestrator run the workflow; Loom keeps what they
learn and hand over.

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

## Start

[Integrate Loom into an agent](../guides/agent-integration.md) covers the identity each
agent sends and what to write; [tokens and grants](../guides/tokens-and-grants.md) gives
each agent its own token.

Run the [planner and worker example](https://github.com/ClickHouse/loom/tree/main/sdk/python/examples#multi_agent_handoffpy)
to follow a task from posting through fenced completion and read its result with
provenance. It uses separate tokens and deletes the result memory after the handoff.

Some ops on this page are marked experimental on the [status page](../reference/status.md).
