# Identities and namespaces

Loom answers two questions on every call: whose row is this (the identity tuple, plus
two columns the server stamps), and may this caller touch it (the token, the namespace's
grants and its classification). This page explains the model. Minting and revoking
tokens is in [Tokens and grants](../guides/tokens-and-grants.md); enforcement is in
[Security](../architecture/security.md).

## Where namespaces fit

- An **org** is the tenant.
- A **namespace** is the unit of sharing and access inside it. A deployment usually has
  one per team or project, one per bot that must stay isolated, one `personal-<slug>`
  per person, and one `code-<repo>[-<branch>]` per indexed branch.
- Every member of a namespace reads every row in it.
- `agent` records who wrote a row; it is not a boundary.
- `user_id` / `read_scope=own_user` separates end users inside one bot's namespace.
- Access is a role (viewer/writer/admin) on a namespace, carried by a token or added by a
  grant.

## The identity tuple

Every row carries five identity columns:

```
(org, namespace, agent, user_id, session_id)
```

| Part         | Meaning                                                                                      | Default    |
| ------------ | -------------------------------------------------------------------------------------------- | ---------- |
| `org`        | The tenant. Nothing crosses an org boundary except through a wildcard operator token.        | (required) |
| `namespace`  | The unit of isolation and sharing inside an org. Every read is scoped to `(org, namespace)`. | (required) |
| `agent`      | Who wrote the row (`support-bot`, `claude-code`). Authorship, not a read boundary.           | (required) |
| `user_id`    | The end user the row is about.                                                               | `-`        |
| `session_id` | One conversation or run of a conversation.                                                   | `-`        |

Many operations also take `run_id` (default `-`) to scope a write or read to one task
execution. The token fixes the org and the namespaces a caller may reach; the rest of the
tuple travels in each request body. The SDKs bind `(org, namespace, agent)` when the
client is constructed and pass `user_id`, `session_id` and `run_id` per call.

**Reads span every author.** A read defaults to `agent="*"`: every row of the namespace,
whoever wrote it. Passing an `agent` only narrows. `user_id` and `session_id` on a read
filter to rows written under them.

**Why the tuple leads the tables.** The identity columns are the first columns of the
memory tables' sort keys, so scoping a read to one namespace, one user or one session is
a primary-key range in ClickHouse rather than a scan. It also makes a user's rows findable
for erasure without a secondary index.

### What the server stamps

Two more columns are decided by the server on every write and are never read from the
request body, except that a caller may downgrade `source_class`
(`server/memory/app/provenance.py` for `actor`, `server/memory/domain/provenance.py` for
`source_class`):

| Column         | Values                                                                 | Decided by                                                                                               |
| -------------- | ---------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `actor`        | `human:<subject>` or `process:<subject>` (a service account)           | The verified token (`writer_actor`). A queued job or CLI import writes as the principal that started it. |
| `source_class` | `asserted`, `extracted`, `observed`, `derived`, `imported`, `external` | The operation and the `source_trace` convention (`source_class_for`).                                    |

A request may declare only a downgrade of `source_class`: `external` (content from a tool
or page), `imported` or `derived`. Asking for anything else is a 422. The reason is that
provenance feeds ranking and `memory.ask`'s choice of evidence: if a caller could label
scraped text `asserted`, the label would mean nothing. `agent` is caller-supplied because
it names a role in the system, while `actor` is the authenticated principal behind it.

## Orgs and namespaces

An **org** is a tenant; a **namespace** is where rows live and the unit access is
granted on. A namespace comes into being when it is registered (`namespace.register`,
which makes the caller its `admin`), claimed at sign-up, provisioned by the server (the
single-user namespace, and a `personal-<slug>` namespace for each signed-in person), or
created for an indexed repository. Its directory row in `loom.namespaces` carries its
configuration: description, kind, visibility, join policy, conflict and proposal policy,
cost cap, retention and retrieval defaults.

| Namespace kind | Holds                                             | Read by                                                     |
| -------------- | ------------------------------------------------- | ----------------------------------------------------------- |
| `memory`       | Facts, documents, conversations: the default kind | memory, knowledge and conversation operations, and fan-outs |
| `code`         | One indexed repository and branch                 | the `code.*` operations, addressed by name                  |
| `bench`        | A benchmark corpus                                | benchmarks; its traffic is classed `bench`                  |

A search that fans out over all of a caller's namespaces spans the `memory` kind only
(`LOOM_MEMORY_SEARCH_FANOUT_KINDS`): sweeping every code index for a personal fact costs
a statement per index and finds nothing.

**Why namespaces, not per-agent stores.** The common case is several agents and people
that should see the same facts: a team's deploy window, a customer's history. Making the
namespace the read boundary means sharing is the default inside it and absent across it,
and a person or bot is given access by granting a role on a namespace, not by copying
rows.

### Visibility, joining and restriction

| Setting             | Values                                          | Effect                                                                                                                                    |
| ------------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `visibility`        | `private` (default), `public`                   | A public namespace is listed in `namespace.browse` and can be joined. Visibility grants no read by itself.                                |
| `join_policy`       | `open`, `approval`, `closed`, `inherit` (unset) | What `namespace.join` does. `inherit` resolves to `open` for a public namespace that does not require approval, and `approval` otherwise. |
| `requires_approval` | `true`, `false`                                 | Read only under `inherit`: every join then waits for an admin (`grants.approve`).                                                         |
| `classification`    | `standard` (default), `restricted`              | Switches every widening default off (below).                                                                                              |

On an `open` namespace a `viewer` join is active at once and a `writer` or `admin` join
is pending until an admin approves it; on `approval` every join is pending; `closed`
refuses joins.

A **restricted** namespace (`classification=restricted` on its directory row, or on its
entry in `LOOM_CODE_REPOS_JSON`) admits a call only when the token belongs to the
namespace's org, names the namespace literally (not through `*`), holds a role there that
reaches the operation's role, and the token's own subject holds a live direct grant. No
org-readable rule, public visibility, wildcard, inherited service-account grant,
`org_admin` or `platform_admin` bypasses it, and a refusal does not reveal whether the
namespace exists. Fan-outs, context packs and rollups skip restricted namespaces the
caller cannot read, and each admitted request is audited. The step-by-step setup is in
[Tokens and grants](../guides/tokens-and-grants.md#restricted-namespaces-classificationrestricted).

## Roles

Roles are ordered, and a higher role satisfies every check for a lower one. Each
operation declares the minimum role it needs (`GET /v1/operations` lists it).

| Role             | Grants                                                                                            | How it is held                                                                                 |
| ---------------- | ------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `viewer`         | Read.                                                                                             | Token claim or namespace grant.                                                                |
| `writer`         | Read, and write memory, knowledge and the other data planes.                                      | Token claim or namespace grant.                                                                |
| `admin`          | Administer one namespace: grants, configuration, policy, restores, promotion to the shared layer. | Token claim or namespace grant; registering a namespace makes you its admin.                   |
| `org_admin`      | Org-wide views (for example cost without a namespace).                                            | A recognised gate with no in-product assignment path.                                          |
| `platform_admin` | Cross-org operator views.                                                                         | Conferred at login to the handles and verified emails in `LOOM_PLATFORM_ADMINS`; never minted. |

Tokens and grants carry only `viewer`, `writer` or `admin`: a role above a namespace
cannot be minted, so org-wide power is an operator decision in configuration, not
something a token can carry away. Code namespaces have readers (`viewer`) and maintainers
(`admin`) and no `writer`.

## Who may call what

A call on `(org, namespace)` is allowed when the namespace is not restricted (or passes
the restricted checks) and either the token's own role for that namespace reaches the
operation's role, or a live grant does.

- **The token** (`sub`, `org`, `namespaces`, `roles`, optional `ns_roles` and `ns_write`)
  says which namespaces the caller reads, in priority order, and with what role. A
  **scoped** token (`ns_roles`, per-namespace roles) is authoritative for exactly the
  namespaces it lists. A **flat** token applies its `roles` to every namespace it names.
  `ns_write` narrows writes to a subset, so one token can read a team namespace and write
  only its own:

  ```
  namespaces = ["team-shared", "agent-private"]
  ns_write   = ["agent-private"]
  ```

- **Grants** (`loom.namespace_grants`, one row per `(org, namespace, subject)`) add a role
  on one namespace above what the token carries. Grants only add. A service account with
  no grant of its own on a namespace uses its creator's grant there, so a token minted
  from the dashboard follows the person who made it, except on a restricted namespace.
- **Operations addressed by an org-wide id** (`memory.get_by_id`, `memory.update_by_id`,
  `memory.redact`) check the fetched row's namespace after the read and answer
  `found=false` on refusal, so a refusal never confirms that the id exists.

Why tokens plus grants: the token is verified locally on every call without a lookup,
which keeps the hot path cheap; grants let an admin give and take access without
re-issuing tokens. Revocations are shared through ClickHouse and reach every replica
within the revocation cache interval.

## Reading several namespaces

A token that names several namespaces can search them together: `namespace_scope` is
`this` (the addressed namespace, the default) or `all_mine` (every namespace the caller
reads), and the response lists `namespaces_searched`. The token's `namespaces` order is
the search priority, and private namespaces are searched before public ones. The
`loom_search` MCP tool and `context.pack` use the same fan-out.

## Read scope inside a namespace

Membership is the read boundary, which is right for a team's shared memory and wrong for
one agent that serves many end users through one token: a prompt from one user could pull
another user's memories into the answer. `read_scope` bounds a read inside the namespace:

| `read_scope`          | A read returns                                        |
| --------------------- | ----------------------------------------------------- |
| `namespace` (default) | every row of the namespace                            |
| `own_user`            | rows written under the `user_id` the request names    |
| `own_session`         | rows written under the `session_id` the request names |

`memory.search`, `memory.search_trace`, `memory.ask`, `memory.recall`, `context.pack` and
`profile.render` take it. A token can carry a `read_scope` claim (and a pinned
`read_user_id`) that is a ceiling: a request can narrow it but never widen it, and a
scoped read without the `user_id` or `session_id` it binds to is refused with
`IDENTITY_INCOMPLETE` instead of silently widening. Mint `own_user` tokens for
multi-user agents.

Both transports resolve the scope once, when they admit the call, and every read of
memory rows in that call carries it, whichever op made the read: `memory.get`,
`get_by_id` (another user's id answers `found=false`), `list`, `bulk_get`, `history`,
`timeline`, `related`, `evidence`, `conflicts.list`, `memory.proposals`, session briefs,
digests, procedures, working memory and the `loom_search` legs. Under a scope narrower
than `namespace`:

- `user_id="*"` is refused with `AUTH_INSUFFICIENT`, and so is `session_id="*"` under
  `own_session`.
- A token pinned with `read_user_id` acts only as that user, on writes too: a body naming
  another `user_id` is refused with `AUTH_INSUFFICIENT` on every op, and a body that names
  none (or `-`) runs as the pinned user.
- A write's answer names no row outside the scope: a merge into, a duplicate of or a
  pinned conflict with another user's row reports the verdict without that row's id,
  hash or key, and `conflicts.list` drops such peers from `conflict_with`.
- Session briefs are kept per user (per session under `own_session`): two users saving
  the same `brief_key` get two briefs, each sees only its own, and a namespace-wide
  token lists them under keys prefixed `~<user>~` (or `~<user>~<session>~`).
- `code.search` breaks ties with the scope's own user's recent files, whatever
  `user_id` the body names.
- A leg whose rows carry no user (conversations, runs) answers nothing.
- An op that cannot bound its answer to one user (the entity graph `facts.*`,
  `conversation.*` reads, namespace exports, admin and curation ops) is refused with
  `AUTH_INSUFFICIENT`. The list is `server/identity/domain/read_scope.py`
  (`SCOPED_OPS`, `NARROW_SAFE_OPS`), and an op it does not name is refused.

## Agent-written rows and the shared layer

Inside a namespace, rows in `loom.memory` are keyed by the full tuple: the same
`memory_key` written by two agents, or for two users, is two rows. That is the per-agent
("private") layer, and it is what `memory.search` reads by default (`scope="private"`).
"Private" here means _not promoted_, not _hidden_: every reader of the namespace can read
these rows unless `read_scope` narrows it.

The **shared layer**, `loom.shared`, holds one row per `(org, namespace, memory_key)`: a
namespace-owned, canonical version of a fact that no longer belongs to the agent that
learned it. `memory.promote` copies a row into it, with a rationale and an optional
approver, and `memory.demote` retires it (the response says whether that happened
inside the promotion's `reversible_for_days` window, default 7, up to 90). A search with
`scope="shared"` reads that layer, and `memory.lineage` walks a shared row back to its
promotion. `memory.promote` and `memory.demote` need `admin`. `memory.cross_promote`
copies a row into another namespace, with provenance, for a caller who is admin on both.

Why two layers: agents write freely and often redundantly under their own identity,
while a curated fact the whole namespace should rely on needs one canonical key, an
owner's decision and an audit trail. Promotion is that decision.

## Identity modes

How a caller gets a token at all is the deployment's identity mode
(`LOOM_IDENTITY_PROVIDER`):

- **`single-user`**: no authentication; every call resolves to one namespace
  (`LOOM_DEFAULT_NAMESPACE`, default `local`, in the org `LOOM_SELF_SIGNUP_ORG`, default
  `dev`). For a laptop only.
- **`trust-network`**: self-signup with email and password; the first principal to
  claim a namespace name owns it. An agent can sign itself up (`loom init --agent`) and a
  person claims it later with a one-time code.
- **`gateway`**: an upstream identity layer authenticates people and mints short-lived
  Loom tokens with the shared secret; Loom only verifies. A subject in
  `LOOM_TRUSTED_CALLERS` may act for an end user with `X-Loom-On-Behalf-Of`, and every
  ledger row records the real caller.

When `LOOM_IDENTITY_PROVIDER` is empty the server infers the mode: auth off gives
`single-user`, auth on with `LOOM_SELF_SIGNUP=open` gives `trust-network`, auth on
otherwise gives `gateway`. [Deployment](../operations/deployment.md#identity-modes) lists
each mode's settings.

## Related

- [Tokens and grants](../guides/tokens-and-grants.md): minting, claims, grants, restricted
  namespaces, history policy and revocation, step by step.
- [Namespaces and sharing](../features/namespaces-and-sharing.md): the namespace
  operations and the dashboard's sharing views.
- [Security](../architecture/security.md): how authentication and authorization are
  enforced.
- [Memory](./memory.md): the row the tuple addresses.
