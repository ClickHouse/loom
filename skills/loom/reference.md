# Loom planes — what each holds, when to use it, its ops

Read the skill first (`mentor.guide`† with `part: "skill"`, or `SKILL.md`
beside this file): it has the loop, the write rules and how to read
answers. This file is the catalog. Each plane lists what it is for, what it
gives you, and its main ops. Ops marked † are experimental: their fields may
change between releases. `ops.capabilities` says what this server serves;
the operations reference (`docs/reference/operations.md` in the Loom
repository) has every field.

Calling convention, by surface: MCP tool `loom_<op_with_underscores>` or
`loom_op {op, arguments}`; Python `await agent.call("<op>", {...})`;
TypeScript `await agent.call("<op>", {...})`; HTTP `POST /v1/<op>`. The SDKs
stamp `org`, `namespace` and `agent` for you; over HTTP you send them. A
stamped `agent` narrows reads that look rows up by author, and
`memory.update`, to your own rows; pass `agent: "*"` there when you mean
every author, except on the ops about you and on `memory.get` and
`memory.delete`, which the skill's Identity section names. Ops
marked ‡ are not served over MCP (the refusal is `OP_NOT_ON_MCP`): over MCP,
`loom_search` searches memory, knowledge and conversations, and the rest are
SDK and HTTP only.

## Durable memory — facts by meaning

One claim under a key in a namespace. Every write is embedded, checked
against similar facts for duplicates and contradictions, and versioned.
Reads rank by relevance, recency, use and the outcomes of runs that relied
on a row.

| Op | Use it to |
|---|---|
| `memory.set` | Store or revise one fact (`memory_key`, `value`, `tags`, `entities`, `confidence`, `valid_at`, `source_resource_id`, `ttl_seconds`). |
| `memory.set_from_messages` | Hand over a transcript; the server extracts the durable facts under short keys. |
| `memory.search`‡ | Ranked hits with scores, `abstain` and provenance. `recipe`: `fast`, `balanced`, `quality`, `verbatim`, `code`, `temporal`, `graph`. |
| `memory.ask` | A synthesized answer with citations, or an abstain. |
| `memory.brief` | Orientation on a task: an answer, its grounding, unknowns, what to read next. |
| `memory.get`, `memory.bulk_get`‡, `memory.list` | Read by exact key, many keys, or a key `prefix` (`memory.list` matches `agent`, `user_id` and `session_id` exactly; `"*"` reads every value). |
| `memory.update`, `memory.update_by_id` | Revise an existing row; `if_match_sha256` refuses a stale overwrite. |
| `memory.delete` | Retire a fact by key: yours, else the same user's row under another agent. |
| `memory.pin`† | Save a fact and protect it: a write that does not pin never replaces it. |
| `memory.link` | Assert an edge between two keys (`supersedes`, `supports`, `depends_on`, `caused`). |
| `memory.move` | Rename keys and leave redirects, so old readers keep working. |
| `memory.history`†, `memory.provenance`, `memory.lineage`† | Every version of a key; where a row came from. |
| `memory.timeline`†, `memory.related`† | Rows about an entity or tag in time order; rows sharing entities with a key. |
| `memory.verify` | Attest that a row is correct (`verified` on later reads). |

Memory types: `semantic` (default, embedded and searched), `episodic` and
`profile` (stored, not searched), `procedural` (procedure bodies). A row
detected or declared `sensitivity: "high"` is never embedded and is read by
key only.

## Typed records — tables of facts with identity

When the facts are rows (a PR by number, an alert by id, a service by name),
an admin registers a schema once and agents write records; queries filter and aggregate
instead of ranking prose.

| Op | Use it to |
|---|---|
| `memory_schema.register` | Declare the record type, its identity fields and columns (admin). |
| `memory_schema.propose`† | Propose a schema as a writer; an admin approves it. |
| `memory.record.write` | Upsert one record by its identity. |
| `memory.record.query` | Filter and page records. |
| `memory.record.history`, `memory.record.conflicts`† | A record's versions; records two writers disagree on. |

## Knowledge — documents you look things up in

Whole documents, chunked on their headings, embedded with their heading
breadcrumb, searched by passage and cited by `source_uri`. Put the document
here and one short claim pointing at it in memory.

| Op | Use it to |
|---|---|
| `knowledge.upsert`† | Add or replace the document at a `source_uri` (preferred for anything you may update). |
| `knowledge.add`, `knowledge.bulk_add` | Store one, or up to 500, documents. |
| `knowledge.search`‡ | Hybrid passage search (vector and keyword). |
| `knowledge.get`, `knowledge.outline`† | The full text; the table of contents or one section. |
| `knowledge.synthesize`† | Run a task (a summary, an evidence sweep) over whole documents. |
| `knowledge.list` | Inventory by tag or URI prefix. |

## Working memory — state you are told, not state you search for

Small named blocks (`block_key`, default `scratchpad`, 4,096 characters by
default, up to 16,384) keyed by `(agent, user_id, session_id, run_id)`:
`working_memory.get`† and `render` read back under exactly that identity, so
another agent does not see them. `context.pack` loads three layers: the
run's blocks, the session's (`run_id` `-`) and the agent's (`session_id` and
`run_id` `-`), so a block written with `session_id` and `run_id` `-`
reaches every later session of that agent and user. Never embedded, ranked or
conflict-checked. Use for the plan, a checklist, a cursor, a resume point; for another agent, a
session brief or the task inbox.

| Op | Use it to |
|---|---|
| `working_memory.set` | Replace a block. Every write returns `fill_ratio`. |
| `working_memory.append`†, `working_memory.replace`† | Append a line; replace text inside a block. |
| `working_memory.get`†, `working_memory.render` | Read one block; render the blocks in scope. |
| `working_memory.delete`† | Drop a block when the task is done. |

## Context — load once, park the rest

| Op | Use it to |
|---|---|
| `context.pack` | The token-budgeted bundle for a task: profile, working memory, relevant memories (every author), your own pinned and recently recalled rows (the memory stack), procedures, decisions, namespace health, mentor hints; optionally knowledge, conversations, recent runs. `mode`: `startup`, `task`, `debug`, `handoff`. |
| `context.offload`† | Park a large blob (a log, a diff, a tool result) and get a pointer and summary. |
| `context.fetch`† | Read back the parts of an offloaded blob that match a pattern. |
| `search.query`† | One search over memory, profile, knowledge and conversations (`loom_search` over MCP). |

## Profiles — always-loaded context about the user and the agent

Persona, preferences, focus, rendered as one prompt fragment. An operator
may define a profile schema per namespace; the server keeps each user's
profile current in that shape.

| Op | Use it to |
|---|---|
| `profile.render` | The profile as prompt text. |
| `profile.set`†, `profile.append`†, `profile.replace`† | Change a section. |
| `profile.structured`† | The profile as fields, when a schema is set. |

## Runs — one execution of a task

A run ties a task's goal, recalls, writes and outcome together. What a
successful run relied on ranks higher next time; what a failed run relied
on loses that boost.

| Op | Use it to |
|---|---|
| `run.begin` | Open a run (`goal`, optional `budget_usd` with a `session_id`, `budget_seconds`); returns `run_id`. |
| `run.complete` | Close it: `status`, `memories_used` (what you relied on), `final_response`, an optional reflection. |
| `run.get`, `run.list`‡ | A run's record, with `over_budget`; recent runs. |
| `observe.record` | Record one tool observation (input, output, error) under the run. |

Python: `async with agent.run_scope(goal=...) as run:` opens and closes it.
Its typed helpers carry the run's `run_id`, and label recall reads with the
`X-Loom-Run-Id` header (a body `run_id` on `memory.search`‡ or `memory.ask`
filters to the run's rows). A raw `agent.call` in the scope gets the run
only on `memory.set`, `pin`, `update`, `set_from_messages`, `bulk_set` and
`link`, `observe.record` and working-memory `set` and `append`; pass
`run_id` yourself on any other call that takes one.
`memory.update_by_id` has no run field: revise by key inside a run.

## Conversations and session briefs

| Op | Use it to |
|---|---|
| `conversation.append` | Store a turn of a thread (only when the namespace owner turned logging on; `stored: false` otherwise). Idempotent on `msg_id`. |
| `conversation.search`†‡, `conversation.threads` | Find past turns; list threads. |
| `session.brief_save`†, `session.brief_get`†, `session.brief_list`† | Save and restore the condensed state of one line of work for a later session or another agent. |

Storing a turn does not create a memory; send the transcript to
`memory.set_from_messages` for that. Coding-agent plugins capture turns and
briefs themselves; do not also append.

## Procedures and learnings — what you learn by doing

A procedure is a graph of steps that Loom stores, serves and improves from
reported runs; Loom never executes it.

| Op | Use it to |
|---|---|
| `procedure.match` | The procedures that fit a situation, with run stats and trust status. |
| `procedure.add` | Register a procedure (`name`, `situation`, `steps`, optional `graph`). |
| `procedure.record_run`† | Report one execution: the path taken, `ok`, a note. |
| `procedure.evolve`†, `procedure.revise`† | Fold runs in (proven path, dead ends); propose a new shape. |
| `learnings.list`†‡ | Memories flagged as worth promoting to a shared namespace, awaiting review. |

## Entity graph — what is connected to what

Entities named in memories become nodes; shared mentions and extracted
`(subject, relation, object)` triples become edges, each traceable to its
rows. Use it for questions vector search cannot answer: "what depends on
the billing service".

| Op | Use it to |
|---|---|
| `facts.traverse`, `facts.list` | Walk from an entity; list triples. |
| `facts.path`†, `facts.related`† | A path between two entities; neighbours. |
| `memory.search`‡ with `recipe: "graph"` | Search that follows graph edges (`loom_search` takes the same `recipe`). |

## Coordination between agents

| Op | Use it to |
|---|---|
| `task.create`† | Post work into another agent's inbox (`inbox`, `kind`, `title`, optional `brief`). The owner decides how far to trust each poster. |
| `task.pull`†, `task.heartbeat`†, `task.complete`† | Claim your next task, keep the claim alive with its fence, finish it. |
| `task.updates`† | Follow a task you posted. |
| `decision.record`†, `decision.precedents`†, `decision.outcome`† | Record a choice with its reasons; find past decisions in similar cases; report how it went. |
| `policy.check` | Ask whether an action is allowed now, given recent history. |

## Conflicts, refusal and hygiene

| Op | Use it to |
|---|---|
| `conflicts.list`‡, `conflicts.resolve`‡ | Contradictions waiting for a decision; settle one (admin). |
| `blacklist.add`†, `blacklist.list`‡ | Refuse a pattern the user never wants stored (422 `BLACKLISTED`). |
| `namespace.validate` | An integrity report for the namespace; fix the findings on keys you wrote. |
| `namespace.audit` | Find whole-namespace mistakes (documents as memories, hash keys, coordination as memory). |
| `mentor.brief` | Your usage profile, the planes you are not using and the counts behind each hint. |
| `mentor.guide`† | This guide (`part`: `skill`, `reference` or `all`). |

## Code index — when a repository is indexed

Answers structural questions as of the indexed commit, so you read one
function instead of a file. Pass the code namespace the server names for
the repository.

| Op | Use it to |
|---|---|
| `code.symbol` | A definition by name, with body, history and examples. |
| `code.search`, `code.grep` | Find code by meaning; by literal or regex, with the enclosing function. |
| `code.callers`, `code.callees`† | Who calls it; what it calls. |
| `code.impact` | What a change breaks: callers, tests, co-changing files. |
| `code.context_pack` | A briefing for a PR or a task: symbols, callers, tests, recent PRs. |
| `code.ask` | A grounded answer to a question about the code, with citations. |
| `code.pr_check`†, `code.bisect_candidates`† | Is someone already doing this; which merged PR introduced a regression. |

## Tracker — issues, PRs and incidents, when a source is connected

| Op | Use it to |
|---|---|
| `tracker.duplicate_check`† | Has this crash or report been seen: `duplicate_of`, `related` or `none`. |
| `tracker.similar`†, `tracker.item`† | Items like this one; one item. |
| `tracker.flaky`†, `tracker.test_signal`† | Is this test known flaky; its recent history. |

## Namespace guides — the README of one namespace

Markdown the namespace's agents and people keep current: what it holds, its
key layout, what each tag means, which ops to call. Conventions only; the
facts stay in memory. `context.pack` loads it at session start.

| Op | Use it to |
|---|---|
| `namespace.guide_get`† | The current guide, or a past `revision`. |
| `namespace.guide_set`† | Save the next revision; pass the `expected_revision` you read, and on `conflict: true` merge into the returned text and save again. |
| `namespace.guide_history`† | Past revisions with their summary, note and writer. |

## Sharing and erasure

| Op | Use it to |
|---|---|
| `namespace.list_mine` | The namespaces your token reaches. |
| `grants.create`, `grants.list` | Share a namespace with another agent or person (`grants.create`: admin); list the grants. |
| `erasure.request`, `erasure.status`†‡ | Erase everything about a user or subject, across every table that can hold memory content (org admin for a user, admin for a namespace). |
