---
title: Build a knowledge factory for AI agents
description: Use Loom to collect source documents, review what agents learn, and reuse cited knowledge across agents and sessions.
---

# Build a knowledge factory for AI agents

A knowledge factory turns source documents and lessons from agent work into knowledge
that teams can review and reuse. Loom stores versioned documents, shared memories,
and links from lessons to code, so a later agent can retrieve a decision and see the
evidence behind it.

## Decide what to keep

| What you learned                                      | Where to keep it                                  | What the next agent can inspect                                     |
| ----------------------------------------------------- | ------------------------------------------------- | ------------------------------------------------------------------- |
| A policy, runbook, or architectural reference         | [Knowledge documents](../features/knowledge.md)   | A cited passage, document revision, and source identifier           |
| A decision or correction that applies across sessions | [Shared agent memory](agent-memory.md)            | The claim, who wrote it, and how it changed                         |
| A lesson about a specific implementation              | [Memory linked to code](memory-linked-to-code.md) | The definition it refers to and whether that definition has changed |
| What an agent tried and how the task ended            | [Agent runs](../features/runs.md)                 | The goal, outcome, and recorded activity                            |

Group sources by readership. Grant agents access to the namespaces their work needs
and give write access to the people and agents who maintain those sources.
[Namespace membership and grants](../features/namespaces-and-sharing.md#membership-and-grants)
explains how a team deployment scopes those permissions.

## Turn a missing answer into reusable knowledge

Suppose an agent preparing a deployment cannot find the condition that triggers a
rollback. Use the question to test the workflow end to end:

1. **Search your sources.** Run [unified search](../features/unified-search.md) from the
   agent's authorized scope. Check whether a returned passage answers the question
   and record its source and revision.
2. **Review the gap.** A person or agent with the right access checks the deployment
   policy and asks its owner to resolve any ambiguity. Namespace admins can also
   inspect recurring unanswered questions through [knowledge gaps](../features/knowledge.md#knowledge-gaps)
   when the deployment enables gap detection and query recording.
3. **Publish the answer.** Update the source runbook and [sync it into Loom](../features/knowledge.md#syncing-a-source).
   If the review produces a decision worth keeping across tasks, save the short
   claim with a [source document reference](../features/knowledge.md#how-do-agents-connect-a-memory-to-a-source-document).
   Link implementation-specific lessons to the relevant code definition.
4. **Test it from a fresh session.** Ask the same question without supplying the
   answer. Require the cited passage and revision, and open the source to confirm
   it supports the answer. Check separately that an agent outside the intended
   namespace cannot read it.

## Keep the knowledge current

Update the published runbook when a rule changes. If a saved memory repeats the
old rule, revise that claim separately so its earlier version remains visible in
[history](../features/memory/history-and-provenance.md). Review
[conflicts](../features/memory/conflicts-and-refusal.md#resolving-conflicts) when agents
report incompatible facts. When the source of a code-linked lesson changes, inspect
the current definition before using the lesson to make a decision.

Start with [document search and citations](document-search.md) to connect your first
source. For a workflow in which implementation and review agents hand work to each
other, see [memory for a software factory](multi-agent.md#memory-for-a-software-factory).
