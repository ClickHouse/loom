# Procedures and learnings

This page covers what an agent learns from doing, as opposed to facts it is told:
procedures ("how to do X", a graph of steps learned from reported runs), reflections
(lessons written at the end of a run) and learnings (a review queue of candidate facts an
admin promotes). Typed records are in [writing memory](./writing.md#typed-records).

## Procedures

A procedure is a directed graph: nodes are steps or tool calls, edges are the allowed
transitions, and each edge carries text the agent reads (`condition`, `guidance`,
`pitfalls`). Loom never executes a procedure. The agent executes and reports; Loom stores
the graph, serves the relevant part, learns from outcomes and promotes what works.

It is stored as a `procedural` memory under the key `procedure_<slug of name>`, with its
run ledger in `loom.procedure_runs`.

### Operations

| Operation              | Role   | Does                                                                                                        |
| ---------------------- | ------ | ----------------------------------------------------------------------------------------------------------- |
| `procedure.add`        | writer | Register `name`, `situation` (embedded for matching), `steps` (2–64), optional `graph`, `tags`, `metadata`. |
| `procedure.match`      | viewer | Top procedures for a `situation` (and `goal`), with graph, run stats and trust `status`.                    |
| `procedure.locate`     | viewer | The active node and the transitions up to `hops` (default 2, at most 4) ahead.                              |
| `procedure.record_run` | writer | Report one execution: node ids visited (`path`), `ok`, optional `note`, cost.                               |
| `procedure.evolve`     | writer | Fold recorded runs in: proven path, dead ends, fail-prone steps, template promotion, quarantine.            |
| `procedure.revise`     | writer | Propose a new topology; the run ledger accepts or reverts it.                                               |
| `procedure.fork`       | writer | Copy a procedure's shape from another readable namespace, without its evidence.                             |
| `procedure.list`       | viewer | Inventory by key `prefix` or `tags`.                                                                        |

Field lists: [operations reference](../../reference/operations.md#procedure).

### The graph

- Node kinds: `step`, `action` (a tool call; `tool` names it), `memory`,
  `consolidation`, `decision`, `template_ref`.
- Edge kinds: `dep`, `dataflow`, `branch`, `converge`.
- `procedure.add` rejects duplicate node ids, edges to unknown nodes, self-loops and
  cycles with no exit.
- Caps: 256 nodes, 1,024 edges, 512 characters per edge attribute.
- A procedure registered with `steps` only gets a linear graph with ids `n1`, `n2`, ….

```json
{
  "nodes": [
    { "id": "cash", "kind": "action", "tool": "check_cash_in_bank" },
    { "id": "forecast", "kind": "action", "tool": "cash_flow_forecast" },
    { "id": "raise", "kind": "action", "tool": "fund_raising_request" },
    { "id": "close", "kind": "action", "tool": "book_closing" }
  ],
  "edges": [
    { "src": "cash", "dst": "forecast", "kind": "dataflow" },
    {
      "src": "forecast",
      "dst": "raise",
      "kind": "branch",
      "condition": "projected runway below the safety buffer",
      "pitfalls": "do not stack a second request while one is pending"
    },
    { "src": "forecast", "dst": "close", "kind": "branch", "condition": "runway healthy" },
    { "src": "raise", "dst": "close" }
  ]
}
```

A `procedural` row written with `memory.set` that is not a procedure body (a non-empty
`situation` and at least two `steps`) is stored as `semantic`, tagged
`retyped_from:procedural`.

### Matching

`procedure.match` ranks by similarity of the caller's `situation` to each stored one,
across the caller's own namespaces, up to the fan-out cap (`namespace_scope="all_mine"`).
It can add:

| Term         | Effect                                                                                      |
| ------------ | ------------------------------------------------------------------------------------------- |
| Tool overlap | `recent_tools` boosts procedures whose action nodes cover them (`tool_boost`, default 0.2). |
| Goal         | With `goal`, situation and goal are scored separately and combined as a geometric mean.     |
| Utility      | `utility_weight` (default 0 = off) adds the ledger's smoothed success rate.                 |

Every match carries a trust `status`:

| Status        | Meaning                                                                                                                   |
| ------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `template`    | Promoted: at least 5 successful runs, a success rate of at least 0.8, and a topology `evolve` no longer changes.          |
| `exercised`   | Has at least one recorded run.                                                                                            |
| `authored`    | Registered and never run.                                                                                                 |
| `candidate`   | Machine-mined and never run: listed last, labelled `(unproven)` in `context.pack`, dropped by `include_candidates=false`. |
| `quarantined` | Its last `strike_limit` runs all failed; dropped unless `include_quarantined=true`. The next success lifts it.            |

`templates_only=true` returns templates alone. `for_agent` shows the calling agent's own
runs; `run_stats_by_agent` breaks them down per agent.

### Localised guidance

`procedure.locate` takes `memory_key` and `at` (the node id or tool just used) or
`recent_path`. It returns the active node and the transitions ahead, each with its
condition, guidance, pitfalls and how often earlier runs took it and succeeded, plus a
prompt-ready `rendered` text. With `recent_path` it marks visited transitions, sets
`looping` with the untaken `exits` on a revisit, and attaches other runs'
`failure_notes`. When `at` matches nothing, `matched=false` and `entry_nodes` say where
to start.

### Learning from runs

`procedure.record_run` appends one row to `loom.procedure_runs`: the path, `ok`, `note`,
graph `revision`, `steps_taken`, `duration_ms` and `tokens`. `run.complete` also records
a run (with an empty path) for any procedure key in `memories_used`; an agent that calls
`record_run` itself should leave the key out of `memories_used`
([runs](../runs.md)).

`procedure.evolve` folds runs newer than the `ledger_folded_through` watermark. Once
`min_runs` (default 3) are in, it:

- follows the edges with the most successful traversals as the proven path;
- flags step and action nodes no run used as dead ends (reversible);
- flags nodes and transitions whose failure rate clearly exceeds the procedure's base
  rate as `fail_prone_steps` / `fail_prone_transitions`: read them as pitfalls;
- promotes a procedure that meets the template bar;
- quarantines one whose last `strike_limit` runs all failed
  (`LOOM_PROCEDURE_STRIKE_LIMIT`, default 3; 0 = off).

### Revisions are gated

`procedure.revise` takes a full `graph` and a `reason`, keeps the statistics of surviving
node ids, and holds the prior graph as `pending_revision`. Later runs carry the new
revision, and `procedure.evolve` compares them with the prior revision's:

| Gate                         | Parameters (defaults)                                                                                                                 |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `gate_mode="sprt"` (default) | Sequential probability ratio test: `gate_min_drop` 0.15, `gate_alpha` 0.05, `gate_beta` 0.20, `gate_max_runs` 50, `gate_min_runs` 3.  |
| `gate_mode="margin"`         | A fixed `gate_margin` on the success-rate difference.                                                                                 |
| Cost                         | A revision whose successful runs cost more than `gate_cost_tolerance` (0.25) above the prior's is reverted (`gate_cost_min_samples`). |

The sequential test decides as soon as the evidence is strong enough. A reverted revision
goes into `rejected_revisions`, and `procedure.revise` refuses to re-propose it unless
`force=true`.

### Whose procedures a pack injects

`context.pack` and `memory.stack` inject a matched procedure as steps to follow, so with
auth on they carry only procedures the calling principal wrote (the row's `actor`). A
teammate's procedure in a shared namespace is still returned by an explicit
`procedure.match`, whose matches carry `actor`. With auth off every match is injected.

### Measuring whether a procedure helps

`context.pack` can withhold its top-matched procedure with probability
`procedure_explore_rate` (default 0).
The pack lists shown procedures in `procedure_keys` and withheld ones in
`procedure_keys_withheld`, so session outcomes can be compared.

### Example

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as bot:
    added = await bot.procedure.add(
        "refund request",
        "a customer asks for a refund on a paid invoice",
        ["look up the invoice", "check the refund policy window", "issue or decline"],
        tags=["billing"],
    )
    hits = await bot.procedure.match(
        "customer wants money back for last month's invoice", goal="resolve a refund"
    )
    key = added["memory_key"]  # procedure_refund_request
    await bot.procedure.record_run(key, ["n1", "n2", "n3"], ok=True, duration_ms=42_000)
    await bot.procedure.evolve(key)
```

The SDK's `evolve` sends `gate_min_runs=5` and `gate_margin=0.05` unless told otherwise.
The SDK's `add` takes `steps` only; register an explicit `graph` over HTTP or MCP.

`memory.delete` on a procedure key also deletes its ledger rows.

## Reflections

A reflection is a lesson about a run (`what_worked`, `what_failed`, `lessons`, with
`evidence` keys), stored as a `reflection` memory. It is embedded but left out of
unfiltered recall, because it is a hypothesis about process, not a fact. Read reflections
with `memory.search(memory_type="reflection")`.

- `run.complete(reflection=...)` writes the agent's own lesson ([runs](../runs.md)).
- The `reflection_distill` worker (off by default; makes model calls) pairs confidently
  failed sessions with their nearest successful ones and distils the difference.

## Learnings review queue

A learning is a candidate fact with its supporters (`supporter_count`, `supporters`,
`confidence_avg`, `rationale`, `source_kind`), waiting for a person to decide. Loom
records the reviewer's choice and never makes it. Any candidate other than a bot's rule
(below) is written by your own process to `loom.learnings_queue`.

Extraction files one kind of candidate itself. With assistant-turn learnings on
(`LOOM_MEMORY_EXTRACT_AGENT_LEARNINGS`, off by default), a rule a bot states about its
own behaviour in a conversation becomes an `assistant_turn` candidate for that agent
(`agent`), never a memory ([extraction](./extraction.md#rules-the-assistant-states-about-itself)). The candidate
row holds the rule and no user's words. Each session that states the rule adds a row to
`loom.learnings_supporters` with that user's own quotes. When the candidate is listed,
`supporter_count` is the distinct end users, `supporter_sessions` the distinct
sessions, `supporter_writers` the distinct principals that wrote those rows (a token
not pinned to one user can send any `user_id`, so many users from one writer is one
source), and the `rationale` the earliest remaining supporter's quotes. Support never
promotes it; promoting it publishes the rule to the namespace's knowledge, tagged
`agent:<agent>`. `context.pack` with `include_knowledge`, `memory.stack`, `loom_search`
and `memory.ask` serve it to that agent's reads for every user and leave it out of other
agents' reads. Over HTTP, a namespace-wide read (agent `*`, the `memory.ask` default)
is served every bot's rules, and a read under no agent (`-`, the `loom_search`
default) is served no rule promoted for a named bot. Over MCP an unnamed `agent` is the
client's (`claude-code`, ...), so those reads serve that agent's rules. A rule for
every agent is served to every read. A token pinned to one end user
(`read_user_id`) acts as the agent it names, never the body's `agent`; any other token,
an `own_user` token without a pin included, acts as the body's `agent`. A token Loom mints with a
`read_user_id` pin names no agent, so it files no bot's rule and is served no rule
promoted for a named bot. `knowledge.search` is not filtered by agent: any agent that
can read the namespace finds it there.

A user erasure deletes that user's supporter rows, and their quotes with them. A
candidate left with no supporter is deleted when pending. When decided, it keeps its
status row with the rule blanked, so a rejected rule never comes back as pending.

`learnings.quality` counts the conversation seeder by default, since it reports
extraction quality; pass `source_kind="assistant_turn"` for the bot rules.

| Op                  | Role   | Does                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `learnings.list`    | viewer | Candidates by `status` (`pending` by default, `promoted`, `rejected`) and `source_kind`.                                                                                                                                                                                                                                                                                                                                                    |
| `learnings.review`  | admin  | `decision` `promote` or `reject`, with `reason`. `destination`: `memory` (the shared zone for a supporters candidate, a namespace row under the approver's `user_id` for a conversation-seeded fact, the agent's `user_id` '-' row for a bot's rule), `knowledge` or `both`; unset, `knowledge` for a bot's rule and `memory` otherwise. A `definition` candidate is refused for `memory` and `both`: it is promoted with `memory.promote`. |
| `learnings.quality` | viewer | Per-namespace pending / promoted / rejected counts and the approve ratio.                                                                                                                                                                                                                                                                                                                                                                   |

`learnings.list` and `learnings.review` are HTTP and SDK only (`bot.learnings`). A
promotion to `memory` carries the candidate id, so
[`memory.lineage`](./history-and-provenance.md#lineage) traces it back to the candidate.
Reviewing a candidate again with the decision it already holds is a no-op; rejecting a
promoted candidate marks it rejected and leaves what the promotion wrote (delete a
knowledge promotion's `learning://<candidate_id>` document with `knowledge.delete` to
stop serving it). A bot's rule whose last supporter was erased answers 404, as a missing
candidate does. Field lists:
[operations reference](../../reference/operations.md#learnings).

## Settings

| Setting                               | Effect                                                                                                                                                          |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `LOOM_PROCEDURE_STRIKE_LIMIT`         | Consecutive failed runs before quarantine ([Workers](../../reference/configuration.md#workers)).                                                                |
| `LOOM_REFLECTION_DISTILL_*`           | The reflection distillation worker ([Consolidation](../../reference/configuration.md#consolidation)).                                                           |
| `LOOM_MEMORY_EXTRACT_AGENT_LEARNINGS` | Off by default: extraction files the assistant's rules about itself as `assistant_turn` candidates ([Extraction](../../reference/configuration.md#extraction)). |

## Limits

- **Evolve is explicit.** Nothing folds the ledger on a timer; call `procedure.evolve`
  after runs land.
- **A path must name real node ids** of the stored graph.
- **The graph keeps the last 50 traces**; aggregate run statistics survive truncation.
- **Forked procedures start without evidence.**

## Related

- [Runs](../runs.md): `run.complete`, `memories_used` and reflections.
- [Working memory and profiles](./working-memory-and-profiles.md): how `context.pack`
  shows procedures.
- [Namespaces and sharing](../namespaces-and-sharing.md): the shared zone a supporters
  candidate promotes into.
- [Knowledge](../knowledge.md): where a bot's rule promotes by default.
