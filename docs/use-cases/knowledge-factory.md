---
title: Build a knowledge factory for AI agents
description: Build a knowledge factory with Loom. Publish a runbook, retrieve a cited revision, and update a saved lesson while keeping its history.
---

# Build a knowledge factory for AI agents

A knowledge factory turns source documents and lessons from agent work into knowledge
that teams can review and reuse. Loom stores versioned documents, shared memories,
and links from lessons to code, so a later agent can retrieve a decision and see the
evidence behind it.

Use the [knowledge factory pilot scorecard](https://loom.clickhouse.com/knowledge-factory#pilot-scorecard)
to choose what to check before running this walkthrough.

## Decide what to keep

| What you learned                                      | Where to keep it                                                                                               | What the next agent can inspect                                           |
| ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| A policy, runbook, or architectural reference         | [Knowledge documents](../features/knowledge.md)                                                                | A cited passage, document revision, and source identifier                 |
| A decision or correction that applies across sessions | [Shared agent memory](agent-memory.md)                                                                         | The claim, who wrote it, and how it changed                               |
| A lesson about a specific implementation              | [Memory linked to code](memory-linked-to-code.md)                                                              | The definition it refers to and whether that definition has changed       |
| What an agent tried and how the task ended            | [Agent runs](../features/runs.md)                                                                              | The goal, outcome, and recorded activity                                  |
| Steps agents can reuse for a recurring task           | [Procedural memory](../features/memory/procedures-and-learnings.md#use-procedural-memory-for-a-recurring-task) | The matching procedure, its trust status, and outcomes from reported runs |

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

## Run a knowledge factory walkthrough

Use a disposable namespace on a [Loom server with authentication enabled](../getting-started/quickstart.md#2-open-the-dashboard),
and have `curl` and `jq` installed. Set `LOOM_BASE_URL`, `LOOM_ORG` and
`LOOM_NAMESPACE` to that server and scope. Set `LOOM_WRITER_TOKEN` and
`LOOM_READER_TOKEN` to tokens with writer and viewer access to the namespace,
respectively. Use the same shell for the commands below.
[Create scoped tokens](../guides/tokens-and-grants.md#mint-a-token)

### Check what the reader can find

```sh
call() {
  jq -cn --arg org "$LOOM_ORG" --arg ns "$LOOM_NAMESPACE" \
    --argjson request "$3" '$request + {org: $org, namespace: $ns}' |
    curl -fsS "$LOOM_BASE_URL/v1/$2" -H "Authorization: Bearer $1" \
      -H 'Content-Type: application/json' --data-binary @-
}

source_uri='urn:loom:example:rollout-runbook'
find_rule() {
  call "$LOOM_READER_TOKEN" knowledge.search "$(jq -cn --arg uri "$source_uri" '{
    query: "deployment rollback threshold", uri_prefix: [$uri],
    namespace_scope: "this", search_mode: "hybrid", rerank: "none"
  }')"
}

find_rule | jq -e '.results | length == 0'
```

On an empty namespace this prints `true`. Treat the gap as a question for the
runbook owner. In this example the owner approves a rule based on failed health checks.

### Publish the approved rule and save a lesson

```sh
publish_rule() {
  call "$LOOM_WRITER_TOKEN" knowledge.upsert "$(jq -cn \
    --arg uri "$source_uri" --arg rule "$1" '{
      source_uri: $uri, title: "Deployment runbook",
      content: ("# Deployment rollback threshold\n\n" + $rule)
    }')"
}

rule='Roll back the deployment if at least 5 health checks fail within 60 seconds.'
reviewed_doc_id=$(publish_rule "$rule" | jq -er .doc_id)
find_rule | jq -e '.results[0] | select(.revision == 1) | {source_uri, section, revision, content}'

lesson='{"agent":"curator","memory_key":"example/rollback-threshold"}'
call "$LOOM_WRITER_TOKEN" memory.set "$(jq -cn --argjson lesson "$lesson" \
  --arg rule "$rule" --arg doc_id "$reviewed_doc_id" \
  '$lesson + {value: $rule, source_resource_id: $doc_id}')" | jq -e .memory_id
```

The reader's search returns the approved passage at revision `1`. The memory
stores a short lesson with the ID of the document version the curator reviewed.

### Check the lesson after a runbook change

```sh
revised_rule='Roll back the deployment if at least 3 health checks fail within 60 seconds.'
current_doc_id=
if published_doc_id=$(publish_rule "$revised_rule" | jq -er .doc_id) &&
  test -n "$published_doc_id" &&
  find_rule | jq -e --arg revised "$revised_rule" '
    .results[0] | select(.revision == 2 and (.content | contains($revised))) |
    {source_uri, section, revision, content}' &&
  lesson_doc_id=$(call "$LOOM_READER_TOKEN" memory.provenance "$lesson" | jq -er .source_resource_id) &&
  test -n "$lesson_doc_id" &&
  test "$lesson_doc_id" = "$reviewed_doc_id" &&
  test "$lesson_doc_id" != "$published_doc_id"; then
  current_doc_id=$published_doc_id
else
  printf '%s\n' 'Check the published revision and the lesson citation before continuing.' >&2
fi
```

Search now returns revision `2` under the same URI. The final check confirms
that the lesson still points to the version the curator reviewed.

### Revise the lesson and verify its history

After the source owner approves the revised rule, the curator updates the same
memory key and cites the reviewed document ID. Read the hash of the old value
to guard the write. If another writer changes the lesson's value before the
update, Loom returns HTTP 409. Review the current lesson and source before retrying.

```sh
revise_lesson() {
  test -n "${current_doc_id:-}" || return 1
  lesson_hash=$(call "$LOOM_READER_TOKEN" memory.get "$lesson" |
    jq -er --arg old "$rule" 'select(.found and .value == $old) | .content_sha256') || return 1
  test -n "$lesson_hash" || return 1

  call "$LOOM_WRITER_TOKEN" memory.set "$(jq -cn --argjson lesson "$lesson" \
    --arg value "$revised_rule" --arg doc_id "$current_doc_id" --arg hash "$lesson_hash" \
    '$lesson + {value: $value, source_resource_id: $doc_id, if_match_sha256: $hash}')" |
    jq -e .memory_id
}

revise_lesson &&
  call "$LOOM_READER_TOKEN" memory.get "$lesson" |
    jq -e --arg revised "$revised_rule" '.found and .value == $revised' &&
  call "$LOOM_READER_TOKEN" memory.provenance "$lesson" |
    jq -e --arg doc_id "$current_doc_id" '.source_resource_id == $doc_id' &&
  call "$LOOM_READER_TOKEN" memory.history "$lesson" |
    jq -e --arg old "$rule" 'any(.rows[]; .value == $old)'
```

The checks print `true` for the updated wording, the citation to the reviewed
document revision, and the old wording retained in memory history.

Repeat `find_rule` and the lesson's `memory.get` with a token whose viewer access
is scoped to a different namespace. Expect HTTP 403 for each request.

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
