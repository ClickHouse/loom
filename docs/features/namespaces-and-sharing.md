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

| Operation            | Answers                                                                                                                                                                                                                           |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `namespace.summary`  | Counts by memory type, top entities, recent writes and recalls, distinct agents, last activity.                                                                                                                                   |
| `namespace.validate` | An integrity report: stuck conflicts, low-confidence rows, rows still waiting for an embedding. Run it after a batch of writes or when a recall looks wrong.                                                                      |
| `namespace.audit`    | Whether the namespace is used as a memory or as an object store: exact-key reads against searches, hash-shaped keys, double-encoded or oversized values, leases stored as memories, version churn. Each finding carries a remedy. |
| `namespace.taxonomy` | The live vocabulary: top tags, entities, memory types and communities.                                                                                                                                                            |
| `agents.connected`   | The agents active in a window (default 24 hours): last and first seen, ops, namespaces touched and cost per agent. Answers "is my client actually talking to Loom?"                                                               |

`loom audit-namespace` runs `namespace.audit` from the shell; the
[optimize a namespace guide](../guides/optimize-namespace.md) turns its findings into a
reviewed rework plan. Vocabulary is registered with `namespace.register_category`,
`namespace.list_categories`, `namespace.search_categories` (categories) and
`namespace.register_ontology`, `namespace.list_ontology` (entity types and relation verbs
for the [entity graph](entity-graph.md)).

Snapshots and point-in-time rollback (`namespace.snapshot`, `namespace.restore`,
`namespace.rollback`) are covered in
[history and provenance](memory/history-and-provenance.md).

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

Every save names the revision it was made from. Of two agents editing from one
revision, the first save lands and the second is answered `conflict: true` with the
guide as it now stands; the agent merges its change into that text and saves again with
the returned revision. The server never merges the text itself. A retry of a save that
landed finds its own text current and writes nothing. A save that leaves out `summary`
keeps the current one, and an empty `content` clears the guide. A personal namespace
takes no guide. Every revision is kept, so `namespace.guide_get` with a `revision` reads
any of them.

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

Reading a guide takes read access to its namespace: a public row's visibility widens no read,
so `namespace.browse` shows every public namespace's summary, and the whole guide is for
its members (`namespace.join`). In the
dashboard, Spaces shows a public namespace's guide summary under its description, and
the **Guide** menu item of a public namespace you belong to, or one you manage, opens
the guide; a writer edits it
there, saving from the revision they opened, as an agent does. A namespace erasure
deletes every revision of its guide.

## The mentor

`mentor.brief` returns advice for one agent with the evidence behind it: its calls over a window (default 7 days),
the planes the namespace offers, and each hint with the counts that fired it. `context.pack` renders up to three hints
at session start, and a successful response carries at most one (`hint` on MCP, `X-Loom-Hint` on HTTP), capped by
`LOOM_MENTOR_MAX_HINTS_PER_SESSION`. `loom mentor --org … --namespace … --agent …` prints the whole brief. See the
[mentor ops](../reference/operations/mentor.md).

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
