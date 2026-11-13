# Integrate Loom into an agent

This guide covers what an agent sends to Loom and how it reads the answers: the client
surface, the identity, what to write, and how to read. It assumes
[memory](../concepts/memory.md) and
[identities and namespaces](../concepts/identities-and-namespaces.md). A coding agent with a
shipped plugin ([Claude Code](../features/integrations/claude-code.md),
[Codex](../features/integrations/codex.md)) gets most of this from the plugin.

The same rules, written for the agent rather than for you, are the `loom` Agent Skill
(`skills/loom/`). An agent with no plugin reads it from the server: the experimental
[`mentor.guide`](../reference/operations/mentor.md#mentorguide) returns
the loop, where each kind of state goes and how to read answers (`part: "skill"`), or every
plane with its ops (`part: "reference"`). Put it in the agent's system prompt:

```python
system_prompt = "You triage support tickets.\n\n" + await agent.guide()
```

Over MCP the primer names the call; an Agent-Skills client installs it with
`npx skills add ClickHouse/loom --skill loom`.

## 1. Choose the client surface

| Surface                                                      | Use it when                                                                                     | What you get                                                                                                                                                                       |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **MCP** (`/mcp`)                                             | The agent is an LLM client that already speaks MCP (Claude Code, Codex, Cursor, a desktop app). | A curated tool listing per `?toolset=`, tool descriptions written for the model, identity defaults from the connection. The model decides when to call.                            |
| **Python SDK** (`clickhouse-loom`, `from loom import Agent`) | The agent is your own Python process.                                                           | Typed clients per plane, retries and idempotency keys, `agent.tools()` for a model's tool loop, run scopes, framework adapters (LangGraph `LoomStore`, Strands `LoomMemoryStore`). |
| **TypeScript SDK** (`@clickhouse/loom`)                      | The agent is a Node or browser-side service.                                                    | `Datapack` / `Agent`, `agent.call(op, body)` typed from the generated op table, `AgentTools`, `Session`.                                                                           |
| **HTTP** (`POST /v1/<op>`)                                   | Any other language, or a one-off script.                                                        | Every op, the same JSON bodies; you handle retries and the `Idempotency-Key` header yourself.                                                                                      |

All four serve the same ops from one registry. Choose by who decides to call: MCP when the
model decides, an SDK when your code decides. Many agents use both: the SDK to load context at
the start and record the run at the end, MCP tools for what the model chooses to remember or
look up. Over MCP, pick the listing with `?toolset=` ([MCP toolsets](../reference/mcp.md#toolsets));
an op the listing leaves out is still callable through `loom_op`.

## 2. Choose the identity

Every row carries the identity tuple `(org, namespace, agent, user_id, session_id)`, plus
`run_id`. The token fixes `org` and the namespaces; the rest travels per call.

| Field        | Set it to                                                         | Why it matters                                                                                                  |
| ------------ | ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `namespace`  | One per agent (`agent-<name>`), or one shared by a team of agents | **Membership is the read boundary**: a reader of the namespace reads every row in it, whichever agent wrote it. |
| `agent`      | A stable name for the writing agent                               | Authorship, not isolation. Over MCP it defaults to the client's name (`clientInfo.name`, slugified).            |
| `user_id`    | The end user the agent is serving, when it serves many            | With a token minted `--read-scope own_user`, one user's prompt cannot pull another user's memories.             |
| `session_id` | The conversation or job                                           | Scopes working memory and `own_session` reads; groups a transcript.                                             |
| `run_id`     | One execution of a task                                           | Ties a run's writes, searches, checkpoints and transcript together ([runs](../features/runs.md)).               |

- **One agent, many users, one token:** mint the token with `--read-scope own_user` (or pin
  `--read-user-id`) and pass `user_id` on every call. A scoped read without that `user_id`
  answers 400 `IDENTITY_INCOMPLETE`; it is never widened.
- **Several agents sharing knowledge:** one shared namespace they all read, each writing under
  its own `agent`; or a token that reads a shared namespace and writes its own (`ns_write`).
  See [namespace access](tokens-and-grants.md#namespace-access).
- **No secret or personal identifier in `agent`:** it is stamped on every ledger row.

## 3. Decide what to write

Pick the plane first. Durable memory is embedded, ranked, conflict-checked and versioned.
Working memory is read by key and never ranked. Knowledge holds chunked documents.

| The agent has…                                                                                            | Write with                                                                                    | Not with                              |
| --------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------- |
| A fact still true next week that another session needs (a decision, a constraint, an owner, a preference) | `memory.set`: one fact per call, one key per fact                                             | working memory                        |
| Its current plan, open assumptions, a checklist for this task                                             | `working_memory.set` / `append`, loaded by `context.pack`                                     | `memory.set`                          |
| A transcript, email or thread worth remembering                                                           | `memory.set_from_messages`: the server extracts short-keyed facts                             | one giant `memory.set`                |
| A document (spec, runbook, report, anything over about 2 KB)                                              | `knowledge.upsert` as Markdown, plus one pointer paragraph in `memory.set` under the same key | one `memory.set` holding the text     |
| A table of facts with identity (PRs by number, services by id)                                            | `memory_schema.register` once, then `memory.record.write`                                     | one memory row per batch              |
| Coordination state (a lease, a freeze flag, "where are we")                                               | `working_memory.set` by `block_key`                                                           | `memory.set` + `memory.get` in a loop |
| A checkpoint to resume from                                                                               | a working-memory block, or `context.offload` for a large blob                                 | a durable memory per step             |
| A conversation to show and search later                                                                   | `conversation.create` / `conversation.append`                                                 | memory                                |
| Reasoning, greetings, intermediate tool output                                                            | nothing                                                                                       | anything                              |

[Investigation agent memory](investigation-agent-memory.md) applies this table to an agent
that works cases; [trace and resume](trace-and-resume.md) covers checkpoints.

**Shape each fact so a later session can trust it:**

- **Claim first.** The value states the fact in plain text; the source line follows. Only the
  start of a value is embedded (the excerpt is clipped at 1,200 characters).
- **Readable keys**, shaped `<scope>/<kind>/<id>`: `sre/case/1234/evidence/dns-sidecar`, not a
  hash. Facets go in `tags` (`case:1234`, `kind:evidence`) and `entities`.
- **A revision rewrites the same key.** Loom keeps the versions (`memory.history`) and serves
  the current one; never mint `deploy_window_v2`, never keep an index row naming the current
  key.
- **Say where it came from:** `source_resource_id` for the file, URL or document the fact was
  read from.
- **Never launder an inference into a fact.** Write "Inferred from A and B: C" with
  `confidence` below 1.0.
- **Resolve dates** ("on <YYYY-MM-DD>", not "yesterday") and set `valid_at` to when the
  observation was made.
- **No secrets.** For a class of thing the user never wants remembered, `blacklist.add` the
  pattern: matching writes are refused with 422 `BLACKLISTED`.

A statement from the user ("we deploy Tuesdays") is the trigger to write. Do not search
first; the server handles duplicates and conflicts.

## 4. Read the write's answer

A `memory.set` reply is a receipt. Read it:

| Field                                     | Means                                                                                                                                              |
| ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_id`, `version`, `content_sha256`  | The write persisted. Without them it did not; never tell the user something was saved when it wasn't.                                              |
| `conflict_verdict: conflict`, `peers`     | The write collides with an existing fact (both rows are marked `conflicted`). Show both values and ask which is current; don't overwrite silently. |
| 409 `PINNED_CONFLICT`                     | A person pinned the existing fact. Report it; do not retry under a new key.                                                                        |
| `duplicate_of`, `merged_into_key`, `noop` | The fact was already known; nothing new was stored.                                                                                                |
| `advisories`                              | A shape warning: base64, double-encoded JSON, a value over `LOOM_MEMORY_VALUE_ADVISORY_CHARS`.                                                     |

Pass `if_match_sha256` (from a `memory.get`) when two writers may race on one key; a stale
hash is refused instead of overwriting.

## 5. Read with one call per question

1. **Load context once per task.** `context.pack` returns a token-budgeted bundle: profile
   sections, working memory, the memories relevant to `query`, procedures, and optionally
   knowledge, conversations and recent runs (`include_knowledge`, `include_conversations`,
   `include_runs`). The plugins call it at session start.

   ```python
   pack = await agent.context.pack("fix the flaky ingest test", token_budget=2000)
   system_prompt = base_prompt + "\n\n" + pack.rendered
   ```

2. **Search once per question.** Over MCP, `loom_search` covers memory, profile, knowledge and
   conversations in one call. In the SDK, use `agent.memory.search(query)`, or
   `agent.memory.ask(question)` for a cited answer. Page with `next_page` only when the answer
   needs more rows (a list, a count).
3. **Treat an abstain as an answer.** `abstain: true` means nothing cleared the similarity
   gates; reply "not in memory", not with the nearest weak hit. `abstain_code` names the gate
   ([abstain codes](../features/memory/search-and-ask.md)). While `pending_extraction` is above
   0, deferred extraction has not run, so an empty answer is not evidence of absence.
4. **Respect provenance.** A hit's `source_class` says how the content entered memory and
   `verified` whether a live attestation covers it. `memory.get` returns `verification`
   (`human`, `process`, `stale` when the content changed after attestation, or `none`).
   `memory.ask` leaves declared-`external`, unattested rows out of its synthesis by default.

```python
hits = await agent.memory.search("refund window for EU customers", top_k=5)
if hits.abstain:
    reply = "I don't have that in memory."
else:
    reply = hits[0].content_excerpt
```

## 6. Record runs

A run is one execution of a task: its goal, what it recalled and relied on, what it wrote and
its outcome. Recorded runs let ranking learn which memories help ([runs](../features/runs.md)).

```python
async with agent.run_scope(goal="Fix the flaky CI job") as run:
    answer = await agent.memory.ask("what broke CI last week?")
    run.used("ci/flaky/runner-image")
    run.final_response = "Pinned the runner image."
```

The scope calls `run.begin` on entry and `run.complete` on exit, with status `completed` or
`failed` and, as `memories_used`, the keys its `memory.ask` answers cited plus those passed to
`used()`. Writes inside the block carry its `run_id`. Without the SDK, call `run.begin` and
`run.complete(run_id, status=..., memories_used=[...])` yourself. `memories_used` is what the
agent relied on, not everything it was shown; recalled keys are tracked separately.

An agent outside the plugins should also hand the finished transcript to
`memory.set_from_messages` (or close an `agent.session(...)`), so the server extracts what was
durable.

## 7. Follow the memory skill's review

The plugins ship a `loom-memory` skill
(`integrations/claude-code-plugin/skills/loom-memory/`, generated into the Codex plugin)
that encodes this guide for a model. A coding agent on plain MCP gets the [agent
instructions](../features/integrations/agent-instructions.md) block, whose last memory
line is this review in short; any other agent copies the questions below into its
instructions. Before reporting done, write only what earns a yes:

1. Did I make or learn a decision, constraint or owner a future session needs?
2. Did an existing memory turn out wrong or outdated? Update it, same key.
3. Did the user correct me? Update the fact; if they rejected it, `memory.delete` it and
   `blacklist.add` the pattern if it would recur.
4. Did I produce an artifact (a file, a PR, a dashboard) future work should know about?
5. Did I discover something non-obvious that cost real effort?
6. Is an inference I wrote now confirmed? Raise `confidence` and drop the "Inferred" prefix.

Then run `namespace.validate` and act on findings that name the keys you wrote. Say "saved"
only when the receipt carried `memory_id` and `version`, "verified" only when a person or
process attested it, and "not verified here" when the check could not run. Other shipped
skills cover code questions (`loom-code`, also at `skills/loom-code/` for any client),
reworking a namespace (`loom-optimize`), session restore (`loom-session`) and client health
(`loom-status`).

## Mistakes to avoid

`namespace.audit` detects each of these; [optimize a namespace](optimize-namespace.md) turns
its findings into a fix.

| Finding                               | The mistake                                                                             | Instead                                                                                         |
| ------------------------------------- | --------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `double_encoded_values`               | `json.dumps(json.dumps(doc))`: the embedded text starts with an escaped quote.          | Send text, or the object itself, as `value`.                                                    |
| `json_prefix_excerpts`                | A JSON object longer than the embed clip; the vector is built from its prefix.          | Claim first as plain text; documents to `knowledge.upsert`.                                     |
| `hash_keys`                           | A sha as the row's identity: unreadable, ungroupable, and a revision becomes a new key. | `<scope>/<kind>/<id>`; revise under the same key.                                               |
| `exact_key_store`                     | Reads by exact key dwarf searches: the namespace is an object store.                    | Recall with `memory.search`; leases and pointers in working memory.                             |
| `coordination_as_memory`              | Leases, locks, freeze flags stored as memories and read in a loop.                      | `working_memory.set` / `get`: never embedded, ranked or conflict-checked.                       |
| `hand_rolled_index_rows`              | A row listing other keys because the agent cannot find its own rows.                    | `tags` + `memory.list prefix=`; chain revisions with `memory.link supersedes`.                  |
| `oversized_values`, `unembedded_rows` | Documents as memories; past the embedder's cap a value is never embedded.               | `knowledge.upsert` plus a one-paragraph pointer row.                                            |
| `episodic_blobs`, `version_churn`     | Run dumps as episodic rows, rewritten every step.                                       | `context.offload` for the blob; one short episodic row per run; revise the claim, not the blob. |

Two more that no audit catches:

- **One blob per turn.** Five facts are five `memory.set` calls, each under its own key.
- **Retiring keys a live agent still reads.** Its reads answer `found: false`, a client may
  fall back to local state, and two stores disagree. Move with `memory.move` (it leaves a
  redirect) and delete nothing until every reader has moved.

## Related

- [Headless bots](headless-bots.md)
- [Writing memory](../features/memory/writing.md)
- [Unified search](../features/unified-search.md)
- [Frameworks](../features/integrations/frameworks.md)
- [MCP clients](../features/integrations/mcp-clients.md)
