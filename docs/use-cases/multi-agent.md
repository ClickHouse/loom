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
- **Working memory.** Text an agent is given every time instead of finding it by
  search, separate from long-term memory. [Working memory and profiles](../features/memory/working-memory-and-profiles.md)
- **Context packs.** `context.pack` builds the bounded bundle an agent loads at the start
  of a task. [Unified search and context packs](../features/unified-search.md)
- **Framework adapters.** The SDKs present Loom through agent frameworks' own memory
  interfaces. [Agent frameworks](../features/integrations/frameworks.md)

## Start

[Integrate Loom into an agent](../guides/agent-integration.md) covers the identity each
agent sends and what to write; [tokens and grants](../guides/tokens-and-grants.md) gives
each agent its own token.
