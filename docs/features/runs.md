# Runs

A run is one execution of a task, such as "triage issue 1234". `run.begin` opens it and returns a `run_id`; the
agent passes that id on its memory calls; `run.complete` closes it with a status, the memories the agent relied on,
and optionally a reflection. A memory that a successful run relied on ranks higher next time; one that a failed run
relied on loses the boost it earned.

## What a run records

A run row lives in `loom.agent_runs`. Most of what `run.get` shows is derived at read time from the other planes:

| Field                              | Stored or derived | Source                                                                              |
| ---------------------------------- | ----------------- | ----------------------------------------------------------------------------------- |
| `goal`, `user_request`, `model`, … | stored            | `run.begin`                                                                         |
| `status`, `final_response`         | stored            | `run.complete` (`active` until then)                                                |
| `memories_used`                    | stored            | `run.complete`: the agent's own statement of what it relied on                      |
| `grade`, `grade_source`, …         | stored            | `run.grade`, after the run closed                                                   |
| `memories_written`                 | derived           | rows in `loom.memory` carrying the run's `run_id`                                   |
| `recalled`, `recalled_keys`        | derived           | the search trace log under the `run_id`, unioned with `run.complete(recalled_keys)` |
| `observations`                     | derived           | episodic rows from `observe.record` tagged with the `run_id`                        |
| `turns`, `cost_usd`                | derived           | `loom.agent_turn_usage` by `session_id` (written by the coding-agent plugins)       |

`recalled` is `-1` when `LOOM_SEARCH_TRACE_LOG_ENABLED` is off and no `recalled_keys` were sent.

## Recalled is not relied on

Two lists travel on `run.complete`:

- **`memories_used`** — the keys the agent actually relied on. Each gets a `used` usage
  event, plus an `evidence` event on a positive outcome or a `harm` event on a negative
  one. `outcome` defaults from `status`: `completed` is positive, `failed` is negative,
  `cancelled` is none.
- **`recalled_keys`** — the keys Loom's tools served during the run (the Claude Code
  and Codex plugins stash them from `loom_search` and `memory.get` results and send them
  at session end). They are recorded for `run.get` and never credited.

A row that was returned but ignored earns nothing; crediting every search hit would reward whatever ranks first.
The `usage_fold` worker folds the events into the reinforcement term that
[memory search](memory/search-and-ask.md) ranks by. A `harm` event takes back earned boost, floored at the row's
write-time base, so one bad run never pushes a row below its relevance and never deletes it.

A procedure key named in `memories_used` also gets a row in the procedure ledger (path
empty, `ok` from the outcome); an agent that calls `procedure.record_run` itself should
leave that key out of `memories_used` so the run is not counted twice. See
[procedures and learnings](memory/procedures-and-learnings.md).

## Reflections

`run.complete(reflection=...)` records `what_worked`, `what_failed`, `assumptions`, `lessons`, `improvements` (up to
20 items each), a `confidence` and the `evidence` keys the lesson rests on. It is stored as one memory of type
`reflection` under `reflection:<run_id>`:

- it is embedded, so a later run can find it with `memory.search(memory_type="reflection")`,
  but it is left out of unfiltered search and `memory.ask`;
- `what_failed` and `lessons` need at least one `evidence` key (a 422 otherwise);
- it expires after `expires_in_days` (default 90, `0` = never) unless `memory.verify`
  or a positive outcome renews it;
- a run opened with `role` (planner, executor, critic, or your own word) tags its
  reflection `role:<role>`, so a same-role agent's `memory.search(role=...)` prefers it.

## What happens to the rows a run wrote

`retain` on `run.complete` decides the fate of the memories written under the run:

| `retain`         | Effect                                                                                                                                        |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `keep` (default) | Nothing changes.                                                                                                                              |
| `demote`         | Their confidence is lowered now.                                                                                                              |
| `expire`         | An expiry `LOOM_MEMORY_RUN_COMPLETE_EXPIRE_DAYS` out (default 7) is set with `on_expire=demote`; the `memory_expiry_sweep` worker applies it. |

Pinned, verified, promoted, locked, episodic and reflection rows are kept whatever the
mode, and no mode tombstones anything. The default comes from
`LOOM_MEMORY_RUN_COMPLETE_RETAIN`.

## Graded runs

`status` says the run ended; a grade says whether its result held up. A grade often arrives days later, when CI
passes or a reviewer rejects the pull request. `run.grade` takes `grade` (`pass` or `fail`), a `source` (`maintainer`,
`ci`, `reviewer`, … free text), a `reason` and a `ref` (a URL, a finding key, a check run
id), addressed to one `run_id` or to every closed run of a `session_id` (for example
`pr_1234`). A regrade replaces the verdict; the same verdict from the same source changes
nothing. Active runs are skipped.

Every memory in a graded run's `memories_used` is counted against the grade. Those
counts are what `memory.proposals` shows beside each fact on trial (a
`memory.set(proposed=true)` row). Loom decides a proposal on them only when the
namespace owner set `proposal_policy.mode` to `auto`; otherwise a person decides with
`memory.proposal_decide`. See [writing memory](memory/writing.md).

`run.list(grade="ungraded")` lists the closed runs still owed a verdict.

An MCP session gets an implicit run without asking: its first run-scoped tool
call begins one, and `DELETE /mcp` or the `mcp_session_run_sweep` worker closes
it (see [MCP](../reference/mcp.md)). The sweep closes any active run whose id
starts with `mcp-` and whose `metadata.implicit` is true once no call of its
token (of anyone, for a run that records no token) reached it within
`LOOM_MCP_SESSION_RUN_IDLE_SECONDS`, so a run begun by
hand should not claim to be implicit. The run sits in the caller's default
namespace, so what its readers see stays there: its goal is the label
`mcp session (<agent>)`, never a call's text (a read's query may reach any
namespace the caller reads), and `memories_written` lists the writes in that
namespace only (the others still carry its `run_id`). Closes racing for one run
(the sweep on two hosts, a `DELETE`) claim it in Keeper first, so one completes
it and its retain mode applies once; without Keeper, or when a close stalls past
the ten minutes after which its claim is taken over, two can both complete it.

## Operations

| Operation      | Role   | Does                                                                                                                                                                                                                                                                      |
| -------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run.begin`    | writer | Open a run: `goal` (required), `user_request`, `expected_outcome`, `run_id` (else minted), `session_id`, `role`, `model`, `metadata`. Re-sending the same `run_id` replaces the row; an id of the MCP transport's shape (`mcp-` and 40 hex digits) gets an id of its own. |
| `run.complete` | writer | Close it once: `status`, `memories_used`, `recalled_keys`, `outcome`, `reflection`, `retain`. Not idempotent: completing twice appends usage events twice.                                                                                                                |
| `run.get`      | viewer | One run with its live derivations.                                                                                                                                                                                                                                        |
| `run.list`     | viewer | Newest first, filtered by `agent`, `session_id`, `status`, `grade`, and `memory_activity` (`with` keeps runs that relied on or wrote a memory, `without` the hollow ones; filters apply before `limit`).                                                                  |
| `run.grade`    | writer | Grade a closed run or a session's runs `pass` / `fail`.                                                                                                                                                                                                                   |

The full field lists are in the [operations reference](../reference/operations.md#run).
`memory.outcome_run` records one helped / hurt outcome for every memory recalled under a
run, for callers that grade in bulk rather than naming keys.

## Example

The Python SDK's `run.scope` opens the run on entry and closes it on exit. The keys a
`memory.ask` answer cited inside the block, plus any passed to `used()`, become
`memories_used`; an exception closes the run as `failed` (outcome negative) and then
propagates. Writes inside the block carry the run's id.

```python
from loom import Agent

async with Agent(org="acme", namespace="support", agent="triage-bot") as agent:
    async with agent.run.scope(goal="Answer ticket 4411: refund policy for annual plans") as run:
        answer = await agent.memory.ask("what is the refund policy for annual plans?")
        run.used("policy/refunds/annual")  # relied on beyond what the answer cited
        run.final_response = answer["answer"]

    replay = await agent.run.get(run.run_id)
    print(replay["run"]["status"], replay["memories_written"], replay["recalled"])

    # Later, once the customer confirmed the answer was right:
    await agent.run.grade("pass", run_id=run.run_id, source="support-lead")
```

Over HTTP the lifecycle is two calls:

```bash
curl -sX POST "$LOOM_BASE_URL/v1/run.begin" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","agent":"triage-bot","goal":"Answer ticket 4411"}'
# → {"run_id":"…","status":"active","started_at":"…","next":"…"}

curl -sX POST "$LOOM_BASE_URL/v1/run.complete" -H "Authorization: Bearer $LOOM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"org":"acme","namespace":"support","run_id":"…","status":"completed",
       "memories_used":["policy/refunds/annual"]}'
```

`Agent(auto_run=True)` (or `LOOM_AUTO_RUN=on` in the client's environment) opens a run
lazily on the first memory-plane call, threads every later call through it, and
completes it when the `Agent` closes, with outcome `none`, so it never credits or charges anything. It is off by
default because a write under a run is scoped to it: a restatement of another run's row is stored as a `duplicate`
rather than merged.

## Settings

| Setting                                                       | Effect                                                                                                                |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `LOOM_MEMORY_RUN_COMPLETE_RETAIN`                             | The `retain` mode when a call does not name one (`keep`).                                                             |
| `LOOM_MEMORY_RUN_COMPLETE_EXPIRE_DAYS`                        | How far out `retain=expire` sets the expiry (7 days).                                                                 |
| `LOOM_SEARCH_TRACE_LOG_ENABLED`                               | Records every search under its `run_id`, which `recalled` is derived from. Off by default because it logs query text. |
| `LOOM_USAGE_FOLD_ENABLED`                                     | The `usage_fold` worker that turns used / evidence / harm events into ranking.                                        |
| `LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT`, `…_HALF_LIFE_DAYS` | How much, and for how long, reliance lifts a row in search.                                                           |

See [configuration: dedup and expiry](../reference/configuration.md#dedup-and-expiry),
[workers](../reference/configuration.md#workers) and
[MemorySearch](../reference/configuration.md#memorysearch).

## Limits

- `memories_used` and `recalled_keys` take at most 500 keys each; `goal` at most 2,000
  characters.
- A key in `memories_used` that does not resolve to a live row earns nothing;
  `used_applied` in the response counts the ones that did.
- `run.complete` is not idempotent. A retried completion double-counts usage events;
  complete once.
- The SDK's `run.scope` writes the ledger best-effort: a failed `run.complete` never
  masks the block's own exception, so a run whose completion failed stays `active`.
- `turns` and `cost_usd` are 0 when the run has no `session_id` or the client does not
  report turn usage.

## Related

- [Trace and resume long runs](../guides/trace-and-resume.md) — checkpoints in working
  memory keyed by `run_id`, and how to trace what a run did.
- [Search and ask](memory/search-and-ask.md) — how reinforcement enters ranking.
- [Tasks and decisions](tasks-and-decisions.md) — `decision.outcome` credits the memories
  a deciding run relied on.
- [Memory architecture](../architecture/memory-architecture.md) — the usage-event fold
  and the tables behind it.
