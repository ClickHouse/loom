# Namespaces and sharing

A namespace is the unit of isolation and sharing inside an org. Every read is scoped to `(org, namespace)`, every
grant names one, and every per-namespace setting (conflict policy, retention, retrieval defaults, reranker) lives on
its row in `loom.namespaces`. The identity model is in
[identities and namespaces](../concepts/identities-and-namespaces.md).

## Registering a namespace

`namespace.register` creates the registry row and makes the caller its admin. Any principal may call it (viewer);
re-registering an existing namespace needs admin. With `LOOM_NAMESPACE_AUTOREGISTER_ENABLED` on (default), a first
write into a namespace with no row registers it, with the token subject as owner and `retention_days = 0`.

Re-registering is a partial update: an omitted field keeps its stored value. `tags` is replaced whole. For
`conflict_threshold`, `capture_generation_io` and `coverage_sweep`, omitted keeps the stored choice and an explicit
`null` returns to the server default.

| Field                                                           | What it controls                                                                                                                                                                                                                                                                         |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kind`                                                          | `memory`, `code` (one indexed repo and branch; read-only reference data) or `bench` (a throwaway harness namespace the GC erases once idle). Omitted on a fresh row, the name decides: a `LOOM_NAMESPACE_BENCH_PREFIXES` prefix is `bench`, anything else `memory`, as on a first write. |
| `visibility`, `join_policy`, `requires_approval`                | Who can find and join it: `private` (members only) or `public` (listed by `namespace.browse`); `join_policy` `open`, `approval`, `closed` or `inherit`.                                                                                                                                  |
| `default_conflict_policy`                                       | `auto_reinforce` (default), `manual_review`, `newest_wins` or `append_only` — how a write is judged against its peers. See [conflicts and refusal](memory/conflicts-and-refusal.md).                                                                                                     |
| `conflict_threshold`                                            | The cosine at which a close write is a conflict; an explicit `null` inherits `LOOM_CONFLICT_THRESHOLD`.                                                                                                                                                                                  |
| `proposal_policy`                                               | How facts on trial are decided; only an owner-set `auto` lets Loom decide them on graded runs. See [writing memory](memory/writing.md).                                                                                                                                                  |
| `retrieval_defaults`                                            | What an unpinned `memory.search` runs with (`search_mode`, `top_k`, `min_similarity`, `floor_mode`, `recency_pool_weight`, …); `{}` clears it. See [search and ask](memory/search-and-ask.md).                                                                                           |
| `rerank_endpoint`, `classify_endpoint`                          | Optional reranker (a builtin or a `LOOM_RERANK_ENDPOINTS_JSON` name, never a URL) and classifier URL, with their prompt templates. Both fail open.                                                                                                                                       |
| `retention_days`, `warm_days`, `tag_registry`                   | Namespace retention and tiering, and per-type retention / dedup thresholds. `0` keeps forever. See [retention and erasure](memory/retention-and-erasure.md).                                                                                                                             |
| `conversation_logging_enabled`, `capture_generation_io`         | Consent for storing transcripts, and for sending prompt and completion text to Langfuse. Both opt-in.                                                                                                                                                                                    |
| `consolidation_enabled`, `daily_cost_cap_usd`, `coverage_sweep` | Background consolidation, the namespace's upstream spend cap, and the extraction coverage sweep.                                                                                                                                                                                         |

```python
from loom import Agent

async with Agent(org="acme", namespace="eng-tooling", agent="setup") as agent:
    await agent.namespaces.register(
        owner_email="platform@acme.example",
        description="Shared memory for the build tooling agents",
        visibility="public",
        default_conflict_policy="newest_wins",
        retrieval_defaults={"search_mode": "hybrid", "top_k": 8},
    )
    # Fields the SDK method does not take go through Agent.call:
    await agent.call(
        "namespace.register",
        owner_email="platform@acme.example",
        join_policy="approval",
        daily_cost_cap_usd=5.0,
    )
```

`namespace.delete` (admin) marks the row inactive and stops accepting writes; it does not
touch the data. To remove the data, run an erasure first
(`erasure.request(scope_type="namespace")`, see
[retention and erasure](memory/retention-and-erasure.md)). `namespace.register` with
`active=true` reactivates it.

## Membership and grants

A grant gives a subject (a person's login or email, or a service account `sa:<name>`) a
role on one namespace: `viewer` reads, `writer` writes, `admin` manages grants and
settings. The token carries the org and the namespaces it may reach; the grant table
decides the role.

| Operation                                                            | Role      | Does                                                                                                                                                   |
| -------------------------------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `grants.create`                                                      | admin     | Grant a role. A create below the subject's current role is refused with 409 `GRANT_WOULD_DOWNGRADE` unless `replace=true`.                             |
| `grants.list`                                                        | viewer    | Who has access, at which role, and any `pending_role`.                                                                                                 |
| `grants.revoke`                                                      | admin     | Remove a grant.                                                                                                                                        |
| `grants.approve`                                                     | admin     | Approve or reject a pending request, optionally with a different role.                                                                                 |
| `grants.pending_for_me`                                              | viewer    | Every pending request in the namespaces the caller administers, org-wide.                                                                              |
| `namespace.browse`                                                   | viewer    | Public namespaces in the org (`closed` ones only with `include_closed`).                                                                               |
| `namespace.join`                                                     | viewer    | Self-join a public namespace. `viewer` is granted at once; a `writer` or `admin` request always lands pending, with no access until an admin approves. |
| `namespace.leave`, `namespace.cancel_request`, `namespace.mark_seen` | viewer    | Drop one's own grant, withdraw a pending request, acknowledge an invitation.                                                                           |
| `namespace.list_mine`                                                | viewer    | The namespaces the caller belongs to.                                                                                                                  |
| `namespace.list`                                                     | org admin | Every namespace in the org.                                                                                                                            |

Minting tokens, service accounts, read scopes and restricted namespaces are covered in
the [tokens and grants guide](../guides/tokens-and-grants.md).

## Sharing facts across agents and namespaces

Inside a namespace every writer's rows are visible to every reader by default
(`agent="*"`). Crossing a boundary is always a copy with lineage, never a shared row:

| Operation              | Role                 | Does                                                                                                                                                                      |
| ---------------------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory.promote`       | admin                | Copy a private memory into the namespace's shared layer (`loom.shared`), readable with `memory.search(scope="shared")`. Reversible for `reversible_for_days` (default 7). |
| `memory.demote`        | admin                | Tombstone the shared copy and its embedding; `was_reversible` says whether it was inside the window.                                                                      |
| `memory.copy_to`       | writer on the target | Self-service copy of a memory you can read into a namespace you can write, by key. Non-destructive; the target is re-embedded by its own embedder.                        |
| `memory.cross_promote` | admin on both ends   | The operator form, addressed by memory id, with `cross_promoted_from_*` lineage on the target.                                                                            |

Copies are not atomic across namespaces: a copy that fails
halfway leaves the source intact and the target without the row. The learnings queue
(`learnings.*`), which proposes rows worth promoting, is covered in
[procedures and learnings](memory/procedures-and-learnings.md).

## Reading a namespace's health

| Operation                                                                        | Answers                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| -------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`namespace.inventory`](../reference/operations/namespace.md#namespaceinventory) | What the namespace holds and who appears in it: live counts and newest write for memories, records, documents, runs and integrations; the member count; the agents, authors and subjects observed on memory rows; whether a real model and embedder serve the process that answered.                                                                                                                                                                                       |
| `namespace.summary`                                                              | Counts by memory type, top entities, recent writes and recalls, distinct agents, last activity.                                                                                                                                                                                                                                                                                                                                                                            |
| `namespace.validate`                                                             | An integrity report: stuck conflicts, low-confidence rows, rows still waiting for an embedding. Run it after a batch of writes or when a recall looks wrong.                                                                                                                                                                                                                                                                                                               |
| `namespace.audit`                                                                | Whether the namespace is used as a memory or as an object store: exact-key reads against searches, hash-shaped keys, double-encoded or oversized values, leases stored as memories, version churn. It also names the deployment's own state (kind `system_state`: missing telemetry, an indexing backlog), and skips the ledger checks rather than report them clean when the ledger recorded none of the calls behind the window's writes. Each finding carries a remedy. |
| `memory.inspect`                                                                 | One user, agent or session: its own rows apart from the rest of the namespace, its calls in a window, and the guide, categories and extraction policy that create its memories ([below](#inspecting-one-user-agent-or-session)).                                                                                                                                                                                                                                           |
| `namespace.taxonomy`                                                             | The live vocabulary: top tags, entities, memory types and communities.                                                                                                                                                                                                                                                                                                                                                                                                     |
| `agents.connected`                                                               | The agents active in a window (default 24 hours): last and first seen, ops, namespaces touched and cost per agent. Answers "is my client actually talking to Loom?"                                                                                                                                                                                                                                                                                                        |

`namespace.inventory` keeps each population apart. `members` counts the grant holders: the
people holding an active grant, not a pending request, a group, an agent or a service
account (`grants.list` on the namespace, and on each ancestor of a collection, lists their
grants). A token whose own claims name the namespace reads it without a grant and is never
counted; a collection counts the grant holders of its ancestors too, since a parent's
grant reaches every child. A grant to `everyone`, a group or an agent (every end user
signed in under it) anywhere on that lineage, or a code namespace readable by the whole
org, leaves the count `complete: false`, since their members are not listed. `agents`,
`authors` (the `actor` that wrote a row) and `subjects` (the `user_id` a row belongs to,
never the unset `-`) are identities observed on live memory rows, not a directory. A
personal collection's owner holds no grant, so its members are not counted (`total` is
null). A section that failed, or that the caller may not read, says so with `complete:
false` and a reason: the integrations count is for namespace admins, and under a read
scope bound to one user (`read_scope=own_user` or `own_session`, or a token pinned to a
user) the memory and run counts and the observed agents, authors and subjects are that
user's, documents and members are counted namespace-wide as `knowledge.*` and
`grants.list` read them, and records (one per key, whoever wrote it last) and integrations
are not counted.

Every `namespace.audit` and `namespace.validate` finding carries the same evidence fields,
so the CLI, the dashboard and an MCP client read one shape:

| Field                              | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kind`                             | `contract_violation` when a stored value breaks a declared invariant or attestation; `usage_heuristic` when a pattern usually costs recall or storage but a legitimate design can match it. A heuristic hit is a warning, not proof of misuse. `system_state` when the finding is about the deployment, not the writer: an indexing backlog (`index_backlog`) or writes the ledger did not record (`telemetry_unavailable`, which skips the ledger checks rather than report them clean). These are `namespace.audit` findings; `namespace.validate`'s `unembedded` check is a `contract_violation` and counts every row past its grace with no vector, held model calls included. |
| `rule_version`, `source_op`        | The rule's version (raised when its predicate or built-in threshold changes, so two reports are compared only at the same version; a setting or request input the rule reads, such as `confidence_floor` or the value size advisory, is not versioned) and the operation that computed it.                                                                                                                                                                                                                                                                                                                                                                                         |
| `window_seconds`                   | The ledger window a usage rule counted over; null for a rule that reads current state.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `population`, `count`              | The denominator of `share` (the rows or calls the finding is measured against, which may include rows the rule leaves out by design), and how many matched. `namespace.validate` checks report a count with no denominator and leave `population` null, as does an audit finding whose summary read was skipped.                                                                                                                                                                                                                                                                                                                                                                   |
| `sample_limit`, `samples_complete` | The sample cap, and whether the samples list every match: false when the cap cut them or they cannot name every match.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `evidence`                         | The live revision of each row a sample names (below). Never the value.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| `exceptions`, `uncertainty`        | What the rule leaves out by design, and why a hit may still be correct.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |

Each `evidence` entry is the live revision of a row a sample names, read once the checks
have run: `memory_key`, `memory_id`, `version`, `content_sha256` (the hash `memory.get`
serves; empty for a high-sensitivity row, for a value longer than
`LOOM_NAMESPACE_REPORT_HASH_MAX_BYTES` (64 KiB;
[configuration](../reference/configuration.md)), or for a row that changed while the
report read it), the identity columns, `memory_type`, `source_class` and `updated_at`. A
sample names its row by `memory_id`, or by its identity columns when it comes from the
embedding table, and a key held under several identities cites only the one that
matched. A row revised after the checks ran is cited at its newer revision; its
`updated_at` is then at or after the report's `computed_at` or `checked_at` (whole
seconds). A write that changes only a row's status can keep its `updated_at`, so the
comparison is a hint, not proof. A sample that names no single row (an `exact_key_store`
hot key, a `version_churn` key) and a row no longer live (an orphan vector) have no
entry.

The report's `coverage` lists the rules that ran (`rules_run`) and those that could not
(`rules_skipped`, also in `skipped` with the reason). Both name the evidence read
(`evidence`) and, for `namespace.audit`, its summary reads, which most findings are
measured against. `complete` is false when any of them was skipped.

`loom audit-namespace` runs `namespace.audit` from the shell; the
[optimize a namespace guide](../guides/optimize-namespace.md) turns its findings into a
reviewed rework plan. Vocabulary is registered with `namespace.register_category`,
`namespace.list_categories`, `namespace.search_categories` (categories) and
`namespace.register_ontology`, `namespace.list_ontology` (entity types and relation verbs
for the [entity graph](entity-graph.md)).

Snapshots and point-in-time rollback (`namespace.snapshot`, `namespace.restore`,
`namespace.rollback`) are covered in
[history and provenance](memory/history-and-provenance.md).

## Inspecting one user, agent or session

`memory.inspect` (viewer) takes `subject` (`user`, `agent` or `session`) and `id`, the
`user_id`, agent name or `session_id` to look at; the unset `-` and `*` name no one and
are refused. It writes nothing: no recall credit, no usage event.

- **`own`**: the live rows whose identity names the subject. For an agent that is what it
  wrote; the agent name records the writer and is not a read boundary. For a user, the
  rows filed under that `user_id`, whoever wrote them: what a token pinned to the user
  (`read_scope: own_user`) reads. For a session, the rows written in that `session_id`
  by any user; a token pinned to one user and the session (`read_scope: own_session`)
  reads that user's part.
- **`others`**: every other live row of the namespace. A member reads these as well, so
  `own` alone never stands for everything a user, agent or session can read. Neither
  view counts the promoted rows (`memory.promote`), which a search serves to every
  agent as well.
- Each view counts its rows by type, status and source class, lists its top tags and the
  registered categories its rows are filed under, and its newest rows (`sample_limit`,
  default 10) with `memory_id`, `version`, `content_sha256`, `actor`, the number of
  versions the history holds (the entity tagger's re-emits included, which
  `memory.history` leaves out) and a 200-character excerpt (none for a high-sensitivity
  row). `complete` is false when the listing holds fewer rows than the view, or the
  view's counts could not be read.
- **`activity`**: the calls the ledger files under the subject per operation in
  `window_seconds` (default 7 days), attempted and succeeded, without the dashboard's
  and the assistant's. The ledger files a call under the `agent`, `user_id` and
  `session_id` its arguments name, or the user and session a narrowed token is pinned
  to, so a call made by anyone that names the subject is counted, and a served
  `memory.inspect`, which names none, is not. A call shows that a tool ran, not that its
  result reached a model. `gap` says when the ledger keeps fewer days than the window (the
  namespace's own `retention_days`, else the deployment's).
- **`settings`**: the guide's revision and summary, the number of registered categories,
  and the operator extraction policy only for an org admin
  (`extraction_policy_unavailable` says why it is missing). Extraction instructions are
  sent per request; no user, agent or session setting stores them.
- A section that cannot be read is listed in `skipped`; a skipped section is not an empty
  one. A token narrowed below the namespace (`read_scope`) cannot call it.

## A namespace's guide

A namespace's guide is a Markdown README for it: what the namespace holds, its key
layout, what each tag means and which ops to call. Agents read it before their first
write and update it when they change one of those conventions; people read the same
text. It holds conventions about the data, never the facts themselves, which stay in
memory where search and erasure cover them.

| Operation                 | Role   | Does                                                                                                                          |
| ------------------------- | ------ | ----------------------------------------------------------------------------------------------------------------------------- |
| `namespace.guide_get`     | viewer | The current guide (or a past `revision`): its text, one-line summary, revision, writer and note.                              |
| `namespace.guide_set`     | writer | Writes the next revision when `expected_revision` is the current one; otherwise refuses with the current guide to merge into. |
| `namespace.guide_history` | viewer | The newest revisions with their summary, note, writer and length.                                                             |

Every save names the revision it was made from. Of two agents editing from one revision,
the first save lands and the second is answered `conflict: true` with the guide as it
now stands; the agent merges its change into that text and saves again with the returned
revision. The server never merges the text itself. A retry of a save that landed finds
its own text current and writes nothing. A save that leaves out `summary` keeps the
current one, and an empty `content` clears the guide. An end user's personal collection
(`col-me-…`) takes no guide. Every revision is kept, so `namespace.guide_get` with a
`revision` reads any of them.

The guide reaches agents without being asked for:

- **At session start,** `context.pack` renders a `namespace_guide` section: the guide's
  revision, writer and summary, then its text cut to the section budget (200 tokens).
  The text is recalled data, escaped and indented under one item like any other
  writer's text.
- **In the directory,** each `namespace.browse` row of a public namespace carries its
  `guide_revision` and `guide_summary`, and `query` also matches the guide's summary,
  so an agent looking through many public namespaces finds the one whose guide
  describes what it needs. A private row shows no guide to someone who is not a member,
  and a restricted row shows none at all.
- **`namespace.get`** carries `guide_revision` and `guide_summary`.

Reading a guide takes read access to its namespace, since a public row's visibility
widens no read: `namespace.browse` shows the org each public namespace's summary, and
the whole guide is for its members (`namespace.join`). In the dashboard, the Memories
page header has a **Guide** button beside **Add memory**, described by the guide's
revision and summary; on a namespace with no guide it opens a writer straight into the
editor. A first guide, from there or from Spaces, starts on an outline of what a
newcomer asks (what the namespace holds, its keys, its tags, how to use it) that cannot
be saved unchanged. Spaces shows a public namespace's guide summary under its
description; for a namespace you belong to, the summary opens the guide, or the
**Guide** menu item where no summary is shown. A writer edits it from either place,
saving from the revision they opened, as an agent does. A namespace erasure deletes
every revision of its guide.

## The mentor

`mentor.brief` returns advice for one agent with the evidence behind it: its calls over a window (default 7 days),
the planes the namespace offers, and each hint with the counts that fired it. `context.pack` renders up to three hints
at session start, and a successful response carries at most one (`hint` on MCP, `X-Loom-Hint` on HTTP), capped by
`LOOM_MENTOR_MAX_HINTS_PER_SESSION`. `loom mentor --org … --namespace … --agent …` prints the whole brief. See the
[mentor ops](../reference/operations/mentor.md).

A brief's `usage_evidence` lists what the agent's use of Loom shows, one rung each, with
who recorded it (`server`: Loom saw it; `client`: a caller reported it, unverified). The
rungs are read at each brief, not from the cached profile, over the window the brief
reports. Those read from the call ledger
(`attempt`, `success`, `committed_write`, `returned_hit` and the `memory.outcome` labels)
count the rows it records under the agent, which is the agent a call's body names (for a
call by key, the row's writer, whoever calls): a call whose body names no agent (a read by
id, an MCP `memory.update`, the Python SDK's `memory.update` by default) or that reads
across agents and is recorded under `*` (`memory.search` and `memory.ask` from the SDKs,
MCP's cross-agent reads, a `memory.outcome` that names no agent) is not counted. The
ladder uses none of the profile's counts, so it answers when the mentor is off too; a
brief about no agent in particular has no ladder.

| Rung                  | Source   | Counts                                                                                                                                                                                                                                                                                              |
| --------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `attempt`             | `server` | calls made, answered or not; not the dashboard's or the assistant's, a brief, or the verdict a deferred `memory.ask` leaves                                                                                                                                                                         |
| `success`             | `server` | those that returned without an error; a read that found nothing counts                                                                                                                                                                                                                              |
| `committed_write`     | `server` | `memory.set` calls that stored a row: not a no-op, a dry run or a replay                                                                                                                                                                                                                            |
| `indexed_write`       | `server` | memories first created in the window (one per user, session and key), by any write op, with a vector from the current embedder, in a served status and not expired                                                                                                                                  |
| `returned_hit`        | `server` | a `memory.search` or `memory.ask` that did not abstain, a `search.query` with a memory, document or conversation hit, a get that found its key                                                                                                                                                      |
| `client_acknowledged` | `client` | distinct memory keys the agent reported relying on: `run.complete` `memories_used` and a `memory.outcome` by id that said `used` or `positive` and landed on a row; not a label by key (recorded under the row writer's agent), the Python SDK's `outcome` calls (no agent) or `memory.outcome_run` |
| `task_outcome`        | `client` | the agent's runs graded `pass` or `fail` (`run.grade`), as the grader reported it                                                                                                                                                                                                                   |

A returned hit does not show that the answer reached the model or changed its work.
`ledger_from` is the agent's oldest ledger row in the window, and `ledger_retention_days`
how long the configured retention keeps the namespace's ledger rows (0: until deleted); a
window longer than that leaves the rungs read from the ledger covering less than it names.

[`mentor.guide`](../reference/operations/mentor.md#mentorguide) returns how to use Loom, written for a model: the
`loom` skill in `skills/loom/`, as `part` `skill` (the loop and the rules), `reference` (every plane with its ops) or
`all`. It is the same for every caller, so any viewer token may read it, including one narrowed to a user, and an end user's over HTTP.

## Throwaway namespaces

Benchmarks and harnesses write into namespaces under the prefixes in
`LOOM_NAMESPACE_BENCH_PREFIXES`; auto-register stamps them `kind = bench`. The
`namespace_gc` worker purges bench namespaces with live memory and no write for
`LOOM_NAMESPACE_GC_IDLE_HOURS` (default 24) through one list-scoped erasure saga per
batch. `loom namespaces-gc --org <org>` lists candidates, and `--erase` purges them by
hand; `loom namespaces-adopt` gives namespaces that hold memory but no registry row an
owner row.

## Settings

| Setting                                                        | Effect                                                                                  |
| -------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `LOOM_NAMESPACE_AUTOREGISTER_ENABLED`                          | First write registers the namespace.                                                    |
| `LOOM_NAMESPACE_BENCH_PREFIXES`, `LOOM_NAMESPACE_GC_*`         | Which namespaces are throwaway, and when the GC erases them.                            |
| `LOOM_MENTOR_TTL_SECONDS`, `LOOM_MENTOR_MAX_HINTS_PER_SESSION` | Mentor cache and the per-session cap on per-call hints; a TTL of 0 disables the mentor. |
| `LOOM_CONFLICT_THRESHOLD`                                      | The deployment conflict threshold a namespace inherits.                                 |

See [configuration: namespace registration and GC](../reference/configuration.md#namespace-registration-and-gc)
and [configuration: context](../reference/configuration.md#context).

## Limits

- A namespace name is 1–128 characters; kebab-case is recommended.
- `namespace.delete` is a soft delete. It never removes memory; an erasure does.
- Changing `default_conflict_policy` or `retrieval_defaults` changes how later writes and
  searches behave; rows already stored are not re-judged.
- `memory.promote` shares within one namespace. Another namespace sees a fact only
  through a copy (`memory.copy_to`, `memory.cross_promote`), which does not follow later
  edits of the source.

## Related

- [Identities and namespaces](../concepts/identities-and-namespaces.md) — the model.
- [Tokens and grants](../guides/tokens-and-grants.md) — minting and scoping access.
- [The dashboard](dashboard.md) — Spaces, Settings and the admin drawer.
- [Security architecture](../architecture/security.md) — how a request is authorized.
- [Operations reference: namespace](../reference/operations.md#namespace),
  [grants](../reference/operations.md#grants), [mentor](../reference/operations.md#mentor),
  [agents](../reference/operations.md#agents).
