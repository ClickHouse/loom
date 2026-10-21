# Glossary

Loom's terms, alphabetically, each defined once. Code identifiers are in backticks.

**Abstain** — `memory.search` returning `abstain=true` with a reason code instead of
the nearest weak match, when the best hit is below the abstain gate calibrated for the
embedder.
It separates "nothing was stored" from "something unrelated scored 0.3".
See [retrieval and ranking](concepts/retrieval-and-ranking.md).

**Actor** — the server-stamped column naming who wrote a row: `human:<subject>` or
`process:<subject>`, with the kind taken from the token. Never read from the request body.
See [identities and namespaces](concepts/identities-and-namespaces.md).

**Agent** — the third element of the identity tuple: the name of the program writing and
reading (`claude-code`, `support-bot`). It records authorship; it is not a read boundary:
every member of a namespace reads every agent's rows.

**Attestation** — the `verified_*` columns recording that a row was checked. An edit
leaves them in place and marks them stale; a reader decides trust by comparing the
attested content hash. See [history and provenance](features/memory/history-and-provenance.md).

**Audit log** — `loom.audit_log`: one content-hashed row per audited operation, grouped
into hash-chained hourly checkpoints. Rows carry hashes, not content.
See [audit](features/observability/audit.md).

**Belief revision** — the design lens for the memory lifecycle: Expand (`memory.set`),
Revise (`memory.update_by_id`), Contract (`memory.delete`), Reinforce (`memory.pin`),
Refuse (`blacklist.add`). See [how a write is judged](concepts/memory.md#how-a-write-is-judged).

**Blacklist** — registered substrings or regexes that refuse matching writes
(`422 BLACKLISTED`); `sweep=true` also tombstones existing matches.

**Chunk** — a piece of a knowledge document or a source file, stored and embedded
separately so retrieval can return the passage rather than the whole document.

**Code namespace** — a namespace of kind `code` holding one indexed git repository:
its chunks, symbols, edges, commits and PR history.
See [code index](features/code-index/overview.md).

**Commit coverage** — whether every index tier has processed a given commit;
`code.index_status(commit=...)` answers it, and `code.coverage.watch` notifies when it
becomes true. See [freshness](features/code-index/freshness.md).

**Compiler tier** — code-index facts derived from a compiler-grade indexer (SCIP) rather
than the parser tier; call edges and references resolved by type information.

**Conflict** — a write semantically close to an existing memory (cosine ≥
`LOOM_CONFLICT_THRESHOLD`). Both rows are marked `conflicted` and cross-linked; a person
or agent resolves them. See [conflicts and refusal](features/memory/conflicts-and-refusal.md).

**Consolidation** — the background pass that merges fragmented memories about the same
subject and records lineage (`memory_belief_sources`, `support_type='derived_from'`).

**Content hash** — `content_sha256` of a memory's value. It deduplicates exact repeats,
backs the `if_match_sha256` precondition and anchors audit rows.

**Context pack** — `context.pack`: a bounded bundle of working memory, profile, relevant
memories, procedures and decisions, loaded once at the start of a session.
See [unified search](features/unified-search.md#contextpack).

**Dashboard** — the web application at the server root, for people: memory, activity, sources, review
queue, tasks, repositories and settings. See [dashboard](features/dashboard.md).

**Decision** — a first-class record of a choice (`decision.record`) with reasoning,
rejected alternatives and, later, its outcome (`decision.outcome`).
See [tasks and decisions](features/tasks-and-decisions.md).

**Entity edge** — a weighted co-occurrence link between two entities in
`loom.entity_edges`, with a validity interval; the graph `facts.traverse` walks.
See [entity graph](features/entity-graph.md).

**Erasure saga** — the resumable, fail-closed sequence of steps `erasure.request` runs to
remove a subject's data from every table (and configured external backends), ending in a
residue census. See [retention and erasure](features/memory/retention-and-erasure.md).

**Extraction** — turning conversation messages into candidate memories
(`memory.set_from_messages`), with an LLM and quality filters.
See [extraction](features/memory/extraction.md).

**Extractor stamp** — a hash of the code-index extractor modules' code tokens. A change
to it re-chunks every indexed repository; comments and docstrings don't count.
See [code-index architecture](architecture/code-index-architecture.md).

**FINAL** — the ClickHouse modifier that collapses a `ReplacingMergeTree` table to the
latest version per key at read time. Loom reads `loom.memory` with it.
See [ClickHouse](architecture/clickhouse.md).

**Fence** — the token `task.claim` returns with a lease; later `task.progress`,
`task.heartbeat`, `task.complete` and `task.fail` calls must present it, so a lapsed claimant cannot write.

**Grant** — a row giving a subject a role on a namespace (`grants.*`).
See [tokens and grants](guides/tokens-and-grants.md).

**Identity tuple** — `(org, namespace, agent, user_id, session_id)` on every row. The
first three are required; the last two default to `'-'`.
See [identities and namespaces](concepts/identities-and-namespaces.md).

**`ignored_args`** — the response field naming request fields the server did not use
(unknown or retired), so a client learns about drift without a failure.

**Inbox** — a named queue of tasks owned by a namespace (`task.*`, `inbox.*`).
See [tasks and decisions](features/tasks-and-decisions.md).

**Knowledge document** — a document added with `knowledge.add`, chunked and embedded for
retrieval. Distinct from a memory: it is reference material, not a belief.
See [knowledge documents](features/knowledge.md).

**Learning** — a candidate waiting in `loom.learnings_queue` for a person's decision
(`learnings.list`, `learnings.review`): a fact several writers back, promoted to the shared
layer, or a rule a bot stated about itself, promoted to the namespace's knowledge.

**`log_comment`** — the ClickHouse query setting Loom fills with the calling op, worker
or CLI command, so `system.query_log` attributes every statement.

**`maturity`** — an op's declared `stable`/`experimental` field. An experimental op that
neither the core nor the atomic toolset lists is left out of the `all` listing; it is
still callable by name. See [MCP toolsets](reference/mcp.md#toolsets).

**MCP tool** — the Model Context Protocol projection of an op, named `loom_<op>` with
dots replaced by underscores (`loom_memory_set`), plus a few synthetic tools such as
`loom_search` and `loom_op`. See [MCP](reference/mcp.md).

**Memory** — one row in `loom.memory`: a key, a value, its identity tuple, type, tags,
validity and provenance. See [memory](concepts/memory.md).

**Memory key** — the readable identifier of a memory within its namespace and agent,
conventionally `<scope>/<kind>/<id>`. A write to an existing key supersedes it.

**Memory type** — the kind of a memory: `semantic`, `episodic`, `procedural`,
`preference`, `profile` and others; the type decides whether it is embedded and how it is
searched.

**Mentor hint** — a short usage suggestion Loom attaches to results or serves through
`mentor.brief`, pointing an agent at a plane or op it is not using well.

**Namespace** — the unit of storage, sharing and access: a named workspace inside an org.
Kinds are `memory`, `code` and `bench`.
See [namespaces and sharing](features/namespaces-and-sharing.md).

**Operation (op)** — one declared entry in the registry (`server.operations.registry.ALL_OPERATIONS`),
served at `POST /v1/<op>`, as an MCP tool and through the SDKs.
See [operations reference](reference/operations.md).

**Org** — the top-level tenant; the first element of the identity tuple.

**Pinned** — a memory marked by `memory.pin`. A conflicting write against a pinned memory
is rejected with `409 PINNED_CONFLICT`.

**Plane** — a product area with its own ops and tables: memory, knowledge, conversation,
graph, code, tracker, runs and tasks, audit.

**Procedure** — a reusable how-to (`procedure.add` / `match`), stored as a memory of type
`procedural`. See [procedures and learnings](features/memory/procedures-and-learnings.md).

**Profile** — an agent's or user's standing context (`profile.*`), loaded first by
`context.pack`.

**Projection** — a row derived from a write (an embedding, a graph edge, a summary).
A projection that fails is recorded and retried (`ops.projection_failures.*`).

**Read scope** — `read_scope`, which bounds a read inside one namespace: `namespace`
(every row, the default), `own_user` or `own_session`. A token's claim is a ceiling a
request can narrow but not widen.
See [identities and namespaces](concepts/identities-and-namespaces.md#read-scope-inside-a-namespace).

**Record** — a structured memory validated against a registered record schema
(`memory_schema.*`). Not to be confused with a tracker record.

**ReplacingMergeTree (RMT)** — the ClickHouse engine behind most Loom tables: a new
version row replaces the old one at merge or `FINAL` read time, so every write path must
carry every column forward. See [ClickHouse](architecture/clickhouse.md).

**Residue census** — the last step of an erasure saga: a count of live rows in scope over
every table the read paths touch. Any residue fails the saga.

**Role** — `viewer`, `writer`, `admin`, `org_admin`, `platform_admin`, in increasing
privilege. Each op declares the minimum it needs.

**Run** — one agent execution, bracketed by `run.begin` and `run.complete`, recording what
it recalled and wrote. See [runs](features/runs.md).

**Session** — a conversation or working period, the fifth element of the identity tuple;
a **session brief** (`session.brief_*`) is its saved summary for resuming later.

**Shared layer** — one canonical row per `(org, namespace, memory_key)`, promoted from
the rows agents write (`memory.promote` / `memory.demote`, table `loom.shared`).
"Private" for the other layer means not promoted, not hidden.

**Source class** — the server-stamped provenance of a row: `asserted`, `extracted`,
`observed`, `derived`, `imported` or `external`. A request may only downgrade it.

**Tier** — which extractor produced a code-index fact (parser or compiler); answers say
which tier backs them.

**Tombstone** — a higher-version row marking a memory deleted. `memory.restore` can undo
it within the grace window; nothing is physically deleted by `memory.delete`.

**Toolset** — the MCP listing a client asks for (`core`, `coding`, `atomic`, `tasks`,
`tracker`, `readonly`, `all`); it changes what is listed, not what the token may call.

**Tracker** — the plane that reads an issue tracker's records (issues, PRs, CI runs)
from a configured source for duplicate checks, bisect and test history.
See [tracker](features/tracker/overview.md).

**Trust mode** — the acceptance policy an inbox owner sets for a poster; Loom stores it
and never changes it itself.

**Valid time** — `valid_at` / `valid_to` on memories (`valid_from` / `valid_to` on
entity edges): when a fact was true in the world. Loom records valid time only; it is not bi-temporal.

**Version** — the monotonically increasing column a `ReplacingMergeTree` keeps the
highest of; every write of a memory (including a delete) is a new version.

**Worker** — a supervised background loop registered as a row in
`server/workers/registry.py` (`_WORKERS`). See [workers](reference/workers.md).

**Working memory** — scratchpad blocks (`working_memory.*`) for the current task: not
embedded, not ranked, loaded ahead of durable memory by `context.pack`.
See [working memory and profiles](features/memory/working-memory-and-profiles.md).
