---
name: loom
license: Apache-2.0
metadata:
  version: "1.0.0"
description: "How an agent uses Loom: memory, documents, working state, runs and code intelligence that persist across sessions and are shared with other agents. WHEN: you have a Loom server (LOOM_BASE_URL, an SDK `Agent`, `loom_*` MCP tools) and you start a task, learn a durable fact, hold a document or a large blob, need to resume, hand work to another agent, or answer from what an earlier session knew. Covers MCP, the Python and TypeScript SDKs and plain HTTP."
---

# loom — what it is and how to use it well

Loom is a server that remembers for you. What you store outlives your
context window and your process, is shared with every agent and person in
the same namespace, and comes back ranked, versioned and with its source.
Every capability is an operation (op) such as `memory.set`, declared once
and served over HTTP and, nearly all, over MCP; the SDKs wrap HTTP:

| You reach it through | An op is called as |
|---|---|
| MCP (`<loom>/mcp`) | the tool `loom_<op, dots as underscores>` (`memory.set` → `loom_memory_set`); an op your tool list does not show runs through `loom_op {op, arguments}` (read ops only on a read-only connection), except the ops marked ‡ (SDK and HTTP only) |
| Python SDK (`from loom import Agent`) | a typed helper (`agent.memory.set(...)`) or `await agent.call("memory.set", {...})` |
| TypeScript SDK (`@clickhouse/loom`) | `agent.call("memory.set", {...})`, or the generated `loom.memory.set({...})` |
| HTTP | `POST /v1/memory.set` with `Authorization: Bearer <token>` and a JSON body |

The fields are the same everywhere. `loom_op {op: "help", query: "<words>"}`
(MCP) finds ops by words, with their arguments; `ops.capabilities` lists
every op this server serves, with the role each one needs.

The full catalog of planes is the reference: `mentor.guide`† with
`part: "reference"` returns it (installed as a skill, it is `reference.md`
beside this file). Both mark experimental ops with †, whose fields may
change between releases, and ops MCP does not serve with ‡.

## Why use it

- **You stop re-learning.** A decision, a constraint, an owner or a fix
  stored once is recalled by meaning in every later session, yours or
  another agent's.
- **You can trust what comes back.** Every hit says who wrote it, when, from
  which source, whether a person verified it, and whether a newer fact
  contradicts it. A search that finds nothing says so (`abstain`).
- **Your context stays small.** One `context.pack` call loads what matters
  for the task inside a token budget; large logs go to `context.offload`†
  and come back by pattern.
- **You coordinate without a side channel.** The task inbox, decisions and
  session briefs are shared state other agents and later sessions read.
- **You get better with use.** Runs record what you relied on; memories
  that helped successful runs rank higher, and the mentor names the planes
  you are not using.

## The loop for every task

1. **Load context once.** `context.pack` with a `query` describing the task
   (default 2,000 tokens) returns profile, working memory, relevant
   memories and procedures, rendered for your prompt. Add
   `include_knowledge`, `include_conversations` or `include_runs` when the
   task needs them.
2. **Open a run** for a task with an outcome: `run.begin(goal=...)`. Put
   its `run_id` on your writes, your working-memory calls and
   `context.pack`, and keep it off `memory.search`‡ and `memory.ask`, where
   a `run_id` filters to the run's own rows (over HTTP, the `X-Loom-Run-Id`
   header labels a read without filtering it). In Python,
   `agent.run_scope(...)` does this for every typed helper; in TypeScript,
   pass `runId` in the scope argument of writes, working-memory calls and
   `contextPack`, never of `recall` or `ask`; over MCP, the connection's own
   run labels reads.
3. **Search once per question.** `search.query`† (`loom_search` over MCP)
   covers memory, profile, knowledge and conversations in one call;
   `memory.ask` returns a cited answer. Do not search before writing a fact
   the user just told you.
4. **Write as you learn**, by the table below. Read each write's receipt.
5. **Close the run** with `run.complete(run_id, status, memories_used=[keys you
   relied on])`, then run the end-of-task review.

## Where each thing goes

| You have | Write with | Not with |
|---|---|---|
| A fact still true next week (a decision, constraint, owner, preference) | `memory.set`: one fact per call, one key per fact | working memory |
| A transcript, email or thread with facts in it | `memory.set_from_messages`: the server extracts them | one giant `memory.set` |
| A document over about 2 KB (spec, runbook, report) | `knowledge.upsert`† as Markdown, plus one pointer `memory.set` | `memory.set` holding the text |
| Rows with identity (PRs by number, services by id) | `memory.record.write` against a schema an admin registered (`memory_schema.register`; a writer proposes one with `memory_schema.propose`†) | one memory per batch |
| Your plan, a checklist or a cursor for the task in hand | `working_memory.set` or `working_memory.append`† by `block_key`; a block belongs to one `(agent, user_id, session_id, run_id)` | `memory.set` read in a loop |
| Where you are, for a later session or another agent to resume | `session.brief_save`†, read back with `session.brief_get`† | working memory, which is read back only under the identity that wrote it |
| Work several agents may pick up | the task inbox: `task.pull`† claims a task, `task.heartbeat`† keeps the claim | a lock row in memory or working memory |
| A log, diff or tool output too big for context | `context.offload`†, then `context.fetch`† | memory or working memory |
| Work for another agent | `task.create`† into its inbox | a memory it might search for |
| A choice made, with its reasons | `decision.record`† | a free-text memory |
| A procedure that worked | `procedure.add`, then `procedure.record_run`† per use | a memory describing steps |
| Something the user never wants stored | `blacklist.add`† the pattern | a memory saying "don't store X" |
| Reasoning, greetings, intermediate output | nothing | anything |

## How to write a fact

- **Key** `<scope>/<kind>/<id>`, readable: `billing/decision/refund-window`,
  never a hash. Facets go in `tags`; people, services and things in
  `entities`.
- **Value** states the claim first, in plain text; the source follows. The
  row's vector is built from its first 1,200 characters, so the claim goes
  there.
- **A revision rewrites the same key.** Loom keeps every version
  (`memory.history`†); never mint `_v2` keys or index rows that name keys.
- **Provenance:** `source_resource_id` for the file, URL or document it came
  from; `valid_at` for when it was observed; absolute dates, never
  "yesterday".
- **Inference is not fact:** "Inferred from A and B: C" with `confidence`
  below 1.0.
- **No secrets** in values, keys or `agent` names.

## Read every answer

| You see | It means | Do |
|---|---|---|
| `memory_id` and `version` on a write | it persisted | only now say "saved" |
| `conflict_verdict: "conflict"`, `peers` | it contradicts a stored fact; both are marked | show both and ask which is current |
| 409 `PINNED_CONFLICT` | a pinned fact holds that key, or one close to it; a write that does not pin never replaces it | if the pinned fact is wrong, `memory.update` it (the pin stays); if yours is a different fact, store it under its own key |
| `conflict_verdict: "pending"` | the conflict judge runs after the write | the row is stored; `memory.get` later shows the settled `status` |
| `noop`, `merged_into_key`, `duplicate_of` | already known | nothing more to store |
| `advisories` on a write | the shape is wrong (a blob, double-encoded JSON) | fix the shape, by the table above |
| `abstain: true` on a read | nothing cleared the relevance floor | treat it as "not in Loom" and use your other context; never present the nearest weak hit as the answer |
| `pending_extraction` > 0 | extraction has not run yet | an empty result is not evidence of absence |
| `source_class`, `verified`, `verification` | how a row entered memory and whether someone attested it | weigh `external` and unverified rows as claims |
| `hint` on a result (header `X-Loom-Hint`) | the mentor names a plane you are not using | act on it once; `mentor.brief` has the counts |
| `found: false` on a read by id | not found, or not yours | the same answer for both, by design |

## Identity

Every row carries `(org, namespace, agent, user_id, session_id)`. The token
fixes `org` and the namespaces you may read and write. A namespace is the
read boundary: everyone who reads it reads every row in it. On writes, set
`agent` to your stable name (authorship, not isolation), `user_id` to the
end user when you serve many, `session_id` to the conversation or job, and
`run_id` to the task execution.

On reads the same fields select rows, and each read op treats them its own
way; its field list (operations reference, `loom_op` help) says how. The
reads the loop uses:

- `search.query`† reads every author and user in the namespaces it fans out
  to (`namespaces_skipped` names any it cut); it has no `user_id` or
  `session_id`. To read one user's rows, use
  `memory.ask` or `memory.search`‡ with `user_id`.
- `memory.search`‡ and `memory.ask` read every author unless you name an
  `agent`; a `user_id` or `session_id` narrows them to those rows.
- `context.pack` has two memory parts. Its relevant memories (with a
  `query`, or in `mode: "startup"` without one) come from every author,
  narrowed by `user_id` and `session_id`. Its memory stack, in every mode,
  holds only your own agent's pinned and recently recalled rows under
  exactly that `user_id` and `session_id`. A pack that names this
  session's `session_id` recalls only this session's memories; keep it on
  anyway, because the pack picks this session's working-memory blocks by
  it, and read what earlier sessions learned with `search.query`† or
  `memory.ask`.
- `memory.get` and `memory.delete` take your row under the key, else the
  same user's row under another agent.
- `memory.list` needs `agent` and matches `agent`, `user_id` and
  `session_id` exactly (`user_id` and `session_id` default to `-`); `"*"`
  on a field reads every value.

Read through the SDKs' typed helpers (Python `agent.memory.*`, which also
cover revisions such as `agent.memory.update`; TypeScript `agent.recall`
and `agent.ask`): they send the identity
each op expects, `agent: "*"` included. A raw `agent.call` stamps your own
`agent` and `-` for the rest, so on a read that defaults to every author
(`memory.search`‡, `memory.ask`) it reads only your own rows unless you
pass `agent: "*"`. In TypeScript, `agent.call` takes `userId`, `sessionId`
and `runId` in its scope argument, not the body.

A token minted with an `own_user` read scope refuses a read without
`user_id` (400 `IDENTITY_INCOMPLETE`) rather than widening it, unless it is
pinned to one user (as an end user's token is), which reads that user's
rows. `search.query`† has no `user_id`, so under an unpinned `own_user`
token it always answers 400; use `memory.ask` or `memory.search`‡ with
`user_id` there. Such a token, and an end user's, reaches only ops that keep their
answer inside that user: any other op answers 403 `AUTH_INSUFFICIENT`
naming it. That covers namespace-wide reads (`namespace.validate`,
`mentor.brief`), namespace-wide refusals (`blacklist.add`†), revisions by
key or id (`memory.update`, `memory.move`, `memory.link`, `memory.verify`),
writes to tables with no user column (`memory.record.write`,
`decision.record`†, `procedure.record_run`†, `context.offload`†),
conversations and the entity graph; skip that step or ask the owner. An end user's token also reaches no op outside a namespace except
`collection.create`†, `collection.list`†, `namespace.list_mine`,
`grants.pending_for_me`† and `mentor.guide`† (so not `ops.capabilities`).

## End-of-task review

Before you report done, write only what earns a yes:

1. Did I make or learn a decision, constraint or owner a later session needs?
2. Did a stored memory turn out wrong? Revise it under the same key.
3. Did the user correct me? Revise; if they rejected it, `memory.delete` and
   `blacklist.add`† when it would recur.
4. Did I produce an artifact (a file, a PR, a dashboard) later work should
   know about?
5. Did I learn something non-obvious that cost real effort?
6. Is an inference I wrote now confirmed? Raise `confidence`, drop
   "Inferred".

Then `namespace.validate` and fix findings on the keys you wrote. Say
"verified" only when a person or process attested it.

## Mistakes that cost you recall

- Five facts in one value: five `memory.set` calls.
- A document, a JSON dump or a run log as a memory: it is embedded from its
  prefix and buries real facts. Knowledge, records or offload instead.
- Cursors and checkpoints as memories rewritten every step: working
  memory. Claims on shared work as lock rows: the task inbox.
- Retiring keys another agent still reads: `memory.move` leaves a redirect;
  delete nothing until every reader has moved.
- Calling `conversation.append` when a plugin already captures the session:
  every turn is stored twice.

`namespace.audit` finds these in a namespace you read; the reference
(`mentor.guide`† with `part: "reference"`, or `reference.md`) lists each
plane with its ops.
