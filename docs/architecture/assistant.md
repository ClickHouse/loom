# Assistant design

This page is the design of Loom's assistant, which will answer questions about what a
namespace holds and how it is used, with a citation for every claim. It names the
surfaces, how each caller's identity will reach the agent's tools, the analysis op and
its isolation, the deployment, what will be stored and erased, and the cost. None of it
is built yet; [the build order](#build-order) lists the parts.

LibreChat will be the assistant: it will host the agent (its model, instructions and
tools) and run it headless beside Loom, reachable only from Loom. Loom will supply the
agent's tools, which read under a token of the asker's own subject, and a read-only SQL
op that ClickHouse itself holds to that token's org and namespaces. People will chat on
the dashboard's Assistant page; agents and scripts will reach the same agent over HTTP,
MCP and both SDKs, through Loom's origin and as themselves.

## Surfaces

```mermaid
flowchart LR
    P["person, signed in"] --> D["dashboard<br/>Assistant page chat"]
    A["agent or script"] --> S["SDK / HTTP / MCP"]
    D --> B["Loom assistant bridge"]
    S --> B
    B -->|"agents API,<br/>the caller's own account key"| LC["LibreChat, headless<br/>(the Loom agent)"]
    LC -->|"MCP, the account's<br/>stored Loom token"| T["assistant toolset"]
    T --> Q["insights.query"]
    Q --> U["ClickHouse user loom_analyst<br/>column grants + signed row policies"]
```

| Surface         | What the caller will call                                                                                               |
| --------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Dashboard       | a chat on the Assistant page, streaming from `POST /v1/assistant/chat/completions` with the signed-in session           |
| HTTP            | `POST /v1/assistant/chat/completions`: LibreChat's agents API request and response, OpenAI-compatible, streaming or not |
| SDKs            | a thin `assistant` client in Python and TypeScript over that route, with streaming; it holds no agent logic             |
| MCP             | the `loom_assistant_ask` tool (op `assistant.ask`), which forwards one question and returns the whole answer            |
| HTTP, op shaped | `POST /v1/assistant.ask` and the generated `assistant_ask` / `assistantAsk` SDK methods, from the same op               |

Each request names its namespace: the `X-Loom-Namespace` header on the chat route (the
token's default namespace when absent), the `namespace` field on `assistant.ask`. The
caller needs `viewer` there, and the answer's model spend is charged there.

LibreChat's web UI and API will not be published: LibreChat listens on an internal
network only Loom reaches. A person has one sign-in and one URL, Loom's. The
[LibreChat integration](../features/integrations/librechat.md), in which each person
runs LibreChat's UI with a pasted token, is replaced by this deployment when it is
built.

**Why LibreChat hosts the agent.** One agent definition, versioned in
`integrations/librechat/`, will serve every surface. LibreChat runs the model, the tool
loop, streaming, cancellation and conversation state, which Loom would otherwise build a
second time. Loom keeps what makes an answer trustworthy: authorization on every tool
call, the request ledger, the analysis boundary in the database, and the citation check.

**Why a native chat, not LibreChat's UI in a frame.** A frame would put a second origin,
a second sign-in and LibreChat's look inside the dashboard, and would need LibreChat's
cookies to work in a third-party frame. A chat panel on the Assistant page uses Loom's
session and components, and links citations into the memory, document and code views.

## The bridge: each caller's own identity

LibreChat's agents API authenticates a LibreChat API key and runs the agent as that
key's LibreChat user. The agent's Loom MCP server sends the Loom token that user has
stored (`customUserVars` `LOOM_TOKEN`, encrypted with LibreChat's `CREDS_KEY`). In
LibreChat v0.8.8 a request cannot carry a token of its own: request-time header
placeholders take only LibreChat's conversation and message ids
(`ALLOWED_BODY_FIELDS`), and `{{LIBRECHAT_OPENID_ACCESS_TOKEN}}` carries LibreChat's own
OpenID sign-in, which a headless LibreChat does not have and Loom does not issue. So
Loom will give every caller its own LibreChat user, and nothing in LibreChat that can
read Loom will be shared.

The bridge (`server/assistant/`) will serve a request in five steps:

1. Verify the caller's Loom token or dashboard session as on any op, and its `viewer`
   role on the request's namespace. A revoked or expired caller is refused here, before
   LibreChat is called, and so is an org-wide (`*`) token: an answer reads one org. An
   org whose model calls an admin switched off (`org.set_model_calls`) is refused too,
   since the agent's model would read its rows.
2. Delegate the account's token from the caller's (`server/auth/delegation.py`
   `delegate`): `viewer` on the caller's namespaces, minted for the caller's own subject
   with consumer `loom-assistant`, expiring at the earlier of the caller token's expiry
   and one hour. Delegation applies every rule a minted child follows: the creator's
   live grant, the end user and `read_scope`, the ancestor subjects and their issue
   times, and the caller's token id among the child's `parents`.
3. Find the account in `loom.assistant_accounts`, or create it. The account key is the
   org and a hash of the delegated token's claims without its ids and times, so a caller
   with a narrower token, or whose claims change, gets a separate account, and two
   scopes never share a stored token. The LibreChat user name is that key's hash; the
   password is random and stored sealed (`server/core/secrets_at_rest.py`) with the
   account's agents API key, so replacing the signing secret (with
   `LOOM_JWT_SECRET_PREVIOUS` set) keeps both readable. The row is written before the
   LibreChat user is registered and the first row of an account wins, so two replicas
   creating one account register one user with one password. LibreChat's registration
   is open only on its internal network.
4. Store the delegated token as the account's `LOOM_TOKEN` when the stored one expires
   within 15 minutes or was delegated from a different caller token. The old token is
   not revoked: a tool call already using it still succeeds, and it runs out within the
   hour.
5. Call LibreChat's agents API with the account's key, the seeded agent's id in place of
   any `model` the caller names, the text parts of the caller's messages (no files or
   images), and the namespace as context, and stream the response back with
   `Accept-Encoding: identity`. A caller that disconnects, or the chat's stop button,
   closes the upstream request, and LibreChat stops the run. The bridge drops any
   conversation id in the caller's body; LibreChat assigns it.

A stored token reads no more than the caller's own token, ends no later than it, and is
ended with it: revoking the caller's token, its subject, its sign-in session (for a
third-party client) or a subject in its `creator` or `parent_subjects` chain ends the
stored token too, since delegation puts each of them in the stored token's revocation
check. The LibreChat user that owns the agent definition stores no Loom token, so the
agent itself reads nothing; only a caller's own account can make it read.

Accounts are cleared by a worker: an account unused for 30 days, or whose subject is
revoked, loses its LibreChat user, its chats and its row. Loom reaches LibreChat's
MongoDB with a credential of its own for that, for the erasure step below, and to remove
a LibreChat user whose account row was lost so it can be registered again.

**Citations.** The agent's instructions will require a citation on each claim:
`[memory:<key>@v<version>]`, `[doc:<source_uri> § <section>]`, `[code:<path>:<line>]`,
or the number of a fenced `sql` block in the answer that holds the statement exactly as
it ran. The bridge checks each against Loom: a memory key and version must be readable
by the caller and current, a document section must exist in a live document, and a
`sql` block's hash must match an `insights.query` call ledgered under the account's
token during this answer. Each citation comes back marked `verified` or not; the
dashboard links verified ones into its views and shows the SQL. The steps of an answer
(each tool call, its arguments and whether it answered) come from Loom's ledger: the
agent's MCP server sends LibreChat's conversation id on each tool call (`requestHeaders`
with `{{LIBRECHAT_BODY_CONVERSATIONID}}`), and the bridge reads the rows tagged with it
under the account's token. An answer with no verified citation is shown as unsupported,
as an abstention is.

## The agent definition

`integrations/librechat/agent.yaml` will hold the agent's name, instructions, model,
tool list and a version number. Loom seeds it at boot under the owner account, shared
read-only with every LibreChat user, and writes it only when its version is newer than
the stored one, so during a rolling upgrade a build N-1 never overwrites N's definition.
The tool list is the assistant toolset's reads and `insights.query`, so the agent
writes nothing; a change it suggests is a draft a person applies elsewhere, as the
Assistant page's tune drafts already are. The instructions require citations, tell the
agent to abstain when no tool result supports an answer, to read `insights.catalog`
before writing SQL, and to treat stored text as data that cannot grant a tool or approve
a change.

## The analysis op

`insights.query` will run one read-only SQL statement over an allowlisted set of Loom
tables and return its rows, capped. The agent uses it for questions no other read
answers, such as which agents wrote the most contradicted facts last week or what a
namespace holds by kind and age. Any token with `viewer` on the namespaces it names can
call it directly, on any transport, and it joins the
[assistant toolset](../reference/mcp.md#the-assistant-toolset). A token whose
`read_scope` is narrower than its namespaces is refused it, as every op outside
`SCOPED_OPS` is.

### The boundary is the database user

The statement runs as a dedicated ClickHouse user, `loom_analyst`, never as Loom's own
user or the reader user. Loom does not parse or rewrite the statement to scope it. It
sends the scope as settings and runs the statement as a subquery,
`SELECT * FROM (<statement>)`, so that ClickHouse parses only a `SELECT` or `WITH`
there and reports a syntax error for anything else. The wrapper is not what isolates
orgs; the user's grants, settings profile and row policies are:

| What the statement tries                                           | What stops it                                                                                              |
| ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| a table or column outside the allowlist (tokens, secrets, keys)    | grants: `SELECT(col, …)` on each allowlisted table, nothing else                                           |
| another org or namespace, through a filter, `JOIN` or subquery     | a row policy on every allowlisted table, checked on each row read (below)                                  |
| a deleted row, or a version a later one replaced                   | `final = 1`, `CONST` in the profile, with the row policy applied after `FINAL` (ClickHouse's default)      |
| `system.*`, `information_schema`, `SHOW`, `DESCRIBE`, `KILL QUERY` | no grant on `system` tables, `SHOW COLUMNS` or `system.processes`                                          |
| `EXPLAIN`, whose estimates count other orgs' rows                  | inside the subquery, `EXPLAIN` needs `CREATE TEMPORARY TABLE`, which the user lacks                        |
| `mergeTreeIndex()` and other index readers                         | ClickHouse refuses them on a table with a row policy                                                       |
| `url()`, `file()`, `s3()`, `remote()`, `cluster()`, `executable()` | no `READ ON URL`, `FILE`, `S3`, `REMOTE` and no `CREATE TEMPORARY TABLE` grant                             |
| `aiGenerate()` and the other AI functions, which send rows out     | they take credentials only from a named collection, and the user has no `NAMED COLLECTION` grant           |
| a write, DDL or a temporary table                                  | `readonly = 2`, held `CONST` in the user's settings profile                                                |
| `SETTINGS` that widen time, memory, rows read or result size       | the profile's `MAX` constraints; `result_overflow_mode` and `read_overflow_mode` are `'throw'` and `CONST` |
| `SETTINGS use_query_cache = 1`                                     | `use_query_cache = 0`, `CONST`                                                                             |
| `SETTINGS` that rename the scope                                   | the row policy's signature check fails, so the statement reads no rows                                     |

**Signed row policies.** Loom sends four custom settings with each statement: the org
(`SQL_loom_org`), the namespaces (`SQL_loom_ns`), an expiry a minute ahead
(`SQL_loom_exp`) and `SQL_loom_sig`, the SipHash-128 of one length-prefixed string of
the other three under a 128-bit key. Each allowlisted table's policy, `TO loom_analyst`,
admits a row only when its `org` and `namespace` match the settings, the expiry is in
the future, and the signature matches the hash ClickHouse computes with the key written
into the policy. A statement may set custom settings itself (`readonly = 2` allows it),
but without the key it cannot sign another scope, so a changed scope reads nothing. A
second policy, `AS PERMISSIVE USING 1 TO ALL EXCEPT loom_analyst`, keeps every other
user, Loom's own included, reading the tables as before, whatever the server's
`users_without_row_policies_can_read_rows` says.

The key is derived from the deployment's signing secret (`LOOM_JWT_SECRET`) with an
HMAC, so every replica holds the same key and none is stored. The analyst user cannot
read it: it has no grant on `system.row_policies`, and it cannot run `EXPLAIN`. Loom
writes the policies with `log_queries = 0`, so the key is not in `system.query_log`;
an administrator who reads `system.row_policies` sees it, as they can read every row
anyway. While `LOOM_JWT_SECRET_PREVIOUS` is set the policy also accepts the previous
secret's signature, so replicas mid-rotation sign under either and all answer. A
mismatch would read no rows rather than fail, so Loom checks for it: when a statement
returns no rows, Loom reads a one-row sentinel table under the same signature, and an
empty sentinel answers `ANALYSIS_UNAVAILABLE` instead of an empty result.

**Why not ClickHouse credentials in LibreChat.** A ClickHouse login in the agent would
read every org, skip the request ledger and the exposure records, and keep no link to
the asker's token. The analysis op runs inside Loom's request path: the token is
verified first, the namespaces are checked as on any read, and the call is ledgered.

### Provisioning

The analyst user, its settings profile, grants and policies will be written with SQL,
never in a ClickHouse config file, which would put the key in the repository. A server
whose ClickHouse user has access management (the bundled ClickHouse's `loom` user, a
self-managed admin) writes them at boot. Elsewhere `loom ch-setup` writes them, or
prints the SQL for an admin, as it does for the reader user. The allowlist carries a
version, stored with the grants; a build writes them only when its version is newer, so
during a rolling upgrade a build N-1 never narrows N's grants, and each build's catalog
names only the columns the grants in place allow. At boot Loom probes the analyst user
with one signed statement, checks that a `system` table is refused, and compares the
grants with the allowlist; a missing or older user is a `loom doctor` problem, and
`insights.query` answers `ANALYSIS_UNAVAILABLE` until it is fixed. The allowlist lives
in code with the column comments that become the agent's catalog, so the grants, the
policies and the catalog come from one definition.

### What it can read

The allowlist covers metadata, not content: the identity tuple, keys, kinds, versions,
timestamps, counts, statuses, op names and latencies, from the memory, conflict, usage,
document and request-ledger tables. Memory values, document text, chunks, prompts,
search queries and ledger arguments are left out; a question about content goes through
`memory.search`, `memory.ask` or `knowledge.search`, which apply their own scope and
sensitivity rules. The allowlist admits a table only when the
[erasure saga](memory-architecture.md#erasure-saga) deletes from it or the census
(`server/erasure/store/census.py`) lists it as holding no memory content, so a
statement after an erasure cannot read what it removed.

### The catalog

`insights.catalog` will return the allowlisted tables, their columns, types and
comments, and the notes a correct query needs: which tables hold one row per key (read
live, since the profile forces `FINAL`) and which one row per event. It is generated from the allowlist, so it never names a
column the user cannot read.

### Limits, fairness and the ledger

A statement runs under the caps the profile pins as `MAX`: execution time, rows read,
memory and result rows, each a setting in the catalog. A statement over a cap fails with
the cap it hit and how to narrow the query. Every org's statements run as the one
analyst user, so fairness between orgs comes from Loom: `insights.query` is rate-limited
per namespace, org and actor like every op (`server/core/ratelimit.py`), and the
profile's `max_concurrent_queries_for_user` bounds the analyst user's share of
ClickHouse. Each replica also runs at most a set number of statements per org at once
(a catalog setting), so one org's slow statements cannot take the whole share. Each
call writes one `agent_events` row with the statement's hash and length, its row count
and its latency; the ledger's argument scrubbing (`server/observability/event_args.py`)
replaces the statement with its hash, and the profile holds `log_queries = 0` `CONST`,
so the statement text and the rows are stored nowhere.

## Isolation proof

`tests/integration/test_analysis_isolation.py`, in the style of
`tests/integration/test_cross_principal_leakage.py`, will seed two orgs, each with a
canary memory and a canary ledger row, and call `insights.query` with the first org's
viewer token. Each case must be refused or return no canary:

- a filter on the other org, a namespace outside the token, a `JOIN` or subquery over
  the same table, `UNION ALL`, `view(...)`, `merge(...)`, and a `throwIf` or
  `sleepEachRow` on the other org's rows;
- `system.query_log`, `system.processes`, `system.row_policies`, `system.users`,
  `information_schema`, `SHOW CREATE TABLE`, `DESCRIBE`, `KILL QUERY … TEST`,
  `loom.signing_keys`, `loom.revoked_tokens` and a column outside the allowlist;
- `EXPLAIN` in each form, also after a `)` or comment that tries to leave the wrapper,
  `mergeTreeIndex()`, `url()`, `file()`, `s3()`, `remote()`, `cluster()`,
  `executable()`, `input()` and `aiGenerate()` with a named collection;
- a deleted memory and a version a later one replaced, with and without `FINAL`;
- `SETTINGS` that change the scope, the caps, `readonly` or `use_query_cache`, the same
  statement text run first by one org with the query cache and then by the other, and
  a scope sent with an expired or forged signature;
- a statement that is not a `SELECT`: `INSERT`, `ALTER`, `CREATE TEMPORARY TABLE`;
- both orgs' statements running at once;
- policies written under another key: the answer is `ANALYSIS_UNAVAILABLE`, not empty,
  and no statement's text or the key appears in `system.query_log`.

The same file will show that a revoked token stops new calls and that an erased memory
is gone from the next statement. A unit test pins the generated grants and policies to
the allowlist. The bridge's tests will show that two callers never share an account, a
narrower token gets its own account, a stored token is never wider than the caller's or
longer-lived, revoking the caller's token or its creator ends the stored token, and a
revoked caller, an org-wide token and an org with model calls switched off are refused
before LibreChat is called.

A probe of this boundary on ClickHouse 26.9, with a scratch table, user and policy of
this shape, showed each refusal in the table above, the query cache keeping the two
orgs' results apart, `EXPLAIN PLAN` printing the folded filter
(`has(['a1'], namespace) AND org = 'acme'`) with no key, `final = 1` hiding a deleted
key without bringing back its earlier version, and `aiGenerate()` refused for want of
a `NAMED COLLECTION` grant.

## Deployment

LibreChat will run beside Loom with its MongoDB in every deployment shape, reachable
only from Loom:

- **Compose:** an `assistant` profile starts LibreChat and MongoDB on an internal network
  shared with the `loom` service alone; neither publishes a port.
- **Helm:** an `assistant.enabled` value adds LibreChat's Deployment and Service and a
  MongoDB StatefulSet, with a NetworkPolicy that admits only Loom's pods.
- **Settings:** a catalog setting names LibreChat's internal address; unset, the
  assistant routes answer `ASSISTANT_UNAVAILABLE` and the Assistant page shows no chat.
  LibreChat's model key (`ANTHROPIC_API_KEY`) is set in its own environment, and
  Loom's MongoDB credential for LibreChat's database in Loom's.

LibreChat's configuration keeps the integration's memory off and search index out, and
differs in two ways: sharing is on for the one seeded agent, and every chat expires
after 24 hours (`generalChatRetention`, with `retentionMode: all`) instead of the
integration's 720.

## Stored data and erasure

| Where                     | What the assistant leaves                                                                    | How it is deleted                                                                      |
| ------------------------- | -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `loom.assistant_accounts` | per account: the org, the claim-set hash, LibreChat user id, the sealed password and API key | holds no memory content (a `NO_MEMORY_CONTENT` entry); dropped with its LibreChat user |
| `loom.agent_events`       | one row per bridge request and per tool call, under the account's token                      | as every ledger row                                                                    |
| LibreChat chats           | questions, answers and tool outputs, which can quote memory content (MongoDB)                | expire after 24 hours; every erasure in the org deletes them (below)                   |
| LibreChat `transactions`  | token counts per message, no content                                                         | kept; deleted with the LibreChat user                                                  |
| LibreChat logs            | user ids and token ids, no message text                                                      | one file per day, kept 14 days                                                         |
| the dashboard             | the current chat, in the tab's memory                                                        | gone when the tab closes                                                               |

A chat can quote any row its account read, and Loom cannot tell which chat holds which
row. So the erasure saga gets a step that deletes, through MongoDB, every LibreChat chat
of every account in the erased scope's org, and a census substrate that counts that
org's chats, so a dry run and the census before the step show what it will delete and
the census after it shows none left. A MongoDB that cannot confirm the deletion fails
the step, and the saga with it, as any backend does. Erasing one user or namespace
therefore also clears that org's other chats, which are at most 24 hours old. An answer
is computed from the rows the account's token reads at that moment, so an erased memory
is absent from every later answer.

## Model and cost

The model will be the agent definition's: `LOOM_ASSISTANT_MODEL` when set (Loom writes it
into the definition when it seeds), `claude-haiku-4-5` otherwise. The model runs in
LibreChat on its own key, so Loom's provider layer does not call it, but Loom holds its
spend to the same budget as its own model calls:

- The bridge reads each answer's token usage from the agents API's final chunk, prices
  it with the model's rate, and records it as `upstream_cost_usd` on the answer's
  `agent_events` row in the request's namespace, where every model spend is counted.
- Before calling LibreChat, the bridge checks the namespace's daily model cost cap
  (`server/observability/budget.py`, `daily_cost_cap_usd`, default
  `LOOM_DEFAULT_DAILY_COST_CAP_USD`) with the assistant held to its own share of it (a
  catalog setting), so questions cannot spend what extraction and the conflict
  resolver need. A namespace over its cap, or over the assistant's share, is refused.

The [qualification](../features/integrations/librechat.md#qualification) of the
assistant toolset measured a mean of $0.03 per answer on that model; the deployed agent
is re-qualified with analysis questions before it ships.

## Who can use it

Any token with `viewer` or higher on the namespace, and any person signed in to the
dashboard with that role. Every tool call checks the account's token again, and that
token never reads more than the caller's own.

## Rolling upgrades

`assistant.ask`, `insights.query` and `insights.catalog` will be new ops and
`/v1/assistant/chat/completions` a new route: a build that does not know them answers
`UNKNOWN_OP` or 404, so a client points at them once every replica serves them. The
analyst user, its grants and policies, and `loom.assistant_accounts` live in ClickHouse
and are shared by every replica; the agent definition is written only by a build whose
definition version is newer.

## Build order

1. `insights.query`, `insights.catalog` and the analyst user, with the isolation proof.
2. LibreChat deployed headless, the seeded agent definition, re-qualified with analysis
   questions.
3. The bridge: the chat route, `assistant.ask`, the SDK clients, the erasure step.
4. The chat on the dashboard's Assistant page.

Each part moves its section of this page from design to the description of what is
built.

## Source entry points

- `server/operations/toolsets.py` (`MCP_ASSISTANT_OPS`, `ASSISTANT_FORCED_ARGS`): the
  assistant toolset the agent's tools come from.
- `server/core/ch.py` (`caller_shaped`, `_reader_auth`): how a read runs as another
  ClickHouse user under caps.
- `server/cli/ch_setup.py`: provisioning of the reader user, which the analyst user
  follows.
- `server/auth/delegation.py` (`delegate`): how a stored token is derived from the
  caller's and inherits its revocation.
- `server/observability/budget.py`: the namespace cost cap the assistant's spend counts
  toward.
- `integrations/librechat/`: the LibreChat configuration and qualification harness.

## Invariants

- The analysis boundary is ClickHouse's: grants, the settings profile and signed row
  policies.
- Every tool call the agent makes reads under a token delegated from the caller's, never
  wider or longer-lived than it and revoked with it, and is ledgered as consumer
  `loom-assistant`.
- No LibreChat account or key is shared between callers, and the account that owns the
  agent stores no Loom token.
- Every erasure reaches LibreChat's chats, and fails if it cannot.
- The assistant writes no memory, document or grant.

## Related

- [The assistant toolset](../reference/mcp.md#the-assistant-toolset)
- [LibreChat integration](../features/integrations/librechat.md)
- [Security](security.md)
- [Rolling upgrades](rolling-upgrades.md)
