# MCP reference

Loom serves its operations as MCP tools at `<loom>/mcp`. Any MCP client that
speaks streamable HTTP connects with a URL and a bearer token. The tool list
is generated from the same operation registry as the [HTTP API](./http-api.md),
and a tool call runs the same authorization, rate limit, audit and idempotency
path as `POST /v1/<op>`. The transport is `server/transport/mcp.py`, with a
tool call in `mcp_call.py` and `mcp_outcome.py`, bearer checks in `mcp_auth.py`, its
listing in `mcp_tools.py`, schema shaping in `mcp_schemas.py`, protocol and
session constants in `mcp_config.py` and the curated descriptions in
`mcp_descriptions.py`.

Wiring a specific client (Claude Code, Codex, Cursor, VS Code, Claude Desktop
and others) with `loom install` or by hand is in
[MCP clients](../features/integrations/mcp-clients.md).

## Endpoint

| Request                          | Does                                                                                                                                                                |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /mcp`                      | One JSON-RPC request or a batch. The answer is a JSON body, not an event stream. A notification answers `202` with no body.                                         |
| `DELETE /mcp` + `Mcp-Session-Id` | Ends the session and closes its implicit run. Rate-limited as `run.complete`, per caller within the org's budget: past the cap it answers `429` with `Retry-After`. |

Query parameters on the URL select what `tools/list` shows for that connection:

- `?toolset=` picks the roster that is listed ([Toolsets](#toolsets)). Default:
  `LOOM_MCP_TOOLSET` (`core`).
- `?schema_profile=` picks the listing's shape: `standard` (the curated
  description and the input schema; the default, `LOOM_MCP_SCHEMA_PROFILE`),
  `full` (adds `outputSchema`) or `compact` (the first paragraph and the
  argument tree).

An unknown value for either falls back to the deployment default, so a typo
never widens or narrows a listing silently. The `?toolset=` value also shapes the
session primer (`initialize` instructions, `server/discover`, the `loom` prompt,
`loom://primer` and `loom_activate`): the primer names an op's own tool only when the
toolset lists it, and `loom_op` with the op name otherwise.

Methods served: `initialize`, `notifications/initialized`,
`notifications/cancelled`, `ping`, `tools/list`, `tools/call`, `prompts/list`,
`prompts/get` (the `loom` prompt: the session primer), `resources/list`,
`resources/read` (`loom://primer`) and `server/discover`. List results carry
`ttlMs` and `cacheScope` hints so a client can cache them.

**Protocol versions.** `2025-11-25`, `2025-06-18`, `2025-03-26` and
`2024-11-05` (`MCP_SUPPORTED_PROTOCOL_VERSIONS`). `initialize` answers with the
client's version when it is one of these, else `2025-11-25`. An
`MCP-Protocol-Version` header naming another revision is HTTP 400 with JSON-RPC
error `-32022`, whose `data.supported` lists the served ones. `server/discover`
answers with the same list, the capabilities, the server identity and the
instructions.

## Authentication

`LOOM_MCP_AUTH_MODE` decides which methods need a bearer:

| Mode                 | Bearer required on                                            |
| -------------------- | ------------------------------------------------------------- |
| `required` (default) | every method except notifications                             |
| `discovery_only`     | `tools/call` only; every other method is open                 |
| `open`               | nothing; an invalid bearer is ignored (trusted networks only) |

A deployment with auth off (`LOOM_AUTH_REQUIRED=false`) behaves as `open`. A
missing or invalid bearer on a gated method is HTTP `401` with
`WWW-Authenticate: Bearer realm="loom"` (plus `error="invalid_token"` when a
token was sent). A token that cannot be checked yet, because the revocation
list has not loaded, is HTTP `503` with `Retry-After` instead, so the client
keeps the token and retries. **Why a 401 and not a JSON-RPC error:** clients key their
"needs authentication" flow, and their OAuth discovery, on the HTTP status; a
JSON-RPC error body would surface as a parse or tool error instead. The token
is an ordinary Loom JWT; see [Tokens and grants](../guides/tokens-and-grants.md).

The bearer is read from the `Authorization` header only. A token passed as `?token=` on
the `/mcp` URL is ignored and the request is unauthenticated: request lines land in proxy
and access logs, where a URL token would be written in the clear.

### OAuth

Served only with `LOOM_OAUTH_ENABLED=true`. Loom is an OAuth 2.1 authorization server for clients that
connect by browser sign-in instead of a pasted token (`server/auth/oauth.py`):

- Discovery at `/.well-known/oauth-authorization-server` and
  `/.well-known/oauth-protected-resource`.
- Dynamic client registration at `/oauth/register`: public clients, PKCE
  `S256` required.
- `/oauth/authorize` and `/oauth/token` issue an access token plus a rotating
  refresh token. A refresh succeeds only when the request names the
  `client_id` the token was issued to.
- The access token is a Loom JWT for the signed-in user, so namespaces the user
  joins later are reachable on the same connection.
- Sign-in uses Loom's password login unless `LOOM_OAUTH_LOGIN_PROVIDER=oidc`
  delegates it to an external identity provider (`LOOM_OIDC_ISSUER`,
  `LOOM_OIDC_CLIENT_ID`, `LOOM_OIDC_CLIENT_SECRET`). Delegating sign-in also
  closes password self-signup.
- A remote deployment must serve HTTPS and set `LOOM_OAUTH_ISSUER` to its
  public `https://` base URL.
- A registered redirect URI must be absolute and fragment-free: `https`, `http`
  to a loopback host, or an app scheme (`cursor://…`). The sign-in page names
  the host the authorization goes to.
- Each refresh re-checks that the account is still active and re-derives its
  roles, so a released account, or a subject removed from
  `LOOM_PLATFORM_ADMINS`, does not keep its access by refreshing.

[Security](../architecture/security.md) covers the token model behind this.

## Sessions and identity

- **Session id.** `initialize` assigns an `Mcp-Session-Id` and every response
  echoes it. The client's `clientInfo.name` is cached against it and becomes the
  `agent` of later calls, slugified (`Cursor` → `cursor`; absent →
  `claude-desktop`). The cache is in-process and bounded.
- **Identity defaults.** Tool calls usually carry only the action's arguments.
  The server fills `org` from the token and `namespace` and `agent` from the
  caller's default workspace; explicit arguments win. `code.*` calls resolve to
  the code plane instead. An `org` argument the token may not act in is refused
  (`ORG_MISMATCH`): a token acts in its own org, an org-wide (`*`) token or a
  platform admin in any org, the same rule as over HTTP (`authz.may_act_in_org`).
  A restricted namespace still refuses every other org, platform admins included. `loom_search` takes no `namespace` argument: it
  searches the caller's home namespace and its other memory-kind namespaces, up to the
  fan-out cap.
- **Code scope.** A client may send `X-Loom-Code-Scope: <org>/<namespace>` (the
  Claude Code plugin sends the repository its checkout resolved to). It applies
  to every `code.*` call that names no namespace, and authorization then runs on
  that namespace as if it had been named. `loom_search` under the header also reads
  the repository's documents and `decision` memories from its trunk code namespace,
  when the caller may read it, and runs the tracker duplicate check for a
  defect-shaped query.
- **Implicit run.** One run per (org, token subject, session), begun in the
  run ledger by the session's first run-scoped tool call and filed in the
  caller's default namespace; writes carry its `run_id` (except `run.*`, which
  names its own run, `code.*`, which acts as the deployment's code identity, and
  `working_memory.*` and `conversation.update`, where `run_id` scopes or binds the
  row), and reads are labelled with it but not filtered. The run closes on
  `DELETE /mcp`, or when the `mcp_session_run_sweep` worker finds no call of the
  token under it for `LOOM_MCP_SESSION_RUN_IDLE_SECONDS` (default 1800); a
  deployment that runs no background workers anywhere keeps idle runs open. The
  run stores no call count: its calls are the `agent_events` rows with its
  `run_id`. See [Runs](../features/runs.md).

## Tool list

Every registry operation not in `_MCP_HIDDEN_OPS` is a tool:

- **Name.** `loom_` plus the op name with dots as underscores (`memory.set` →
  `loom_memory_set`).
- **Description.** The curated text in `TOOL_DESCRIPTIONS`
  (`server/transport/mcp_descriptions.py`) when there is one, else a one-line
  fallback with the op's latency budget, idempotency and minimum role. The
  curated texts say when to use a tool and when not to; the choice is left to
  the model.
- **Input schema.** The op's Pydantic input model without the fields the
  server fills itself (`org`, `agent`, `user_id`, `session_id`, `consumer`),
  the dashboard-only fields (`_HIDDEN_OP_PROPS` in
  `server/transport/mcp_schemas.py`) and every field marked `MCP_HIDDEN`;
  `namespace` stays as an optional property. Hidden fields stay accepted by
  name.
- **Annotations.** `readOnlyHint`, `destructiveHint` and `idempotentHint` are
  derived from the registry (role, idempotency, audit, destructive verbs in the
  name). `readOnlyHint` is true only for an idempotent viewer op that is
  neither audited nor marked `writes`, so a client that auto-approves read-only
  tools never runs a state change unprompted. They are hints; the role check at
  call time is the boundary.

Three synthetic tools are listed on every toolset but `assistant`:

| Tool            | Does                                                                                                                                                           |
| --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `loom_search`   | One call searches memory, profile, knowledge and conversations in parallel and returns all four sections. `explain: true` adds why each memory hit ranked.     |
| `loom_activate` | Returns the session primer (profile, preferences, default workspace), the same text as the `loom` prompt.                                                      |
| `loom_op`       | `{op, arguments}` runs any op the roster does not list, through the same path as a listed tool; `{op: "help", query}` lists matching ops with their arguments. |

`loom_search` is the registry's `search.query` operation (`POST /v1/search.query`
over HTTP, the same sections without the text summary). Over MCP it runs the same
path as every registry tool (caller defaults, session run, authorization, restricted
access audit, traffic class, history policy, rate limit, idempotency) and keeps its
own listed schema, its lenient options (an out-of-range `top_k` or an unknown
`recipe` / `bodies` is clamped, not refused) and a text summary beside the lean
result. `Operation.mcp_tool` names it, so no second `loom_search_query` tool is
listed or callable. See
[unified search](../features/unified-search.md#searchquery-over-http-and-the-sdks).

**Hidden ops** (`_MCP_HIDDEN_OPS` in `server/operations/toolsets.py`) are
neither listed nor callable over MCP, except `memory.search` and `knowledge.search` on
the `assistant` toolset: the per-store searches `loom_search`
replaces (`memory.search`, `knowledge.search`, `conversation.search`), review
queues, snapshot and restore, policy administration, audit and cost
dashboards, code-index ingest. A call to one answers `isError` with code
`OP_NOT_ON_MCP` and what to use instead. They stay reachable over HTTP and the
SDKs. The rule for hiding an op: would a typical end user, not an operator,
reach for it in chat? A namespace-wide view can be hidden while a variant bounded
to the caller stays on MCP: `namespace.list` is hidden and `namespace.list_mine`
listed; `memory.extraction_status` (every batch queued in the namespace) is hidden
and `memory.extraction_status_mine` (the batches the caller queued, by `queue_id`,
at most 20 a call) is callable by name or through `loom_op` on every toolset but
`assistant`, `readonly` included. Its write twin, `memory.extraction_retry_mine`, replays
the caller's failed batches once more; `loom_op` routes it on every toolset but
`readonly` and `assistant`.

### Toolsets

A toolset decides what is **listed**; every non-hidden op stays callable by
tool name, or through `loom_op`, whatever the toolset. The `assistant` toolset is the
exception: on it only its own reads are callable.

| Toolset     | Lists                                                                                                                                                                                                                                                                                                                                              | For                                            |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `core`      | The chat loop: memory save, recall and manage, working memory, knowledge documents, profile, conversation logging, namespace orientation, erasure, `blacklist.add`, `context.pack`. The code tools join with `LOOM_MCP_CODE_TOOLS=true`.                                                                                                           | chat clients (the default)                     |
| `coding`    | `memory.set/update/get/delete`, `working_memory.set`, `context.pack`, the code tools (`code.search`, `grep`, `enclosing`, `ask`, `symbol`, `outline`, `callers`, `context_pack`), `tracker.duplicate_check`, `code.bisect_candidates`, `code.pr_check`, `code.impact`.                                                                             | coding agents                                  |
| `tracker`   | `tracker.answer`, `binding`, `component`, `duplicate_check`, `flaky`, `item`, `links`, `release`, `similar`, `test_signal`, plus `code.bisect_candidates`, `code.pr_check`, `code.impact`.                                                                                                                                                         | sessions working a failing test, crash or PR   |
| `tasks`     | `task.pull/create/complete/fail/wont_do/defer/heartbeat/updates/list/get`, `inbox.list`.                                                                                                                                                                                                                                                           | a session acting as an agent with a task inbox |
| `atomic`    | `memory.set/get/delete/ask`, `working_memory.set/append`, `run.begin/complete`.                                                                                                                                                                                                                                                                    | minimal, policy-shaped agents                  |
| `readonly`  | The `all` listing minus every tool whose `readOnlyHint` is false.                                                                                                                                                                                                                                                                                  | read-only registrations                        |
| `assistant` | A fixed set of reads for investigating Loom on a person's behalf: `namespace.get/summary/taxonomy/audit/validate/guide_get/guide_history/list_categories`, `mentor.brief`, `grants.list`, `memory.search/ask/get/list/history/provenance/aggregate`, `facts.traverse/path`, `knowledge.search/get/list`, `code.search/symbol`. No synthetic tools. | a chat client acting for a signed-in person    |
| `all`       | Every non-hidden op whose `maturity` is `stable`, plus the `experimental` ops the core or atomic roster lists. An experimental op that neither roster lists is left out; it is still callable by name or through `loom_op`. Code tools only with `LOOM_MCP_CODE_TOOLS=true`.                                                                       | operators                                      |

Curated rosters combine: `?toolset=coding+tracker`, `coding,tracker`, or an
unescaped `+` that arrives as a space. Only curated rosters join (`all`,
`readonly` and `assistant` are whole surfaces), and an unknown part makes the whole value
unknown, so the listing falls back to the deployment default. The rosters are
the frozensets in `server/operations/toolsets.py`; `ops.capabilities` reports
each op's `toolsets`. A second MCP registration on another toolset
(`?toolset=tracker`, `?toolset=tasks`) adds a roster without changing the first.

**Why the listing is small.** Every listed tool's name, description and schema
is paid for in tokens on every turn of every session, and tool-selection
accuracy falls as the list grows. `tests/unit/test_mcp_listing_budget.py`
holds each roster under a token ceiling. Prefer leaving an op callable by name
over listing it; [Adding an operation](../development/adding-an-operation.md) covers
when and how to list one.

## Results

`structuredContent` (and the JSON in `content[0].text`) is the op's output,
trimmed to the answer (`server/transport/lean_results.py`): hits with their
key, content, namespace, dates, similarity, source and status, plus counts,
paging and abstain fields. Score components, explanations, retrieval plans and
costs, hashes and empty fields are dropped. **Why:** some clients hand
`structuredContent` to the model as well as the text block, and the full
diagnostic shape costs several thousand tokens for a short answer.
`explain: true` on the call returns the full shape. HTTP, the SDKs and the
`assistant` toolset always get the full result.

### The assistant toolset

`?toolset=assistant` is the connection a chat client uses to investigate Loom on behalf
of the signed-in person: what a namespace holds, how it is used, and cited answers over
memory, documents and code. The value must be exactly `assistant`: a misspelling or a
union such as `assistant+core` does not resolve and the connection gets the deployment's
default listing, with no error. The URL the operator configures and a `viewer` token
carry the guarantee, not the client. On it:

- **Only its reads are callable.** A `tools/call` for any other tool, `loom_op` and
  `loom_search` included, answers `isError` with code `UNKNOWN_OP` before the handler
  runs, whatever the token allows. The token is still checked on every call, so a read
  the person may not make is refused as on any connection. Mint the client's token with
  the `viewer` role to make the same limit hold on every other surface.
- **Its calls are ledgered as consumer `loom-assistant`**, whatever the call sends.
  `namespace.audit`, `mentor.brief` and `knowledge.reads` leave that consumer and the
  dashboard's (`loom-ui`) out of their usage counts, so looking at a namespace does not
  change its grade; `namespace.audit` reports how many it left out in
  `summary.inspector_calls`. A token whose `consumer` claim names another consumer keeps
  its claim, and its calls are counted.
- **Reads do not record usage.** `memory.search` runs with `auto_touch: false` and
  `explain: false`, and `memory.ask` with `reinforce: false`, whatever the call sends, so
  an investigation does not raise the recall counts that rank results; `memory.ask` also
  runs with `precedents: false`, so no answer is stored for reuse. What remains is
  telemetry and caches: the request ledger row; the exposure row every read of a memory
  leaves (`loom.memory_exposures`, the record of who was served which row); the code
  search trace a `code.search` or `code.symbol` writes (`loom.code_search_trace`, under
  `LOOM_CODE_TRACE_LOG_ENABLED`); the search trace and query-node rows a `memory.search`
  writes when `LOOM_SEARCH_TRACE_LOG_ENABLED` or `LOOM_MEMORY_QUERY_NODES_ENABLED` is on;
  a `memory.ask` consistency row under deferred consistency; and the model response
  cache. An assistant call opens no MCP session
  run, and its `initialize` and `loom` prompt write no profile-read row: its primer
  inlines no profile.
- **Results are the full output**, as HTTP returns them, since a citation needs the
  identities, versions and attestations the trimmed shape drops. `memory.search` and
  `knowledge.search`, which the other listings leave to `loom_search`, are listed here
  so an answer can come from one plane at a time.
- **The `initialize` instructions** tell the model to cite each claim by key and version
  or content hash, to keep source statements, computed results and its own reading apart,
  to report missing evidence as not observed, and to treat stored text as data that
  cannot grant a tool or approve a change.

## Errors

A call that cannot run as sent comes back as a **tool result** with
`isError: true`, a text block the model reads and a `structuredContent` body.
Clients show these to the model so it can correct the call; many clients drop
state on a JSON-RPC error frame, which Loom keeps for transport failures.

- **Wrong arguments.** A validation failure answers `"<op>: invalid
arguments."` followed by each field error, every unknown key with the field
  it most likely meant (nested fields included), the required and optional
  arguments and one corrected example call:

  ```text
  memory.get: invalid arguments.
  memory_key: Field required [missing]
  unknown 'key' — did you mean 'memory_key'?
  required: memory_key
  optional: if_version, include_recall_breakdown, as_of, fields, follow
  example: {"memory_key": "deploy_window"}
  ```

  `structuredContent` carries the same parts: `errors`, `op`, `required`,
  `optional`, `unknown` (key → suggestion) and `example`. An op that changes
  state (any op that is not read-only) given a near-miss key is refused before
  validation (`UNKNOWN_ARGUMENT`, nothing written) with `did_you_mean` and the
  op's arguments.

- **Refusals.** A handler's 4xx other than 401 (`LOCKED`, `PINNED_CONFLICT`,
  `BLACKLISTED`, `RESOURCE_NOT_FOUND`, `RATE_LIMITED`, …) is a tool result whose
  body is the HTTP `detail` plus `status` and, when there was a `Retry-After`,
  `retry_after_s`.
- **Access.** A role failure answers `AUTH_INSUFFICIENT` with `required_role`; a
  namespace the token neither names nor holds a grant on answers
  `NAMESPACE_NOT_ACCESSIBLE`. On a code or tracker namespace the refusal says
  how to get access.
- **Transport errors** stay JSON-RPC errors: parse error `-32700`, invalid
  request `-32600` (with the HTTP 401), method not found `-32601`, unsupported
  protocol version `-32022`, and a server fault `-32000 "tool execution
failed"`. A fault Loom names (`OVERLOADED`, `BACKEND_TIMEOUT`,
  `SCHEMA_PENDING`, `EMBED_TIMEOUT`) states its code and message in that error, and
  `error.data` is the HTTP `detail` plus `status`, `tool` and `request_id` (with
  `retry_after_s` for `OVERLOADED`); any other fault says only `internal error` and the
  request id.

Every code, with its cause and fix, is in the [errors reference](./errors.md).

## Security

`tools/call` runs the HTTP transport's gates (`server/transport/gates.py`):

- **Authorization** per call: the op's `required_role` and the namespace's
  access rules.
- **Rate limit**: the same buckets as HTTP. Because MCP clients do not retry, a
  limited call whose wait hint is at most `LOOM_MCP_RATELIMIT_RETRY_MAX_WAIT_S`
  (default 2 s) sleeps that long once and checks the same buckets again; a
  longer hint or a second refusal answers `RATE_LIMITED` with `retry_after_s`
  (`LOOM_MCP_RATELIMIT_RETRY_MAX_WAIT_S=0` never waits).
- **Audit**: ops declared `audit=True` write the same `loom.audit_log` rows.
- **Idempotency**: MCP has no idempotency header, so an idempotent op takes
  `_idempotency_key` (or `idempotency_key`) in its arguments. It is removed
  before validation and replays exactly as the HTTP `Idempotency-Key` does: only
  after authorization, and only to the principal that stored it.

## Exporting the action schema

A policy gateway can govern Loom's tools from a schema generated from the
registry (`server/transport/action_schema.py`):

```bash
loom export-actions --format json  -o loom-tools.json    # tools/list manifest + a `loom` block per tool
loom export-actions --format cedar -o loom.cedarschema   # Cedar: one action per operation
```

The JSON manifest has the MCP `tools/list` shape (`name`, `description`,
`inputSchema`, `outputSchema`) for every op, plus a `loom` block with what a
policy engine needs and MCP has no field for: the op name, the HTTP path, the
required role, the effect class (`read`, `write` or `destructive`, from the same
classifier the annotations use), idempotency, the audit flag, MCP visibility and
the input and output model names. The Cedar schema declares, in namespace
`Loom`, an `Agent` principal and a `Namespace` resource, one record type per
input and output model, the action groups `Read`, `Write` and `Destructive`
(`Destructive` in `Write`), and one action per operation in its group. `-o -`
writes to stdout.

## Related

- [MCP clients](../features/integrations/mcp-clients.md): connecting Claude Code, Codex, Cursor and other clients.
- [Claude Code](../features/integrations/claude-code.md) and [Codex](../features/integrations/codex.md): the plugins that mount the server themselves.
- [Operations reference](./operations.md): every op with its role, kind and whether `all` lists it.
- [Errors reference](./errors.md).
